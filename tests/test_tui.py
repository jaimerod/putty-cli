"""Textual TUI tests via run_test() pilots (no real terminal needed)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

pytest.importorskip("textual")

from utils.tui_browser import SessionBrowser, build_rows, preview_text

USABLE = {
    "prod:web-1": {"HostName": "a.example.com", "UserName": "ops",
                   "PortNumber": "2222"},
    "prod:web-2": {"HostName": "b.example.com", "UserName": "ops"},
    "staging:web": {"HostName": "c.example.com"},
    "solo": {"HostName": "d.example.com"},
}


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
