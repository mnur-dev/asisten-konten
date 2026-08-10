import logging, os, platform, shutil, socket, subprocess, tempfile, time
from pathlib import Path
import requests
from renderer.render import render_video
from shared.timeline import parse_pgn
logging.basicConfig(level=logging.INFO,format="[%(asctime)s] %(message)s",datefmt="%H:%M:%S")
URL=os.environ["CONTROLLER_URL"].rstrip("/"); TOKEN=os.environ["WORKER_TOKEN"]; WORKER=os.getenv("WORKER_ID",socket.gethostname()); H={"Authorization":f"Bearer {TOKEN}"}
def nvenc(): return bool(shutil.which("ffmpeg") and subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-f","lavfi","-i","color=size=64x64:rate=1","-frames:v","1","-c:v","h264_nvenc","-f","null","-"],capture_output=True).returncode==0)
def get_next_job():
    response = requests.get(URL+"/api/jobs/next",headers=H,timeout=30)
    if response.status_code == 401:
        raise RuntimeError("Controller authentication failed: check WORKER_TOKEN")
    response.raise_for_status()
    return response.json()["job"]
def run_once():
    job=get_next_job()
    if not job:return False
    jid=job["id"]; claim=requests.post(f"{URL}/api/jobs/{jid}/claim",headers=H,json={"worker_id":WORKER},timeout=30); claim.raise_for_status()
    try:
        requests.post(f"{URL}/api/jobs/{jid}/status",headers=H,json={"status":"PROCESSING"},timeout=30).raise_for_status()
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"game.pgn"; out=Path(d)/"game.mp4"; response=requests.get(URL+claim.json()["pgn_url"],headers=H,timeout=60); response.raise_for_status(); p.write_bytes(response.content)
            timeline=parse_pgn(p.read_text(encoding="utf-8")); logging.info("Parsed %d plies",len(timeline["moves"])); render_video(timeline,out)
            with out.open("rb") as f: requests.post(f"{URL}/api/jobs/{jid}/result",headers=H,files={"result_file":("game.mp4",f,"video/mp4")},timeout=600).raise_for_status()
        logging.info("Job %s COMPLETED",jid)
    except Exception as e:
        requests.post(f"{URL}/api/jobs/{jid}/status",headers=H,json={"status":"FAILED","error_message":str(e)[:1000]},timeout=30); logging.exception("Job failed")
    return True
def main():
    logging.info("Chess Render Worker | ID=%s | FFmpeg=%s | NVENC=%s",WORKER,bool(shutil.which("ffmpeg")),nvenc())
    while True:
        try:
            if not run_once(): time.sleep(int(os.getenv("POLL_SECONDS","10")))
        except requests.RequestException as e: logging.error("Controller unavailable: %s",e); time.sleep(10)
if __name__=="__main__":main()
