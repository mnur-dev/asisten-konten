import logging, os, shutil, socket, subprocess, sys, tempfile, threading, time
from pathlib import Path
import requests
from renderer.render import render_video
from shared.timeline import parse_pgn
from shared.timestamps import parse_timestamps
from worker.detect_timestamps import detect_timestamps

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
URL = os.environ["CONTROLLER_URL"].rstrip("/")
TOKEN = os.environ["WORKER_TOKEN"]
WORKER = os.getenv("WORKER_ID", socket.gethostname())
H = {"Authorization": f"Bearer {TOKEN}"}


def request(method, url, attempts=3, **kwargs):
    for attempt in range(attempts):
        try:
            response = requests.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException:
            if attempt == attempts - 1: raise
            time.sleep(2 ** attempt)


def nvenc_capability():
    if not shutil.which("ffmpeg"): return False, "FFmpeg not found in PATH"
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=size=640x360:rate=1", "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-"], capture_output=True)
    return result.returncode == 0, result.stderr.decode(errors="replace").strip()


def yt_dlp_command(url, output):
    return [sys.executable, "-m", "yt_dlp", "-f", "bv*[height<=720]+ba/b[height<=720]", "--merge-output-format", "mp4", "-o", str(output), url]


def get_next_job():
    response = requests.get(URL + "/api/jobs/next", headers=H, timeout=30)
    if response.status_code == 401: raise RuntimeError("Controller authentication failed: check WORKER_TOKEN")
    response.raise_for_status()
    return response.json()["job"]


def run_once():
    job = get_next_job()
    if not job: return False
    jid = job["id"]
    claim = request("POST", f"{URL}/api/jobs/{jid}/claim", headers=H, json={"worker_id": WORKER}, timeout=30)
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(int(os.getenv("HEARTBEAT_SECONDS", "30"))):
            try: request("POST", f"{URL}/api/jobs/{jid}/heartbeat", attempts=1, headers=H, json={"worker_id": WORKER}, timeout=30)
            except requests.RequestException: logging.warning("Heartbeat failed for %s", jid)
    thread = threading.Thread(target=heartbeat, daemon=True); thread.start()
    try:
        request("POST", f"{URL}/api/jobs/{jid}/status", headers=H, json={"worker_id": WORKER, "status": "PROCESSING"}, timeout=30)
        with tempfile.TemporaryDirectory() as directory:
            pgn_path, output = Path(directory) / "game.pgn", Path(directory) / "game.mp4"
            payload = claim.json()
            pgn_path.write_bytes(request("GET", URL + payload["pgn_url"], headers=H, timeout=60).content)
            timeline = parse_pgn(pgn_path.read_text(encoding="utf-8")); timestamps = None
            if payload.get("video_url") and not payload.get("timestamps_url"):
                video = Path(directory) / "source.mp4"; detected = Path(directory) / "timestamps.json"
                subprocess.run(yt_dlp_command(payload["video_url"], video), check=True)
                detect_timestamps(video, len(timeline["moves"]), detected)
                request("POST", f"{URL}/api/jobs/{jid}/status", headers=H, json={"worker_id": WORKER, "status": "UPLOADING"}, timeout=30)
                with detected.open("rb") as file:
                    request("POST", f"{URL}/api/jobs/{jid}/timestamps-result", headers=H, data={"worker_id": WORKER}, files={"timestamps_file": ("timestamps.json", file, "application/json")}, timeout=60)
                logging.info("Job %s COMPLETED | timestamps detected", jid)
                return True
            if payload.get("timestamps_url"):
                data = request("GET", URL + payload["timestamps_url"], headers=H, timeout=60).content
                timestamps = parse_timestamps(data, len(timeline["moves"]))
            logging.info("Parsed %d plies | timing=%s", len(timeline["moves"]), "timestamps.json" if timestamps else "fixed")
            render_video(timeline, output, timestamps=timestamps)
            request("POST", f"{URL}/api/jobs/{jid}/status", headers=H, json={"worker_id": WORKER, "status": "UPLOADING"}, timeout=30)
            with output.open("rb") as file:
                request("POST", f"{URL}/api/jobs/{jid}/result", headers=H, data={"worker_id": WORKER}, files={"result_file": ("game.mp4", file, "video/mp4")}, timeout=600)
        logging.info("Job %s COMPLETED", jid)
    except Exception as error:
        try:
            request("POST", f"{URL}/api/jobs/{jid}/status", headers=H, json={"worker_id": WORKER, "status": "FAILED", "error_message": str(error)[:1000]}, timeout=30)
        except requests.RequestException: logging.error("Could not report FAILED status")
        logging.exception("Job failed")
    finally:
        stop.set(); thread.join(timeout=1)
    return True


def main():
    available, reason = nvenc_capability()
    logging.info("Chess Render Worker | ID=%s | FFmpeg=%s | NVENC=%s", WORKER, bool(shutil.which("ffmpeg")), available)
    if not available: logging.warning("NVENC check failed: %s", reason or "unknown FFmpeg error")
    while True:
        try:
            if not run_once(): time.sleep(int(os.getenv("POLL_SECONDS", "10")))
        except Exception as error:
            logging.exception("Worker loop error: %s", error); time.sleep(10)


if __name__ == "__main__": main()
