"""Shared launcher for chat.py / chat.pyw."""

from __future__ import annotations

import threading
import time
import webbrowser

from app import create_app
from app.config import load_settings


def _open_browser(url: str, delay: float = 1.2) -> None:
    time.sleep(delay)
    webbrowser.open(url)


def main() -> None:
    settings = load_settings()
    app = create_app()
    url = f"http://{settings.host}:{settings.port}/"
    threading.Thread(target=_open_browser, args=(url,), daemon=True).start()
    app.run(host=settings.host, port=settings.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
