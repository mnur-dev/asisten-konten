"""Start the local app: python -m app"""
import logging
import webbrowser

import uvicorn

HOST, PORT = "127.0.0.1", 8420

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")
    webbrowser.open(f"http://{HOST}:{PORT}")
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="warning")
