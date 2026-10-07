import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from utils.display_util import generate_menu_items, make_display_pair
from utils.putty_parse_util import parse_putty_reg
from utils.recent_sessions_util import load_recent_sessions, update_recent_sessions
from utils.ssh_command_util import build_ssh_command
from utils.titlecase_util import smart_title, unquote_display

REG_PATH = Path(__file__).parent / "data" / "sample.reg"
PPK_FIXTURE = Path(__file__).parent / "data" / "test_key.ppk"

REG_HEADER = '[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\'


def write_reg(tmp_path: Path, content: str, encoding: str = "utf-8-sig") -> Path:
    p = tmp_path / "sessions.reg"
    p.write_text(content, encoding=encoding)
    return p


def mini_session(name: str, host: str, **overrides) -> str:
    lines = [f"{REG_HEADER}{name}]", f'"HostName"="{host}"']
    for k, v in overrides.items():
        lines.append(f'"{k}"="{v}"')
    return "\n".join(lines) + "\n"


# ── Parser: happy paths ──────────────────────────────────────────────────────

class TestParsePuttyReg:
    def test_parses_utf16le(self, tmp_path):
        p = write_reg(tmp_path, mini_session("dev", "dev.example.com"), "utf-16-le")
        sessions = parse_putty_reg(p)
        assert sessions["dev"]["HostName"] == "dev.example.com"

    def test_parses_utf8_with_bom(self, tmp_path):
        p = write_reg(tmp_path, mini_session("dev", "dev.example.com"), "utf-8-sig")
        sessions = parse_putty_reg(p)
        assert sessions["dev"]["HostName"] == "dev.example.com"

    def test_parses_plain_utf8(self, tmp_path):
        p = write_reg(tmp_path, mini_session("dev", "dev.example.com"), "utf-8")
        sessions = parse_putty_reg(p)
        assert sessions["dev"]["HostName"] == "dev.example.com"

    def test_sample_file_sessions(self):
        sessions = parse_putty_reg(REG_PATH)
        assert set(sessions) == {
            "prod-web-1", "prod-db", "xxx-archived", "broken",
            "Sessions%20with%20space", "jumphost-session", "socks-session",
            "with-key", "with-remote-cmd",
        }

    def test_dword_port_decoded(self):
        sessions = parse_putty_reg(REG_PATH)
        assert sessions["prod-web-1"]["PortNumber"] == "22"  # 0x16

    def test_backslash_unescape_in_strings(self):
        sessions = parse_putty_reg(REG_PATH)
        assert "C:\\Users\\hiiam\\keys\\id_rsa.ppk" == \
            sessions["with-key"]["PublicKeyFile"]

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(Exception):
            parse_putty_reg(tmp_path / "nope.reg")

    def test_garbage_file_no_sessions(self, tmp_path):
        p = tmp_path / "garbage.reg"
        p.write_text("this is not a registry file\n", encoding="utf-8")
        assert parse_putty_reg(p) == {}

    def test_value_after_new_session_header(self, tmp_path):
        content = f"""{REG_HEADER}a]
"HostName"="a.example.com"

{REG_HEADER}b]
"HostName"="b.example.com"
"""
        sessions = parse_putty_reg(write_reg(tmp_path, content, "utf-8"))
        assert sessions["a"]["HostName"] == "a.example.com"
        assert sessions["b"]["HostName"] == "b.example.com"


# ── Display ──────────────────────────────────────────────────────────────────

class TestDisplay:
    def test_display_pair_basic(self):
        sort_key, display, name = make_display_pair("prod-web-1", {
            "HostName": "web1.example.com", "PortNumber": "2222", "UserName": "deploy",
        })
        assert name == "prod-web-1"
        assert display.lower() == sort_key
        assert "Prod-Web-1" in display
        assert "deploy@web1.example.com:2222" in display

    def test_default_port_omitted(self):
        _, display, _ = make_display_pair("d", {"HostName": "h.example.com"})
        assert ":22" not in display

    def test_hints(self):
        _, display, _ = make_display_pair("k", {
            "HostName": "h", "PublicKeyFile": "x.ppk",
            "ProxyMethod": "6", "ProxyHost": "j",
            "RemoteCommand": "htop",
        })
        assert "🔑" in display and "🔀" in display and "⚡" in display


class TestMenuItems:
    def test_recents_first_then_separator_then_sorted(self):
        sessions = {
            "b": {"HostName": "b"}, "a": {"HostName": "a"},
            "r": {"HostName": "r"},
        }
        items = list(generate_menu_items(sessions, ["r"]))
        displays = [d for d, _ in items]
        assert any("RECENT" in d for d in displays)
        names = [n for _, n in items if n is not None]
        assert names == ["r", "a", "b"]
        # separator right before the sorted block
        sep_idx = next(i for i, (d, n) in enumerate(items) if n is None
                       and "RECENT" not in d)
        assert items[sep_idx + 1][1] == "a"

    def test_stale_recents_skipped(self):
        sessions = {"a": {"HostName": "a"}}
        items = list(generate_menu_items(sessions, ["ghost", "a"]))
        names = [n for _, n in items if n is not None]
        assert names == ["a"]

    def test_no_recents_no_header(self):
        items = list(generate_menu_items({"a": {"HostName": "a"}}, []))
        assert all(n is not None for _, n in items)

    def test_separator_not_when_all_recent(self):
        sessions = {"a": {"HostName": "a"}}
        items = list(generate_menu_items(sessions, ["a"]))
        displays = [d for d, _ in items]
        plain_sep = [d for d in displays if d.startswith("──") and "RECENT" not in d]
        assert not plain_sep


# ── Recents ──────────────────────────────────────────────────────────────────

class TestRecents:
    def test_load_filters_to_usable(self, tmp_path):
        f = tmp_path / "recent.txt"
        f.write_text("ghost\nreal\n", encoding="utf-8")
        assert load_recent_sessions(f, {"real": {}}) == ["real"]

    def test_load_dedupes(self, tmp_path):
        f = tmp_path / "recent.txt"
        f.write_text("a\na\nb\na\n", encoding="utf-8")
        assert load_recent_sessions(f, {"a": {}, "b": {}}) == ["a", "b"]

    def test_load_max(self, tmp_path):
        f = tmp_path / "recent.txt"
        f.write_text("\n".join(str(i) for i in range(10)), encoding="utf-8")
        assert len(load_recent_sessions(f, {str(i): {} for i in range(10)})) == 5

    def test_update_prepends_and_keeps_20(self, tmp_path):
        f = tmp_path / "recent.txt"
        f.write_text("\n".join(f"s{i}" for i in range(25)), encoding="utf-8")
        update_recent_sessions(f, "new")
        lines = f.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "new"
        assert len(lines) == 20
        assert "s25" not in lines

    def test_update_dedupes_selected(self, tmp_path):
        f = tmp_path / "recent.txt"
        f.write_text("a\nb\nc\n", encoding="utf-8")
        update_recent_sessions(f, "b")
        lines = f.read_text(encoding="utf-8").splitlines()
        assert lines == ["b", "a", "c"]


# ── Titlecase ────────────────────────────────────────────────────────────────

class TestTitlecase:
    def test_smart_title_boundaries(self):
        assert smart_title("dev-prod_web.host 2b") == "Dev-Prod_Web.Host 2b"

    def test_unquote_display(self):
        assert unquote_display("Foo%20Bar") == "Foo Bar"
        assert unquote_display("a%2Fb%5Cc%3Ad") == "a/b\\c:d"

    def test_unquote_no_passthrough(self):
        assert unquote_display("Plain-Name_1.0") == "Plain-Name_1.0"


# ── SSH command building ─────────────────────────────────────────────────────

class TestBuildSshCommand:
    def test_missing_hostname(self):
        assert build_ssh_command("x", {"PortNumber": "22"}) == (None, None)

    @pytest.fixture(autouse=True)
    def _no_tmux(self, monkeypatch):
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.setenv("PUTTY_LOG_DIR", "/tmp/opencode/putty-test-logs")

    def test_basic_host(self):
        cmd, temp_key = build_ssh_command("s", {"HostName": "h.example.com"})
        assert temp_key is None
        assert "ssh" in cmd and "h.example.com" in cmd

    def test_user_and_port(self):
        cmd, _ = build_ssh_command("s", {
            "HostName": "h.example.com", "UserName": "u", "PortNumber": "2222",
        })
        assert "u@h.example.com" in cmd
        assert "-p 2222" in cmd

    def test_jump_proxy(self):
        cmd, _ = build_ssh_command("s", {
            "HostName": "t.internal", "ProxyMethod": "6",
            "ProxyHost": "jump.example.com", "ProxyPort": "2222",
            "ProxyUsername": "ju",
        })
        assert "-J ju@jump.example.com:2222" in cmd

    def test_remote_command_present(self):
        cmd, _ = build_ssh_command("s", {
            "HostName": "c.example.com", "RemoteCommand": "htop",
        })
        assert "htop" in cmd

    def test_no_hardcoded_user_paths(self):
        cmd, _ = build_ssh_command("s", {"HostName": "h"})
        assert "hiiam" not in cmd
        assert "/mnt/c/Users" not in cmd

    def test_log_dir_from_env(self, monkeypatch, tmp_path):
        logdir = tmp_path / "mylogs"
        monkeypatch.setenv("PUTTY_LOG_DIR", str(logdir))
        cmd, _ = build_ssh_command("s", {"HostName": "h.example.com"})
        assert str(logdir) in cmd

    def test_log_dir_defaults_to_reg_parent(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PUTTY_LOG_DIR", raising=False)
        reg = tmp_path / "settings" / "putty.reg"
        reg.parent.mkdir(parents=True)
        reg.touch()
        cmd, _ = build_ssh_command("s", {"HostName": "h"}, reg_path=reg)
        assert f"{tmp_path}/settings" in cmd

    def test_ppk_resolved_relative_to_reg_path(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("PUTTY_LOG_DIR", "/tmp/opencode/putty-test-logs")
        reg = tmp_path / "putty.reg"
        reg.touch()
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        shutil.copy(PPK_FIXTURE, keys_dir / "id.ppk")
        cmd, temp_key = build_ssh_command(
            "s",
            {"HostName": "h", "PublicKeyFile": "keys/id.ppk"},
            reg_path=reg,
        )
        out = capsys.readouterr().out
        assert str(keys_dir / "id.ppk") in out
        assert temp_key is not None
        Path(temp_key).unlink(missing_ok=True)
        assert "-i" in cmd