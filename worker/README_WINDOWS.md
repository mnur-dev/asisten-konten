# Windows worker setup

1. Install Python 3.11+ from https://www.python.org/downloads/windows/ and enable **Add Python to PATH**.
2. Install current NVIDIA driver. Install FFmpeg build containing `h264_nvenc`; add its `bin` directory to `PATH`.
3. Clone/copy repository, then open PowerShell in repository root.
4. Create environment and install dependencies:
```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```
5. Verify FFmpeg and real NVENC initialization:
```powershell
ffmpeg -version
ffmpeg -hide_banner -encoders | findstr h264_nvenc
ffmpeg -y -f lavfi -i color=size=64x64:rate=1 -frames:v 1 -c:v h264_nvenc -f null NUL
```
Last command must exit without encoder error. Worker automatically uses `libx264` when NVENC cannot initialize.

6. Set environment for current PowerShell session:
```powershell
$env:CONTROLLER_URL="https://YOUR-CONTROLLER-DOMAIN"
$env:WORKER_TOKEN="SAME-LONG-TOKEN-AS-VPS"
$env:WORKER_ID="desktop-01"
$env:POLL_SECONDS="10"
```
Use HTTPS in production. Never commit token. `.env` remains ignored; current MVP reads process environment directly.

7. Start from repository root:
```powershell
.\.venv\Scripts\python.exe -m worker.worker
```
Expected startup includes worker ID, FFmpeg status, and NVENC status. PC may stop anytime; WAITING jobs stay on VPS.
