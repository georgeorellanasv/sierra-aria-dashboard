"""
Serve the reports/ directory on localhost:8765 and open the diagnostic in
the default browser.

Run:
  python scripts/serve_reports.py
"""
from __future__ import annotations

import http.server
import socketserver
import sys
import threading
import webbrowser
from pathlib import Path

PORT = 8765
ROOT = Path(__file__).resolve().parent.parent / "reports"
LANDING = "diagnostic.html"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, fmt, *args):
        # Quieter default logging — one line per request
        sys.stderr.write(f"  {self.address_string()} -> {fmt % args}\n")


def main() -> int:
    if not (ROOT / LANDING).exists():
        print(f"Missing {ROOT / LANDING}. Run build_diagnostic_html.py first.",
              file=sys.stderr)
        return 1

    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
        url = f"http://127.0.0.1:{PORT}/{LANDING}"
        print(f"Serving {ROOT}")
        print(f"Open:   {url}")
        print(f"Stop:   Ctrl-C")
        threading.Timer(0.6, webbrowser.open, args=[url]).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
