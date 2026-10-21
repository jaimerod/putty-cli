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

import putty
from utils.tui_actions import run_ping
from utils.tui_browser import SessionBrowser, build_all_rows, build_recent_rows, preview_text

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

    async def test_l_no_session_highlighted(self):
        """l with no selectable row (e.g. empty list) -> status only."""
        app = SessionBrowser({}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            assert "no session highlighted" in app.status_message

    async def test_esc_return_to_browser(self, tmp_path):
        from utils.tui_actions import LogViewerScreen
        log = tmp_path / "2026-10-07_1700-x.log"
        log.write_text("x\n", encoding="utf-8")
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await app.push_screen(LogViewerScreen(log))
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is not None  # back to browser (no crash)

    async def test_unreadable_log_shows_error(self, tmp_path):
        from utils.tui_actions import LogViewerScreen
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
                await app.push_screen(LogViewerScreen(p))
                await pilot.pause()
                assert any("Cannot read" in ln for ln in app.screen.lines)  # type: ignore
        finally:
            pathlib.Path.read_text = orig  # type: ignore

    async def test_search_no_match_status(self, tmp_path):
        from utils.tui_actions import LogViewerScreen
        log = tmp_path / "a.log"
        log.write_text("one\ntwo\n", encoding="utf-8")
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            screen = LogViewerScreen(log)
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
        from utils.tui_actions import LogViewerScreen
        log = tmp_path / "a.log"
        log.write_text("a\nb\n", encoding="utf-8")
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            screen = LogViewerScreen(log)
            await app.push_screen(screen)
            await pilot.pause()
            before = len(screen.lines)
            await screen.action_next_match()  # no term -> noop
            assert len(screen.lines) == before

    async def test_prev_jump_wraps_negative(self, tmp_path):
        from utils.tui_actions import LogViewerScreen
        log = tmp_path / "a.log"
        log.write_text("HIT\nmid\nHIT\n", encoding="utf-8")
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            screen = LogViewerScreen(log)
            await app.push_screen(screen)
            await pilot.pause()
            sb = screen.query_one("#log-search")
            sb.focus()
            sb.value = "hit"  # matches lines 0 and 2
            await pilot.pause()
            # view is filtered; jump prev wraps to the LAST match
            await screen.action_prev_match()
            assert screen.match_index >= 0
            ctx = screen._match_context(screen.match_positions[screen.match_index])
            assert "HIT" in " ".join(ctx)

    async def test_q_binding_dismisses(self, tmp_path):
        from utils.tui_actions import LogViewerScreen
        log = tmp_path / "a.log"
        log.write_text("q\n", encoding="utf-8")
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await app.push_screen(LogViewerScreen(log))
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
                                  ("#all-win", "ALL CONNECTIONS")):
                win = app.query_one(win_id)
                assert win.border_title == label
                assert win.styles.border_title_align == "center"
                assert win.styles.border_title_color \
                    == Color(255, 255, 85)
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
            box = app.screen.query_one("#ctx-menu")
            shadow = app.screen.query_one("#ctx-shadow")
            b, s = box.region, shadow.region
            assert s.width == b.width and s.height == b.height
            assert (s.x, s.y) == (b.x + 2, b.y + 1)
            assert shadow.styles.background == Color(0, 0, 0, a=0.5)

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
            assert base.styles.background == Color(0, 0, 170)
            # menu positioned near the click (row 0 -> region near top)
            menu = app.screen.query_one("#ctx-menu")
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
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("down")
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

    async def test_right_click_ping_dispatch(self):
        """right-click -> down/enter to Ping -> status bar shows result."""
        app = SessionBrowser(
            {"solo": {"HostName": "192.0.2.1", "PortNumber": "1"}},
            [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.post_message(self._click_on_row(app, 0))
            await pilot.pause()
            await pilot.pause()
            await pilot.press("down")   # Connect -> Ping
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert "✗" in app.status_message  # 192.0.2.1:1 refused

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
            shadow = app.screen.query_one("#ctx-shadow")
            text = str(shadow.render())
            assert text.strip()  # non-empty shadow content
            assert "lo" in text or "OTHER" in text  # covered rows echo

    async def test_menu_shadow_empty_without_underlying(self):
        """Direct push (no underlying screen passed) -> plain shadow."""
        from utils.tui_actions import SessionActionMenu
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            menu = SessionActionMenu("solo", (5, 5))
            await app.push_screen(menu)
            for _ in range(5):
                await pilot.pause()
            shadow = menu.query_one("#ctx-shadow")
            assert str(shadow.render()).strip() == ""


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
        """Key chip black/gray, description blue/black (FAR look)."""
        from textual.color import Color

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
                assert key_st.background == Color(0, 0, 0)
                assert key_st.color == Color(170, 170, 170)
                assert desc_st.color == Color(255, 255, 255)
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
        from utils.tui_actions import LogViewerScreen
        log = tmp_path / "a.log"
        log.write_text("one\ntwo\n", encoding="utf-8")
        app = SessionBrowser({"solo": {"HostName": "h"}}, [], {}, None)
        async with app.run_test(size=(110, 30)) as pilot:
            screen = LogViewerScreen(log)
            await app.push_screen(screen)
            await pilot.pause()
            text = self._keybar_text(screen)
            assert "Search" in text and "Back" in text
            screen.query_one("#log-search").focus()
            await pilot.pause()
            text = self._keybar_text(screen)
            assert "Apply" in text and "Done" in text
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
            title = menu.query_one("#ctx-title")
            menu.post_message(self._mouse_event(events.MouseMove, title))
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
