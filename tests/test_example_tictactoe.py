"""examples/tic-tac-toe — acceptance harness, mutation checks, the engine-built game, serve.py.

- The acceptance tests (Node + DOM shim) must PASS on the reference fixture and FAIL on
  every deliberately broken copy of it — otherwise they would not prove anything about
  the engine-built game.
- The engine-built index.html (written by the implementation agent) must pass them; while
  the build has not produced it yet, that test is SKIPPED with the reason, never faked.
- serve.py serves only the example directory, `/` → index.html, no listings, and moves
  to the next port when the preferred one is taken.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, "examples", "tic-tac-toe")
FIXTURE = os.path.join(EX, "acceptance", "fixtures", "reference.html")
ACCEPT = os.path.join(EX, "acceptance", "acceptance.test.js")
NODE = shutil.which("node")

MUTANTS = {
    "missing diagonal 2-4-6": ("[0, 4, 8], [2, 4, 6]]", "[0, 4, 8]]"),
    "occupied square overwritten": ('if (g.winner || g.board[i] !== "") return false;', "if (g.winner) return false;"),
    "draw never detected": ('else if (g.board.every((v) => v !== "")) g.winner = "draw";', ""),
    "moves accepted after game over": ('if (g.winner || g.board[i] !== "") return false;',
                                       'if (g.board[i] !== "") return false;'),
    "no alternation": ('g.current = g.current === "X" ? "O" : "X";', 'g.current = "X";'),
    "restart keeps the turn": ('reset() { g.board = Array(9).fill(""); g.current = "X"; g.winner = null; }',
                               'reset() { g.board = Array(9).fill(""); g.winner = null; }'),
    "restart keeps the board": ('reset() { g.board = Array(9).fill(""); g.current = "X"; g.winner = null; }',
                                'reset() { g.current = "X"; g.winner = null; }'),
    "occupied click consumes the turn": ('if (g.winner || g.board[i] !== "") return false;',
                                         'if (g.winner) return false; if (g.board[i] !== "") '
                                         '{ g.current = g.current === "X" ? "O" : "X"; return false; }'),
    "external script": ("<script id=\"game-logic\">",
                        "<script src=\"https://cdn.example.com/x.js\"></script><script id=\"game-logic\">"),
}


def _accept(page):
    p = subprocess.run([NODE, "--test", ACCEPT], env={**os.environ, "TTT_HTML": page},
                       capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout + p.stderr


@unittest.skipUnless(NODE, "node not installed")
class TestAcceptanceHarness(unittest.TestCase):
    def test_passes_on_the_reference_fixture(self):
        code, out = _accept(FIXTURE)
        self.assertEqual(code, 0, out[-3000:])
        self.assertIn("# fail 0", out)

    def test_every_deliberate_defect_is_caught(self):
        with open(FIXTURE, encoding="utf-8") as f:
            good = f.read()
        d = tempfile.mkdtemp()
        survived = []
        for name, (old, new) in MUTANTS.items():
            self.assertIn(old, good, f"mutant {name!r} no longer applies to the fixture")
            path = os.path.join(d, "mutant.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(good.replace(old, new, 1))
            code, _ = _accept(path)
            if code == 0:
                survived.append(name)
        self.assertEqual(survived, [], "acceptance tests missed these defects")


BUILT = os.path.join(EX, "index.html")


@unittest.skipUnless(NODE, "node not installed")
@unittest.skipUnless(os.path.isfile(BUILT), "engine build has not produced examples/tic-tac-toe/index.html "
                                            "yet (implementation task blocked — see examples/tic-tac-toe/README.md)")
class TestEngineBuiltGame(unittest.TestCase):
    def test_engine_built_game_passes_acceptance(self):
        code, out = _accept(BUILT)
        self.assertEqual(code, 0, out[-4000:])

    def test_agent_written_tests_pass(self):
        agent_tests = os.path.join(EX, "tests", "game.test.js")
        if not os.path.isfile(agent_tests):
            self.skipTest("the test agent's tests/game.test.js is not present")
        p = subprocess.run([NODE, "--test", agent_tests], cwd=EX, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, (p.stdout + p.stderr)[-3000:])


class TestServe(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, EX)
        import serve
        self.serve = serve
        self.tmp = tempfile.mkdtemp()
        self.orig = serve.ROOT
        serve.ROOT = os.path.realpath(self.tmp)  # serve a scratch copy, not the real example
        os.makedirs(os.path.join(self.tmp, "acceptance"))
        with open(os.path.join(self.tmp, "acceptance", "x.txt"), "w") as f:
            f.write("x")

    def tearDown(self):
        self.serve.ROOT = self.orig

    def _start(self, preferred=8001):
        httpd = self.serve.bind(preferred)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]

    def _get(self, port, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            e.close()
            return e.code, ""

    def test_root_is_index_html_and_never_a_listing(self):
        port = self._start()
        self.assertEqual(self._get(port, "/")[0], 503, "missing index.html must not show a listing")
        with open(os.path.join(self.tmp, "index.html"), "w") as f:
            f.write("<h1>THIS RUN's GAME</h1>")
        status, body = self._get(port, "/")
        self.assertEqual((status, body), (200, "<h1>THIS RUN's GAME</h1>"))
        self.assertEqual(self._get(port, "/acceptance/")[0], 404)
        self.assertEqual(self._get(port, "/../../etc/passwd")[0] in (403, 404), True)

    def test_busy_port_moves_to_the_next_one_without_killing(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy = blocker.getsockname()[1]
        try:
            port = self._start(busy)
            self.assertNotEqual(port, busy)
            self.assertGreater(port, busy)
            blocker.getsockname()  # the other listener is still alive
        finally:
            blocker.close()


if __name__ == "__main__":
    unittest.main()
