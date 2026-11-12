"""Test install.sh against a sandboxed HOME with a stub `claude` on PATH."""
import json
import os
import subprocess
import tempfile
import unittest


def _read(*a, **k):
    with open(*a, **k) as f:
        return f.read()


def _writefile(path, text, mode="w"):
    with open(path, mode) as f:
        f.write(text)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestInstaller(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.bin = tempfile.mkdtemp()
        # stub `claude` that records `mcp` calls and supports --version
        self.log = os.path.join(self.home, "claude-calls.log")
        stub = os.path.join(self.bin, "claude")
        with open(stub, "w") as f:
            f.write("#!/usr/bin/env bash\n"
                    'if [ "$1" = "--version" ]; then echo "9.9.9 (stub)"; exit 0; fi\n'
                    f'echo "$@" >> "{self.log}"\n'
                    'exit 0\n')
        os.chmod(stub, 0o755)
        self.env = {**os.environ, "HOME": self.home,
                    "CLAUDE_CONFIG_DIR": os.path.join(self.home, ".claude"),
                    "PATH": self.bin + os.pathsep + os.environ["PATH"]}

    def _run(self, *args):
        return subprocess.run(["bash", os.path.join(REPO, "install.sh"), *args],
                              env=self.env, capture_output=True, text=True)

    def test_install_places_everything(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        cdir = self.env["CLAUDE_CONFIG_DIR"]
        # skills + commands symlinked
        self.assertTrue(os.path.islink(os.path.join(cdir, "skills", "deployable-app")))
        self.assertTrue(os.path.islink(os.path.join(cdir, "commands", "new-app.md")))
        # settings.json has our hook and is valid JSON
        cfg = json.loads(_read(os.path.join(cdir, "settings.json")))
        cmds = [h["command"] for g in cfg["hooks"]["SessionStart"] for h in g["hooks"]]
        self.assertTrue(any("session-start.sh" in c for c in cmds))
        # MCP registered via the stub
        log = _read(self.log)
        self.assertIn("mcp add", log)
        self.assertIn("engine-ai", log)

    def test_idempotent(self):
        self._run(); self._run()  # twice
        cfg = json.loads(_read(os.path.join(self.env["CLAUDE_CONFIG_DIR"], "settings.json")))
        cmds = [h["command"] for g in cfg["hooks"]["SessionStart"] for h in g["hooks"]
                if "session-start.sh" in h["command"]]
        self.assertEqual(len(cmds), 1)  # not duplicated

    def test_preserves_existing_settings(self):
        cdir = self.env["CLAUDE_CONFIG_DIR"]
        os.makedirs(cdir, exist_ok=True)
        _writefile(os.path.join(cdir, "settings.json"), json.dumps(
            {"editor": {"theme": "dark"}, "hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": "echo existing"}]}]}}))
        self._run()
        cfg = json.loads(_read(os.path.join(cdir, "settings.json")))
        self.assertEqual(cfg["editor"]["theme"], "dark")  # untouched
        cmds = [h["command"] for g in cfg["hooks"]["SessionStart"] for h in g["hooks"]]
        self.assertIn("echo existing", cmds)              # existing hook kept
        self.assertTrue(any("session-start.sh" in c for c in cmds))  # ours added

    def test_uninstall_reverses(self):
        self._run()
        r = self._run("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        cdir = self.env["CLAUDE_CONFIG_DIR"]
        self.assertFalse(os.path.exists(os.path.join(cdir, "skills", "deployable-app")))
        cfg = json.loads(_read(os.path.join(cdir, "settings.json")))
        cmds = [h["command"] for g in cfg.get("hooks", {}).get("SessionStart", []) for h in g["hooks"]]
        self.assertFalse(any("session-start.sh" in c for c in cmds))
        self.assertIn("mcp remove", _read(self.log))

    def test_fails_without_claude(self):
        env = {**self.env, "PATH": "/usr/bin:/bin"}  # no stub claude
        r = subprocess.run(["bash", os.path.join(REPO, "install.sh")],
                           env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("NOT installed", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
