"""Offline regression checks; do not contact or modify the installed app."""
import argparse
from contextlib import closing, redirect_stdout
import io
import itertools
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-bridge" / "scripts"))
import antigravity as bridge

ID = "11111111-2222-3333-4444-555555555555"


def uint(value):
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    return bytes(out) + bytes([value])


def field(number, content):
    return uint(number * 8 + 2) + uint(len(content)) + content


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "conversations").mkdir()
        self.path = self.root / "conversations" / (ID + ".db")
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, status INTEGER, metadata BLOB, step_payload BLOB, step_format INTEGER)")
            user = field(19, field(2, "first line\ncafé ✓".encode()))
            thought_only = field(20, field(3, b"private reasoning"))
            reply = field(20, field(1, b"PONG") + field(3, b"private reasoning"))
            conn.executemany("INSERT INTO steps VALUES (?,?,?,?,?,?)", [
                (0, 14, 3, b"", user, 0),
                (1, 15, 3, b"", thought_only, 0),
                (2, 132, 3, b"", b"not a message", 0),
                (3, 15, 3, b"", reply, 0)])
            conn.commit()
        self.store = bridge.Store(self.root)
        with closing(sqlite3.connect(self.root / "conversation_summaries.db")) as conn:
            conn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT, preview TEXT, step_count INTEGER, last_modified_time TEXT, status TEXT, not_fully_idle INTEGER, killed INTEGER, workspace_uris TEXT)")
            conn.execute("INSERT INTO conversation_summaries VALUES (?, 'Synthetic test', '', 4, '2000-01-01', 'CASCADE_RUN_STATUS_IDLE', 0, 0, '')", (ID,))
            conn.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def test_unicode_cursor_and_reasoning_exclusion(self):
        messages = self.store.messages(ID)
        self.assertEqual([(m["index"], m["role"], m["text"]) for m in messages],
                         [(0, "user", "first line\ncafé ✓"), (3, "assistant", "PONG")])
        self.assertEqual([m["index"] for m in self.store.messages(ID, after=0)], [3])
        self.assertEqual(self.store.max_index(ID), 3)

    def test_readonly_connection_and_path_validation(self):
        with closing(bridge.readonly(self.path)) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM steps")
        with self.assertRaises(bridge.BridgeError):
            self.store.path("../../some-file")
        with self.assertRaises(bridge.BridgeError):
            list(bridge.fields(b"\x0a\x10short"))

    def test_draft_is_preserved_without_typing_or_navigation(self):
        app = object.__new__(bridge.App)
        from unittest.mock import Mock
        app.cdp = Mock()
        app.state = lambda: {"draft": "user's existing unsent work"}
        with self.assertRaisesRegex(bridge.BridgeError, "unsent draft"):
            app.prepare(ID, float("inf"))
        self.assertEqual(app.cdp.call.call_count, 1)  # focus emulation only
        app.cdp.evaluate.assert_not_called()

    def test_busy_target_never_opens_ui(self):
        args = argparse.Namespace(message="harmless", thread=ID)
        with patch.object(self.store, "summaries", return_value=[{"conversation_id": ID}]), \
             patch.object(self.store, "idle", return_value=False), \
             patch.object(bridge, "App") as app:
            with self.assertRaisesRegex(bridge.BridgeError, "busy"):
                bridge.send_locked(args, self.store)
            app.assert_not_called()

    def test_two_sends_cannot_share_ui(self):
        with bridge.SendLock(self.root):
            with self.assertRaisesRegex(bridge.BridgeError, "Another bridge send"):
                with bridge.SendLock(self.root):
                    self.fail("concurrent UI lock was granted")
        with bridge.SendLock(self.root):
            pass  # lock released after the first owner exits

    def args(self, **kw):
        values = dict(data_dir=self.root, profile_dir=self.root, port=None, target=None,
                      cmd="read", thread=ID, after=None, last=10, all=False, only=None, max_chars=0)
        values.update(kw)
        return argparse.Namespace(**values)

    def test_shared_read_returns_peer_thread_and_filtered_text(self):
        out = io.StringIO()
        with redirect_stdout(out):
            result = bridge.dispatch(self.args(only="assistant", after=0))
        message = json.loads(out.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual((message["peer"], message["thread"], message["text"]), ("antigravity", ID, "PONG"))

    def test_shared_dry_run_never_connects_or_changes_ui(self):
        out = io.StringIO()
        args = self.args(cmd="send", message="multi\nline", timeout=5, via="auto", extra=[], dry_run=True)
        with patch.object(bridge, "App") as app, redirect_stdout(out):
            self.assertEqual(bridge.dispatch(args), 0)
            app.assert_not_called()
        self.assertTrue(json.loads(out.getvalue())["dry_run"])

    def test_shared_send_waits_by_default(self):
        args = self.args(cmd="send", message="harmless", timeout=5, via="auto", extra=[], dry_run=False, no_wait=False)
        with patch.object(bridge, "send") as send:
            self.assertEqual(bridge.dispatch(args), 0)
            self.assertTrue(send.call_args[0][0].wait)
        args.no_wait = True
        with patch.object(bridge, "send") as send:
            self.assertEqual(bridge.dispatch(args), 0)
            self.assertFalse(send.call_args[0][0].wait)

    def test_remote_debugging_endpoints_are_rejected(self):
        from cdp import local_url
        for endpoint in ("ws://example.com/devtools/page/test", "file:///tmp/test", "ws://user@localhost/test"):
            with self.subTest(endpoint=endpoint), self.assertRaises(bridge.BridgeError):
                local_url(endpoint)

    def test_return_path_replays_cursor_and_finishes_without_ui_or_cli(self):
        args = self.args(after=0, from_start=False, only="assistant", until_reply=True,
                         timeout=5, poll=0.01, include_bridge=False)
        out = io.StringIO()
        with patch.object(bridge, "App") as app, \
             patch.object(bridge.time, "monotonic", side_effect=itertools.count(0, 0.2)), \
             patch.object(bridge.time, "sleep"), redirect_stdout(out):
            bridge.listen(args, self.store)
        events = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([e["event"] for e in events], ["listening", "message", "turn_complete"])
        self.assertEqual((events[1]["text"], events[2]["index"]), ("PONG", 3))
        app.assert_not_called()


if __name__ == "__main__":
    unittest.main()
