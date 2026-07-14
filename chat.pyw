"""
Launch the Prohance Flask chat app.

Windows:
  python chat.pyw

On Windows, .pyw runs without a console window. To see logs/errors, run:
  python -c "import runpy; runpy.run_path('chat.pyw')"
or temporarily rename to chat.py.
"""

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
    # use_reloader=False so double-process does not open two browser tabs
    app.run(host=settings.host, port=settings.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
