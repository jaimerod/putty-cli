import os
import time
from pathlib import Path
from typing import Dict, List, Optional

MAX_ENTRIES = 20
MAX_SHOW = 5


def _parse_line(line: str) -> Optional[Dict[str, object]]:
    """Parse one recents line. Supports the new TSV format
    'name<tab>epoch<tab>exit_code' and the legacy 'name' format."""
    line = line.strip()
    if not line:
        return None
    parts = line.split("\t")
    name = parts[0]
    if not name:
        return None
    entry: Dict[str, object] = {"name": name, "ts": None, "exit": None}
    if len(parts) >= 2:
        try:
            entry["ts"] = int(parts[1])
        except ValueError:
            pass
    if len(parts) >= 3:
        try:
            entry["exit"] = int(parts[2])
        except ValueError:
            pass
    return entry


def _serialize(entry: Dict[str, object]) -> str:
    ts = entry.get("ts")
    exit_code = entry.get("exit")
    line = str(entry["name"])
    if ts is not None or exit_code is not None:
        line += f"\t{ts if ts is not None else ''}"
        if exit_code is not None:
            line += f"\t{exit_code}"
    return line


def load_recent_sessions(
    recent_file: Path, usable: dict, max_sessions: int = MAX_SHOW
) -> List[str]:
    """Load up to max_sessions recent usable session names from file."""
    if not recent_file.exists():
        recent_file.touch()
    try:
        lines = recent_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        lines = []
    seen = set()
    names = []
    for line in lines:
        parsed = _parse_line(line)
        if parsed is None:
            continue
        name = str(parsed["name"])
        if name in usable and name not in seen:
            seen.add(name)
            names.append(name)
    return names[:max_sessions]


def load_recent_entries(recent_file: Path) -> Dict[str, Dict[str, object]]:
    """Full recents data keyed by session name (newest first in file)."""
    if not recent_file.exists():
        return {}
    try:
        lines = recent_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}
    entries: Dict[str, Dict[str, object]] = {}
    for line in lines:
        parsed = _parse_line(line)
        if parsed is not None and str(parsed["name"]) not in entries:
            entries[str(parsed["name"])] = parsed
    return entries


def update_recent_sessions(
    recent_file: Path, selected_name: str, exit_code: Optional[int] = None
) -> None:
    """Prepend selected_name to the recents file, keeping MAX_ENTRIES
    unique entries. Atomic write (temp file + os.replace) so concurrent
    menu instances can't truncate each other's data."""
    prev_names: List[str] = []
    try:
        if recent_file.exists():
            parsed = (_parse_line(raw) for raw in
                      recent_file.read_text(encoding="utf-8").splitlines())
            prev_names = [
                str(p["name"]) for p in parsed
                if p is not None and str(p["name"]) != selected_name
            ]
    except Exception:  # nosec - recents read is best-effort
        prev_names = []

    entry: Dict[str, object] = {
        "name": selected_name, "ts": int(time.time()), "exit": exit_code,
    }
    lines = [_serialize(entry)] + [
        _serialize({"name": n}) for n in prev_names[:MAX_ENTRIES - 1]
    ]
    text = "\n".join(lines) + "\n"

    import tempfile
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(recent_file.parent), prefix=".putty_recent_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, recent_file)
        tmp_path = None
    except Exception:  # nosec - best-effort write, never fatal
        pass
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def format_relative_time(ts: Optional[int]) -> str:
    """'5m ago', '2h ago', '3d ago', or '' for unknown."""
    if ts is None:
        return ""
    delta = int(time.time()) - int(ts)
    if delta < 0:
        delta = 0
    if delta < 60:
        return f"{delta}s ago" if delta > 0 else "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"
