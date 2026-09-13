"""Serve a local, auto-refreshing dashboard for volatility decision logs."""
from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def _read_records(path: Path) -> list[dict[str, Any]]:
    """Read valid JSON objects from an append-only decision log.

    A log line being written while the browser polls may be incomplete. It is
    skipped until the next refresh instead of breaking the dashboard.

    :param path: JSONL decision-log path.
    :returns: Valid decision records in file order.
    """

    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    content = path.read_text(encoding="utf-8")
    offset = 0
    while offset < len(content):
        while offset < len(content) and content[offset].isspace():
            offset += 1
        if offset >= len(content):
            break
        try:
            record, offset = decoder.raw_decode(content, offset)
        except json.JSONDecodeError:
            break
        if isinstance(record, dict) and record.get("event") == "volatility_decision":
            records.append(record)
    return records


def _page() -> bytes:
    """Return the single-page dashboard HTML.

    :returns: UTF-8 encoded browser page.
    """

    return Path(__file__).with_name("app.html").read_bytes()


def handler_for(log_path: Path) -> type[BaseHTTPRequestHandler]:
    """Create a request handler bound to one local decision log.

    :param log_path: JSONL file the dashboard polls.
    :returns: HTTP handler class serving the page and its JSON endpoint.
    """

    class DashboardHandler(BaseHTTPRequestHandler):
        """Serve the dashboard page and current log records."""

        def do_GET(self) -> None:
            """Handle dashboard-page and JSON polling requests."""

            if self.path == "/api/decisions":
                body = json.dumps(_read_records(log_path), allow_nan=False).encode("utf-8")
                content_type = "application/json"
            elif self.path == "/":
                body = _page()
                content_type = "text/html; charset=utf-8"
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            """Suppress routine browser polling lines from the terminal.

            :param format: Standard-library HTTP log format.
            :param args: Values for the format string.
            """

    return DashboardHandler


def main() -> None:
    """Start the local dashboard server bound to loopback only."""

    parser = argparse.ArgumentParser(description="Show live explainable volatility decisions in a browser.")
    parser.add_argument("--log", default="data/volatility-decisions.jsonl", help="Decision JSONL file to tail")
    parser.add_argument("--port", type=int, default=8765, help="Loopback HTTP port")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(Path(args.log)))
    print(f"Dashboard: http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
