"""The pollers ride out GitHub/Notion rate limits and brief outages instead of failing the whole poll."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from teampulse import http
from teampulse.ingest import github_poller


@pytest.fixture()
def flaky_server(monkeypatch):
    """Answers 503, then 429 (Retry-After: 0), then the real page."""
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            if len(hits) == 1:
                self.send_response(503); self.end_headers(); return
            if len(hits) == 2:
                self.send_response(429); self.send_header("Retry-After", "0"); self.end_headers(); return
            body = json.dumps([{"ok": True}]).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(http, "RETRY", http.RETRY.new(backoff_factor=0))
    monkeypatch.setattr(github_poller, "API", f"http://127.0.0.1:{server.server_port}")
    yield hits
    server.shutdown()


def test_github_poll_retries_rate_limits_and_outages(flaky_server):
    client = github_poller.GitHubClient("token")
    assert list(client.paginate("/repos/x/y/issues")) == [{"ok": True}]
    assert len(flaky_server) == 3


def test_retries_are_bounded():
    assert http.RETRY.total == 3 and 429 in http.RETRY.status_forcelist and http.RETRY.respect_retry_after_header
