#!/usr/bin/env python3
"""Serve ONLY this example's directory, with the game at the root URL.

  python3 serve.py                 # first free port from 8001
  python3 serve.py --label B       # prefer 8002 (A→8001, B→8002), else the next free one
  python3 serve.py --port 9000     # prefer a specific port

- `/` returns this directory's index.html (the engine-built game) — never a listing;
  directories without an index.html are 404, paths outside the directory are refused.
- If the preferred port is taken it tries the next one (up to 50); it never stops or
  kills another process to free a port.
- Prints the absolute file path, the actual port and the browser URL.
"""
from __future__ import annotations

import argparse
import errno
import http.server
import os
import socketserver
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
LABEL_PORTS = {"A": 8001, "B": 8002}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def list_directory(self, path):  # never expose a directory listing
        self.send_error(404, "no directory listings")
        return None

    def send_head(self):
        if self.path.split("?", 1)[0] in ("", "/") and not os.path.isfile(os.path.join(ROOT, "index.html")):
            self.send_error(503, "index.html has not been generated yet (engine build incomplete)")
            return None
        real = os.path.realpath(self.translate_path(self.path))
        if real != ROOT and not real.startswith(ROOT + os.sep):
            self.send_error(403, "outside the example directory")
            return None
        return super().send_head()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("[serve] " + fmt % args + "\n")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # a port in use must fail the bind, not be shared


def bind(preferred: int, host: str = "127.0.0.1", tries: int = 50) -> Server:
    last = None
    for port in range(preferred, preferred + tries):
        try:
            return Server((host, port), Handler)
        except OSError as e:
            if e.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
            last = e
    raise OSError(f"no free port in {preferred}-{preferred + tries - 1}: {last}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--label", help="trajectory label: A → 8001, B → 8002")
    ap.add_argument("--port", type=int, help="preferred port (default 8001)")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    preferred = args.port or LABEL_PORTS.get((args.label or "").upper(), 8001)
    httpd = bind(preferred, args.host)
    port = httpd.server_address[1]
    index = os.path.join(ROOT, "index.html")
    print(f"file: {index}{'' if os.path.isfile(index) else '  (MISSING — engine build incomplete)'}")
    print(f"port: {port}{'' if port == preferred else f'  (preferred {preferred} was busy)'}")
    print(f"url:  http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
