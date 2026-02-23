from pathlib import Path
import re
import sys
from typing import Dict

def parse_putty_reg(file_path: Path) -> Dict[str, Dict[str, str]]:
    if not file_path.is_file():
        print(f"Error: .reg file not found → {file_path}", file=sys.stderr)
        sys.exit(1)

    sessions: Dict[str, Dict[str, str]] = {}
    current_session = None

    try:
        content = file_path.read_text(encoding="utf-16-le", errors="replace")
    except UnicodeDecodeError:
        content = file_path.read_text(encoding="utf-8", errors="replace")

    content = content.replace("\r\n", "\n").lstrip("\ufeff")

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue

        m = re.match(r'^\[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\(.+?)\]$', line, re.IGNORECASE)
        if m:
            session_name = m.group(1).strip()
            sessions[session_name] = {}
            current_session = session_name
            continue

        if current_session is None:
            continue

        m_val = re.match(r'^"([^"]+)"=(.+)$', line)
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
