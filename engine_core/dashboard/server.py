"""Local live dashboard server — stdlib only, real-time sync over Server-Sent Events.

  GET /               the dashboard page
  GET /api/snapshot   the current snapshot (JSON) — also the polling fallback
  GET /api/stream     SSE: `snapshot` on connect and on every change, `heartbeat` every 10s
  GET /healthz        liveness

A watcher thread stats engine-ai's files every `interval` seconds; when anything changes
(a progress event, a report, a memory save, an MCP tool call, a build process dying) it
rebuilds the snapshot ONCE and pushes it to every connected client, each stamped with a
monotonically increasing version — so every open tab shows the same state.

Binds to 127.0.0.1 by default and rejects foreign Host headers (DNS-rebinding guard):
the data is your local engine-ai history and is never meant to leave the machine.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from engine_core.dashboard.collector import Collector

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
HEARTBEAT_S = 10.0


class Hub:
    """Latest snapshot + version, and a condition that wakes every streaming client."""

    def __init__(self, collector: Collector, interval: float = 1.0):
        self.collector = collector
        self.interval = max(0.2, float(interval))
        self.cond = threading.Condition()
        self.version = 0
        self.payload = b"{}"
        self.error: str | None = None
        self._fp = None
        self._stop = threading.Event()
        self.refresh(force=True)

    def refresh(self, force: bool = False) -> bool:
        fp = self.collector.fingerprint()
        if not force and fp == self._fp:
            return False
        try:
            snap = self.collector.snapshot()
            self.error = None
        except Exception as e:  # noqa: BLE001 - keep serving the last good snapshot
            self.error = f"{type(e).__name__}: {e}"
            return False
        with self.cond:
            self._fp = fp
            self.version += 1
            snap["version"] = self.version
            self.payload = json.dumps(snap, default=str, separators=(",", ":")).encode()
            self.cond.notify_all()
        return True

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            self.refresh()

    def stop(self) -> None:
        self._stop.set()
        with self.cond:
            self.cond.notify_all()

    def wait_newer(self, version: int, timeout: float) -> tuple[int, bytes]:
        with self.cond:
            if self.version <= version and not self._stop.is_set():
                self.cond.wait(timeout)
            return self.version, self.payload


def _handler(hub: Hub, allowed_hosts: set[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "engine-ai-dashboard"

        def log_message(self, fmt, *args):  # quiet: the dashboard is the log
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return not allowed_hosts or host in allowed_hosts

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return self._send(403, b"forbidden host", "text/plain")
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                try:
                    with open(STATIC, "rb") as f:
                        body = f.read()
                except OSError:
                    return self._send(500, b"dashboard page missing", "text/plain")
                return self._send(200, body, "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'self'; style-src 'self' "
                                   "'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:"})
            if path == "/api/snapshot":
                return self._send(200, hub.payload, "application/json")
            if path == "/healthz":
                return self._send(200, json.dumps({"ok": hub.error is None, "version": hub.version,
                                                   "error": hub.error}).encode(), "application/json")
            if path == "/api/stream":
                return self._stream()
            return self._send(404, b"not found", "text/plain")

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                last = int(self.headers.get("Last-Event-ID") or 0)
            except ValueError:
                last = 0
            sent = -1 if last != hub.version else last  # reconnect: resend only if missed one
            try:
                self.wfile.write(b"retry: 2000\n\n")
                while not hub._stop.is_set():
                    version, payload = hub.wait_newer(sent, HEARTBEAT_S)
                    if version != sent:
                        self.wfile.write(b"id: %d\nevent: snapshot\ndata: " % version + payload + b"\n\n")
                        sent = version
                    else:
                        beat = json.dumps({"ts": time.time(), "version": version, "error": hub.error})
                        self.wfile.write(b"event: heartbeat\ndata: " + beat.encode() + b"\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return  # client went away

    return Handler


def serve(home: str, host: str = "127.0.0.1", port: int = 8765, baseline_model: str | None = None,
          interval: float = 1.0, port_tries: int = 10, ready=None) -> int:
    hub = Hub(Collector(home, baseline_model), interval)
    allowed = {"localhost", "127.0.0.1", "::1", host.lower()} if host not in ("0.0.0.0", "::") else set()
    httpd, err = None, None
    for p in range(port, port + max(1, port_tries)):
        try:
            httpd = ThreadingHTTPServer((host, p), _handler(hub, allowed))
            break
        except OSError as e:
            err = e
            if port == 0:
                break
    if httpd is None:
        raise OSError(f"cannot bind {host}:{port}-{port + port_tries - 1}: {err} — pass --port")
    httpd.daemon_threads = True
    watcher = threading.Thread(target=hub.run, name="dashboard-watcher", daemon=True)
    watcher.start()
    url = f"http://{'localhost' if host in ('127.0.0.1', '::1') else host}:{httpd.server_address[1]}/"
    if ready:
        ready(url, httpd, hub)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop()
        httpd.server_close()
    return 0
