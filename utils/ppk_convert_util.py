import hashlib
import os
import subprocess  # nosec
import sys
import tempfile
from pathlib import Path
from typing import Optional

PUTTYGEN_CMD = "puttygen"

CACHE_ENV = "PUTTY_PPK_CACHE_DIR"


def convert_ppk_to_openssh(ppk_path: str) -> Optional[str]:
    """Convert a PuTTY .ppk key to OpenSSH format using puttygen.

    Results are cached under PUTTY_PPK_CACHE_DIR (default:
    ~/.cache/putty-cli/keys), keyed by source path + mtime, so repeat
    connections skip the puttygen run. Returns the key file path or
    None on failure.
    """
    ppk_file = Path(ppk_path)
    if not ppk_file.is_file():
        print(f"  → Key file not found: {ppk_path}", file=sys.stderr)
        return None

    cached = _cached_key(ppk_file)
    if cached is not None:
        return str(cached)

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".key")
        os.close(fd)

        # fixed args: binary + our temp paths; no untrusted input
        result = subprocess.run(  # nosec B603, B607
            [PUTTYGEN_CMD, str(ppk_file), "-O", "private-openssh", "-o", tmp_path],
            capture_output=True,
            text=True,
            timeout=45
        )

        if result.returncode != 0:
            msg = result.stderr.strip() or result.stdout.strip()
            print("puttygen failed:", msg, file=sys.stderr)
            os.unlink(tmp_path)
            return None

        os.chmod(tmp_path, 0o600)
        final_path = _install_to_cache(ppk_file, Path(tmp_path))
        if final_path is not None:
            return str(final_path)
        return tmp_path  # cache unavailable; keep temp file

    except FileNotFoundError:
        print("Error: 'puttygen' not found. Run: sudo apt install putty-tools", file=sys.stderr)
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return None
    except Exception as e:
        print(f"Key conversion error: {e}", file=sys.stderr)
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return None


def ppk_cache_dir() -> Path:
    """Converted-key cache dir: $PUTTY_PPK_CACHE_DIR or the default."""
    return Path(os.environ.get(CACHE_ENV)
                or (Path.home() / ".cache" / "putty-cli" / "keys")).expanduser()


def _cache_key(ppk_file: Path) -> str:
    """Stable cache name: hash of abs path + mtime + size."""
    st = ppk_file.stat()
    raw = f"{ppk_file.resolve()}|{st.st_mtime_ns}|{st.st_size}".encode()
    return hashlib.sha256(raw).hexdigest()[:32] + ".key"


def _cached_key(ppk_file: Path) -> Optional[Path]:
    """Return a cached converted key if one exists for this file version."""
    candidate = ppk_cache_dir() / _cache_key(ppk_file)
    if candidate.is_file() and candidate.stat().st_size > 0:
        return candidate
    return None


def _install_to_cache(ppk_file: Path, tmp_key: Path) -> Optional[Path]:
    """Atomically move the converted key into the cache. Returns the
    final path, or None if the cache dir cannot be created/written."""
    try:
        cache_dir = ppk_cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        final = cache_dir / _cache_key(ppk_file)
        os.replace(tmp_key, final)
        os.chmod(final, 0o600)
        return final
    except OSError:
        return None
