"""Route A (ZCode CLI) must replay history into its one-shot prompt.

`_cli_ask_all` opens a fresh CLI session per call and holds no state, which is
fine *because* the caller sends the whole conversation every time. It then
reduced that history to `msgs[-1]`, so the earlier turns it had just walked
were thrown away: with Codex, which resends full history on every turn, every
turn after the first was answered with no context at all.

These tests pin the flattening: history reaches the prompt, the last turn is
still the verbatim tail, and oversized history is dropped from the front
instead of being passed through whole.
"""
import importlib.util
import os
import sys
import unittest
from unittest import mock

BRIDGE_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), os.pardir, "bridges", "zcode"))
sys.path.insert(0, BRIDGE_DIR)

spec = importlib.util.spec_from_file_location(
    "zcode_cli_prompt", os.path.join(BRIDGE_DIR, "zcode_bridge.py"))
zb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zb)


class _FakeBackend:
    """Stand-in for cli_backend that records the prompt it was handed."""

    class QuotaError(Exception):
        def __init__(self, message, code=None):
            super().__init__(message)
            self.code = code

    def __init__(self, text="ok"):
        self.text = text
        self.calls = []

    def ask(self, model="", prompt="", provider=None, timeout=0, log_path=None):
        self.calls.append({"model": model, "prompt": prompt, "provider": provider})
        return self.text


class CliPromptTest(unittest.TestCase):
    def test_single_turn_prompt_is_the_message_verbatim(self):
        self.assertEqual(zb._cli_prompt([("user", "Reply exactly: PONG")]),
                         "Reply exactly: PONG")

    def test_history_is_replayed_into_the_prompt(self):
        turns = [("user", "my name is Ada"),
                 ("assistant", "noted"),
                 ("user", "what is my name?")]
        prompt = zb._cli_prompt(turns)
        self.assertIn("my name is Ada", prompt)
        self.assertIn("noted", prompt)
        self.assertIn("what is my name?", prompt)

    def test_last_turn_is_the_verbatim_tail(self):
        turns = [("user", "first"), ("assistant", "second"),
                 ("user", "Reply exactly: PONG")]
        prompt = zb._cli_prompt(turns)
        self.assertTrue(prompt.endswith("Reply exactly: PONG"), prompt)

    def test_oversized_history_is_dropped_from_the_front(self):
        big = "x" * 30000
        turns = [("user", big), ("user", "keep me")]
        prompt = zb._cli_prompt(turns)
        self.assertLessEqual(len(prompt), zb.CLI_PROMPT_MAX_CHARS)
        self.assertTrue(prompt.endswith("keep me"), prompt)
        self.assertNotIn(big, prompt)

    def test_cli_ask_all_sends_the_history_not_just_the_last_turn(self):
        fake = _FakeBackend("E2E_OK")
        messages = [{"role": "user", "content": "my name is Ada"},
                    {"role": "assistant", "content": "noted"},
                    {"role": "user", "content": "what is my name?"}]
        with mock.patch.object(zb, "_cli_module", lambda: fake):
            text, why = zb._cli_ask_all("GLM-5.3", messages)
        self.assertEqual((text, why), ("E2E_OK", ""))
        self.assertEqual(len(fake.calls), 1)
        prompt = fake.calls[0]["prompt"]
        self.assertIn("my name is Ada", prompt)
        self.assertIn("what is my name?", prompt)


if __name__ == "__main__":
    unittest.main()
