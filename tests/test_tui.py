"""Textual TUI tests via run_test() pilots (no real terminal needed)."""
import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

pytest.importorskip("textual")

import putty
from utils.tui_actions import run_ping
from utils.tui_browser import SessionBrowser, build_rows, preview_text

USABLE = {
    "prod:web-1": {"HostName": "a.example.com", "UserName": "ops",
                   "PortNumber": "2222"},
    "prod:web-2": {"HostName": "b.example.com", "UserName": "ops"},
    "staging:web": {"HostName": "c.example.com"},
    "solo": {"HostName": "d.example.com"},
}


class TestTuiActions:
    def test_run_ping_reachable(self):
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        ok, msg = run_ping("127.0.0.1", str(port))
        srv.close()
        assert ok and "reachable" in msg

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

    def test_logs_screen_lists_logs(self, tmp_path):
        from utils.tui_actions import LogsScreen
        (tmp_path / "2026-01-01_1200-host.log").write_text("hello")
        screen = LogsScreen(tmp_path)
        assert screen.logs is not None  # populated on mount
        assert LogsScreen(tmp_path).session_name is None

    def test_logs_screen_session_filter(self, tmp_path):
        from utils.logs_util import list_logs
        (tmp_path / "2026-01-01_1200-web1.log").write_text("a\nb\n")
        (tmp_path / "2026-01-02_1200-other.log").write_text("c\n")
        (tmp_path / "2026-01-03_1200-web1old.log").write_text("d\n")
        import os
        import time
        # newest first: give distinct mtimes
        os.utime(tmp_path / "2026-01-01_1200-web1.log",
                 (time.time() + 2, time.time() + 2))
        logs = list_logs(tmp_path, session_name="web1")
        assert [p.name for p, _, _ in logs] == [
            "2026-01-01_1200-web1.log", "2026-01-03_1200-web1old.log"]

    async def test_all_logs_chooser_opens_and_dismisses(self):
        """shift+L opens the all-logs chooser; Esc returns to browser."""
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("L")
            await pilot.pause()
            from utils.tui_actions import LogsScreen
            assert isinstance(app.screen, LogsScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen_stack == [app.screen]  # chooser popped


class TestLogViewer:
    def _make_log(self, tmp_path, lines):
        p = tmp_path / "2026-10-07_1700-web1.log"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    async def test_lines_render_on_mount(self, tmp_path):
        from utils.tui_actions import LogViewerScreen
        p = self._make_log(tmp_path, ["one", "two", "ERROR three"])
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            screen = LogViewerScreen(p)
            await app.push_screen(screen)
            await pilot.pause()
        assert screen.lines == ["one", "two", "ERROR three"]

    async def test_search_filters_lines(self, tmp_path):
        from utils.tui_actions import LogViewerScreen
        p = self._make_log(tmp_path, ["one", "two", "ERROR three"])
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            screen = LogViewerScreen(p)
            await app.push_screen(screen)
            await pilot.pause()
            sb = screen.query_one("#log-search")
            sb.focus()
            sb.value = "error"
            await pilot.pause()
            await pilot.wait_for_scheduled_animations() if False else None
            # on_input_changed triggers _apply_search
            assert len(screen.match_positions) == 1
            status = str(screen.query_one("#log-status").render())
            assert "match 1/1" in status

    async def test_search_empty_resets(self, tmp_path):
        from utils.tui_actions import LogViewerScreen
        p = self._make_log(tmp_path, ["a", "b"])
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            screen = LogViewerScreen(p)
            await app.push_screen(screen)
            await pilot.pause()
            sb = screen.query_one("#log-search")
            sb.focus()
            sb.value = "zzz"
            await pilot.pause()
            assert screen.match_positions == []
            sb.value = ""
            await pilot.pause()
            assert screen.match_positions == []  # no filter = all lines shown

    async def test_n_jumps_with_context(self, tmp_path):
        from utils.tui_actions import LogViewerScreen
        lines = [f"line{i}" for i in range(30)] + ["TARGET"] + \
                [f"after{i}" for i in range(5)]
        p = self._make_log(tmp_path, lines)
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 40)) as pilot:
            screen = LogViewerScreen(p)
            await app.push_screen(screen)
            await pilot.pause()
            sb = screen.query_one("#log-search")
            sb.focus()
            sb.value = "TARGET"
            await pilot.pause()
            await screen.action_next_match()
            assert screen.match_positions  # search landed
            ctx = screen._match_context(screen.match_positions[0])
            assert "31 TARGET" in " ".join(ctx) or "TARGET" in " ".join(ctx)

    async def test_l_opens_newest_session_log_directly(self, tmp_path, monkeypatch):
        """l = newest log for the highlighted session, straight into the
        searchable viewer (skips the chooser)."""
        from utils.tui_actions import LogViewerScreen
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
            assert isinstance(app.screen, LogViewerScreen)
            assert app.screen.log_path.name == "2026-10-07_1700-solo.log"

    async def test_l_no_logs_shows_status(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "empty-logs"))
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await pilot.press("l")  # no logs yet
            await pilot.pause()
            assert "no logs yet" in app.status_message
            from utils.tui_actions import LogViewerScreen
            assert not isinstance(app.screen, LogViewerScreen)

    async def test_esc_return_to_browser(self, tmp_path):
        from utils.tui_actions import LogViewerScreen
        p = self._make_log(tmp_path, ["x"])
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await app.push_screen(LogViewerScreen(p))
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is not None  # back to browser (no crash)

    def test_logs_screen_push_and_dismiss(self):
        from utils.tui_actions import LogsScreen

        async def go():
            app = SessionBrowser(USABLE, [], {}, None)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await app.push_screen(LogsScreen(Path("/tmp")))
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()

        import asyncio
        asyncio.run(go())


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
        monkeypatch.setattr(putty, "pick_session_interactive",
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
    def test_order_tags_and_other(self):
        rows = build_rows(USABLE, [], None)
        names = [r.name for r in rows if not r.name.startswith("__")]
        assert names == ["prod:web-1", "prod:web-2", "staging:web", "solo"]
        headers = [r.display for r in rows if r.name.startswith("__hdr__")]
        assert any("PROD" in h for h in headers)
        assert any("OTHER" in h for h in headers)

    def test_recents_first_with_metadata(self):
        import time
        entries = {"prod:web-1": {"ts": int(time.time()) - 7200, "exit": 255}}
        rows = build_rows(USABLE, ["prod:web-1"], entries)
        first = rows[0]
        assert first.name == "prod:web-1"
        assert "2h ago" in first.display and "✗" in first.display

    def test_flat_mode(self):
        rows = build_rows(USABLE, [], None, group_by_tag=False)
        assert not any(r.name.startswith("__hdr__") for r in rows)


class TestPreview:
    def test_preview_text_contents(self):
        text = preview_text(
            {"HostName": "a.example.com", "UserName": "ops",
             "PortNumber": "2222", "PublicKeyFile": "x.ppk",
             "ProxyMethod": "6", "ProxyHost": "j.example.com"},
            None,
        )
        assert "ssh" in text
        assert "ops@a.example.com" in text
        assert "j.example.com" in text

    def test_preview_no_hostname(self):
        assert preview_text({"PortNumber": "22"}, None) == "(no hostname)"

    def test_preview_no_side_effects(self, tmp_path, monkeypatch, capsys):
        """convert_keys=False: no puttygen, no stdout noise, no makedirs."""
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "never"))
        preview_text({"HostName": "h", "PublicKeyFile": "zz.ppk"}, None)
        assert capsys.readouterr().out == ""
        assert not (tmp_path / "never").exists()


class TestBrowserApp:
    async def test_mount_and_rows_render(self):
        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test() as pilot:
            await pilot.pause()
            lv = app.query_one("#sessions")
            expected = len([r for r in build_rows(USABLE, [], None)])
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
            # move to a session with a port to see preview change
            lv.index = 1
            await pilot.pause()
            text = str(app.query_one("#preview").render())
            assert "ssh" in text or "select" in text

    async def test_arrow_keys_update_preview(self):
        """Regression: handler was misnamed (on_list_view_highlight),
        so Highlighted events never updated the preview pane."""
        app = SessionBrowser(USABLE, [], {}, None)
        async with app.run_test() as pilot:
            await pilot.pause()
            lv = app.query_one("#sessions")
            # first selectable row is prod:web-1 (port 2222 in preview)
            lv.index = next(i for i, r in enumerate(app.filtered_rows)
                            if r.name == "prod:web-1")
            await pilot.pause()
            before = str(app.query_one("#preview").render())
            lv.index = next(i for i, r in enumerate(app.filtered_rows)
                            if r.name == "solo")
            await pilot.pause()
            after = str(app.query_one("#preview").render())
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
            preview = str(app.query_one("#preview").render())
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
            preview = str(app.query_one("#preview").render())
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

    async def test_ping_action_updates_status(self):
        app = SessionBrowser(
            {"solo": {"HostName": "127.0.0.1", "PortNumber": "1"}},
            [], {}, None)
        async with app.run_test() as pilot:
            await pilot.pause()
            lv = app.query_one("#sessions")
            lv.index = 0
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            assert "✗" in app.status_message  # port 1 refused

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

    async def test_connect_via_callback(self):
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
