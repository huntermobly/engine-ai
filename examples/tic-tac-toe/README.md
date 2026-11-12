# Tic-tac-toe — built by the engine-ai agents

A classic two-player tic-tac-toe game in one self-contained `index.html`, built by the
engine's own agents through MCP, plus independent acceptance tests.

## Build status: BLOCKED at the implementation step (provider quota)

| Task | Profile → model | Depends on | Artifact | Result |
|---|---|---|---|---|
| `spec-contract` | documentation → `nvidia/nemotron-3.5-super-vl:free` | — | [`SPEC.md`](SPEC.md) | **done**: written by the agent, 3/3 sandboxed checks passed |
| `implement-game` | frontend → `nvidia/nemotron-3.5-super-vl:free` | spec-contract | `index.html` | **blocked**: OpenRouter `free-models-per-day` (HTTP 429) on the frontend key |
| `author-tests` | test-author → same free model | spec-contract, implement-game | `tests/game.test.js` | not-run (dependency blocked) |
| `review-game` | reviewer (read-only) → same free model | implement-game, author-tests | — | not-run (dependency blocked) |

- The objective was sent from **Claude Code** with `engine_build_start` as run `run-20261109-160438-994897`.
  The planner made 1 call, and the contract agent made 6 model calls and 5 tool calls. Records are in
  [`build/plan.json`](build/plan.json) and [`build/run-1-report.json`](build/run-1-report.json).
- **Codex** then called `engine_build_resume`, which started run `run-20261109-161109-7dd99e`. The stored
  plan was reused, the contract was kept, and the implementation retried. It was still out of quota, so
  the run was blocked again. That record is in [`build/latest-report.json`](build/latest-report.json).
- The engine stopped the run instead of moving the implementation to another profile's key.
  **`index.html` has not been produced yet.** Nothing in this folder was written by hand in its place.

### Finish the build (after the daily quota resets at 00:00 UTC)

From Claude Code or Codex: `engine_build_resume({workspace: "~/.engine-ai/builds/build-a-classic-two-player-tic-t-994897"})`,
then `engine_build_status({run_id, wait_s: 40})` until it is terminal. Or from a shell:

```bash
engine-ai runtime resume ~/.engine-ai/builds/build-a-classic-two-player-tic-t-994897
python3 collect_build.py ~/.engine-ai/builds/build-a-classic-two-player-tic-t-994897
```

`collect_build.py` copies **only** the artifacts of tasks the report marks `done`, after
checking each file's sha256 against the hash recorded at verification time.

The exact objective is the `objective` field of `build/plan.json`. To rebuild from scratch, pass
that objective to `engine_build_start`.

## Tests

| What | Command | Runs in |
|---|---|---|
| Acceptance (independent of the agents) | `node --test acceptance/acceptance.test.js` | Node + `acceptance/dom-shim.js` (**a DOM shim, not a browser**) |
| Same scenarios, real browser | `python3 browser_check.py` | headless Edge/Chrome/Chromium (WSL: Windows Edge via interop) |
| The test agent's own tests | `node --test tests/game.test.js` | Node (once the agent has written them) |
| All of the above + mutation checks + server | `python3 -m unittest tests/test_example_tictactoe.py` (repo root) | Python + Node |

The acceptance tests drive the page only through the DOM hooks the objective requires
(`cell-0` … `cell-8`, `#status`, `#restart`). They cover:
- X moving first and turns alternating;
- rejection of occupied squares;
- all 8 winning lines, for X and for O;
- a draw, and a win on the 9th move;
- rejection of moves after the game is over;
- Restart, both after a win and mid-game;
- self-containment;
- no script errors.

Point them at any page with `TTT_HTML=<file>`.

`acceptance/fixtures/reference.html` is a **hand-written fixture for testing the harness**,
not the game. The repo suite checks that the acceptance tests pass on it and **fail on each of
9 deliberately broken copies**: a missing diagonal, overwriting a square, no draw detection,
moves accepted after a win, no alternation, Restart keeping the turn or the board, an occupied
click consuming the turn, and an external script.

## Serve

```bash
python3 serve.py              # first free port from 8001 → prints file, port and URL
python3 serve.py --label B    # prefer 8002 (A → 8001, B → 8002), else the next free port
```

It serves only this directory, with `/` → `index.html`. It returns `503` while `index.html`
doesn't exist yet and never shows a directory listing. A busy port moves it to the next one,
and it never stops another process.
