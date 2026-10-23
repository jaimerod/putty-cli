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


def _read_lines(recent_file: Path) -> List[str]:
    """File lines, or [] when unreadable (best-effort read)."""
    try:
        return recent_file.read_text(encoding="utf-8").splitlines()
    except Exception:  # nosec - best-effort read, never fatal
        return []


def _read_entries(recent_file: Path) -> List[Dict[str, object]]:
    """Parsed recents entries in file order (newest first)."""
    return [entry for entry
            in (_parse_line(line) for line in _read_lines(recent_file))
            if entry is not None]


def _atomic_write(recent_file: Path, text: str) -> None:
    """Atomic recents write (temp file + os.replace) so concurrent
    menu instances can't truncate each other's data. Best-effort:
    never raises."""
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


def load_recent_sessions(
    recent_file: Path, usable: dict, max_sessions: int = MAX_SHOW
) -> List[str]:
    """Load up to max_sessions recent usable session names from file."""
    if not recent_file.exists():
        recent_file.touch()
    seen = set()
    names = []
    for parsed in _read_entries(recent_file):
        name = str(parsed["name"])
        if name in usable and name not in seen:
            seen.add(name)
            names.append(name)
    return names[:max_sessions]


def load_recent_entries(recent_file: Path) -> Dict[str, Dict[str, object]]:
    """Full recents data keyed by session name (newest first in file)."""
    entries: Dict[str, Dict[str, object]] = {}
    for parsed in _read_entries(recent_file):
        if parsed is not None and str(parsed["name"]) not in entries:
            entries[str(parsed["name"])] = parsed
    return entries


def update_recent_sessions(
    recent_file: Path, selected_name: str, exit_code: Optional[int] = None
) -> None:
    """Prepend selected_name to the recents file, keeping MAX_ENTRIES
    unique entries. The atomic replace prevents truncation, not
    races: concurrent instances can still lose each other's entries
    (last writer wins) - acceptable for a personal recents list."""
    prev_names = [
        str(e["name"]) for e in _read_entries(recent_file)
        if str(e["name"]) != selected_name
    ]
    entry: Dict[str, object] = {
        "name": selected_name, "ts": int(time.time()), "exit": exit_code,
    }
    lines = [_serialize(entry)] + [
        _serialize({"name": n}) for n in prev_names[:MAX_ENTRIES - 1]
    ]
    _atomic_write(recent_file, "\n".join(lines) + "\n")


def rename_entry(recent_file: Path, old_name: str, new_name: str) -> None:
    """Rename a session inside the recents file, preserving per-line
    timestamps/exit codes. Silently skips a missing file."""
    if not recent_file.is_file() or old_name == new_name:
        return
    lines = _read_lines(recent_file)
    out = []
    for line in lines:
        parsed = _parse_line(line)
        if parsed is not None and str(parsed["name"]) == old_name:
            parsed = dict(parsed, name=new_name)
        if parsed is not None:
            out.append(_serialize(parsed))
    if out == lines:  # unchanged - do not touch the mtime
        return
    _atomic_write(recent_file, "\n".join(out) + "\n")


def remove_entry(recent_file: Path, old_name: str) -> None:
    """Drop a session's lines from the recents file (after a Delete).
    Silently skips a missing file."""
    if not recent_file.is_file():
        return
    lines = _read_lines(recent_file)
    out = [
        _serialize(parsed) for line in lines
        if (parsed := _parse_line(line)) is not None
        and str(parsed["name"]) != old_name
    ]
    if out == lines:  # nothing dropped - do not touch the mtime
        return
    _atomic_write(recent_file, "\n".join(out) + "\n")


def format_relative_time(ts: Optional[int],
                         now: Optional[int] = None) -> str:
    """'5m ago', '2h ago', '3d ago', or '' for unknown. `now` injects
    the clock (tests / pure callers)."""
    if ts is None:
        return ""
    delta = (now if now is not None else int(time.time())) - int(ts)
    if delta < 0:
        delta = 0
    if delta < 60:
        return f"{delta}s ago" if delta > 0 else "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def recent_info_suffix(entry: Dict[str, object],
                       now: Optional[int] = None) -> str:
    """The '(2h ago ✗)' recents info suffix for a menu/pane row
    ('' when the entry carries no usable metadata)."""
    ts = entry.get("ts")
    info = format_relative_time(
        int(ts) if isinstance(ts, (int, float)) else None, now=now)
    exit_code = entry.get("exit")
    if isinstance(exit_code, (int, float)) and int(exit_code) != 0:
        info += (" " if info else "") + "✗"
    return info
