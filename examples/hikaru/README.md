# Example: hikaru (Chess.com)

Everything in this folder came from one real run on 6 November 2026, with Stockfish 19 and no Lc0:

```bash
chessprep run hikaru --site chesscom --max 200
chessprep practice hikaru --color black --auto 12
```

The full pipeline took about one minute on a many-core machine. On a normal laptop expect a few minutes.

| File | What it is |
|---|---|
| [`cheatsheet.pdf`](cheatsheet.pdf) | The one-page A4 cheatsheet ([preview](cheatsheet-preview.png)) |
| [`cheatsheet.md`](cheatsheet.md), [`cheatsheet.txt`](cheatsheet.txt) | The same cheatsheet as Markdown and plain text |
| [`prep.md`](prep.md) | Full opening trees with the engine's replies and surprise moves |
| [`prep.json`](prep.json), [`report.json`](report.json) | Machine-readable prep and weakness report |
| [`run-output.txt`](run-output.txt) | Terminal output of `chessprep run` |
| [`practice-output.txt`](practice-output.txt) | A practice game where Stockfish played 12 moves for "you" against the clone |

The games are Hikaru Nakamura's public Chess.com games. Your own run will show different numbers, because the tool always downloads the latest games.
