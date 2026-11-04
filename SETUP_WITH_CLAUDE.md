# Set up chess-prep on this computer

You are Claude Code, and the user has asked you to set up this project for them. Follow the steps below in order. The user may be new to the command line, so explain what you are doing in plain words.

## Goal

When you are done, the user can run `chessprep` from a terminal, `chessprep doctor` reports no failures, Stockfish is installed, the test suite passes, and the user has seen a real cheatsheet for an example player.

## Rules

- Keep the user informed with short updates: one or two sentences before each step and a short result after it.
- Ask the user before any command that needs `sudo`, administrator rights, or installs something system-wide (apt, brew, winget, and so on). Show the exact command and wait for a yes.
- Do not change files outside this project folder, except for installing the prerequisites in step 2 (and the engines that `chessprep setup-engines` downloads into its own data folder).
- Never print, store or commit secrets such as tokens, passwords or API keys. This project does not need any.
- Do not commit or push anything with git.
- If a step fails twice, stop. Tell the user what failed, show the error, and ask how they want to continue. Do not try random fixes.
- Use the same commands as `README.md`. If the README and this file ever disagree, follow the README and tell the user.

## Step 1: Find out what system this is

Detect the operating system and CPU architecture and tell the user what you found.

- Linux or macOS: `uname -s` and `uname -m`. On Linux also run `cat /etc/os-release` to get the distribution and version.
- Windows (PowerShell): `[System.Environment]::OSVersion.VersionString` and `$env:PROCESSOR_ARCHITECTURE`.

Use this to pick the right install commands below. Report it like: "You are on Ubuntu 24.04, x86_64."

## Step 2: Check the prerequisites

Check each one. If it is missing or too old, show the install command for this system, ask the user, then install it.

**Python 3.10 or newer** (3.12 recommended)

- Check: `python3 --version` (Windows: `py --version`)
- Install:
  - Ubuntu / Debian: `sudo apt update && sudo apt install python3.12 python3.12-venv`
  - macOS: `brew install python@3.12`
  - Windows: `winget install Python.Python.3.12`
- On Ubuntu 22.04 and older, `python3.12` needs the deadsnakes PPA. If the system Python is already 3.10 or newer, use it instead.
- On Debian and Ubuntu, also check that virtual environments work: `python3 -m venv --help`. If not, install the matching `python3.X-venv` package.

**git**

- Check: `git --version`
- Install: `sudo apt install git` (Ubuntu / Debian), `brew install git` (macOS), `winget install Git.Git` (Windows)

**Internet access**

- Check: `curl -sI https://lichess.org/api/status` (Windows: `Invoke-WebRequest https://lichess.org/api/status -Method Head`)
- If it fails, ask the user whether they use a proxy. If they do, set `HTTPS_PROXY` for the rest of the setup.

## Step 3: Get into the project folder

- If the current folder already contains `pyproject.toml` and a `chess_prep` folder, you are inside the repository. Stay here.
- Otherwise download it and go into it:

  ```bash
  git clone https://github.com/iwdissi-rl/chess-prep.git
  cd chess-prep
  ```

## Step 4: Install the package

Create a virtual environment in the project folder and install the project into it, including the developer extras (they add pytest, ruff and the optional browser board):

```bash
python3 -m venv .venv              # Windows: py -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Then check that the command exists: `chessprep --version`.

Remember that every later command must run with the virtual environment active (or call `.venv/bin/chessprep`, on Windows `.venv\Scripts\chessprep`). Tell the user about this, because they will need it too. If the user prefers a command that works from anywhere, `pipx install .` is the alternative from the README.

## Step 5: Check the setup and install the engines

1. Run `chessprep doctor`. Read the table and explain any problems to the user.
2. Run `chessprep setup-engines`. It downloads Stockfish (about 80 MB) and the Maia weights into the chess-prep data folder. No `sudo` is needed.
3. Run `chessprep doctor` again. Fix anything still marked `FAIL`, using the hint that doctor prints.
   - Lc0 showing as missing is fine. It is optional. Only offer to install it if the user wants the more human-like practice bot, and follow the hint that `chessprep doctor` prints for this system.
   - If Stockfish was installed separately in an unusual place, use `chessprep config set stockfish /path/to/stockfish`.

## Step 6: Run the tests

```bash
pytest
ruff check .
```

All tests should pass (one test is skipped unless `CHESSPREP_TEST_STOCKFISH` is set; that is expected). If something fails, show the user the failing test name and error, and stop as described in the rules.

## Step 7: First real run

Run the example from the README:

```bash
chessprep run hikaru --site chesscom --max 200
```

It takes a few minutes. When it finishes, show the user:

- the summary panel and the "Main openings" table from the output,
- the folder where the cheatsheet was saved (printed under "Cheatsheet ready"),
- the first lines of `cheatsheet.txt` from that folder.

If you can, offer to open `cheatsheet.pdf` for them (`xdg-open` on Linux, `open` on macOS, `start` on Windows).

Then show that practice works without typing, by running a short scripted game:

```bash
chessprep practice hikaru --color black --auto 5
```

## Step 8: Explain what to do next

Finish with a short, plain-words summary for the user:

- How to start: activate the virtual environment (`source .venv/bin/activate`, Windows `.venv\Scripts\activate`), then run `chessprep` for the menu.
- How to scout their own opponent: `chessprep run USERNAME --site chesscom` or `--site lichess`.
- How to practise: `chessprep practice USERNAME --color white` (type moves like `e4`, `hint`, `moves`, `resign`).
- Where the cheatsheets are saved.

Include this command table:

| Command | What it does |
|---|---|
| `chessprep` | Opens an interactive menu. |
| `chessprep fetch USER --site chesscom\|lichess` | Downloads recent games. |
| `chessprep analyze USER` | Runs Stockfish and writes the weakness report. |
| `chessprep prep USER` | Builds the opening tree with suggested replies. |
| `chessprep cheatsheet USER` | Writes `cheatsheet.pdf`, `.md` and `.txt`. |
| `chessprep run USER --site ...` | Does all four steps above in a row. |
| `chessprep practice USER` | Plays against the clone in the terminal (`--web` for the browser). |
| `chessprep players` | Lists downloaded players. |
| `chessprep doctor` | Checks the setup and explains how to fix problems. |
| `chessprep setup-engines` | Downloads Stockfish and the Maia weights. |
| `chessprep config show` | Shows saved engine paths. |

Point them to the "Troubleshooting" section of `README.md` if anything goes wrong later.
