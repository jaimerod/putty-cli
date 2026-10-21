import re
from pathlib import Path
from typing import Dict

SESSION_HEADER_RE = re.compile(
    r'^\[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\(.+?)\]$',
    re.IGNORECASE,
)
VALUE_RE = re.compile(r'^"([^"]+)"=(.+)$')


def _read_reg_text(file_path: Path) -> str:
    """Read a .reg file: BOM-sniff UTF-16, detect BOM-less UTF-16LE (null
    bytes early in the file), otherwise treat as UTF-8."""
    data = file_path.read_bytes()
    if data.startswith(b"\xff\xfe"):
        return data.decode("utf-16", errors="replace")
    if data[:2] == b"\x00[" or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16-be", errors="replace")
    if b"\x00" in data[:256]:
        return data.decode("utf-16-le", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def _parse_content(content: str) -> Dict[str, Dict[str, str]]:
    """Parse already-decoded .reg text into {session_name: {key: value}}.

    (Exposed separately so write-back validation can parse in-memory
    text without touching a file.)"""
    content = content.replace("\r\n", "\n").lstrip("\ufeff")

    sessions: Dict[str, Dict[str, str]] = {}
    current_session = None

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue

        m = SESSION_HEADER_RE.match(line)
        if m:
            session_name = m.group(1).strip()
            sessions[session_name] = {}
            current_session = session_name
            continue

        if current_session is None:
            continue

        m_val = VALUE_RE.match(line)
        if m_val:
            key = m_val.group(1).strip()
            val_raw = m_val.group(2).strip()

            if val_raw.startswith('"') and val_raw.endswith('"'):
                val = val_raw[1:-1].replace('\\\\', '\\')
            elif val_raw.startswith("dword:"):
                try:
                    val = str(int(val_raw[6:], 16))
                except ValueError:
                    val = "0"
            else:
                val = val_raw

            sessions[current_session][key] = val

    return sessions


def parse_putty_reg(file_path: Path) -> Dict[str, Dict[str, str]]:
    """Parse a PuTTY .reg export into {session_name: {key: value}}.

    Raises FileNotFoundError if the file does not exist, and OSError
    (or ValueError) if it cannot be read.
    """
    if not file_path.is_file():
        raise FileNotFoundError(f".reg file not found: {file_path}")

    return _parse_content(_read_reg_text(file_path))
