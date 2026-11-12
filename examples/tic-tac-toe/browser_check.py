#!/usr/bin/env python3
"""Real-browser acceptance check (headless Chromium-family browser), not the Node DOM shim.

  python3 browser_check.py                         # the engine-built ./index.html
  python3 browser_check.py --page acceptance/fixtures/reference.html

Starts serve.py's server on the first free port from 8001, opens
acceptance/browser-check.html in headless Edge/Chrome/Chromium (WSL: the Windows Edge
install via interop; Linux: chromium/google-chrome on PATH) with a throwaway profile,
and reads the JSON results the harness writes into the DOM. Exit 0 = every scenario
passed, 1 = a scenario failed, 3 = no usable browser (the check did NOT run).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import serve  # noqa: E402

WINDOWS_BROWSERS = ["/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
                    "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
                    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"]


def find_browser() -> tuple[str, bool] | None:
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        p = shutil.which(name)
        if p:
            return p, False
    for p in WINDOWS_BROWSERS:
        if os.path.isfile(p):
            return p, True
    return None


def profile_dir(windows: bool) -> tuple[str, str]:
    """(path to clean up from WSL, path to pass to the browser)."""
    if not windows:
        d = tempfile.mkdtemp(prefix="ttt-browser-")
        return d, d
    win_tmp = subprocess.run(["cmd.exe", "/c", "echo %TEMP%"], capture_output=True, text=True,
                             cwd="/mnt/c").stdout.strip()
    unix_tmp = subprocess.run(["wslpath", "-u", win_tmp], capture_output=True, text=True).stdout.strip()
    d = tempfile.mkdtemp(prefix="ttt-browser-", dir=unix_tmp)
    return d, subprocess.run(["wslpath", "-w", d], capture_output=True, text=True).stdout.strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", default="/", help="page to test, relative to this directory (default: /)")
    ap.add_argument("--label")
    args = ap.parse_args(argv)
    target = os.path.join(HERE, args.page.lstrip("/") or "index.html")
    if not os.path.isfile(target):
        print(f"NOT RUN: {target} does not exist yet (engine build incomplete — see README.md)")
        return 3
    found = find_browser()
    if not found:
        print("NO BROWSER: install chromium (or run under WSL with Windows Edge) — check NOT run")
        return 3
    browser, windows = found
    httpd = serve.bind(serve.LABEL_PORTS.get((args.label or "").upper(), 8001))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    page = args.page if args.page.startswith("/") else "/" + args.page
    url = f"http://localhost:{port}/acceptance/browser-check.html?page={page}"
    cleanup, prof = profile_dir(windows)
    try:
        p = subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-first-run",
                            "--no-default-browser-check", f"--user-data-dir={prof}",
                            "--virtual-time-budget=20000", "--dump-dom", url],
                           capture_output=True, text=True, timeout=120,
                           cwd="/mnt/c" if windows else None)
    finally:
        httpd.shutdown()
        shutil.rmtree(cleanup, ignore_errors=True)
    m = re.search(r'<pre id="result">(.*?)</pre>', p.stdout, re.S)
    if not m or m.group(1).strip() == "PENDING":
        print(f"browser produced no results (exit {p.returncode}); stderr tail:\n{p.stderr[-800:]}")
        return 1
    import html as _html
    data = json.loads(_html.unescape(m.group(1)))
    print(f"browser: {data['userAgent']}")
    print(f"served:  http://localhost:{port}{page}  (file {os.path.join(HERE, page.lstrip('/') or 'index.html')})")
    failed = [r for r in data["results"] if not r["ok"]]
    for r in data["results"]:
        print(f"  {'PASS' if r['ok'] else 'FAIL'}  {r['name']}" + ("" if r["ok"] else f"  — {r['detail']}"))
    print(f"{len(data['results']) - len(failed)}/{len(data['results'])} browser scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
