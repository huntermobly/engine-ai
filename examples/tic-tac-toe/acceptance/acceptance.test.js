// Independent ACCEPTANCE tests for the engine-built tic-tac-toe (node --test).
// They drive the page ONLY through the DOM hooks the build objective requires
// (cell-0..cell-8 buttons, #status, #restart) inside a Node DOM SHIM — not a browser;
// browser_check.py repeats the scenario in a real (headless) browser.
// Page under test: $TTT_HTML, default ../index.html (the engine build's artifact).
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { load } = require("./dom-shim.js");

const PAGE = path.resolve(process.env.TTT_HTML || path.join(__dirname, "..", "index.html"));
const HTML = fs.readFileSync(PAGE, "utf8");
const LINES = [[0, 1, 2], [3, 4, 5], [6, 7, 8], [0, 3, 6], [1, 4, 7], [2, 5, 8], [0, 4, 8], [2, 4, 6]];

function game() {
  const { document } = load(HTML);
  const cell = (i) => {
    const el = document.getElementById(`cell-${i}`);
    assert.ok(el, `missing #cell-${i}`);
    return el;
  };
  return {
    document,
    click: (i) => cell(i).click(),
    mark: (i) => cell(i).textContent.trim(),
    board: () => Array.from({ length: 9 }, (_, i) => cell(i).textContent.trim()),
    status: () => (document.getElementById("status") || { textContent: "" }).textContent.trim(),
    restart: () => document.getElementById("restart").click(),
    errors: () => document.errors,
  };
}
// X plays `xs`, O plays `os`, alternating X first
function play(g, xs, os) {
  for (let i = 0; i < Math.max(xs.length, os.length); i++) {
    if (i < xs.length) g.click(xs[i]);
    if (i < os.length) g.click(os[i]);
  }
}
const says = (s, who) => new RegExp(`\\b${who}\\b`).test(s);
const winText = (s, who) => says(s, who) && /win/i.test(s);

test("self-contained single file: inline style + script, no external URLs", () => {
  assert.match(HTML, /<style[\s>]/i);
  assert.match(HTML, /<script[\s>]/i);
  assert.doesNotMatch(HTML, /<script[^>]+\bsrc\s*=/i, "external script");
  assert.doesNotMatch(HTML, /<link[^>]+rel\s*=\s*["']?stylesheet/i, "external stylesheet");
  assert.doesNotMatch(HTML, /(src|href)\s*=\s*["']?(https?:)?\/\//i, "external URL");
  assert.doesNotMatch(HTML, /@import/i);
});

test("required DOM hooks: nine buttons cell-0..cell-8, #status, #restart", () => {
  const g = game();
  for (let i = 0; i < 9; i++) assert.equal(g.document.getElementById(`cell-${i}`).tagName, "BUTTON");
  assert.ok(g.document.getElementById("status"));
  assert.ok(g.document.getElementById("restart"));
  assert.deepEqual(g.board(), Array(9).fill(""));
  assert.ok(says(g.status(), "X"), `status should name X first, got ${JSON.stringify(g.status())}`);
});

test("X moves first and turns alternate", () => {
  const g = game();
  g.click(0);
  assert.equal(g.mark(0), "X");
  assert.ok(says(g.status(), "O"), g.status());
  g.click(4);
  assert.equal(g.mark(4), "O");
  assert.ok(says(g.status(), "X"), g.status());
  g.click(8);
  assert.deepEqual(g.board(), ["X", "", "", "", "O", "", "", "", "X"]);
});

test("an occupied square is rejected and the turn does not pass", () => {
  const g = game();
  g.click(0); g.click(0);
  assert.equal(g.mark(0), "X", "occupied square was overwritten");
  g.click(1);
  assert.equal(g.mark(1), "O", "a rejected click must not consume O's turn");
  g.click(1);
  assert.equal(g.mark(1), "O");
});

for (const line of LINES) {
  test(`X wins on line ${line.join("-")}`, () => {
    const g = game();
    const others = [0, 1, 2, 3, 4, 5, 6, 7, 8].filter((i) => !line.includes(i));
    // O's two moves never complete a line; X completes `line` on its 3rd move
    play(g, line, others.slice(0, 2));
    assert.ok(winText(g.status(), "X"), `expected an X win, status=${JSON.stringify(g.status())}`);
    const before = g.board();
    for (const i of others.slice(2)) g.click(i); // moves after game over
    assert.deepEqual(g.board(), before, "moves were accepted after the game was won");
  });
}

test("O can win too", () => {
  const g = game();
  play(g, [0, 1, 8], [3, 4, 5]);
  assert.ok(winText(g.status(), "O"), g.status());
  assert.ok(!winText(g.status(), "X"));
});

test("a full board with no line is a draw, and nothing changes afterwards", () => {
  const g = game();
  // X O X / X O O / O X X
  play(g, [0, 2, 3, 7, 8], [1, 4, 5, 6]);
  assert.deepEqual(g.board(), ["X", "O", "X", "X", "O", "O", "O", "X", "X"]);
  assert.match(g.status(), /draw/i);
  assert.doesNotMatch(g.status(), /win/i);
});

test("a win on the ninth move is a win, not a draw", () => {
  const g = game();
  play(g, [0, 7, 2, 6, 4], [1, 3, 5, 8]); // X completes 2-4-6 on move 9
  assert.ok(g.board().every((v) => v !== ""));
  assert.ok(winText(g.status(), "X"), g.status());
  assert.doesNotMatch(g.status(), /draw/i);
});

test("Restart empties the board and X moves first again (after a win)", () => {
  const g = game();
  play(g, [0, 1, 2], [3, 4]);
  assert.ok(winText(g.status(), "X"));
  g.restart();
  assert.deepEqual(g.board(), Array(9).fill(""));
  assert.ok(says(g.status(), "X") && !/win/i.test(g.status()), g.status());
  g.click(5);
  assert.equal(g.mark(5), "X", "X must move first after restart");
  g.click(6);
  assert.equal(g.mark(6), "O");
});

test("Restart mid-game resets the turn to X", () => {
  const g = game();
  g.click(0);
  g.restart();
  g.click(4);
  assert.equal(g.mark(4), "X");
});

test("no script errors while playing", () => {
  const g = game();
  play(g, [0, 1, 2], [3, 4]);
  g.restart();
  play(g, [0, 2, 3, 7, 8], [1, 4, 5, 6]);
  assert.deepEqual(g.errors().map(String), []);
});
