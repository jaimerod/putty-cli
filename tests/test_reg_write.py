"""Tests for the surgical .reg write-back (utils/putty_reg_write_util).

These tests exist because the .reg file is the data source for ALL
connections: any write bug means corruption. The writer must never
regenerate bytes it was not asked to change.
"""
import difflib
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.putty_parse_util import parse_putty_reg
from utils.putty_reg_write_util import (
    MAX_BACKUPS,
    RegEditError,
    delete_session,
    edit_session_values,
    reg_file_stamp,
)

SAMPLE = (
    "Windows Registry Editor Version 5.00\r\n"
    "\r\n"
    "[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\SshHostKeys]\r\n"
    '"ssh-rsa@22:10.0.0.1"="0x23,aa,bb"\r\n'
    "\r\n"
    "[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\prod-web-1]\r\n"
    '"Present"=hex:01\r\n'
    '"HostName"="web1.example.com"\r\n'
    '"PortNumber"=dword:00000016\r\n'
    '"UserName"="deploy"\r\n'
    "\r\n"
    "[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\prod-db]\r\n"
    '"HostName"="db.example.com"\r\n'
    '"PortNumber"=dword:00000016\r\n'
    '"UserName"="postgres"\r\n'
)


def make_reg(tmp_path: Path, name: str = "putty.reg", text: str = SAMPLE,
             encoding: str = "utf-16-le", bom: bytes = b"\xff\xfe") -> Path:
    reg = tmp_path / name
    data = (bom or b"") + text.encode(encoding)
    reg.write_bytes(data)
    return reg


def reg_line_diff(before_text: str, after_text: str) -> list:
    def clean(t: str) -> list:
        return t.replace("\ufeff", "").replace("\r\n", "\n").splitlines()
    diff = list(difflib.unified_diff(
        clean(before_text), clean(after_text), lineterm="", n=0))
    return [d for d in diff[2:] if d[:1] in "+-"]


class TestSurgicalEdit:
    def test_only_targeted_lines_change(self, tmp_path):
        reg = make_reg(tmp_path)
        edit_session_values(
            reg, "prod-web-1",
            {"HostName": "web1-new.example.com", "PortNumber": "2222"},
            expect_stamp=reg_file_stamp(reg))

        after = reg.read_bytes().decode("utf-16")
        diff = reg_line_diff(SAMPLE, after)
        assert diff == [
            '-"HostName"="web1.example.com"',
            '-"PortNumber"=dword:00000016',
            '+"HostName"="web1-new.example.com"',
            '+"PortNumber"=dword:000008ae',
        ]

    def test_other_sections_untouched(self, tmp_path):
        reg = make_reg(tmp_path)
        before_text = reg.read_bytes().decode("utf-16")
        edit_session_values(
            reg, "prod-db", {"RemoteCommand": "bash"},
            expect_stamp=reg_file_stamp(reg))
        after_text = reg.read_bytes().decode("utf-16")

        # SshHostKeys survives verbatim
        assert '"ssh-rsa@22:10.0.0.1"="0x23,aa,bb"' in after_text
        assert '"Present"=hex:01' in after_text

        # exactly one inserted line, everything else byte-identical
        assert reg_line_diff(before_text, after_text) \
            == ['+"RemoteCommand"="bash"']

    def test_encoding_bom_and_crlf_preserved(self, tmp_path):
        reg = make_reg(tmp_path)
        edit_session_values(
            reg, "prod-web-1", {"HostName": "h"},
            expect_stamp=reg_file_stamp(reg))
        raw = reg.read_bytes()
        assert raw.startswith(b"\xff\xfe")          # UTF-16LE BOM kept
        # every original CRLF, encoded as UTF-16LE pairs, no bare LF
        assert raw.count(b"\x0d\x00\x0a\x00") == SAMPLE.count("\r\n")
        sessions = parse_putty_reg(reg)
        assert sessions["prod-web-1"]["HostName"] == "h"
        assert sessions["prod-db"]["UserName"] == "postgres"

    def test_inserts_missing_keys_in_section(self, tmp_path):
        reg = make_reg(tmp_path)
        edit_session_values(
            reg, "prod-db",
            {"RemoteCommand": "tmux a", "ProxyHost": "j.example.com"},
            expect_stamp=reg_file_stamp(reg))
        after = reg.read_bytes().decode("utf-16")
        # the prod-db section ends at the next [ bracket: the inserted
        # lines must live inside that slice
        section = after.split("Sessions\\prod-db]")[1].split("\n[")[0]
        assert '"RemoteCommand"="tmux a"' in section
        assert '"ProxyHost"="j.example.com"' in section

    def test_port_written_as_dword(self, tmp_path):
        reg = make_reg(tmp_path)
        edit_session_values(
            reg, "prod-web-1", {"PortNumber": "2222"},
            expect_stamp=reg_file_stamp(reg))
        assert '"PortNumber"=dword:000008ae' in reg.read_bytes()\
            .decode("utf-16")
        assert parse_putty_reg(reg)["prod-web-1"]["PortNumber"] == "2222"

    def test_quoted_port_line_stays_quoted(self, tmp_path):
        reg = make_reg(tmp_path, text=SAMPLE.replace(
            '"PortNumber"=dword:00000016', '"PortNumber"="22"'))
        edit_session_values(
            reg, "prod-web-1", {"PortNumber": "2222"},
            expect_stamp=reg_file_stamp(reg))
        assert '"PortNumber"="2222"' in reg.read_bytes().decode("utf-16")

    def test_backslash_paths_survive(self, tmp_path):
        reg = make_reg(tmp_path)
        ppk = r"C:\Users\me\Documents\keys\my.ppk"
        edit_session_values(
            reg, "prod-web-1", {"PublicKeyFile": ppk},
            expect_stamp=reg_file_stamp(reg))
        assert parse_putty_reg(reg)["prod-web-1"]["PublicKeyFile"] == ppk


class TestGuards:
    def test_stamp_mismatch_refuses_and_keeps_file(self, tmp_path):
        reg = make_reg(tmp_path)
        stamp = reg_file_stamp(reg)
        # someone re-exported on the Windows side after the dialog opened
        reg.write_bytes(b"\xff\xfe" + SAMPLE
                        .replace("deploy", "deployer")
                        .encode("utf-16-le"))
        expected = reg.read_bytes()
        with pytest.raises(RegEditError, match="changed on disk"):
            edit_session_values(
                reg, "prod-web-1", {"HostName": "h"}, expect_stamp=stamp)
        # the re-export survived untouched - no silent clobber
        assert reg.read_bytes() == expected

    def test_quote_value_refused(self, tmp_path):
        reg = make_reg(tmp_path)
        before = reg.read_bytes()
        with pytest.raises(RegEditError, match="quote"):
            edit_session_values(
                reg, "prod-web-1", {"RemoteCommand": 'say "hi"'},
                expect_stamp=reg_file_stamp(reg))
        assert reg.read_bytes() == before

    def test_unknown_session_refused(self, tmp_path):
        reg = make_reg(tmp_path)
        before = reg.read_bytes()
        with pytest.raises(RegEditError, match="not in the file"):
            edit_session_values(
                reg, "ghost-box", {"HostName": "h"},
                expect_stamp=reg_file_stamp(reg))
        assert reg.read_bytes() == before

    def test_non_editable_key_refused(self, tmp_path):
        reg = make_reg(tmp_path)
        before = reg.read_bytes()
        with pytest.raises(RegEditError, match="not editable"):
            edit_session_values(
                reg, "prod-web-1", {"Present": "0"},
                expect_stamp=reg_file_stamp(reg))
        assert reg.read_bytes() == before

    def test_hex_encoded_key_refused(self, tmp_path):
        text = SAMPLE.replace(
            '"HostName"="web1.example.com"', '"HostName"=hex(1):61,00,62')
        reg = make_reg(tmp_path, text=text)
        before = reg.read_bytes()
        with pytest.raises(RegEditError):
            edit_session_values(
                reg, "prod-web-1", {"HostName": "h"},
                expect_stamp=reg_file_stamp(reg))
        assert reg.read_bytes() == before

    def test_undecodable_refused(self, tmp_path):
        reg = tmp_path / "putty.reg"
        reg.write_bytes(b"\x01\x02\xff\x00 garbage \xff\xfe")
        with pytest.raises(RegEditError):
            edit_session_values(
                reg, "prod-web-1", {"HostName": "h"},
                expect_stamp=reg_file_stamp(reg))

    def test_port_out_of_range_refused(self, tmp_path):
        reg = make_reg(tmp_path)
        before = reg.read_bytes()
        with pytest.raises(RegEditError, match="1-65535"):
            edit_session_values(
                reg, "prod-web-1", {"PortNumber": "99999"},
                expect_stamp=reg_file_stamp(reg))
        assert reg.read_bytes() == before

    def test_no_changes_refused_upfront(self, tmp_path):
        reg = make_reg(tmp_path)
        before = reg.read_bytes()
        with pytest.raises(RegEditError, match="nothing to change"):
            edit_session_values(
                reg, "prod-web-1", {}, expect_stamp=reg_file_stamp(reg))
        assert reg.read_bytes() == before


class TestPostWriteVerification:
    def test_bad_replace_is_caught(self, tmp_path, monkeypatch):
        """The post-write re-parse check: if os.replace somehow put down
        wrong bytes, the writer raises loudly instead of staying quiet."""
        import utils.putty_reg_write_util as writer

        def bad_replace(src, dst):
            Path(dst).write_bytes(b"\xff\xfe garbage")

        monkeypatch.setattr(writer.os, "replace", bad_replace)

        reg = make_reg(tmp_path)
        with pytest.raises(RegEditError, match="did not verify"):
            writer.edit_session_values(
                reg, "prod-web-1", {"HostName": "h"},
                expect_stamp=reg_file_stamp(reg))


class TestBackups:
    def test_backup_created_and_parses_to_original(self, tmp_path):
        reg = make_reg(tmp_path)
        stamp = reg_file_stamp(reg)
        result = edit_session_values(
            reg, "prod-web-1", {"HostName": "h"}, expect_stamp=stamp)
        bak = result.backup_path
        assert bak is not None and reg_file_stamp(bak) == stamp
        assert parse_putty_reg(bak)["prod-web-1"]["HostName"] == \
            "web1.example.com"

    def test_trim_keeps_newest_five(self, tmp_path):
        reg = make_reg(tmp_path, name="keep.reg")
        stamp = reg_file_stamp(reg)
        for i in range(MAX_BACKUPS + 2):
            (tmp_path / f"keep.reg.bak-2026010{i}-000000").write_bytes(
                b"\xff\xfe")
        edit_session_values(
            reg, "prod-web-1", {"HostName": "h"}, expect_stamp=stamp)
        backups = sorted(tmp_path.glob("keep.reg.bak-*"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        assert len(backups) == MAX_BACKUPS
        # the fresh backup (timestamped today) is the newest of the kept
        assert re.fullmatch(r".*bak-\d{8}-\d{6}$", backups[0].name)


class TestEncodings:
    def test_bom_less_utf16le_edit(self, tmp_path):
        reg = make_reg(tmp_path, encoding="utf-16-le", bom=b"")
        edit_session_values(
            reg, "prod-web-1", {"HostName": "h"},
            expect_stamp=reg_file_stamp(reg))
        raw = reg.read_bytes()
        assert not raw.startswith(b"\xff\xfe")   # still BOM-less
        assert parse_putty_reg(reg)["prod-web-1"]["HostName"] == "h"

    def test_utf8_bom_reg_edit(self, tmp_path):
        reg = make_reg(tmp_path, encoding="utf-8", bom=b"\xef\xbb\xbf")
        edit_session_values(
            reg, "prod-web-1", {"HostName": "user-ser"},
            expect_stamp=reg_file_stamp(reg))
        raw = reg.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")   # UTF-8 BOM kept
        sessions = parse_putty_reg(reg)
        assert sessions["prod-web-1"]["HostName"] == "user-ser"

    def test_plain_utf8_reg_edit(self, tmp_path):
        reg = make_reg(tmp_path, encoding="utf-8", bom=b"")
        edit_session_values(
            reg, "prod-web-1", {"UserName": "dev"},
            expect_stamp=reg_file_stamp(reg))
        assert parse_putty_reg(reg)["prod-web-1"]["UserName"] == "dev"


class TestStampsAndParseParity:
    def test_parse_putty_reg_still_works_on_sample_fixture(self):
        parsed = parse_putty_reg(Path("tests/data/sample.reg"))
        assert parsed["prod-web-1"] == {
            "HostName": "web1.example.com", "PortNumber": "22",
            "UserName": "deploy", "Protocol": "ssh"}

    def test_stamp_is_plain_sha1(self, tmp_path):
        import hashlib as h
        reg = make_reg(tmp_path)
        expect = h.sha1(reg.read_bytes(),
                        usedforsecurity=False).hexdigest()
        assert reg_file_stamp(reg) == expect


class TestRenaming:
    def test_rename_rewrites_header_line_only(self, tmp_path):
        reg = make_reg(tmp_path)
        before_text = reg.read_bytes().decode("utf-16")
        result = edit_session_values(
            reg, "prod-web-1", {}, expect_stamp=reg_file_stamp(reg),
            rename_to="Legacy Web 1")
        after_text = reg.read_bytes().decode("utf-16")
        # exactly one line differs: the section header (URL-quoted)
        assert reg_line_diff(before_text, after_text) == [
            '-[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\'
            'prod-web-1]',
            '+[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\'
            'Legacy%20Web%201]',
        ]
        parsed = parse_putty_reg(reg)
        assert "prod-web-1" not in parsed
        assert parsed["Legacy%20Web%201"]["UserName"] == "deploy"
        assert result.name == "Legacy%20Web%201"

    def test_rename_with_value_edit_together(self, tmp_path):
        reg = make_reg(tmp_path)
        result = edit_session_values(
            reg, "prod-db", {"HostName": "db2.example.com"},
            expect_stamp=reg_file_stamp(reg), rename_to="prod-db-v2")
        parsed = parse_putty_reg(reg)
        assert "prod-db-v2" in parsed and "prod-db" not in parsed
        assert parsed["prod-db-v2"]["HostName"] == "db2.example.com"
        assert result.name == "prod-db-v2"

    def test_rename_to_existing_session_refused(self, tmp_path):
        reg = make_reg(tmp_path)
        before = reg.read_bytes()
        with pytest.raises(RegEditError, match="already exists"):
            edit_session_values(
                reg, "prod-db", {}, expect_stamp=reg_file_stamp(reg),
                rename_to="prod-web-1")
        assert reg.read_bytes() == before

    def test_rename_noop_when_same_after_quoting(self, tmp_path):
        """Typing the same name (raw vs decoded) = edits only."""
        reg = make_reg(tmp_path)
        result = edit_session_values(
            reg, "prod-web-1", {"UserName": "dev"},
            expect_stamp=reg_file_stamp(reg), rename_to="prod-web-1")
        assert result.name == "prod-web-1"
        assert parse_putty_reg(reg)["prod-web-1"]["UserName"] == "dev"
        # the header line unchanged
        assert "\\Sessions\\prod-web-1]" in reg.read_bytes().decode("utf-16")

    def test_rename_url_quotes_and_round_trips(self, tmp_path):
        reg = make_reg(tmp_path)
        edit_session_values(
            reg, "prod-web-1", {}, expect_stamp=reg_file_stamp(reg),
            rename_to='My "Cool" Server 100% up')
        quoted = "Sessions\\My%20%22Cool%22%20Server%20100%25%20up]"
        assert quoted in reg.read_bytes().decode("utf-16")
        from utils.titlecase_util import unquote_display

        parsed = parse_putty_reg(reg)
        key = [k for k in parsed if k.startswith("My%20")][0]
        assert unquote_display(key) == 'My "Cool" Server 100% up'
        assert parsed[key]["UserName"] == "deploy"   # values intact


class TestRecentsRename:
    def test_rename_entry_preserves_metadata(self, tmp_path):
        import time

        from utils.recent_sessions_util import rename_entry
        path = tmp_path / "recents"
        ts = int(time.time())
        path.write_text(
            f"old-name\t{ts}\t0\nother\nthird\t{ts - 5}\t255\n",
            encoding="utf-8")
        rename_entry(path, "old-name", "new-name")
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[0] == f"new-name\t{ts}\t0"
        assert lines[1] == "other"
        assert lines[2] == f"third\t{ts - 5}\t255"

    def test_rename_entry_missing_file_ok(self, tmp_path):
        from utils.recent_sessions_util import rename_entry
        rename_entry(tmp_path / "nope", "a", "b")   # no crash, no file
        assert not (tmp_path / "nope").exists()


class TestDeleting:
    def test_delete_removes_section_and_separator(self, tmp_path):
        reg = make_reg(tmp_path)
        before_text = reg.read_bytes().decode("utf-16")
        result = delete_session(
            reg, "prod-web-1", expect_stamp=reg_file_stamp(reg))
        after_text = reg.read_bytes().decode("utf-16")

        parsed = parse_putty_reg(reg)
        assert "prod-web-1" not in parsed
        assert parsed["prod-db"]["UserName"] == "postgres"
        assert '"ssh-rsa@22:10.0.0.1"="0x23,aa,bb"' in after_text
        # structure: exactly one blank line between sections
        assert "\r\n\r\n\r\n" not in after_text
        assert result.name == "" and result.session_values == {}
        diff = reg_line_diff(before_text, after_text)
        # header + 4 value lines + the trailing blank separator
        assert [d[:1] for d in diff] == ["-", "-", "-", "-", "-", "-"]

    def test_delete_file_last_section_takes_prior_blank(self, tmp_path):
        reg = make_reg(tmp_path)
        delete_session(reg, "prod-db", expect_stamp=reg_file_stamp(reg))
        text = reg.read_bytes().decode("utf-16")
        assert not text.rstrip("\r\n").endswith("\r\n\r\n")
        parsed = parse_putty_reg(reg)
        assert "prod-db" not in parsed
        assert parsed["prod-web-1"]["UserName"] == "deploy"

    def test_delete_only_session_leaves_header(self, tmp_path):
        """Deleting the ONLY session: the file keeps its regedit header
        line and nothing else; parses to an empty dict."""
        single = ("Windows Registry Editor Version 5.00\r\n\r\n"
                  "[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\"
                  "Sessions\\only]\r\n\"HostName\"=\"h\"\r\n")
        reg = make_reg(tmp_path, text=single)
        delete_session(reg, "only", expect_stamp=reg_file_stamp(reg))
        after = reg.read_bytes().decode("utf-16")
        assert parse_putty_reg(reg) == {}
        assert "Windows Registry Editor Version 5.00" in after
        assert "[HKEY" not in after

    def test_delete_unknown_refused(self, tmp_path):
        reg = make_reg(tmp_path)
        before = reg.read_bytes()
        with pytest.raises(RegEditError, match="not in the file"):
            delete_session(reg, "ghost", expect_stamp=reg_file_stamp(reg))
        assert reg.read_bytes() == before

    def test_delete_stamp_guard(self, tmp_path):
        reg = make_reg(tmp_path)
        stamp = reg_file_stamp(reg)
        # someone re-exported on the Windows side after the dialog
        reg.write_bytes(
            b"\xff\xfe" + SAMPLE.replace(
                "postgres", "otheruser").encode("utf-16-le"))
        expected = reg.read_bytes()
        with pytest.raises(RegEditError, match="changed on disk"):
            delete_session(reg, "prod-db", expect_stamp=stamp)
        assert reg.read_bytes() == expected

    def test_delete_backs_up_first(self, tmp_path):
        reg = make_reg(tmp_path)
        stamp = reg_file_stamp(reg)
        result = delete_session(reg, "prod-db", expect_stamp=stamp)
        assert result.backup_path is not None
        bak = parse_putty_reg(result.backup_path)
        assert "prod-db" in bak   # the backup still has it


class TestRecentsRemove:
    def test_remove_entry_drops_only_that_name(self, tmp_path):
        import time

        from utils.recent_sessions_util import remove_entry
        path = tmp_path / "recents"
        ts = int(time.time())
        path.write_text(f"gone\t{ts}\t0\nkeep\t{ts - 9}\n", encoding="utf-8")
        remove_entry(path, "gone")
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines == [f"keep\t{ts - 9}"]

    def test_remove_entry_missing_file_ok(self, tmp_path):
        from utils.recent_sessions_util import remove_entry
        remove_entry(tmp_path / "nope", "x")
        assert not (tmp_path / "nope").exists()
