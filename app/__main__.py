"""Start the local app: python -m app"""
import logging
import os
import threading
import webbrowser

import uvicorn

HOST, PORT = "127.0.0.1", 8420


def open_browser_when_ready(url: str, timeout: float = 30.0) -> None:
    """Wait for the port to accept connections, then open the browser once."""
    import socket
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=0.5):
                break
        except OSError:
            time.sleep(0.25)
    else:
        return
    webbrowser.open(url)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")
    # The .bat launcher opens the browser itself once the port is up; without
    # this guard both it and we would open a tab.
    if os.environ.get("ASISTEN_NO_BROWSER") != "1":
        threading.Thread(
            target=open_browser_when_ready,
            args=(f"http://{HOST}:{PORT}",),
            daemon=True,
        ).start()
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="warning")
