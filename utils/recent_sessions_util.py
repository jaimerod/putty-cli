from pathlib import Path
from typing import List

def load_recent_sessions(recent_file: Path, usable: dict, max_sessions: int = 5) -> List[str]:
    """Load up to max_sessions recent usable session names from file."""
    if not recent_file.exists():
        recent_file.touch()
    try:
        lines = recent_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        lines = []
    seen = set()
    # Only keep unique, existing, and in order
    return [n for n in lines if n in usable and not (n in seen or seen.add(n))][:max_sessions]

def update_recent_sessions(recent_file: Path, selected_name: str):
    """Prepend selected_name to recent sessions file, keeping only 20 unique entries."""
    try:
        prev = []
        if recent_file.exists():
            prev = [line.strip() for line in recent_file.read_text(encoding="utf-8").splitlines() if line.strip() and line.strip() != selected_name]
        lines = [selected_name] + prev[:19]
        recent_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass
