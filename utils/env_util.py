import os
from pathlib import Path
from typing import Optional

ENV_FILENAME = ".env"


def _parse_env_file(text: str) -> dict:
    """Parse KEY=VALUE lines. Ignores blanks, comments, and export prefixes."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key:
            values[key] = val
    return values


def _find_env_file() -> Optional[Path]:
    """Look for .env next to the entry point, then in ~/.putty-cli/."""
    candidates = [
        Path(__file__).resolve().parent.parent / ENV_FILENAME,
        Path.home() / ".putty-cli" / ENV_FILENAME,
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def load_dotenv() -> Optional[Path]:
    """Load KEY=VALUE pairs from the first .env found into os.environ.

    Real environment variables always win over .env values. Returns the
    path of the file that was loaded, or None.
    """
    env_file = _find_env_file()
    if env_file is None:
        return None
    try:
        values = _parse_env_file(env_file.read_text(encoding="utf-8"))
    except OSError:
        return None
    for key, val in values.items():
        if key not in os.environ:
            os.environ[key] = val
    return env_file