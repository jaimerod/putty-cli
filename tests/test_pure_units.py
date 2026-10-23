"""Direct unit tests for the pure helpers extracted in the refactor.

These were previously reachable only through integration paths
(build_ssh_command, run_ping, menu generation); purity makes them
directly testable - no monkeypatching of subprocess/time required.
"""
import collections

import shutil
from pathlib import Path

from utils.display_util import separator, separator_term, terminal_width
from utils.net_util import interpret_ping_output
from utils.recent_sessions_util import (
    format_relative_time,
    recent_info_suffix,
)
from utils.session_util import (
    SESSION_DEFAULTS,
    SESSION_FIELDS,
    jump_host_of,
    session_value,
    usable_sessions,
)
from utils.ssh_command_util import (
    _plan_mosh, _plan_ssh, browsable_key_dir, reg_friendly_path,
)

# ── net_util.interpret_ping_output ───────────────────────────────────────────


class TestInterpretPingOutput:
    def test_success_with_latency(self):
        ok, needs_tcp, msg = interpret_ping_output(
            "h.example.com", 0, "1 received, 0% loss\ntime=23.4 ms", "")
        assert (ok, needs_tcp) == (True, False)
        assert msg == "h.example.com: 23.4 ms"

    def test_success_busybox_time_angle_form(self):
        # busybox ping prints 'time<10ms' instead of 'time=10.0 ms'
        ok, _, msg = interpret_ping_output("h", 0, "time<1ms", "")
        assert ok and msg == "h: 1 ms"

    def test_success_without_latency_line(self):
        ok, _, msg = interpret_ping_output("h", 0, "", "")
        assert (ok, msg) == (True, "h: reachable")

    def test_operation_not_permitted_needs_tcp_fallback(self):
        assert interpret_ping_output(
            "h", 2, "", "ping: Operation not permitted") \
            == (False, True, "")

    def test_unknown_host_variants(self):
        for err in ("ping: unknown host",
                    "ping: Name or service not known",
                    "no address associated with hostname"):
            assert interpret_ping_output("h", 68, "", err) \
                == (False, False, "h: unknown host")

    def test_unreachable(self):
        assert interpret_ping_output(
            "h", 1, "", "From x: Destination Host Unreachable") \
            == (False, False, "h: unreachable")

    def test_packet_loss(self):
        assert interpret_ping_output(
            "h", 1, "1 packets transmitted, 0 received, 100% packet loss",
            "") == (False, False, "h: unreachable (no reply)")

    def test_other_exit_code(self):
        assert interpret_ping_output("h", 7, "weird output", "") \
            == (False, False, "h: unreachable (ping exit 7)")


# ── ssh_command_util._plan_ssh / _plan_mosh ──────────────────────────────────


class TestPlanSsh:
    def test_bare_host(self):
        parts, ppk, notes = _plan_ssh({"HostName": "h.example.com"})
        assert parts == ["ssh", "h.example.com"]
        assert ppk is None and notes == []

    def test_user_and_port(self):
        parts, ppk, _ = _plan_ssh(
            {"HostName": "h", "UserName": "u", "PortNumber": "2222"})
        assert parts == ["ssh", "-p", "2222", "u@h"]
        assert ppk is None

    def test_jump_host_with_user_and_port(self):
        parts, _, _ = _plan_ssh({
            "HostName": "h", "ProxyMethod": "6", "ProxyHost": "jump",
            "ProxyPort": "2222", "ProxyUsername": "ju"})
        assert parts == ["ssh", "-J", "ju@jump:2222", "h"]

    def test_jump_host_default_port_omitted(self):
        parts, _, _ = _plan_ssh(
            {"HostName": "h", "ProxyMethod": "6", "ProxyHost": "jump"})
        assert parts == ["ssh", "-J", "jump", "h"]

    def test_socks_proxy_becomes_warning_note(self):
        parts, _, notes = _plan_ssh(
            {"HostName": "h", "ProxyMethod": "2", "ProxyHost": "socksp"})
        assert parts == ["ssh", "h"]  # no -J for SOCKS/HTTP proxies
        assert len(notes) == 1 and "SOCKS/HTTP" in notes[0]

    def test_key_placeholder_position(self):
        parts, ppk, notes = _plan_ssh(
            {"HostName": "h", "PublicKeyFile": "/keys/k.ppk"})
        assert parts == ["ssh", "h", "-i", ""]
        assert ppk == "/keys/k.ppk" and notes == []

    def test_key_note_when_not_converting(self):
        _, _, notes = _plan_ssh(
            {"HostName": "h", "PublicKeyFile": "/k"}, convert_keys=False)
        assert notes == ["  -> Using key: (converted on connect)"]

    def test_remote_command_inserts_tty_flag(self):
        parts, _, _ = _plan_ssh(
            {"HostName": "h", "RemoteCommand": "tmux ls"})
        assert parts == ["ssh", "-t", "h", "tmux ls"]

    def test_key_sits_before_remote_command(self):
        parts, _, _ = _plan_ssh({
            "HostName": "h", "PublicKeyFile": "/k",
            "RemoteCommand": "top"})
        assert parts == ["ssh", "-t", "h", "-i", "", "top"]

    def test_command_line_args_shlex_split(self):
        parts, _, _ = _plan_ssh({
            "HostName": "h",
            "CommandLineArgs": "-o StrictHostKeyChecking=no"})
        assert parts == ["ssh", "h", "-o", "StrictHostKeyChecking=no"]

    def test_malformed_command_line_args_is_a_note(self):
        parts, _, notes = _plan_ssh(
            {"HostName": "h", "CommandLineArgs": 'x "unterminated'})
        assert parts == ["ssh", "h"]
        assert len(notes) == 1 and "CommandLineArgs" in notes[0]

    def test_pure_repeated_calls_agree(self):
        session = {"HostName": "h", "UserName": "u",
                   "PublicKeyFile": "/k", "RemoteCommand": "top"}
        assert _plan_ssh(session) == _plan_ssh(session)


class TestPlanMosh:
    def test_bare_host(self):
        parts, ssh_opts, ppk, notes = _plan_mosh({"HostName": "h"})
        assert parts == ["mosh", "h"]
        assert ssh_opts == [] and ppk is None and notes == []

    def test_user_target(self):
        parts, _, _, _ = _plan_mosh({"HostName": "h", "UserName": "u"})
        assert parts == ["mosh", "u@h"]

    def test_nonstandard_port_goes_to_ssh_opts(self):
        _, ssh_opts, _, _ = _plan_mosh(
            {"HostName": "h", "PortNumber": "2222"})
        assert ssh_opts == ["-p", "2222"]

    def test_key_placeholder_in_ssh_opts(self):
        _, ssh_opts, ppk, _ = _plan_mosh(
            {"HostName": "h", "PublicKeyFile": "/k"})
        assert ssh_opts == ["-i", ""]
        assert ppk == "/k"

    def test_key_note_when_not_converting(self):
        _, _, _, notes = _plan_mosh(
            {"HostName": "h", "PublicKeyFile": "/k"}, convert_keys=False)
        assert notes == ["  → Using key: (cached on connect)"]


# ── display_util.separator family ────────────────────────────────────────────


class TestSeparatorUnit:
    def test_exact_width(self):
        assert len(separator("TAG", width=20)) == 20
        assert separator("TAG", width=20).startswith("── TAG")

    def test_text_wider_than_width_keeps_prefix(self):
        # '── X' is 4 columns: no negative dashes, prefix survives
        assert separator("X", width=3) == "── X"

    def test_empty_text_is_all_dashes(self):
        assert separator("", width=10) == "─" * 10

    def test_terminal_width_clamps_to_80(self, monkeypatch):
        Size = collections.namedtuple("Size", "columns lines")
        monkeypatch.setattr(shutil, "get_terminal_size",
                            lambda fallback: Size(40, 24))
        assert terminal_width() == 80

    def test_terminal_width_follows_wide_terminals(self, monkeypatch):
        Size = collections.namedtuple("Size", "columns lines")
        monkeypatch.setattr(shutil, "get_terminal_size",
                            lambda fallback: Size(120, 40))
        assert terminal_width() == 120
        assert separator_term("TAG") == separator("TAG", width=120)


# ── recent_sessions_util.recent_info_suffix / format_relative_time ───────────


class TestRecentInfoSuffix:
    def test_timestamp_only(self):
        assert recent_info_suffix({"ts": 1000, "exit": 0}, now=1300) \
            == "5m ago"

    def test_failed_exit_appends_cross(self):
        assert recent_info_suffix({"ts": 1000, "exit": 255}, now=1300) \
            == "5m ago ✗"

    def test_exit_code_without_timestamp(self):
        assert recent_info_suffix({"ts": None, "exit": 1}, now=1300) == "✗"

    def test_success_exit_no_cross(self):
        assert "✗" not in recent_info_suffix(
            {"ts": 1000, "exit": 0}, now=1300)

    def test_no_metadata_is_empty(self):
        assert recent_info_suffix({"ts": None, "exit": None}, now=1300) == ""


class TestFormatRelativeTimeNow:
    def test_now_injection(self):
        assert format_relative_time(1000, now=2000) == "16m ago"

    def test_just_now(self):
        assert format_relative_time(2000, now=2000) == "just now"

    def test_seconds(self):
        assert format_relative_time(1995, now=2000) == "5s ago"

    def test_negative_delta_clamps(self):
        # clock skew / future timestamp never renders negative ages
        assert format_relative_time(5000, now=2000) == "just now"

    def test_hours_days(self):
        assert format_relative_time(0, now=3 * 3600) == "3h ago"
        assert format_relative_time(0, now=2 * 86400) == "2d ago"

    def test_none_is_empty(self):
        assert format_relative_time(None, now=12345) == ""


# ── session_util ─────────────────────────────────────────────────────────────


class TestSessionValue:
    def test_missing_key(self):
        assert session_value({}, "HostName") == ""
        assert session_value({}, "PortNumber", "22") == "22"

    def test_strips_whitespace(self):
        assert session_value({"HostName": "  h  "}, "HostName") == "h"

    def test_empty_falls_back_to_default(self):
        assert session_value({"PortNumber": "  "}, "PortNumber", "22") == "22"


class TestJumpHostOf:
    def test_method_6_with_host(self):
        assert jump_host_of(
            {"ProxyMethod": "6", "ProxyHost": "jump"}) == "jump"

    def test_method_6_whitespace_host(self):
        assert jump_host_of({"ProxyMethod": "6", "ProxyHost": " j "}) == "j"

    def test_method_6_without_host(self):
        assert jump_host_of({"ProxyMethod": "6"}) == ""

    def test_other_methods_ignored(self):
        assert jump_host_of({"ProxyMethod": "3", "ProxyHost": "socks"}) == ""

    def test_missing_method_defaults_to_zero(self):
        assert jump_host_of({"ProxyHost": "jump"}) == ""


class TestUsableSessions:
    def test_requires_hostname(self):
        sessions = {
            "ok": {"HostName": "h"},
            "missing": {"UserName": "u"},
            "blank": {"HostName": "  "},
        }
        assert list(usable_sessions(sessions)) == ["ok"]

    def test_archived_hidden_by_default(self):
        sessions = {
            "xxx-old": {"HostName": "h"},
            "XXX-upper": {"HostName": "h"},
            "live": {"HostName": "h"},
        }
        assert list(usable_sessions(sessions)) == ["live"]

    def test_archived_included_on_flag(self):
        sessions = {
            "xxx-old": {"HostName": "h"},
            "live": {"HostName": "h"},
        }
        assert list(usable_sessions(sessions, include_archived=True)) \
            == ["xxx-old", "live"]


class TestSessionFieldDefs:
    def test_every_field_has_a_default(self):
        assert {key for _, key in SESSION_FIELDS} == set(SESSION_DEFAULTS)

# ── net_util.run_jump_probe ──────────────────────────────────────────────────


class TestRunJumpProbe:
    def test_success(self, monkeypatch):
        import utils.net_util as mod
        monkeypatch.setattr(
            mod.subprocess, "run",
            lambda *a, **k: __import__("subprocess").CompletedProcess(
                [], 0, stdout="", stderr=""))
        ok, msg = mod.run_jump_probe(
            ["ssh", "-J", "jump.example.com", "h"], "jump.example.com")
        assert ok and msg == "reachable via jump.example.com"

    def test_nonzero_exit_quotes_last_stderr_line(self, monkeypatch):
        import subprocess as sp
        import utils.net_util as mod
        monkeypatch.setattr(
            mod.subprocess, "run",
            lambda *a, **k: sp.CompletedProcess(
                [], 255, stdout="",
                stderr="kex_exchange_identification: banner\n"
                       "ssh: connect to host h port 22: timed out"))
        ok, msg = mod.run_jump_probe(["ssh", "h"], "j")
        assert not ok
        assert msg == "unreachable via j (ssh: connect to host h port 22: timed out)"

    def test_nonzero_exit_empty_output_shows_exit_code(self, monkeypatch):
        import subprocess as sp
        import utils.net_util as mod
        monkeypatch.setattr(
            mod.subprocess, "run",
            lambda *a, **k: sp.CompletedProcess([], 3, stdout="", stderr=""))
        ok, msg = mod.run_jump_probe(["ssh", "h"], "j")
        assert not ok and msg == "unreachable via j (ssh exit 3)"

    def test_ssh_binary_missing(self, monkeypatch):
        import utils.net_util as mod
        monkeypatch.setattr(
            mod.subprocess, "run",
            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("ssh")))
        ok, msg = mod.run_jump_probe(["ssh", "h"], "j")
        assert not ok and "ssh not found" in msg

    def test_timeout(self, monkeypatch):
        import subprocess as sp
        import utils.net_util as mod
        monkeypatch.setattr(
            mod.subprocess, "run",
            lambda *a, **k: (_ for _ in ()).throw(
                sp.TimeoutExpired("ssh", 10)))
        ok, msg = mod.run_jump_probe(["ssh", "h"], "j")
        assert not ok and "unreachable via" in msg

    def test_empty_argv(self):
        import utils.net_util as mod
        assert mod.run_jump_probe([], "j") == (False, "no command to probe")
        assert mod.run_jump_probe(None, "j") == (False, "no command to probe")

    def test_ping_dead_tuple_removed(self):
        import utils.net_util as mod
        assert "_PING_DEAD" not in vars(mod)


class TestKeyPathHelpers:
    """reg_friendly_path / browsable_key_dir: picked key files are
    stored so PuTTY (Windows) and putty-cli both resolve them."""

    def test_next_to_reg_is_bare_filename(self, tmp_path):
        assert reg_friendly_path(tmp_path / "k.ppk", tmp_path) == "k.ppk"

    def test_subdir_is_absolute(self, tmp_path):
        assert reg_friendly_path(tmp_path / "sub" / "k.ppk", tmp_path) \
            == str(tmp_path / "sub" / "k.ppk")

    def test_wsl_drive_letter_becomes_windows_path(self, tmp_path,
                                                   monkeypatch):
        from utils import ssh_command_util as scu
        monkeypatch.setattr(scu, "WSL_MNT_ROOT", tmp_path / "mnt")
        picked = tmp_path / "mnt" / "e" / "Users" / "me" / "k.ppk"
        assert reg_friendly_path(picked, tmp_path / "other") \
            == "E:\\Users\\me\\k.ppk"

    def test_browse_from_windows_path(self):
        assert browsable_key_dir(r"C:\Users\me\keys\k.ppk", None) \
            == Path("/mnt/c/Users/me/keys")

    def test_browse_from_absolute_path(self):
        assert browsable_key_dir("/home/u/.ssh/k", None) \
            == Path("/home/u/.ssh")

    def test_browse_bare_name_starts_at_reg_dir(self, tmp_path):
        assert browsable_key_dir("k.ppk", tmp_path) == tmp_path

    def test_browse_relative_subpath(self, tmp_path):
        assert browsable_key_dir("keys/k.ppk", tmp_path) == tmp_path / "keys"

    def test_browse_empty_starts_at_reg_dir(self, tmp_path):
        assert browsable_key_dir("", tmp_path) == tmp_path
