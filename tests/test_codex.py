"""Guard the existing Codex CLI contract without contacting a live session."""
import argparse
from contextlib import redirect_stdout
import io
import json
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-bridge" / "scripts"))
import bridge


class CodexTests(unittest.TestCase):
    def test_visible_final_and_commentary_messages_keep_their_phases(self):
        for phase in ("commentary", "final_answer"):
            line = json.dumps({"type": "response_item", "timestamp": "2000-01-01", "payload": {
                "type": "message", "role": "assistant", "phase": phase,
                "content": [{"type": "output_text", "text": "synthetic reply"}]}})
            result = bridge.parse_line("codex", line)
            self.assertEqual(result["text"], "synthetic reply")
            self.assertEqual(bridge.is_final(result), phase == "final_answer")

    def test_codex_send_still_tags_and_uses_existing_delivery_function(self):
        args = argparse.Namespace(peer="codex", thread="synthetic-thread", message="hello",
                                  dry_run=False, via="auto", no_wait=False)
        out = io.StringIO()
        with patch.object(bridge, "resolve_thread", return_value=("synthetic-thread", "synthetic.log")), \
             patch.object(bridge, "find_cli", return_value="codex"), \
             patch.object(bridge, "read_messages", return_value=[]), \
             patch.object(bridge, "session_cwd", return_value=None), \
             patch.object(bridge, "send_codex", return_value=("queue", "PONG")) as send, \
             redirect_stdout(out):
            bridge.cmd_send(args)
        self.assertTrue(send.call_args[0][4].startswith("[agent-bridge] from claude (msg "))
        result = json.loads(out.getvalue())
        self.assertEqual((result["thread"], result["sent"], result["via"], result["reply"]),
                         ("synthetic-thread", True, "queue", "PONG"))

    def test_existing_codex_parser_defaults_and_routing_are_unchanged(self):
        with patch.object(sys, "argv", ["bridge.py", "send", "--peer", "codex", "--thread", "test", "hello"]), \
             patch.object(bridge, "cmd_send") as send:
            bridge.main()
        args = send.call_args[0][0]
        self.assertEqual((args.timeout, args.interval, args.via, args.no_wait, args.extra),
                         (600, 1.0, "auto", False, []))

    def test_antigravity_is_dispatched_from_the_same_entry_point(self):
        import antigravity
        with patch.object(sys, "argv", ["bridge.py", "doctor", "--peer", "antigravity"]), \
             patch.object(antigravity, "dispatch", return_value=0) as dispatch, \
             self.assertRaises(SystemExit) as result:
            bridge.main()
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(dispatch.call_args[0][0].cmd, "doctor")


if __name__ == "__main__":
    unittest.main()
