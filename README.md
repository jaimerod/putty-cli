# putty-cli

Reads a PuTTY `.reg` export on Linux/WSL, shows a fuzzy-searchable session
menu, and connects with native `ssh` — including `.ppk` key conversion,
jump-host proxies, mosh, remote commands, tmux session logging, and a
scriptable CLI.

## Requirements

- Linux or WSL
- `python3` (3.9+) with a venv
- `openssh-client`; `tmux` recommended (falls back to plain ssh without it)
- `putty-tools` (`sudo apt install putty-tools`) — only for sessions with `.ppk` keys
- Optional: `fzf` (richer picker with live preview), `mosh` (for mosh sessions)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock    # exact pins; requirements.txt for floating
cp .env.example .env                # then edit to your paths
```

Three ways to run:

```bash
./putty.py                          # from the repo
putty-cli                           # console script (pip install -e .)
~/path/to/putty-cli/bin/putty-cli   # from anywhere (auto-bootstraps venv)
```

## Configuration

Config comes from env vars. Values may live in `.env` (gitignored; see
`.env.example`), searched next to the repo root then in `~/.putty-cli/.env`.
Real environment variables take precedence over `.env`, and CLI flags top both.

| Variable              | Required | Meaning |
|-----------------------|----------|---------|
| `PUTTY_REG_PATH`      | yes      | Path to the PuTTY `.reg` export file |
| `PUTTY_LOG_DIR`       | no       | Where tmux session logs are written (default: dir containing the `.reg` file, else `~/.putty-cli/logs`) |
| `PUTTY_PPK_CACHE_DIR` | no       | Cache for converted OpenSSH keys (default: `~/.cache/putty-cli/keys`) |

## CLI usage

```
putty-cli                      Textual session browser (default UI)
putty-cli --no-tui             modal menu picker (action menu per session)
putty-cli --list               print usable session names (scriptable)
putty-cli --connect NAME       connect directly, no menu
putty-cli --connect NAME --dry-run     print the tmux/ssh argv and exit
putty-cli --logs               browse session logs in $PAGER
putty-cli --logs --logs-dir D  browse logs in a specific dir
putty-cli --fzf                pick with fzf (live ssh-command preview; falls back to menu)
putty-cli --archived           include archived sessions (names starting with xxx)
putty-cli --flat               no tag grouping
putty-cli --no-tmux            run ssh directly (no logging)
putty-cli --no-record          don't touch the recents file
putty-cli --reg FILE           use a specific .reg file
## Textual browser (default)

`putty-cli` with no flags runs the Textual session browser:

- filter box (`/` to focus, type to narrow, Enter connects the current)
- **left column, two stacked panes** (FAR panel style): RECENT
  CONNECTIONS on top (recents with `2h ago` / `✗` metadata), ALL
  CONNECTIONS below (every session, tag-grouped); `Tab` toggles panes
- **live preview pane**: the ssh command for the highlighted session,
  built without side effects (no puttygen run, no logs written)
- keys: Enter connect · click select · Tab panes · right-click context
  menu · p ping · y copy command · l logs · q quit
- right-click opens a cursor-positioned context menu (arrows navigate,
  Enter executes, Esc or clicking outside closes)
- connect suspends the UI and hands the terminal to tmux; the browser
  redraws when the session ends
- Copy uses the tmux buffer when inside tmux, else OSC52

Requires the optional extra: `pip install 'putty-cli[tui]'` (or
`pip install textual`). Without it, the browser prints a note and the
classic simple-term-menu picker takes over (`--no-tui` forces the
classic picker; `--fzf` overrides both).
```

## Interactive features

- **Session menu**: recents first — with `(2h ago)` last-connected time and
  `✗` marker when the last attempt failed — then `tag:` groups (`prod:web-1`
  groups under a `PROD` header), then everything else. Type to filter, `/`
  to search, Esc to quit.
- **Action menu** on Enter: Connect, Copy ssh command (OSC52 clipboard,
  works through tmux/ssh chains), Ping host (TCP probe, 3s timeout), Back.
- **fzf mode** (`--fzf`): preview pane dry-builds the ssh command for the
  highlighted session.
- **Hot reload**: if the `.reg` file changes while the menu is open, the
  list re-parses on the next selection loop (mtime/size check).
- Recents are written atomically, so parallel menu instances don't clobber
  the file.

## Session name conventions

- `tag:name` → grouped under a `TAG` header in the menu
- `xxx*` → archived, hidden unless `--archived`

## How connection works

1. Parses `[HKEY_CURRENT_USER\Software\SimonTatham\PuTTY\Sessions\...]`
   (UTF-16LE/BE with or without BOM, or UTF-8).
2. Builds ssh argv: `UserName@HostName`, `-p PortNumber`, ProxyMethod 6 →
   `-J`, `PublicKeyFile` → `puttygen` conversion (cached by path+mtime) →
   `-i`, `RemoteCommand` → `-t <cmd>`, PuTTY's `CommandLineArgs` appended.
3. **mosh sessions** (`Protocol=mosh`) build a `mosh` argv instead, passing
   port/key via `--ssh=`.
4. Launches detached in tmux with `pipe-pane` logging started *before*
   attach (first screenful is never lost), then attaches.
   Without tmux (or with `--no-tmux`): plain ssh.

## Logs

Each connection pipes the session to a timestamped log in `PUTTY_LOG_DIR`.
**Logs capture everything shown, including passwords you type.** Browse
with `putty-cli --logs`.

## Development

```bash
source .venv/bin/activate
pip install -e .[dev]
pre-commit install          # ruff + bandit on commit
pytest --cov=utils --cov=putty --cov-fail-under=90
mypy utils putty.py
ruff check .
bandit -c pyproject.toml -r utils putty.py
```

CI (GitHub Actions) runs the suite on Python 3.9/3.12 with puttygen/tmux,
with a 90% coverage gate, mypy, ruff and bandit.

The test suite covers the reg parser (all encodings), menu display and tag
grouping, recents (incl. legacy format + atomic writes), PPK conversion and
caching, ssh/mosh command building, fzf fallbacks, the CLI modes, and
regression tests that shell metacharacters in PuTTY session data can never
escape command construction.