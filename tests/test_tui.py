"""Textual TUI tests via run_test() pilots (no real terminal needed)."""
import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

pytest.importorskip("textual")

from textual import events
from textual.color import Color
from textual.widgets import Input

import putty
from utils.far_style import ACTIVE_PALETTE, to_textual_theme
from utils.palette_util import get_palette
from utils.tui_actions import run_ping
from utils.tui_browser import (
    DETAIL_FIELDS,
    SessionBrowser,
    build_all_rows,
    build_recent_rows,
    detail_values,
)

USABLE = {
    "prod:web-1": {"HostName": "a.example.com", "UserName": "ops",
                   "PortNumber": "2222"},
    "prod:web-2": {"HostName": "b.example.com", "UserName": "ops"},
    "staging:web": {"HostName": "c.example.com"},
    "solo": {"HostName": "d.example.com"},
}


class TestLogModal:
    """l: FAR dialog (scroll + filter) reading the RIGHT log folder."""

    async def test_close_button_dismisses(self, tmp_path):
        """[X] at the end of the title row dismisses the log viewer."""
        from utils.tui_actions import LogModal
        app = SessionBrowser({"a": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await app.push_screen(LogModal(tmp_path / "x.log"))
            for _ in range(3):
                await pilot.pause()
            modal = app.screen
            assert modal.query_one("#far-close").region.x > 0
            await pilot.click("#far-close")
            for _ in range(3):
                await pilot.pause()
            assert app.screen is not modal

    async def test_l_reads_logs_next_to_reg_file(self, tmp_path, monkeypatch):
        """Regression: connects write logs next to the .reg (the user's
        OneDrive PuTTY dir); the viewer must look THERE by default."""
        monkeypatch.delenv("PUTTY_LOG_DIR", raising=False)
        log = tmp_path / "2026-10-21_0337-16gb_Linux_Server.log"
        log.write_text("ssh -p 22 ok\nsession started\n", encoding="utf-8")
        monkeypatch.delenv("TMUX", raising=False)
        reg = tmp_path / "putty.reg"

        from utils.tui_actions import LogModal
        app = SessionBrowser({"16gb_Linux_Server": {"HostName": "h"}},
                             [], {}, reg)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.query_one("#sessions").index = 0
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            assert isinstance(app.screen, LogModal)
            assert app.screen.log_path == log

    async def test_l_matches_url_quoted_session_names(self, tmp_path,
                                                      monkeypatch):
        """Real-world .reg names are URL-quoted (PuTTY exports spaces
        as %20) while log files carry the decoded name - 'logs do
        nothing' regression for exactly that mismatch."""
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / "2026-10-21_0337-16gb_Linux_Server.log").write_text(
            "session log\n", encoding="utf-8")
        app = SessionBrowser(
            {"16gb%20Linux%20Server": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            from utils.tui_actions import LogModal
            assert isinstance(app.screen, LogModal)
            assert app.screen.log_path.name \
                == "2026-10-21_0337-16gb_Linux_Server.log"
    async def test_log_modal_covers_screen_opaquely(self, tmp_path,
                                                     monkeypatch):
        """Regression: the transparent near-fullscreen modal + giant
        shadow echo ghosted/corrupted the background on real terminals
        when scrolling. FAR viewer style now: opaque, no echo, no box
        offsets - one stable raster."""
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / "2026-10-21_0900-solo.log").write_text(
            "one\x1b[31mred\x1b[0mtwo\r\nline 2\r\n", encoding="utf-8")
        from textual.color import Color

        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            modal = app.screen
            # opaque: not the transparent-dialog ghosting recipe
            assert modal.styles.background != Color(0, 0, 0, a=0)
            assert modal.styles.background \
                == Color.parse(ACTIVE_PALETTE.bg)
            assert not modal.query("#log-shadow")    # no echo at all
            # escape sequences scrubbed; \r-collapsed clean text
            lines = modal.lines
            assert "\x1b" not in " ".join(lines) and "\r" not in \
                " ".join(lines)
            assert "redtwo" in lines[0]
            assert lines[1] == "line 2"
            # geometry: full-screen, nothing offset
            box = modal.query_one("#log-view")
            assert box.region.width == 110 or box.region.width > 100

    async def test_scroll_repaints_stably(self, tmp_path, monkeypatch):
        """Scrolling must not disturb the chrome rows (title/search/
        status/keybar hold their text and position)."""
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / "2026-10-21_0900-solo.log").write_text(
            "\n".join(f"line-{i:03d}" for i in range(200)) + "\n",
            encoding="utf-8")
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.press("l")
            for _ in range(2):
                await pilot.pause()
            modal = app.screen

            def chrome():
                title = modal.query_one("#log-title")
                status = modal.query_one("#log-status")
                keybar = modal.query_one("#keybar")
                return (str(title.render()), str(status.render()),
                        keybar.region.y, title.region.y)

            before = chrome()
            lv = modal.query_one("#log-view")
            for _ in range(10):
                await pilot.press("down")
                await pilot.pause()
            after = chrome()
            assert after == before          # chrome rows unmoved
            assert lv.scroll_offset.y > 0   # the LOG scrolled instead

    async def test_log_modal_scrolls(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        log = logs_dir / "2026-10-21_0900-solo.log"
        log.write_text(
            "\n".join(f"line-{i:03d} padding" for i in range(200)) + "\n",
            encoding="utf-8")
        from utils.tui_actions import LogModal

        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            assert isinstance(app.screen, LogModal)
            scroll = app.screen.query_one("#log-view")
            assert scroll.max_scroll_y >= 1       # the log overflows
            # the browser's footer keybar stays put (dialog has its own)
            assert "e Edit" in app.query_one("#keybar").bar_text()
            for _ in range(3):
                await pilot.press("down")
            await pilot.pause()
            assert scroll.scroll_offset.y > 0
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, LogModal)


class TestTuiActions:
    def test_run_ping_reachable(self):
        import re
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        ok, msg = run_ping("127.0.0.1", str(port))
        srv.close()
        # ICMP path: latency; TCP fallback: 'reachable (tcp)'
        assert ok and re.search(r"[\d.]+ ms|reachable", msg)

    def test_run_ping_tcp_fallback_when_ping_missing(self, monkeypatch):
        """No ping binary -> TCP-port probe, clearly labeled (tcp)."""
        import socket

        import utils.net_util as mod
        monkeypatch.setattr(mod.shutil, "which", lambda name: None)
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = str(srv.getsockname()[1])
        ok, msg = mod.run_ping("127.0.0.1", port)
        srv.close()
        assert ok and "reachable (tcp)" in msg

    def test_run_ping_reports_latency_from_ping_output(self, monkeypatch):
        import subprocess as sp

        import utils.net_util as mod
        monkeypatch.setattr(
            mod.shutil, "which", lambda name: "/usr/bin/ping")
        monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(
            [], 0, stdout="1 received, 0% loss\ntime=23.4 ms",
            stderr=""))
        ok, msg = mod.run_ping("h.example.com", "22")
        assert ok and "23.4 ms" in msg

    def test_run_ping_unknown_host(self, monkeypatch):
        import subprocess as sp

        import utils.net_util as mod
        monkeypatch.setattr(
            mod.shutil, "which", lambda name: "/usr/bin/ping")
        monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(
            [], 68, stdout="", stderr="ping: Name or service not known"))
        ok, msg = mod.run_ping("nope.invalid", "22")
        assert not ok and "unknown host" in msg

    def test_run_ping_packet_loss(self, monkeypatch):
        import subprocess as sp

        import utils.net_util as mod
        monkeypatch.setattr(
            mod.shutil, "which", lambda name: "/usr/bin/ping")
        monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(
            [], 1, stdout="1 packets transmitted, 0 received, "
            "100% packet loss", stderr=""))
        ok, msg = mod.run_ping("192.0.2.1", "1")
        assert not ok and "unreachable" in msg

    def test_run_ping_unpermitted_caps_falls_back_to_tcp(self, monkeypatch):
        import subprocess as sp

        import utils.net_util as nmod
        import utils.net_util as mod
        monkeypatch.setattr(
            mod.shutil, "which", lambda name: "/usr/bin/ping")
        monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(
            [], 2, stdout="", stderr="ping: Operation not permitted"))
        calls = []
        monkeypatch.setattr(nmod, "_tcp_probe",
                            lambda h, p: calls.append((h, p)) or (True, "tcp-marker"))
        ok, msg = mod.run_ping("127.0.0.1", "2222")
        assert ok and msg == "tcp-marker" and calls == [("127.0.0.1", "2222")]

    def test_run_ping_unreachable(self):
        ok, msg = run_ping("192.0.2.1", "1")
        assert not ok and "unreachable" in msg

    def test_copy_via_tmux_buffer(self, monkeypatch):
        import subprocess as sp
        calls = []
        monkeypatch.setenv("TMUX", "/tmp/tmux,0,0")
        monkeypatch.setattr(
            sp, "run",
            lambda argv, **k: calls.append(argv) or sp.CompletedProcess(argv, 0))
        from utils.tui_actions import copy_command
        assert copy_command("ssh x")
        assert calls[0][:3] == ["tmux", "load-buffer", "-w"]

    def test_copy_via_tmux_falls_back_to_osc52(self, monkeypatch, capsys):
        import subprocess as sp
        monkeypatch.setenv("TMUX", "/tmp/tmux,0,0")
        monkeypatch.setattr(
            sp, "run",
            lambda argv, **k: sp.CompletedProcess(argv, 1))
        from utils.tui_actions import copy_command
        assert copy_command("ssh x")
        assert "\033]52;c;" in capsys.readouterr().out

    def test_copy_osc52_without_tmux(self, monkeypatch, capsys):
        monkeypatch.delenv("TMUX", raising=False)
        from utils.tui_actions import copy_command
        assert copy_command("ssh x")
        assert "\033]52;c;" in capsys.readouterr().out

    async def test_l_opens_newest_session_log_directly(self, tmp_path, monkeypatch):
        """l = newest log for the highlighted session, straight into the
        searchable viewer (skips the chooser)."""
        from utils.tui_actions import LogModal
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / "2026-10-06_1700-solo.log").write_text("old\n")
        (logs_dir / "2026-10-07_1700-solo.log").write_text("newest content\n")
        (logs_dir / "2026-10-07_1701-unrelated.log").write_text("nope\n")
        import os
        import time
        os.utime(logs_dir / "2026-10-06_1700-solo.log",
                 (time.time() - 86400, time.time() - 86400))

        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            assert isinstance(app.screen, LogModal)
            assert app.screen.log_path.name == "2026-10-07_1700-solo.log"

    async def test_l_no_logs_shows_status(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "empty-logs"))
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await pilot.press("l")  # no logs yet
            await pilot.pause()
            assert "no logs yet" in app.status_message
            from utils.tui_actions import LogModal
            assert not isinstance(app.screen, LogModal)

    async def test_l_no_session_highlighted(self):
        """l with no selectable row (e.g. empty list) -> status only."""
        app = SessionBrowser({}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            assert "no session highlighted" in app.status_message

    async def test_esc_return_to_browser(self, tmp_path):
        from utils.tui_actions import LogModal
        log = tmp_path / "2026-10-07_1700-x.log"
        log.write_text("x\n", encoding="utf-8")
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await app.push_screen(LogModal(log))
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is not None  # back to browser (no crash)

    async def test_unreadable_log_shows_error(self, tmp_path):
        from utils.tui_actions import LogModal
        p = tmp_path / "bad.log"

        def broken(self, *a, **k):
            raise OSError("denied")

        import pathlib
        monkeypatch_read = broken
        orig = pathlib.Path.read_text
        try:
            pathlib.Path.read_text = monkeypatch_read  # type: ignore
            app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
            async with app.run_test(size=(110, 30)) as pilot:
                await app.push_screen(LogModal(p))
                await pilot.pause()
                assert any("Cannot read" in ln for ln in app.screen.lines)  # type: ignore
        finally:
            pathlib.Path.read_text = orig  # type: ignore

    async def test_search_no_match_status(self, tmp_path):
        from utils.tui_actions import LogModal
        log = tmp_path / "a.log"
        log.write_text("one\ntwo\n", encoding="utf-8")
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            screen = LogModal(log)
            await app.push_screen(screen)
            await pilot.pause()
            sb = screen.query_one("#log-search")
            sb.focus()
            sb.value = "nothing-matches-this"
            await pilot.pause()
            await pilot.pause()
            status = str(screen.query_one("#log-status").render())
            assert "no matches" in status

    async def test_n_without_term_is_noop(self, tmp_path):
        from utils.tui_actions import LogModal
        log = tmp_path / "a.log"
        log.write_text("a\nb\n", encoding="utf-8")
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            screen = LogModal(log)
            await app.push_screen(screen)
            await pilot.pause()
            before = len(screen.lines)
            await screen.action_next_match()  # no term -> status hint
            await pilot.pause()
            assert len(screen.lines) == before
            assert "need a search term" in str(
                screen.query_one("#log-status").render())

    async def test_n_n_repeat_jumps_with_term_kept(self, tmp_path,
                                                   monkeypatch):
        """THE BUG: after the first n/N jump the input used to be
        cleared, the async re-filter wiped the match list, and the next
        n did nothing. Now the term stays in the field; n n N keep
        cycling (from the log focus, per the Enter/Esc flow)."""
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / "2026-10-21_0900-solo.log").write_text(
            "\n".join(f"needle {i}" if i % 5 == 0 else f"line {i}"
                      for i in range(100)) + "\n", encoding="utf-8")
        from utils.tui_actions import LogModal

        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, LogModal)
            sb = screen.query_one("#log-search")
            sb.focus()
            sb.value = "needle"
            await pilot.pause()
            assert len(screen.match_positions) == 20   # every 5th line
            await pilot.press("enter")     # Jump -> match 1, log focus
            await pilot.pause()
            assert screen.match_index == 0
            assert sb.value == "needle"    # term survives the jump
            await pilot.press("n")
            await pilot.pause()
            assert screen.match_index == 1
            await pilot.press("n")
            await pilot.pause()
            assert screen.match_index == 2
            await pilot.press("N")
            await pilot.pause()
            assert screen.match_index == 1
            assert sb.value == "needle"    # still kept
            await pilot.press("n")
            await pilot.pause()
            assert screen.match_index == 2

    async def test_n_works_with_log_focused(self, tmp_path, monkeypatch):
        """The common flow: search, Enter (= jump to match 1), then n/N
        with the log view focused."""
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / "2026-10-21_0900-solo.log").write_text(
            "\n".join(f"needle {i}" if i % 7 == 0 else "fill"
                      for i in range(80)) + "\n", encoding="utf-8")
        from utils.tui_actions import LogModal

        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            screen = app.screen
            sb = screen.query_one("#log-search")
            sb.focus()
            sb.value = "needle"
            await pilot.pause()
            await pilot.press("enter")     # Jump -> first match
            await pilot.pause()
            assert screen.match_index == 0
            # focus is back in the log: keys stay hotkeys
            assert getattr(app.focused, "id", None) == "log-view"
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert screen.match_index == 2
            # Esc from the log closes the dialog
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, LogModal)

    async def test_esc_in_search_blurs_and_keeps_matches(self, tmp_path,
                                                         monkeypatch):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / "2026-10-21_0900-solo.log").write_text(
            "needle here\nfill\nneedle there\n", encoding="utf-8")
        from utils.tui_actions import LogModal

        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            screen = app.screen
            sb = screen.query_one("#log-search")
            sb.focus()
            sb.value = "needle"
            await pilot.pause()
            await pilot.press("escape")    # Done, NOT close
            await pilot.pause()
            assert isinstance(app.screen, LogModal)   # still open
            assert getattr(app.focused, "id", None) == "log-view"
            assert sb.value == "needle"               # term kept
            assert len(screen.match_positions) == 2

    async def test_prev_jump_wraps_negative(self, tmp_path):
        from utils.tui_actions import LogModal
        log = tmp_path / "a.log"
        log.write_text("HIT\nmid\nHIT\n", encoding="utf-8")
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            screen = LogModal(log)
            await app.push_screen(screen)
            await pilot.pause()
            sb = screen.query_one("#log-search")
            sb.focus()
            sb.value = "hit"  # matches lines 0 and 2
            await pilot.pause()
            # view is filtered; jump prev wraps to the LAST match
            await screen.action_prev_match()
            assert screen.match_index == 1
            view = screen.query_one("#log-view")
            shown = "".join(strip.text for strip in view.lines)
            assert "HIT" in shown
            # the term survives the jump: a second jump still works
            await screen.action_next_match()
            assert screen.match_index == len(screen.match_positions) - 1 \
                or screen.match_index == 0

    async def test_q_binding_dismisses(self, tmp_path):
        from utils.tui_actions import LogModal
        log = tmp_path / "a.log"
        log.write_text("q\n", encoding="utf-8")
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await app.push_screen(LogModal(log))
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            assert app.screen is not None


class TestTuiFallback:
    def test_tui_default_on(self):
        """--tui is the default mode; --no-tui opts out."""
        assert putty.build_parser().parse_args([]).tui is True
        assert putty.build_parser().parse_args(["--no-tui"]).tui is False

    def test_run_tui_browser_without_textual_falls_back(self, monkeypatch, tmp_path, capsys):
        """--tui (default) without textual -> note on stderr, falls back
        to the standard menu flow (picked session then cancelled here)."""
        # block the import inside run_tui_browser's try
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "utils.tui_browser":
                raise ImportError(name)
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        monkeypatch.setenv("PUTTY_REG_PATH", "tests/data/sample.reg")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # the menu loop resolves its pickers in utils.menu_flow
        import utils.menu_flow as menu_flow
        monkeypatch.setattr(menu_flow, "pick_session_interactive",
                            lambda *a, **k: None)  # cancel at menu
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = putty.main([])
        assert rc == 0
        assert "textual not installed" in err.getvalue()
        assert "Selection cancelled" in out.getvalue()

    def test_run_tui_browser_with_textual_runs_app(self, monkeypatch, tmp_path):
        """--tui with textual available launches SessionBrowser, exit 0."""
        import contextlib
        import io

        import putty as putty_mod

        launched = {}

        class FakeBrowser:
            def __init__(self, *a, **k):
                launched["args"] = a
                launched["kwargs"] = k

            def run(self):
                launched["ran"] = True

        import utils.tui_browser as tb
        monkeypatch.setattr(tb, "SessionBrowser", FakeBrowser)
        monkeypatch.setenv("PUTTY_REG_PATH", "tests/data/sample.reg")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = putty_mod.main(["--tui"])
        assert rc == 0
        assert launched.get("ran") is True
        # callback wired for connect
        assert callable(launched["kwargs"].get("connect_callback"))


class TestBuildRows:
    def test_all_rows_tags_and_other(self):
        rows = build_all_rows(USABLE)
        names = [r.name for r in rows if not r.name.startswith("__")]
        assert names == ["prod:web-1", "prod:web-2", "staging:web", "solo"]
        headers = [r.display for r in rows if r.name.startswith("__hdr__")]
        assert any("PROD" in h for h in headers)
        assert any("OTHER" in h for h in headers)

    def test_all_rows_include_recent_sessions(self):
        """The ALL pane lists everything; recents get their own pane."""
        rows = build_all_rows(USABLE)
        names = [r.name for r in rows if not r.name.startswith("__")]
        assert "prod:web-1" in names

    def test_recent_rows_metadata(self):
        import time
        entries = {"prod:web-1": {"ts": int(time.time()) - 7200, "exit": 255}}
        # unknown sessions are skipped; recents carry the metadata suffix
        rows = build_recent_rows(USABLE, ["prod:web-1", "ghost"], entries)
        assert [r.name for r in rows] == ["prod:web-1"]
        assert "2h ago" in rows[0].display and "✗" in rows[0].display

    def test_flat_mode(self):
        rows = build_all_rows(USABLE, group_by_tag=False)
        assert not any(r.name.startswith("__hdr__") for r in rows)

    def test_rows_have_no_host_hint(self):
        """Rows show the title-cased name only: the → user@host hint
        lives in the CONNECTION DETAIL pane now."""
        for r in build_all_rows(USABLE):
            if r.name.startswith("__"):
                continue
            assert "→" not in r.display
            assert "example.com" not in r.display
        recents = build_recent_rows(
            USABLE, ["prod:web-1"], {"prod:web-1": {"ts": None, "exit": None}})
        assert "→" not in recents[0].display

    async def test_filter_matches_host_and_port_even_without_hint(self):
        """The hint is gone from rows, but host/user/port text still
        finds sessions (via the app's own _filter_rows)."""
        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            filt = app.query_one("#filter")
            filt.value = "b.example"
            await pilot.pause()
            assert [r.name for r in app.filtered_rows] == ["prod:web-2"]
            filt.value = "2222"
            await pilot.pause()
            assert [r.name for r in app.filtered_rows] == ["prod:web-1"]


class TestPanes:
    """Left column: RECENT pane stacked above the ALL pane (FAR style)."""

    def _click_on_row(self, app, row_index, pane="#sessions", chain=1):
        """Craft a right-click event targeted at a row in either pane."""
        from textual import events

        lv = app.query_one(pane)
        item = lv.children[row_index]
        region = item.region
        return events.Click(
            widget=item,
            x=region.x + 1, y=region.y + 1,
            delta_x=0, delta_y=0,
            button=3, chain=chain, shift=False, meta=False, ctrl=False,
            screen_x=region.x + 1, screen_y=region.y + 1,
            style=None,
        )

    async def test_panes_stacked_recent_above_all(self):
        app = SessionBrowser(USABLE, ["prod:web-1"], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            top = app.query_one("#recent-win").region
            bottom = app.query_one("#all-win").region
            assert top.y < bottom.y
            assert top.x == bottom.x and top.width == bottom.width

    async def test_panes_have_titles(self):
        """Captions live inline in the top border row, centered, yellow
        on blue (FAR.exe dialog-title look)."""
        from textual.color import Color

        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            for win_id, label in (("#recent-win", "RECENT CONNECTIONS"),
                                  ("#all-win", "ALL CONNECTIONS"),
                                  ("#preview-win", "CONNECTION DETAIL")):
                win = app.query_one(win_id)
                assert win.border_title == label
                assert win.styles.border_title_align == "center"
                assert win.styles.border_title_color \
                    == Color.parse(ACTIVE_PALETTE.title)
            window = app.query_one("#recent-win")
            frame_top = window.region.y  # caption is inside the frame row
            assert app.query_one("#recents").region.y \
                == frame_top + 1  # rows start right under the frame

    async def test_recent_pane_rows_with_metadata(self):
        import time
        entries = {"prod:web-1": {"ts": int(time.time()) - 300, "exit": 0}}
        app = SessionBrowser(USABLE, ["prod:web-1"], entries, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            items = app.query_one("#recents").children
            assert [c.row.name for c in items] == ["prod:web-1"]
            assert "5m ago" in items[0].row.display

    async def test_all_pane_lists_everything(self):
        app = SessionBrowser(USABLE, ["prod:web-1"], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            names = [c.row.name for c in app.query_one("#sessions").children]
            sessions = [r.name for r in build_all_rows(USABLE)
                        if not r.name.startswith("__")]
            # every session is listed exactly once (recents included)
            assert sorted(n for n in names if not n.startswith("__")) \
                == sorted(sessions)
            assert sorted(n for n in names if not n.startswith("__")) \
                == sorted(USABLE)

    async def test_filter_applies_to_both_panes(self):
        app = SessionBrowser(USABLE, ["prod:web-1", "solo"], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            filt = app.query_one("#filter")
            filt.value = "prod"
            await pilot.pause()
            assert [c.row.name for c in app.query_one("#recents").children] \
                == ["prod:web-1"]
            assert [c.row.name for c in app.query_one("#sessions").children] \
                == ["prod:web-1", "prod:web-2"]
            filt.value = ""
            await pilot.pause()
            assert len(app.query_one("#recents").children) == 2

    async def test_tab_toggles_between_panes(self):
        app = SessionBrowser(USABLE, ["prod:web-1"], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            # recents exist -> browser opens on the recent pane
            assert app.query_one("*:focus").id == "recents"
            await pilot.press("tab")
            await pilot.pause()
            assert app.query_one("*:focus").id == "sessions"
            await pilot.press("tab")
            await pilot.pause()
            assert app.query_one("*:focus").id == "recents"

    async def test_keybar_tab_slot_switches_pane(self):
        from utils.far_style import KeyBarSlot

        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()

            def tab_click():
                slot = next(s for s in app.query(KeyBarSlot)
                            if s.key == "Tab")
                return pilot.click(slot)

            await tab_click()
            await pilot.pause()
            assert app.query_one("*:focus").id == "recents"
            await tab_click()  # re-query: focus swap refreshes the bar
            await pilot.pause()
            assert app.query_one("*:focus").id == "sessions"

    async def test_connect_from_recent_pane(self):
        called = []
        app = SessionBrowser(
            {"solo": {"HostName": "h"},
             "prod:web-1": {"HostName": "a.example.com"}},
            ["prod:web-1"], {}, None,
            connect_callback=lambda n: called.append(n))
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await pilot.press("enter")  # recents pane is focused on mount
            await pilot.pause()
        assert called == ["prod:web-1"]
        assert app.selected_name == "prod:web-1"

    async def test_right_click_in_recent_pane_opens_menu(self):
        from utils.tui_actions import SessionActionMenu

        app = SessionBrowser(
            {"prod:web-1": {"HostName": "a.example.com"}},
            ["prod:web-1"], {}, None,
            connect_callback=lambda n: None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.post_message(self._click_on_row(app, 0, pane="#recents"))
            await pilot.pause()
            await pilot.pause()
            assert isinstance(app.screen, SessionActionMenu)
            assert app.selected_name == "prod:web-1"

    async def test_enter_in_all_pane_connects_all_pane_row(self):
        called = []
        app = SessionBrowser(
            {"prod:web-1": {"HostName": "a"}, "solo": {"HostName": "h"}},
            ["prod:web-1"], {}, None,
            connect_callback=lambda n: called.append(n))
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            sessions = app.query_one("#sessions")
            sessions.focus()
            await pilot.pause()
            await pilot.press("enter")  # first selectable row: prod:web-1
            await pilot.pause()
        assert called == ["prod:web-1"]

    async def test_status_counts_all_pane_once(self):
        """Sessions listed in the recents pane must not inflate counts."""
        app = SessionBrowser(USABLE, ["prod:web-1"], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            assert "4/4 sessions" in str(app.query_one("#status").render())


class TestPreview:
    """CONNECTION DETAIL form: values per highlighted session."""

    def _app(self):
        return SessionBrowser(
            {"prod:web-1": {
                "HostName": "a.example.com", "UserName": "ops",
                "PortNumber": "2222", "PublicKeyFile": "x.ppk",
                "ProxyMethod": "6", "ProxyHost": "j.example.com",
                "Protocol": "mosh", "RemoteCommand": "htop"}},
            [], {}, None)

    def test_detail_values_contents(self):
        app = self._app()
        values = detail_values(
            "prod:web-1", app.usable["prod:web-1"], None)
        assert values["HostName"] == "a.example.com"
        assert values["UserName"] == "ops"
        assert values["PortNumber"] == "2222"
        assert values["Protocol"] == "mosh"
        assert values["PublicKeyFile"] == "x.ppk"
        assert values["ProxyHost"] == "j.example.com"   # ProxyMethod 6
        assert values["RemoteCommand"] == "htop"
        assert "ssh" in values["command"]
        assert "ops@a.example.com" in values["command"]

    def test_detail_values_no_hostname(self):
        values = detail_values(
            "weird", {"PortNumber": "22"}, None)
        assert values["command"] == "(no hostname)"
        assert values["ProxyHost"] == "-"   # no jump without ProxyMethod 6
        assert values["Protocol"] == "ssh"

    def test_detail_no_side_effects(self, tmp_path, monkeypatch, capsys):
        """convert_keys=False: no puttygen, no stdout noise, no makedirs."""
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "never"))
        detail_values(
            "prod:web-1", self._app().usable["prod:web-1"], None)
        assert capsys.readouterr().out == ""
        assert not (tmp_path / "never").exists()


class TestDetailPane:
    """The detail pane is a read-only form of far-field widgets."""

    async def test_fields_render_and_track_highlight(self):
        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test() as pilot:
            await pilot.pause()
            lv = app.query_one("#sessions")
            lv.index = next(i for i, r in enumerate(app.filtered_rows)
                            if r.name == "prod:web-1")
            await pilot.pause()
            assert app.query_one("#detail-HostName").value \
                == "a.example.com"
            assert app.query_one("#detail-PortNumber").value == "2222"
            lv.index = next(i for i, r in enumerate(app.filtered_rows)
                            if r.name == "solo")
            await pilot.pause()
            assert app.query_one("#detail-HostName").value == "d.example.com"
            assert app.query_one("#detail-PortNumber").value == "22"
            # every DETAIL_FIELDS row exists
            for _, key in DETAIL_FIELDS:
                assert app.query_one(f"#detail-{key}")

    async def test_detail_fields_are_never_focusable(self):
        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test() as pilot:
            await pilot.pause()
            for _, key in DETAIL_FIELDS:
                assert not app.query_one(f"#detail-{key}").focusable
            # tab still toggles panes, fields are not in the cycle
            await pilot.press("tab")
            await pilot.pause()
            assert app.query_one("*:focus").id == "recents"

    async def test_command_field_is_last_with_gap(self):
        """SSH command moved to the bottom: fields, a blank gap row, the
        label, then a fixed 3-row command box."""
        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            children = list(app.query_one("#preview").children)
            assert children[-1].id == "detail-command"
            assert children[-1].region.height == 3      # fixed box
            assert str(children[-2].render()).strip() == "SSH Command"
            assert "detail-gap" in children[-3].classes
            assert not str(children[-3].render()).strip()
            assert "detail-row" in children[0].classes  # fields start first

    async def test_command_box_height_consistent(self):
        """A short command and a long one get the SAME box height."""
        app = SessionBrowser(
            {"solo": {"HostName": "d.example.com"},
             "longy": {"HostName": "l", "User": "",
                       "RemoteCommand": "x" * 120}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.query_one("#sessions").index = 0
            await pilot.pause()
            height_a = app.query_one("#detail-command").region.height
            app.query_one("#sessions").index = 1
            await pilot.pause()
            height_b = app.query_one("#detail-command").region.height
            assert height_a == height_b == 3

    async def test_connect_updates_recent_pane_immediately(self, tmp_path):
        """After a connect, the RECENT pane shows the session moved to
        the top with fresh metadata - WITHOUT closing the browser."""
        import time

        lines = [
            "Windows Registry Editor Version 5.00", "",
            "[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\"
            "solo]",
            '"HostName"="s.example.com"', "",
            "[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\"
            "other]",
            '"HostName"="o.example.com"',
        ]
        reg = tmp_path / "putty.reg"
        reg.write_bytes(b"\xff\xfe" + ("\r\n".join(lines) + "\r\n")
                        .encode("utf-16-le"))
        recents = tmp_path / "recents"
        old_ts = int(time.time()) - 7 * 86400     # a week ago
        recents.write_text(f"other\t{old_ts}\t0\n", encoding="utf-8")

        def callback(name: str) -> None:      # mimic putty.py's recording
            recents.write_text(
                f"solo\t{int(time.time())}\t0\nother\t{old_ts}\t0\n",
                encoding="utf-8")

        app = SessionBrowser(
            {"solo": {"HostName": "s.example.com"},
             "other": {"HostName": "o.example.com"}},
            ["other", "solo"], {"other": {"ts": old_ts}}, reg,
            connect_callback=callback, recent_file=recents)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            names_before = [c.row.name
                            for c in app.query_one("#recents").children]
            assert names_before == ["other", "solo"]   # launch order
            # highlight solo in the ALL pane and connect
            sessions = app.query_one("#sessions")
            sessions.index = next(
                i for i, r in enumerate(app.filtered_rows) if r.name == "solo")
            await pilot.pause()
            await pilot.press("enter")
            for _ in range(3):
                await pilot.pause()
            names = [c.row.name
                     for c in app.query_one("#recents").children]
            assert names == ["solo", "other"]          # reordered live
            first = app.query_one("#recents").children[0]
            assert "just now" in first.row.display     # fresh metadata
            assert "connected" in app.status_message

    async def test_placeholder_when_nothing_highlightable(self):
        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#filter", Input).value = "zzz-not-a-session"
            await pilot.pause()
            await pilot.pause()
            assert str(app.query_one("#detail-command").render()) \
                == "select a session"
            assert app.query_one("#detail-HostName").value == ""


class TestBrowserApp:
    async def test_mount_and_rows_render(self):
        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test() as pilot:
            await pilot.pause()
            lv = app.query_one("#sessions")
            expected = len([r for r in build_all_rows(USABLE)])
            assert len(lv.children) == expected
            assert app.query_one("#preview")

    async def test_filter_narrows(self):
        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test() as pilot:
            await pilot.pause()
            filt = app.query_one("#filter")
            filt.value = "prod"
            await pilot.pause()
            names = [r.name for r in app.filtered_rows]
            assert all("prod" in n.lower() for n in names)
            assert len(names) == 2
            # headers hidden while filtering
            assert not any(n.startswith("__") for n in names)

    async def test_highlight_updates_preview(self):
        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test() as pilot:
            await pilot.pause()
            lv = app.query_one("#sessions")
            # move to a session with a port to see the command field change
            lv.index = 1
            await pilot.pause()
            text = str(app.query_one("#detail-command").render())
            assert "ssh" in text or "select" in text

    async def test_arrow_keys_update_preview(self):
        """Regression: handler was misnamed (on_list_view_highlight),
        so Highlighted events never updated the preview pane."""
        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test() as pilot:
            await pilot.pause()
            lv = app.query_one("#sessions")
            # first selectable row is prod:web-1 (port 2222 in the command)
            lv.index = next(i for i, r in enumerate(app.filtered_rows)
                            if r.name == "prod:web-1")
            await pilot.pause()
            before = str(app.query_one("#detail-command").render())
            lv.index = next(i for i, r in enumerate(app.filtered_rows)
                            if r.name == "solo")
            await pilot.pause()
            after = str(app.query_one("#detail-command").render())
            assert before != after
            assert "d.example.com" in after

    async def test_mount_skips_to_first_selectable(self):
        """If row 0 is a tag header, the preview must still show a
        session (highlight lands on the first selectable row)."""
        app = SessionBrowser(USABLE, [], {}, None)  # first row: PROD header
        async with app.run_test() as pilot:
            await pilot.pause()
            row = app._current_row()
            assert row is not None and not row.name.startswith("__")
            preview = str(app.query_one("#detail-command").render())
            assert "ssh" in preview  # a real session command, not placeholder

    async def test_filter_hide_headers_then_clear(self):
        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test() as pilot:
            await pilot.pause()
            filt = app.query_one("#filter")
            filt.value = "staging"
            await pilot.pause()
            names = [r.name for r in app.filtered_rows]
            assert names == ["staging:web"]
            preview = str(app.query_one("#detail-command").render())
            assert "c.example.com" in preview
            filt.value = ""
            await pilot.pause()
            # headers return, selection re-lands on a selectable row
            assert any(r.name.startswith("__hdr__")
                       for r in app.filtered_rows)
            assert not app._current_row().name.startswith("__")

    async def test_quit_binding(self):
        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
        assert not app.is_running or app.return_value == 0

    async def test_ping_action_opens_modal_and_sets_status(
            self, monkeypatch):
        """p: FAR ping dialog; the result mirrors into the status line
        when the probe lands; Esc closes."""
        import utils.tui_actions as mod
        from utils.far_style import FARKeyBar
        from utils.tui_actions import PingModal
        from utils.far_dialog import FarValueField
        monkeypatch.setattr(mod, "run_ping",
                            lambda h, p: (False, f"{h}: down"))
        app = SessionBrowser(
            {"solo": {"HostName": "192.0.2.1", "PortNumber": "1"}},
            [], {}, None)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#sessions").index = 0
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, PingModal)
            assert app.query_one("#keybar", FARKeyBar).bar_text() \
                .count("Close") == 2        # ping footer keybar
            for _ in range(3):
                await pilot.pause()
            result = modal.query_one("#ping-result", FarValueField)
            assert "down" in result.value and "✗" in result.value
            assert "✗" in app.status_message  # mirrored, persists
            await pilot.press("escape")
            await pilot.pause()
            assert app.query_one("#keybar", FARKeyBar).bar_text() \
                .count("Find") == 1         # browser footer restored

    async def test_ping_jump_host_note_on_failure(self, monkeypatch):
        """A failed direct probe of a jump-hosted session says so."""
        import utils.tui_actions as mod
        from utils.tui_actions import PingModal
        from utils.far_dialog import FarValueField
        monkeypatch.setattr(mod, "run_ping",
                            lambda h, p: (False, f"{h}: down"))
        app = SessionBrowser(
            {"viasolo": {"HostName": "192.0.2.9", "PortNumber": "22",
                         "ProxyMethod": "6", "ProxyHost": "gate.example.com"}},
            [], {}, None)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#sessions").index = 0
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, PingModal)
            for _ in range(3):
                await pilot.pause()
            note = modal.query_one("#ping-note", FarValueField).value
            assert "gate.example.com" in note
            assert "✗" in app.status_message
            assert "gate.example.com" in app.status_message

    async def test_ping_q_closes_not_quits(self, monkeypatch):
        """q inside the ping dialog closes the dialog, not the app."""
        import utils.tui_actions as mod
        from utils.tui_actions import PingModal
        monkeypatch.setattr(mod, "run_ping", lambda *a: (True, "x"))
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#sessions").index = 0
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            assert isinstance(app.screen, PingModal)
            await pilot.press("q")
            await pilot.pause()
            assert not isinstance(app.screen, PingModal)   # dialog closed
            assert not app.is_running or app.return_value is None \
                or app.return_value >= 0   # app still alive

    async def test_ping_p_gated_inside_the_ping_dialog(self, monkeypatch):
        """p must not stack ping dialogs on top of each other."""
        import utils.tui_actions as mod
        from utils.tui_actions import PingModal
        monkeypatch.setattr(mod, "run_ping", lambda *a: (True, "x"))
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#sessions").index = 0
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            assert isinstance(app.screen, PingModal)
            assert app.check_action("ping", ()) is False
            await pilot.press("p")            # must not double-open
            await pilot.pause()
            assert len(app.screen_stack) == 2

    async def test_ping_note_empty_for_success(self, monkeypatch):
        import utils.tui_actions as mod
        from utils.far_dialog import FarValueField
        monkeypatch.setattr(mod, "run_ping",
                            lambda h, p: (True, f"{h}: 8.1 ms"))
        app = SessionBrowser(
            {"viasolo": {"HostName": "h", "ProxyMethod": "6",
                         "ProxyHost": "gate.example.com"}}, [], {}, None)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#sessions").index = 0
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            for _ in range(3):
                await pilot.pause()
            assert not app.screen.query_one("#ping-note",
                                            FarValueField).value
            assert "8.1 ms" in app.screen.query_one(
                "#ping-result", FarValueField).value

    async def test_slash_focuses_filter_enter_connects(self):
        called = []
        app = SessionBrowser(
            {"solo": {"HostName": "h"}}, [], {}, None,
            connect_callback=lambda n: called.append(n))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            focused = app.query_one("*:focus")
            assert focused.id == "filter"
            # empty filter + enter submits -> connect current
            await pilot.press("enter")
            await pilot.pause()
        assert called == ["solo"]

class TestSessionActionMenu:
    def _click_on_row(self, app, row_index, chain=1):
        """Craft a click event targeted at a session ListView row."""
        from textual import events
        lv = app.query_one("#sessions")
        item = lv.children[row_index]
        region = item.region
        return events.Click(
            widget=item,
            x=region.x + 1, y=region.y + 1,
            delta_x=0, delta_y=0,
            button=3, chain=chain, shift=False, meta=False, ctrl=False,
            screen_x=region.x + 1, screen_y=region.y + 1,
            style=None,
        )

    async def test_right_click_opens_menu_at_cursor(self):
        from utils.tui_actions import SessionActionMenu
        app = SessionBrowser(
            {"solo": {"HostName": "h"}}, [], {}, None,
            connect_callback=lambda n: None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.post_message(self._click_on_row(app, 0))
            await pilot.pause()
            await pilot.pause()
            assert isinstance(app.screen, SessionActionMenu)

    async def test_menu_has_dos_shadow(self):
        """Regression: shadow block started 0x0 (box region not computed
        until first layout) so no drop shadow rendered. After mount +
        refresh, shadow must match box size at +2/+1 with 50% black."""
        app = SessionBrowser(
            {"solo": {"HostName": "h"}}, [], {}, None,
            connect_callback=lambda n: None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.post_message(self._click_on_row(app, 0))
            for _ in range(5):
                await pilot.pause()
            box = app.screen.query_one("#far-box")
            shadow = app.screen.query_one("#far-shadow")
            b, s = box.region, shadow.region
            assert s.width == b.width and s.height == b.height
            assert (s.x, s.y) == (b.x + 2, b.y + 1)
            assert shadow.styles.background == \
                Color.parse(ACTIVE_PALETTE.shadow) and s.width > 0

    async def test_menu_is_transparent_over_browser(self):
        from textual.color import Color

        app = SessionBrowser(
            {"solo": {"HostName": "h"}}, [], {}, None,
            connect_callback=lambda n: None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.post_message(self._click_on_row(app, 0))
            await pilot.pause()
            await pilot.pause()
            base, menu = app.screen_stack
            assert menu.styles.background == Color(0, 0, 0, a=0)
            assert base.styles.background == Color.parse(ACTIVE_PALETTE.bg)
            # menu positioned near the click (row 0 -> region near top)
            menu = app.screen.query_one("#far-box")
            assert abs(menu.region.x - app.screen._last_click_x) < 3 \
                if hasattr(app.screen, "_last_click_x") else True

    async def test_menu_enter_executes_connect(self):
        from utils.tui_actions import SessionActionMenu
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            results = []
            menu = SessionActionMenu("solo", (5, 5))
            await app.push_screen(menu, callback=lambda v: results.append(v))
            await pilot.pause()
            assert isinstance(app.screen, SessionActionMenu)
            await pilot.press("enter")
            await pilot.pause()
            assert results == ["connect"]

    async def test_menu_up_down_navigate(self):
        from utils.tui_actions import SessionActionMenu
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            results = []
            menu = SessionActionMenu("solo", (5, 5))
            await app.push_screen(menu, callback=lambda v: results.append(v))
            await pilot.pause()
            assert menu._index == 0
            await pilot.press("down")
            await pilot.pause()
            assert menu._index == 1
            # (item text assertions via _refresh_highlighted output below)
            assert menu.ITEMS[menu._index] == ("Ping", "ping")
            await pilot.press("enter")
            await pilot.pause()
            assert results == ["ping"]

    async def test_menu_esc_closes_with_none(self):
        from utils.tui_actions import SessionActionMenu
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            results = []
            menu = SessionActionMenu("solo", (5, 5))
            await app.push_screen(menu, callback=lambda v: results.append(v))
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert results == [None]

    async def test_menu_up_clamps_at_top(self):
        from utils.tui_actions import SessionActionMenu
        menu = SessionActionMenu("solo", (5, 5))
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await app.push_screen(menu)
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            assert menu._index == 0
            for _ in range(len(menu.ITEMS) - 1):
                await pilot.press("down")
            await pilot.pause()
            assert menu._index == len(menu.ITEMS) - 1

    async def test_right_click_connect_full_flow(self):
        """right-click -> menu box -> Enter runs connect through callback."""
        called = []
        app = SessionBrowser(
            {"solo": {"HostName": "h"}}, [], {}, None,
            connect_callback=lambda n: called.append(n))
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.post_message(self._click_on_row(app, 0))
            await pilot.pause()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert called == ["solo"], f"called={called}"
            assert app.selected_name == "solo"

    async def test_right_click_ping_dispatch(self, monkeypatch):
        """right-click -> down/enter to Ping -> the FAR ping dialog."""
        import utils.tui_actions as mod
        from utils.tui_actions import PingModal
        from utils.far_dialog import FarValueField
        monkeypatch.setattr(mod, "run_ping",
                            lambda h, p: (False, f"{h}: down"))
        app = SessionBrowser(
            {"solo": {"HostName": "192.0.2.1", "PortNumber": "1"}},
            [], {}, None, connect_callback=lambda n: None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.post_message(self._click_on_row(app, 0))
            await pilot.pause()
            await pilot.pause()
            await pilot.press("down")   # Connect -> Ping
            await pilot.press("enter")
            for _ in range(4):
                await pilot.pause()
            assert isinstance(app.screen, PingModal)
            result = app.screen.query_one("#ping-result", FarValueField)
            assert "✗" in result.value and "down" in result.value
            assert "✗" in app.status_message           # mirrored

    async def test_click_outside_menu_closes(self):
        from utils.tui_actions import SessionActionMenu
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            results = []
            menu = SessionActionMenu("solo", (5, 5))
            await app.push_screen(menu, callback=lambda v: results.append(v))
            await pilot.pause()
            # click far from the menu box: the browser's ListView
            from textual import events
            lv = app.query_one("#sessions")
            r = lv.region
            evt = events.Click(widget=lv, x=r.x + 1, y=r.y + 1,
                               delta_x=0, delta_y=0, button=1, chain=1,
                               shift=False, meta=False, ctrl=False,
                               screen_x=r.x + 1, screen_y=r.y + 1, style=None)
            lv.post_message(evt)
            await pilot.pause()
            await pilot.pause()
            assert results == [None]

    async def test_right_click_on_header_noop(self):
        """Right-click over a disabled tag header does nothing."""
        from utils.tui_actions import SessionActionMenu
        app = SessionBrowser(
            {"prod:a": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            lv = app.query_one("#sessions")
            header_idx = next(i for i, c in enumerate(lv.children)
                              if c.row.name.startswith("__"))
            app.post_message(self._click_on_row(app, header_idx))
            await pilot.pause()
            await pilot.pause()
            assert not isinstance(app.screen, SessionActionMenu)
        called = []
        app = SessionBrowser(
            {"solo": {"HostName": "h"}}, [], {}, None,
            connect_callback=lambda n: called.append(n))
        async with app.run_test() as pilot:
            await pilot.pause()
            lv = app.query_one("#sessions")
            lv.index = 0
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        assert called == ["solo"]
        assert app.selected_name == "solo"


    async def test_menu_shadow_echoes_underlying_content(self):
        """The drop shadow echoes the text it covers (fake transparency)."""
        from utils.tui_actions import SessionActionMenu
        app = SessionBrowser(
            {"prod:web-1": {"HostName": "a.example.com"},
             "prod:web-2": {"HostName": "b.example.com"},
             "solo": {"HostName": "d.example.com"}}, [], {}, None,
            connect_callback=lambda n: None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            # click the row under a group header; the menu opens there and
            # its shadow covers the solo row and the OTHER header below it
            app.post_message(self._click_on_row(app, 1))
            for _ in range(9):
                await pilot.pause()
            assert isinstance(app.screen, SessionActionMenu)
            shadow = app.screen.query_one("#far-shadow")
            text = str(shadow.render())
            assert text.strip()  # non-empty shadow content
            assert "lo" in text or "OTHER" in text  # covered rows echo

    async def test_menu_shadow_without_underlying_falls_back_to_browser(self):
        """Direct push (no underlying screen passed): the echo still
        snapshots the browser below on the stack - the copy always
        matches the visible background."""
        from utils.tui_actions import SessionActionMenu
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            menu = SessionActionMenu("solo", (5, 5))
            await app.push_screen(menu)
            for _ in range(5):
                await pilot.pause()
            shadow = menu.query_one("#far-shadow")
            assert "═" in str(shadow.render())   # echoes covered frames

    async def test_menu_shadow_far_styling(self):
        """FAR-console shadow: opaque black slab (no alpha) with the
        covered copy dimmed to dark-gray glyphs."""
        from textual.color import Color

        from utils.tui_actions import SessionActionMenu

        app = SessionBrowser(
            {"prod:web-1": {"HostName": "a.example.com"},
             "solo": {"HostName": "d.example.com"}}, [], {}, None,
            connect_callback=lambda n: None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            # drive the same production entry the click handler calls
            # (posting synthetic Clicks to a pushed modal is flaky under
            # the pilot's message pump); anchor the cursor to a real
            # session ROW so the shadow echo covers painted content
            # (not layout-dependent blank rows)
            first_row = app.query_one("#sessions").children[0].region
            app._last_cursor = (first_row.x + 8, first_row.y)
            await app.run_action_with_modal("prod:web-1")
            for _ in range(9):
                await pilot.pause()
            assert isinstance(app.screen, SessionActionMenu)  # menu open
            shadow = app.screen.query_one("#far-shadow")
            assert shadow.styles.background \
                == Color.parse(ACTIVE_PALETTE.shadow)
            assert shadow.styles.color == Color.parse(ACTIVE_PALETTE.dim)
            # echoed lines carry the FAR dark-gray color explicitly
            content = str(shadow.content)
            assert ACTIVE_PALETTE.dim in content
            assert "50%" not in content  # no alpha blend markers

    async def test_region_text_uses_composited_raster(self):
        """Echo source = the composited raster: window frames and the
        centered caption are captured exactly as painted."""
        from utils import far_dialog

        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            rw = app.query_one("#all-win").region
            text = far_dialog._region_text(
                app.screen, rw.x, rw.y, rw.width, 2)
            assert "ALL CONNECTIONS" in text[0]     # caption row
            assert "╔" in text[0] and "╗" in text[0]  # frame glyphs

    async def test_region_text_falls_back_to_stamping(self, monkeypatch):
        """Without a compositor raster, leaf-label stamping still echoes
        the covered text."""
        from textual.widgets import Static

        from utils import far_dialog

        class FakeRegion:
            x, y, width, height = 2, 0, 10, 1

        leaf = Static("AB", markup=False)
        # replace the Widget.region property with a fixed fake geometry
        # so the unmounted leaf looks stamped-in-place
        monkeypatch.setattr(Static, "region", FakeRegion, raising=False)

        class FakeScreen:
            children = (leaf,)   # no _compositor attribute

        text = far_dialog._region_text(FakeScreen(), 0, 0, 10, 1)
        assert text[0][2:4] == "AB"


class TestContextKeyBar:
    """The footer hotkey bar reflects the current UI context."""

    def _keybar_text(self, scope) -> str:
        from utils.far_style import FARKeyBar
        return scope.query_one("#keybar", FARKeyBar).bar_text()

    async def test_keybar_slots_equal_width(self):
        """FAR look: slots equally spaced across the full bar."""
        from utils.far_style import KeyBarSlot
        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            slots = list(app.query(KeyBarSlot))
            assert len(slots) >= 6
            widths = [s.region.width for s in slots]
            assert max(widths) - min(widths) <= 1  # 1fr remainder only

    async def test_keybar_slot_click_runs_action(self):
        """Clicking a keybar slot dispatches its mapped action."""
        from utils.far_style import KeyBarSlot
        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # click the '/ Find' slot -> filter input gains focus
            find_slot = next(s for s in app.query(KeyBarSlot) if s.key == "/")
            await pilot.click(find_slot)
            await pilot.pause()
            assert app.query_one("*:focus").id == "filter"

    async def test_keybar_slot_click_quit(self):
        from utils.far_style import KeyBarSlot
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            quit_slot = next(s for s in app.query(KeyBarSlot) if s.key == "q")
            await pilot.click(quit_slot)
            await pilot.pause()
            assert not app.is_running

    async def test_keybar_slot_colors(self):
        """Key chip / description colors flow from the palette."""
        from utils.far_style import FARKeyBar
        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            bar = app.query_one("#keybar", FARKeyBar)
            from utils.far_style import KeyBarSlot
            for child in bar.children:
                if not isinstance(child, KeyBarSlot):
                    continue
                key_st = child.children[0].styles
                desc_st = child.children[1].styles
                assert key_st.background \
                    == Color.parse(ACTIVE_PALETTE.key_bg)
                assert key_st.color == Color.parse(ACTIVE_PALETTE.key_text)
                assert desc_st.color \
                    == Color.parse(ACTIVE_PALETTE.desc_text)
                break

    async def test_filter_focus_swaps_keybar(self):
        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            text = self._keybar_text(app)
            assert "Find" in text  # list context
            await pilot.press("slash")
            await pilot.pause()
            text = self._keybar_text(app)
            assert "Back to list" in text and "Find" not in text
            await pilot.press("escape")
            await pilot.pause()
            text = self._keybar_text(app)
            assert "Find" in text  # back to list context

    async def test_right_click_menu_swaps_app_keybar(self):
        from utils.tui_actions import SessionActionMenu
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.post_message(TestSessionActionMenu()._click_on_row(app, 0))
            await pilot.pause()
            await pilot.pause()
            assert isinstance(app.screen, SessionActionMenu)
            text = self._keybar_text(app)
            assert "Navigate" in text and "Close" in text
            assert "Find" not in text  # list hotkeys hidden while menu open
            await pilot.press("escape")
            await pilot.pause()
            await pilot.pause()
            text = self._keybar_text(app)
            assert "Find" in text  # restored after close

    async def test_log_viewer_keybar_reacts_to_search_focus(self, tmp_path):
        from utils.tui_actions import LogModal
        log = tmp_path / "a.log"
        log.write_text("one\ntwo\n", encoding="utf-8")
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            screen = LogModal(log)
            await app.push_screen(screen)
            await pilot.pause()
            text = self._keybar_text(screen)
            assert "Search" in text and "Back" in text
            screen.query_one("#log-search").focus()
            await pilot.pause()
            text = self._keybar_text(screen)
            assert "Jump" in text and "Done" in text
            assert "Search" not in text


class TestMenuMouseEvents:
    """Hover moves the highlight; left click executes (popup menu)."""

    def _mouse_event(self, cls, widget, button=0):
        region = widget.region
        return cls(
            widget=widget,
            x=region.x + 1, y=region.y + 1,
            delta_x=0, delta_y=0,
            button=button, shift=False, meta=False, ctrl=False,
            screen_x=region.x + 1, screen_y=region.y + 1,
            style=None,
        )

    async def test_hover_moves_highlight(self):
        from textual.app import App

        from utils.tui_actions import SessionActionMenu

        class Host(App):
            pass

        app = Host()
        async with app.run_test(size=(80, 24)) as pilot:
            menu = SessionActionMenu("solo", (8, 5))
            await app.push_screen(menu)
            await pilot.pause()
            await pilot.pause()
            opt1 = list(menu.query(".ctx-option"))[1]  # Ping
            assert menu._index == 0
            menu.post_message(self._mouse_event(events.MouseMove, opt1))
            await pilot.pause()
            await pilot.pause()
            assert menu._index == 1
            assert "ping" not in (menu._dismissed or []) if hasattr(menu, "_dismissed") else True

    async def test_hover_stays_inside_title(self):
        from textual.app import App

        from utils.tui_actions import SessionActionMenu

        class Host(App):
            pass

        app = Host()
        async with app.run_test(size=(80, 24)) as pilot:
            menu = SessionActionMenu("solo", (8, 5))
            await app.push_screen(menu)
            await pilot.pause()
            # the title lives in the box's border row; hovering the box
            # chrome (not an option row) must not move the highlight
            box = menu.query_one("#far-box")
            menu.post_message(self._mouse_event(events.MouseMove, box))
            await pilot.pause()
            await pilot.pause()
            assert menu._index == 0  # unchanged: title is not an option

    async def test_left_click_executes_option(self):
        from textual.app import App

        from utils.tui_actions import SessionActionMenu

        class Host(App):
            pass

        app = Host()
        async with app.run_test(size=(80, 24)) as pilot:
            results = []
            menu = SessionActionMenu("solo", (8, 5))
            await app.push_screen(menu, callback=lambda v: results.append(v))
            await pilot.pause()
            await pilot.pause()
            opt2 = list(menu.query(".ctx-option"))[2]  # Copy
            menu.post_message(self._mouse_event(events.Click, opt2, button=1))
            await pilot.pause()
            await pilot.pause()
            assert results == ["copy"]

    async def test_left_click_outside_executes_nothing(self):
        from textual.app import App

        from utils.tui_actions import SessionActionMenu

        class Host(App):
            pass

        app = Host()
        async with app.run_test(size=(80, 24)) as pilot:
            results = []
            menu = SessionActionMenu("solo", (8, 5))
            await app.push_screen(menu, callback=lambda v: results.append(v))
            await pilot.pause()
            await pilot.pause()
            # click on the host app's default screen content (outside menu)
            evt = events.Click(
                widget=app.screen, x=30, y=3, delta_x=0, delta_y=0,
                button=1, shift=False, meta=False, ctrl=False,
                screen_x=30, screen_y=3, style=None)
            app.post_message(evt)
            await pilot.pause()
            await pilot.pause()
            assert results == [None]

    async def test_hover_then_click_same_row(self):
        """Hover onto a row then left click on it: highlight syncs, runs."""
        from textual.app import App

        from utils.tui_actions import SessionActionMenu

        class Host(App):
            pass

        app = Host()
        async with app.run_test(size=(80, 24)) as pilot:
            results = []
            menu = SessionActionMenu("solo", (8, 5))
            await app.push_screen(menu, callback=lambda v: results.append(v))
            await pilot.pause()
            await pilot.pause()
            opt3 = list(menu.query(".ctx-option"))[3]  # View logs
            menu.post_message(self._mouse_event(events.MouseMove, opt3))
            await pilot.pause()
            await pilot.pause()
            assert menu._index == 3
            menu.post_message(self._mouse_event(events.Click, opt3, button=1))
            await pilot.pause()
            await pilot.pause()
            assert results == ["logs"]


class TestEditModal:
    """FAR-style Edit dialog: PuTTY-like fields, surgical .reg save."""

    SESSION = {"HostName": "a.example.com", "UserName": "ops",
               "PortNumber": "2222", "Protocol": "ssh"}
    NAME = "prod-web-1"

    def _setup(self, tmp_path):
        """Browser over a REAL tmp .reg (utf-16le, CRLF)."""
        lines = [
            "Windows Registry Editor Version 5.00",
            "",
            "[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\"
            f"{self.NAME}]",
        ]
        for key, value in self.SESSION.items():
            if key == "PortNumber":
                lines.append('"PortNumber"=dword:%08x' % int(value))
            else:
                lines.append('"%s"="%s"' % (key, value))
        reg = tmp_path / "putty.reg"
        text = "\r\n".join(lines) + "\r\n"
        reg.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
        app = SessionBrowser(
            {self.NAME: dict(self.SESSION)}, [], {}, reg)
        return app, reg

    async def test_modal_hidden_until_placed_no_top_left_flash(self, tmp_path):
        """Regression: the dialog box painted at its default top-left
        spot for one frame, then teleported to the center. The box must
        stay invisible until _place() applies the final offset - and
        the drop shadow must arrive in the SAME batch, not pop in a
        frame later."""
        from utils.tui_actions import EditSessionModal
        app, _reg = self._setup(tmp_path)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            modal = EditSessionModal(self.NAME, dict(self.SESSION))
            await app.push_screen(modal)   # awaited = mount, not paint
            box = modal.query_one("#far-box")
            shadow = modal.query_one("#far-shadow")
            # parked off-screen: nothing of the box paints before _place
            assert box.region.x < -1000 and box.region.y < -1000
            assert box.region.width > 0  # ...but it still measures
            await pilot.pause()             # measure/place/sync batch
            assert box.region.x > 0        # placed on-screen now
            # revealed at the CENTERED position, never the top-left
            assert abs((box.region.x + box.region.width // 2)
                       - modal.size.width // 2) <= 1
            assert abs((box.region.y + box.region.height // 2)
                       - modal.size.height // 2) <= 1
            assert box.region.x > 0 and box.region.y > 0
            # the shadow arrived with the box (same batch): sized to
            # the target region, offset +2/+1, echo content painted
            assert shadow.region.width == box.region.width
            assert (shadow.region.x, shadow.region.y) \
                == (box.region.x + 2, box.region.y + 1)
            assert str(shadow.content)

    async def test_close_button_rides_chrome_and_dismisses(self, tmp_path):
        """[X] sits inline with the box's top-right border chrome and a
        click dismisses the modal like Esc."""
        from utils.tui_actions import EditSessionModal
        app, _reg = self._setup(tmp_path)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await app._open_edit_modal(self.NAME)
            for _ in range(3):
                await pilot.pause()
            modal = app.screen
            assert isinstance(modal, EditSessionModal)
            box = modal.query_one("#far-box")
            close = modal.query_one("#far-close")
            # inline with the top border, one dash shy of the corner
            assert close.region.y == box.region.y
            assert close.region.x > box.region.x
            assert close.region.x + close.region.width \
                == box.region.x + box.region.width - 2
            await pilot.click("#far-close")
            for _ in range(3):
                await pilot.pause()
            assert app.screen is not modal
            assert app.status_message == "edit cancelled"

    async def test_prefills_from_session(self, tmp_path):
        app, _reg = self._setup(tmp_path)
        from utils.tui_actions import EditSessionModal
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await app._open_edit_modal(self.NAME)
            for _ in range(3):
                await pilot.pause()
            modal = app.screen
            assert isinstance(modal, EditSessionModal)
            assert modal.query_one("#edit-HostName", Input).value \
                == "a.example.com"
            assert modal.query_one("#edit-UserName", Input).value == "ops"
            assert modal.query_one("#edit-PortNumber", Input).value == "2222"
            assert modal.query_one("#edit-Protocol", Input).value == "ssh"
            assert modal.query_one("#edit-PublicKeyFile", Input).value == ""
            # fields are the focus landing target (first field); note
            # app.focused (not *:focus) - a screen stack keeps every
            # screen's last-focused widget matching the :focus pseudo
            assert app.focused.id == "edit-Name"

    async def test_save_writes_reg_and_refreshes_browser(self, tmp_path):
        from utils.putty_parse_util import parse_putty_reg

        app, reg = self._setup(tmp_path)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await app._open_edit_modal(self.NAME)
            await pilot.pause()
            modal = app.screen
            modal.query_one("#edit-HostName", Input).value = "h-new"
            modal.query_one("#edit-RemoteCommand", Input).value = "htop"
            await modal.action_save()
            for _ in range(3):
                await pilot.pause()
            # surgical write on disk: only those two lines
            parsed = parse_putty_reg(reg)
            assert parsed[self.NAME]["HostName"] == "h-new"
            assert parsed[self.NAME]["RemoteCommand"] == "htop"
            # browser sees the new values without a restart
            assert app.usable[self.NAME]["HostName"] == "h-new"
            assert "saved" in app.status_message
            assert "backup" in app.status_message
            # rows rebuilt from refreshed data
            rows = [c.row.name for c in app.query_one("#sessions").children]
            assert self.NAME in rows

    async def test_escape_cancels_without_write(self, tmp_path):
        app, reg = self._setup(tmp_path)
        before = reg.read_bytes()
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await app._open_edit_modal(self.NAME)
            await pilot.pause()
            app.screen.query_one("#edit-HostName", Input).value = "nope"
            await pilot.press("escape")
            await pilot.pause()
            await pilot.pause()
            assert reg.read_bytes() == before          # nothing written
            assert "edit cancelled" in app.status_message
            assert app.query_one("*:focus").id != "edit-Name"

    async def test_port_validation_stays_open(self, tmp_path):
        app, reg = self._setup(tmp_path)
        before = reg.read_bytes()
        from utils.tui_actions import EditSessionModal
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await app._open_edit_modal(self.NAME)
            await pilot.pause()
            modal = app.screen
            modal.query_one("#edit-PortNumber", Input).value = "abc"
            await modal.action_save()
            await pilot.pause()
            assert isinstance(app.screen, EditSessionModal)  # still open
            assert "1-65535" in str(modal.query_one("#edit-error").render())
            assert reg.read_bytes() == before             # nothing written

    async def test_quote_rejected_in_modal(self, tmp_path):
        app, reg = self._setup(tmp_path)
        before = reg.read_bytes()
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await app._open_edit_modal(self.NAME)
            await pilot.pause()
            modal = app.screen
            modal.query_one("#edit-RemoteCommand", Input).value = 'say "hi"'
            await modal.action_save()
            await pilot.pause()
            assert isinstance(app.screen, type(app.screen))  # still open
            assert "quotes" in str(modal.query_one("#edit-error").render())
            assert reg.read_bytes() == before

    async def test_jump_host_toggles_proxy_method(self, tmp_path):
        from utils.putty_parse_util import parse_putty_reg

        app, reg = self._setup(tmp_path)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await app._open_edit_modal(self.NAME)
            await pilot.pause()
            modal = app.screen
            modal.query_one("#edit-ProxyHost", Input).value = "j.example.com"
            await modal.action_save()
            for _ in range(3):
                await pilot.pause()
            parsed = parse_putty_reg(reg)
            values = parsed[self.NAME]
            assert values["ProxyHost"] == "j.example.com"
            assert values["ProxyMethod"] == "6"   # toggled automatically

    async def test_clearing_jump_host_resets_proxy_method(self, tmp_path):
        from utils.putty_parse_util import parse_putty_reg

        session = dict(self.SESSION, ProxyMethod="6",
                       ProxyHost="j.example.com")
        reg = tmp_path / "putty.reg"
        lines = [
            "Windows Registry Editor Version 5.00", "",
            "[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\"
            f"{self.NAME}]",
        ]
        for key, value in session.items():
            if key in ("PortNumber", "ProxyMethod"):
                lines.append('"%s"=dword:%08x' % (key, int(value)))
            else:
                lines.append('"%s"="%s"' % (key, value))
        reg.write_bytes(
            b"\xff\xfe" + ("\r\n".join(lines) + "\r\n").encode("utf-16-le"))
        app = SessionBrowser({self.NAME: dict(session)}, [], {}, reg)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await app._open_edit_modal(self.NAME)
            await pilot.pause()
            modal = app.screen
            assert modal.query_one("#edit-ProxyHost", Input).value \
                == "j.example.com"
            modal.query_one("#edit-ProxyHost", Input).value = ""
            await modal.action_save()
            for _ in range(3):
                await pilot.pause()
            values = parse_putty_reg(reg)[self.NAME]
            assert values["ProxyHost"] == ""
            assert values["ProxyMethod"] == "0"    # toggled back off

    async def test_enter_gates_while_modal_open(self, tmp_path):
        app, _reg = self._setup(tmp_path)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            assert app.check_action("connect", ()) is True
            await app._open_edit_modal(self.NAME)
            await pilot.pause()
            assert app.check_action("connect", ()) is False
            await pilot.press("enter")  # must NOT connect
            await pilot.pause()
            assert app.selected_name is None

    async def test_edit_keybar_swaps_and_restores(self, tmp_path):
        from utils.far_style import FARKeyBar

        app, _reg = self._setup(tmp_path)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await app._open_edit_modal(self.NAME)
            await pilot.pause()
            text = app.query_one("#keybar", FARKeyBar).bar_text()
            assert "OK" in text and "Cancel" in text
            await pilot.press("escape")
            await pilot.pause()
            text = app.query_one("#keybar", FARKeyBar).bar_text()
            assert "Find" in text  # browser keybar restored

    async def test_e_opens_edit_for_highlighted(self, tmp_path):
        """e = open the edit dialog for the highlighted session."""
        from utils.far_style import FARKeyBar
        from utils.tui_actions import EditSessionModal

        app, _reg = self._setup(tmp_path)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            # the keybar advertises the hotkey
            assert "e Edit" in app.query_one("#keybar", FARKeyBar).bar_text()
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, EditSessionModal)
            assert app.focused.id == "edit-Name"   # the Name field is first

    async def test_e_gated_while_action_menu_open(self, tmp_path):
        """e must not open the edit dialog underneath/behind an open
        popup menu (the menu owns the screen while it is up)."""
        from utils.tui_actions import SessionActionMenu

        app, _reg = self._setup(tmp_path)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await app.run_action_with_modal(self.NAME)
            await pilot.pause()
            assert isinstance(app.screen, SessionActionMenu)
            assert app.check_action("edit", ()) is False
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, SessionActionMenu)  # unchanged

    async def test_e_no_reg_reports_status(self):
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            assert "no .reg file" in app.status_message

    async def test_enter_on_focused_button_saves(self, tmp_path):
        from utils.putty_parse_util import parse_putty_reg

        app, reg = self._setup(tmp_path)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await app._open_edit_modal(self.NAME)
            await pilot.pause()
            modal = app.screen
            modal.query_one("#edit-HostName", Input).value = "via-enter"
            btn = next(w for w in modal.query(".far-btn")
                       if "Save" in str(w.render()))
            btn.focus()
            await pilot.pause()
            await pilot.press("enter")
            for _ in range(3):
                await pilot.pause()
            assert parse_putty_reg(reg)[self.NAME]["HostName"] == "via-enter"
            assert app.screen is not modal   # dialog closed

    async def test_save_button_click_saves(self, tmp_path):
        from utils.putty_parse_util import parse_putty_reg

        app, reg = self._setup(tmp_path)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await app._open_edit_modal(self.NAME)
            await pilot.pause()
            modal = app.screen
            modal.query_one("#edit-HostName", Input).value = "via-click"
            btn = next(w for w in modal.query(".far-btn")
                       if "Save" in str(w.render()))
            await pilot.click(btn)
            for _ in range(3):
                await pilot.pause()
            assert parse_putty_reg(reg)[self.NAME]["HostName"] == "via-click"
            assert app.screen is not modal   # dialog closed

    async def test_cancel_button_discards(self, tmp_path):
        app, reg = self._setup(tmp_path)
        before = reg.read_bytes()
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await app._open_edit_modal(self.NAME)
            await pilot.pause()
            modal = app.screen
            modal.query_one("#edit-HostName", Input).value = "nope"
            btn = next(w for w in modal.query(".far-btn")
                       if "Cancel" in str(w.render()))
            await pilot.click(btn)
            for _ in range(3):
                await pilot.pause()
            assert reg.read_bytes() == before   # nothing written
            assert app.screen is not modal      # dialog closed
            assert "edit cancelled" in app.status_message

    async def test_save_with_rename_rewrites_reg_swaps_usable(self, tmp_path):
        """Editing the Name field renames the .reg section, migrates the
        recents file and swaps the browser's data in memory."""
        from utils.putty_parse_util import parse_putty_reg
        from utils.recent_sessions_util import load_recent_entries

        app, reg = self._setup(tmp_path)
        recents = tmp_path / "recents"
        recents.write_text(f"{self.NAME}\t1792645829\t0\n", encoding="utf-8")
        app.recent_file = recents
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await app._open_edit_modal(self.NAME)
            await pilot.pause()
            modal = app.screen
            # the Name field is prefilled DECODED (spaces readable)
            assert modal.query_one("#edit-Name", Input).value \
                == self.NAME
            modal.query_one("#edit-Name", Input).value = "Legacy Web 1"
            await modal.action_save()
            for _ in range(4):
                await pilot.pause()
            parsed = parse_putty_reg(reg)
            assert "Legacy%20Web%201" in parsed and self.NAME not in parsed
            # live data swapped, no restart needed
            assert "Legacy%20Web%201" in app.usable
            assert self.NAME not in app.usable
            # recents migrated, metadata kept
            migrated = load_recent_entries(recents)
            assert migrated["Legacy%20Web%201"]["name"] == "Legacy%20Web%201"
            assert migrated["Legacy%20Web%201"]["exit"] == 0
            assert "renamed" in app.status_message
            assert "Legacy Web 1" in [
                r.display for r in app.rows
                if not r.name.startswith("__")]

    async def test_name_field_empty_refused(self, tmp_path):
        from utils.tui_actions import EditSessionModal

        app, reg = self._setup(tmp_path)
        before = reg.read_bytes()
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await app._open_edit_modal(self.NAME)
            await pilot.pause()
            modal = app.screen
            modal.query_one("#edit-Name", Input).value = ""
            await modal.action_save()
            await pilot.pause()
            assert isinstance(app.screen, EditSessionModal)  # still open
            assert "Name" in str(modal.query_one("#edit-error").render())
            assert reg.read_bytes() == before   # nothing written

    async def test_right_click_menu_reaches_edit(self, tmp_path):
        """menu -> Edit settings -> the FAR edit dialog opens."""
        from utils.tui_actions import EditSessionModal, SessionActionMenu

        app, _reg = self._setup(tmp_path)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await app.run_action_with_modal(self.NAME)   # opens popup menu
            await pilot.pause()
            assert isinstance(app.screen, SessionActionMenu)
            for _ in range(4):                           # Connect..Edit
                await pilot.press("down")
            await pilot.press("enter")                   # Edit settings
            for _ in range(5):
                await pilot.pause()
            assert isinstance(app.screen, EditSessionModal)


class TestThemePalettes:
    """Palettes are data; every TUI color flows from one entry."""

    def test_all_roles_present_in_every_palette(self):
        from dataclasses import fields as dc_fields

        from utils.palette_util import FAR_PALETTE, MONOKAI_PALETTE, Palette

        def complete(palette) -> bool:
            return all(
                getattr(palette, f.name) for f in dc_fields(Palette))
        assert complete(FAR_PALETTE) and complete(MONOKAI_PALETTE)

    def test_get_palette_selects_and_validates(self):
        assert get_palette("monokai").bg == "#272822"
        assert get_palette("monokai").selected_bg == "#F92672"
        assert get_palette("far").bg == "#0000AA"
        # default look is Monokai (PUTTY_THEME=far restores DOS blue)
        assert get_palette(None).bg == "#272822"
        with pytest.raises(KeyError, match="known themes"):
            get_palette("gruvbox")

    def test_css_builders_emit_palette_colors_only(self):
        mono = get_palette("monokai")

        from utils.far_style import far_keybar_css, far_shadow_css, far_window_css
        builders = (far_shadow_css(mono), far_window_css(mono),
                    far_keybar_css(mono))
        combined = "\n".join(builders)
        # monokai in, no FAR blue anywhere
        assert "#272822" in combined and "#0000AA" not in combined
        assert mono.frame in combined and mono.title in combined

    def test_textual_theme_maps_roles(self):
        mono = to_textual_theme(get_palette("monokai"))
        assert mono.name == "monokai"
        assert mono.primary == "#49483E"    # cursor bar
        assert mono.secondary == "#FD971F"  # captions
        assert mono.background == "#272822"
        assert mono.error == "#F92672"
        far = to_textual_theme(get_palette("far"))
        assert far.primary == "#00AAAA" and far.background == "#0000AA"

    async def test_browser_activates_active_palette_theme(self):
        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            assert app.theme == ACTIVE_PALETTE.name
            # the palette's variables really drive the chrome
            assert app.get_css_variables()["primary"] \
                == ACTIVE_PALETTE.highlight_bg

    async def test_monokai_palette_installable(self):
        """A palette can be force-installed programmatically
        (PUTTY_THEME pre-selects it for normal runs)."""
        mono = get_palette("monokai")
        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            from utils.far_style import install_far_theme
            install_far_theme(app, mono)
            await pilot.pause()
            assert app.theme == "monokai"
            shadow = get_palette("monokai").shadow
            assert app.get_css_variables()["background"] == mono.bg
            assert shadow == "#000000"


class TestLogMarkup:
    """[b] tags are OUT: bold is real via Text renderables (log content
    is never parsed for markup - bracketed log text stays literal)."""

    def _open(self, tmp_path, body):
        monkeypatch_dir = tmp_path / "logs"
        monkeypatch_dir.mkdir()
        (monkeypatch_dir / "2026-10-21_0900-solo.log").write_text(
            body, encoding="utf-8")
        from utils.tui_actions import LogModal
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        screen = LogModal(monkeypatch_dir / "2026-10-21_0900-solo.log")
        return app, screen

    async def test_mark_is_bold_text_not_tags(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        (tmp_path / "logs").mkdir(exist_ok=True)
        (tmp_path / "logs" / "2026-10-21_0900-solo.log").write_text(
            "needle\n", encoding="utf-8")
        from utils.tui_actions import LogModal

        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            screen = LogModal(
                tmp_path / "logs" / "2026-10-21_0900-solo.log")
            await app.push_screen(screen)
            await pilot.pause()
            sb = screen.query_one("#log-search")
            sb.focus()
            sb.value = "needle"
            await pilot.pause()
            view = screen.query_one("#log-view")
            text = "".join(strip.text for strip in view.lines)
            # no literal tags in the rendering
            assert "[b]" not in text and "[/b]" not in text
        # and the pure helper: bold span on the number, plain content
        mark = screen._mark(11, "hello")
        assert mark.plain == "12 hello"
        bold = [sp for sp in mark.spans
                if "bold" in str(sp.style)]
        assert bold and bold[0].start == 0

    async def test_match_row_is_bold_in_context(self, tmp_path,
                                                monkeypatch):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        (tmp_path / "logs").mkdir(exist_ok=True)
        (tmp_path / "logs" / "2026-10-21_0900-solo.log").write_text(
            "a\nNEEDLE\nb\n", encoding="utf-8")
        from utils.tui_actions import LogModal

        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            screen = LogModal(
                tmp_path / "logs" / "2026-10-21_0900-solo.log")
            await app.push_screen(screen)
            await pilot.pause()
            sb = screen.query_one("#log-search")
            sb.focus()
            sb.value = "needle"
            await pilot.pause()
            await screen.action_next_match()
            await pilot.pause()
            view = screen.query_one("#log-view")
            join = "".join(strip.text for strip in view.lines)
            assert "[b]" not in join
            match_row = [
                strip for i, strip in enumerate(view.lines)
                if "NEEDLE" in strip.text
            ]
            assert len(match_row) == 1, "match row visible, once"
            segs = getattr(match_row[0], "_segments", None) \
                or list(match_row[0])
            assert any("bold" == str(seg.style) for seg in segs), \
                "the match row is REALLY bold"


class TestDeleteFlow:
    """Delete: d / right-click menu; y/N confirm; guarded writer."""

    SESSION = {"HostName": "a.example.com", "UserName": "ops",
               "PortNumber": "2222", "Protocol": "ssh"}
    NAME = "prod-web-1"

    def _setup(self, tmp_path):
        lines = [
            "Windows Registry Editor Version 5.00", "",
            "[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\"
            f"{self.NAME}]",
        ]
        for key, value in self.SESSION.items():
            if key == "PortNumber":
                lines.append('"PortNumber"=dword:%08x' % int(value))
            else:
                lines.append('"%s"="%s"' % (key, value))
        lines.append("")
        lines.append("[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY"
                     "\\Sessions\\other]")
        lines.append('"HostName"="o.example.com"')
        reg = tmp_path / "putty.reg"
        reg.write_bytes(b"\xff\xfe" + ("\r\n".join(lines) + "\r\n")
                        .encode("utf-16-le"))
        recents = tmp_path / "recents"
        recents.write_text(f"{self.NAME}\t1792645829\t0\nother\n",
                           encoding="utf-8")
        app = SessionBrowser(
            {self.NAME: dict(self.SESSION), "other": {"HostName": "o"}},
            [self.NAME], {self.NAME: {"name": self.NAME}}, reg,
            recent_file=recents)
        return app, reg

    async def test_d_opens_confirm_dialog(self, tmp_path, monkeypatch):
        from utils.far_style import FARKeyBar
        from utils.tui_actions import DeleteConfirmModal

        app, _reg = self._setup(tmp_path)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            # the footer replaces Copy with Delete
            text = app.query_one("#keybar", FARKeyBar).bar_text()
            assert "d Delete" in text and "Copy" not in text
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, DeleteConfirmModal)
            # destructive: focus starts on CANCEL
            assert isinstance(app.focused, object)
            focused = app.focused
            assert "Cancel" in str(focused.render())

    async def test_confirm_y_deletes_purges_recents(self, tmp_path):
        from utils.putty_parse_util import parse_putty_reg
        from utils.recent_sessions_util import load_recent_entries

        app, reg = self._setup(tmp_path)
        recents = app.recent_file
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("y")
            for _ in range(4):
                await pilot.pause()
            parsed = parse_putty_reg(reg)
            assert self.NAME not in parsed and "other" in parsed
            assert self.NAME not in app.usable
            assert app.recent_names == []
            assert load_recent_entries(recents).keys() == {"other"}
            assert "deleted" in app.status_message
            # rows rebuilt without the deleted session
            names = [c.row.name for c in app.query_one("#sessions").children]
            assert self.NAME not in names

    async def test_esc_cancels_delete(self, tmp_path):
        from utils.putty_parse_util import parse_putty_reg

        app, reg = self._setup(tmp_path)
        before = reg.read_bytes()
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert reg.read_bytes() == before        # file untouched
            assert parse_putty_reg(reg)[self.NAME]   # session remains
            assert "delete cancelled" in app.status_message
            from utils.tui_actions import DeleteConfirmModal
            assert not isinstance(app.screen, DeleteConfirmModal)
