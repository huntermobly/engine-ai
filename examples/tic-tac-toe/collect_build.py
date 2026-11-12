#!/usr/bin/env python3
"""Copy an engine build's agent-produced artifacts into this example — nothing else.

  python3 collect_build.py ~/.engine-ai/builds/<workspace>

Reads the workspace's stored report (builds-state/<tag>-<hash>/report.json), and copies
ONLY the artifacts of tasks whose status is `done`, after checking each file's sha256
against the hash the engine recorded when the task was verified. A failed/blocked task
contributes nothing, so this cannot turn an incomplete build into a finished example.
Also refreshes build/plan.json and build/latest-report.json (with $HOME shown as ~).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))


def _clean(o, home):
    if isinstance(o, str):
        return o.replace(home, "~")
    if isinstance(o, list):
        return [_clean(x, home) for x in o]
    if isinstance(o, dict):
        return {k: _clean(v, home) for k, v in o.items()}
    return o


def main(argv=None) -> int:
    from engine_core.pipeline import BuildPipeline
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__)
        return 2
    ws = os.path.realpath(os.path.expanduser(args[0]))
    run_dir = BuildPipeline().run_dir(ws)
    try:
        with open(os.path.join(run_dir, "report.json"), encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"✗ no build report for {ws} ({e.__class__.__name__}) — is this the workspace of an "
              "engine build? Start or resume one first (engine_build_start / engine-ai runtime resume).",
              file=sys.stderr)
        return 1
    copied, skipped = [], []
    for t in report["tasks"]:
        if t["status"] != "done":
            skipped.append(f"{t['id']} ({t['status']})")
            continue
        for rel, digest in (t.get("artifact_hashes") or {}).items():
            src = os.path.join(ws, rel)
            with open(src, "rb") as f:
                actual = hashlib.sha256(f.read()).hexdigest()
            if actual != digest:
                print(f"✗ {rel} changed since task {t['id']} was verified — refusing to copy")
                return 1
            dst = os.path.join(HERE, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            copied.append(f"{rel} ← {t['id']} ({t['profile']}, sha256 {digest[:12]})")
    home = os.path.expanduser("~")
    os.makedirs(os.path.join(HERE, "build"), exist_ok=True)  # $HOME → ~ in the records
    with open(os.path.join(run_dir, "plan.json"), encoding="utf-8") as f:
        plan = json.load(f)
    with open(os.path.join(HERE, "build", "plan.json"), "w", encoding="utf-8") as f:
        json.dump(_clean(plan, home), f, indent=2)
    with open(os.path.join(HERE, "build", "latest-report.json"), "w", encoding="utf-8") as f:
        json.dump(_clean(report, home), f, indent=2)
    print(f"run {report['run_id']}: {report['status']} ({report['complete']}/{report['total']})")
    for c in copied:
        print("  copied ", c)
    for s in skipped:
        print("  skipped", s, "— not done, nothing copied")
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
