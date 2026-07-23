"""frame_viewer — launch the frame-by-frame algorithm visualizer in a browser.

The actual visualizer is frame_viewer.html (this directory) — a self-contained page
with no build step and no dependencies, so opening that file directly and dragging a
.jsonl frame log onto it already works with zero setup. This module exists purely for
convenience: it serves the page over a throwaway local HTTP server, and if you gave it
a file, serves that too at a fixed path the page auto-loads on open — so the browser
comes up with your data already there instead of you needing to click "Open" yourself.

Usage:
    python -m antbot.frame_viewer                     # opens the empty viewer
    python -m antbot.frame_viewer path/to/frames.jsonl # opens it pre-loaded

Generate a frames file with:
    python -m antbot.navtests <test name> --frame-log frames.jsonl
"""
from __future__ import annotations

import argparse
import http.server
import threading
import webbrowser
from pathlib import Path

HERE = Path(__file__).parent
HTML_PATH = HERE / "frame_viewer.html"


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves exactly two things: the viewer page, and (if given) one JSONL file at a
    fixed path — never the rest of antbot/, unlike a plain SimpleHTTPRequestHandler
    rooted at this directory. `jsonl_path` is set via functools.partial in `serve()`.
    """
    jsonl_path: Path | None = None

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — http.server's naming convention
        path = self.path.split("?", 1)[0]
        if path == "/data.jsonl":
            if self.jsonl_path is None or not self.jsonl_path.exists():
                self.send_error(404, "no frame log loaded for this session")
                return
            self._send(self.jsonl_path.read_bytes(), "application/x-ndjson; charset=utf-8")
            return
        # Everything else (including "/") serves the one page there is.
        self._send(HTML_PATH.read_bytes(), "text/html; charset=utf-8")

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — stdlib's signature
        pass  # a throwaway local viewer has no business narrating GETs to the console


def serve(jsonl_path: Path | None, port: int, open_browser: bool) -> None:
    handler_cls = type("_BoundHandler", (_Handler,), {"jsonl_path": jsonl_path})
    httpd = http.server.HTTPServer(("127.0.0.1", port), handler_cls)
    actual_port = httpd.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/frame_viewer.html"
    print(f"antbot frame viewer: serving {url}"
          + (f"  (preloaded {jsonl_path})" if jsonl_path else "  (no file — drag one in)"))
    print("Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("jsonl", nargs="?", default=None,
                    help="a frames JSONL file (from navtests --frame-log) to preload")
    ap.add_argument("--port", type=int, default=0,
                    help="fixed local port (default: let the OS pick a free one)")
    ap.add_argument("--no-browser", action="store_true",
                    help="print the URL instead of opening it automatically")
    args = ap.parse_args()

    jsonl_path = None
    if args.jsonl:
        jsonl_path = Path(args.jsonl).resolve()
        if not jsonl_path.exists():
            raise SystemExit(f"no such file: {jsonl_path}")

    serve(jsonl_path, args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
