"""Real-terminal (pty) smoke test for the FAR modal dialog placement.

Two corner-flash bugs in the modal chassis were invisible to the
headless suite: App.run_test() coalesces Textual's message batches, so
the transient mis-placed frames never render. Only a REAL pty capture
reproduced them. This test locks in the fixed behavior at the
terminal-byte level: the modal box must be drawn exactly once, at one
position, centered - never at the top-left corner.

Parsing model: before drawing a run of glyphs, Textual emits a
cursor-position escape (ESC[<row>;<col>H, 1-based) and then the text.
The rounded top-left corner of the dialog box is the UTF-8 glyph
'╭' (b"\\xe2\\x95\\xad"), so every place the box border started
painting can be located from the escape immediately preceding it.
"""
import fcntl
import os
import pty
import re
import select
import struct
import sys
import termios
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BOX_CORNER = b"\xe2\x95\xad"          # '╭'
CUP_RE = re.compile(rb"\x1b\[(\d+);(\d+)H")


@pytest.mark.skipif(os.name != "posix", reason="needs a posix pty")
def test_modal_paints_once_centered(tmp_path):
    """Press 'e' in the real TUI: the edit dialog's box corner must be
    drawn at exactly one position - centered, never top-left."""
    master, slave = pty.openpty()
    # window size: rows=30, cols=100
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))

    env = dict(
        os.environ,
        PUTTY_REG_PATH=str(REPO / "tests" / "data" / "sample.reg"),
        HOME=str(tmp_path),          # isolate the recents file
        TERM="xterm-256color",
    )
    env.pop("TMUX", None)

    pid = os.fork()
    if pid == 0:  # child: the TUI, with the pty as its controlling tty
        try:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.close(master)
            os.chdir(REPO)
            os.execve(sys.executable, [sys.executable, "putty.py"], env)
        except Exception:
            pass
        os._exit(1)

    try:
        os.close(slave)
        chunks = []          # (data, ...) accumulated output
        sent_e = False
        rendered = False
        modal_seen = False
        deadline = time.monotonic() + 8.0
        modal_seen_at = None

        while time.monotonic() < deadline:
            blob = b"".join(chunks)
            if not sent_e and b"ALL CONNECTIONS" in blob:
                os.write(master, b"e")    # open the edit dialog
                sent_e = True
            if sent_e and b"Save" in blob:
                if not modal_seen:
                    modal_seen = True
                    modal_seen_at = time.monotonic()
                elif time.monotonic() - modal_seen_at > 0.5:
                    break                 # let one more repaint settle
            if not rendered and b"ALL CONNECTIONS" in blob:
                rendered = True
            r, _, _ = select.select([master], [], [], 0.02)
            if r:
                try:
                    chunks.append(os.read(master, 1 << 16))
                except OSError:
                    break                 # child exited early

        if not rendered:
            pytest.fail("TUI never rendered (no ALL CONNECTIONS pane)")
        if not modal_seen:
            pytest.fail("edit modal never rendered (no Save button)")

        # ── parse: where was the box corner drawn? ──────────────────
        data = b"".join(chunks)
        draws = []            # (row, col) at each corner-glyph draw
        pos = 0
        last_row, last_col = 1, 1
        for m in CUP_RE.finditer(data):
            segment = data[pos:m.start()]
            if BOX_CORNER in segment:
                draws.append((last_row, last_col))
            last_row = int(m.group(1))
            last_col = int(m.group(2))
            pos = m.start()
        if BOX_CORNER in data[pos:]:
            draws.append((last_row, last_col))

        assert draws, "no dialog box corner (╭) was ever drawn"
        assert len(set(draws)) == 1, (
            f"the box border was drawn at multiple positions "
            f"(teleport/flash): {sorted(set(draws))}")
        row, col = draws[0]
        assert not (row < 4 and col < 4), (
            f"box drawn in the top-left corner: row={row} col={col}")
        assert 20 <= col <= 35, f"box not horizontally centered: col={col}"
        assert 4 <= row <= 12, f"box not vertically centered: row={row}"
    finally:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        try:
            os.close(master)
        except OSError:
            pass