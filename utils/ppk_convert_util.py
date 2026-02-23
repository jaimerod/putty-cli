import subprocess
import tempfile
import os
import sys
from pathlib import Path
from typing import Optional

PUTTYGEN_CMD = "puttygen"

def convert_ppk_to_openssh(ppk_path: str) -> Optional[str]:
    ppk_file = Path(ppk_path)
    if not ppk_file.is_file():
        print(f"  → Key file not found: {ppk_path}", file=sys.stderr)
        return None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".key") as tmp:
            tmp_path = tmp.name

        result = subprocess.run(
            [PUTTYGEN_CMD, str(ppk_file), "-O", "private-openssh", "-o", tmp_path],
            capture_output=True,
            text=True,
            timeout=45
        )

        if result.returncode != 0:
            print("puttygen failed:", result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            return None

        os.chmod(tmp_path, 0o600)
        return tmp_path

    except FileNotFoundError:
        print("Error: 'puttygen' not found. Run: sudo apt install putty-tools", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Key conversion error: {e}", file=sys.stderr)
        if 'tmp_path' in locals() and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return None
