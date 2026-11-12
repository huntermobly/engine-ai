# Tic-Tac-Toe Specification

## 1. Overview

This document specifies a two-player Tic-Tac-Toe game played in the browser.
Player "X" always moves first, players alternate turns, and the game ends when
one player completes a winning line or all nine cells are occupied (a draw).

## 2. DOM Hooks

### 2.1 Cell buttons (`cell-0` … `cell-8`)

Nine `<button>` elements represent the board cells in row-major order:

| Button id | Board position | Row | Column |
|-----------|----------------|-----|--------|
| `cell-0`  | index 0        | 0   | 0      |
| `cell-1`  | index 1        | 0   | 1      |
| `cell-2`  | index 2        | 0   | 2      |
| `cell-3`  | index 3        | 1   | 0      |
| `cell-4`  | index 4        | 1   | 1      |
| `cell-5`  | index 5        | 1   | 2      |
| `cell-6`  | index 6        | 2   | 0      |
| `cell-7`  | index 7        | 2   | 1      |
| `cell-8`  | index 8        | 2   | 2      |

- Each button's text content is empty (`""`) while the cell is unoccupied.
- After a move, the button displays `"X"` or `"O"` and becomes disabled so it
  cannot be clicked again.
- Clicking `cell-N` invokes `play(N)` on the game instance.

### 2.2 Status element (`status`)

A single element with `id="status"` displays the game state as plain text:

| Situation                  | `status` text                     |
|----------------------------|-----------------------------------|
| Player to move             | `"Player X to move"` / `"Player O to move"` |
| Winner announced           | `"Player X wins!"` / `"Player O wins!"`     |
| Draw                       | `"Draw!"`                          |

The text is updated after every move and whenever the game is reset.

### 2.3 Restart button (`restart`)

A `<button>` with `id="restart"` resets the game to its initial state:

- Board cleared: all cells empty and enabled.
- `current` set back to `"X"`.
- `winner` set back to `null`.
- `status` shows `"Player X to move"`.
- Equivalent to calling `reset()` on the game instance.

## 3. JavaScript API: `createGame()`

`createGame()` is a global function that returns a game object with the
following shape:

```js
{
  board,   // string[] of length 9; "" = empty, "X" or "O" = occupied
  current, // "X" | "O" — player whose turn it is
  winner,  // "X" | "O" | null — null while the game is in progress
  play(index),   // (number) -> void
  reset(),       // () -> void
}
```

### 3.1 Initial state

Immediately after `createGame()`:

- `board` is `["", "", "", "", "", "", "", "", ""]`
- `current` is `"X"`
- `winner` is `null`

### 3.2 `play(index)`

`play(index)` records a move for `current` at board position `index` (0–8,
row-major).

Preconditions:
- `index` must be an integer in the range `[0, 8]`.
- `board[index]` must be `""` (unoccupied).
- `winner` must be `null` (the game must not already be over).

If any precondition is violated, `play(index)` throws an `Error` with a
descriptive message and the state is unchanged.

On a successful move:

1. `board[index]` is set to `current`.
2. If `current` now occupies all cells of any winning line (see section 4),
   `winner` is set to `current` and the game is over.
3. Else if all cells are occupied, the game is a draw (`winner` stays `null`,
   game over).
4. Else `current` toggles to the other player (`"X"` ↔ `"O"`).

### 3.3 `reset()`

`reset()` restores the initial state:

- `board` becomes nine empty strings.
- `current` becomes `"X"`.
- `winner` becomes `null`.

## 4. Game Rules

### 4.1 Players

Two players alternate: `"X"` moves first, then `"O"`, then `"X"`, and so on.

### 4.2 Winning lines

A player wins by occupying all three cells of any one of the following 8 lines
(expressed as cell indices):

1. `[0, 1, 2]` — top row
2. `[3, 4, 5]` — middle row
3. `[6, 7, 8]` — bottom row
4. `[0, 3, 6]` — left column
5. `[1, 4, 7]` — middle column
6. `[2, 5, 8]` — right column
7. `[0, 4, 8]` — main diagonal (top-left to bottom-right)
8. `[2, 4, 6]` — anti-diagonal (top-right to bottom-left)

### 4.3 Draw

If all nine cells are occupied and no player has completed a winning line, the
game is a draw.

### 4.4 Moves after game over

Once `winner` is non-null or the board is full, no further moves are accepted;
`play(index)` throws an error until `reset()` is called.

## 5. Test Plan

The following test cases must be covered:

1. **Alternation** — Verify `current` starts as `"X"`, toggles to `"O"` after
   the first move, back to `"X"` after the second, and so on.
2. **All 8 winning lines** — For each winning line in section 4.2, play the
   three cells in any order and verify the corresponding player is announced as
   winner and the game ends.
3. **Draw** — Fill all nine cells without completing a winning line and verify
   the game ends in a draw (`winner` remains `null`, status shows `"Draw!"`).
4. **Occupied cells** — Attempt `play(index)` on an already occupied cell and
   verify it throws and the board/turn are unchanged.
5. **Moves after game over** — After a win or a draw, attempt `play(index)` on
   any empty cell and verify it throws and state is unchanged.
6. **Reset** — After a finished game, call `reset()` (or click the `restart`
   button) and verify `board` is all empty, `current` is `"X"`, `winner` is
   `null`, all cell buttons are enabled and empty, and `status` shows
   `"Player X to move"`.
