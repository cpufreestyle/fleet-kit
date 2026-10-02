"""The base_url re-pin must touch exactly one line and stay idempotent.

CC Switch owns ~/.codex/config.toml and rewrites the custom provider's base_url
back to 127.0.0.1:15721 on every provider switch, so the image-cap shim that
listens on 15722 has to be re-pinned after every switch (see
tools/pin_shim_base_url.py). The pin therefore has to be surgical:

  * it rewrites only the provider Codex opens on, never a sibling provider that
    legitimately talks to CC Switch directly;
  * it leaves every other 15721 reference alone. The same file carries
    ANTHROPIC_BASE_URL for servers served on that port, and repointing that
    would break the Anthropic path while the Responses path looked fine;
  * it is idempotent: a second run reports "no change" and leaves the file
    byte-for-byte identical, so it is safe on every setup run.
"""
import os
import subprocess
import sys
import tempfile
import unittest

KIT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
TOOL = os.path.join(KIT, "tools", "pin_shim_base_url.py")
sys.path.insert(0, os.path.join(KIT, "tools"))

import pin_shim_base_url  # noqa: E402  (path set up above)

CONFIG = '''model_provider = "custom"
model = "stepfun/step-5-preview"

[model_providers.custom]
name = "custom"
base_url = "http://127.0.0.1:15721/v1"
wire_api = "responses"

[model_providers.other]
name = "other"
base_url = "http://127.0.0.1:15721"

[shell_environment_policy]
inherit = "all"
ANTHROPIC_BASE_URL = "http://127.0.0.1:15721"
'''


def _run(config, extra=()):
    return subprocess.run([sys.executable, TOOL, "--config", config] +
                          list(extra), capture_output=True, text=True,
                          timeout=60)


class PinBaseUrl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = os.path.join(self.tmp.name, "config.toml")
        with open(self.config, "w", encoding="utf-8") as fh:
            fh.write(CONFIG)

    def _read(self):
        with open(self.config, encoding="utf-8") as fh:
            return fh.read()

    def test_only_the_active_provider_is_rewritten(self):
        proc = _run(self.config)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        lines = self._read().splitlines()
        self.assertIn("base_url = \"http://127.0.0.1:15722/v1\"", lines,
                      "the active provider base_url was not repointed at the shim")
        self.assertIn("base_url = \"http://127.0.0.1:15721\"", lines,
                      "a sibling provider was rewritten; only the active one changes")
        self.assertIn('ANTHROPIC_BASE_URL = "http://127.0.0.1:15721"', lines,
                      "ANTHROPIC_BASE_URL was rewritten; the Anthropic path broke")

    def test_the_scheme_and_path_survive_verbatim(self):
        _run(self.config)
        text = self._read()
        self.assertIn("http://127.0.0.1:15722/v1", text)
        self.assertNotIn("15722/v1/v1", text)
        self.assertNotIn("15722:15722", text)

    def test_a_second_run_reports_no_change(self):
        _run(self.config)
        after_first = self._read()
        proc = _run(self.config)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("no change", proc.stdout)
        self.assertEqual(after_first, self._read(),
                         "the second pin rewrote the file; it must be idempotent")

    def test_an_already_pinned_file_is_left_alone(self):
        pinned = CONFIG.replace("127.0.0.1:15721/v1", "127.0.0.1:15722/v1")
        with open(self.config, "w", encoding="utf-8") as fh:
            fh.write(pinned)
        proc = _run(self.config)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("no change", proc.stdout)
        self.assertEqual(pinned, self._read())

    def test_a_bare_dry_run_writes_nothing(self):
        before = self._read()
        proc = _run(self.config, ["--dry-run"])
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("dry-run", proc.stdout)
        self.assertEqual(before, self._read())

    def test_a_missing_config_is_not_an_error(self):
        proc = _run(os.path.join(self.tmp.name, "absent.toml"))
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("not found", proc.stdout + proc.stderr)

    def test_a_file_without_model_provider_still_targets_custom(self):
        with open(self.config, "w", encoding="utf-8") as fh:
            fh.write(CONFIG.replace('model_provider = "custom"\n', ""))
        proc = _run(self.config)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("15722", self._read())

    def test_a_pin_reports_what_it_did_as_a_pair(self):
        """pin_once() is the entry point the shim timer calls.

        It returns a decision rather than printing, so a caller running it on a
        schedule can tell "wrote" from "nothing to do" from "refused" without
        parsing a log line.
        """
        changed, detail = pin_shim_base_url.pin_once(self.config)
        self.assertTrue(changed)
        self.assertIn("15722", detail)
        again, detail = pin_shim_base_url.pin_once(self.config)
        self.assertFalse(again)
        self.assertIn("no change", detail)

    def test_a_dry_run_pin_reports_it_would_write(self):
        before = self._read()
        changed, detail = pin_shim_base_url.pin_once(self.config, dry_run=True)
        self.assertTrue(changed)
        self.assertIn("dry-run", detail)
        self.assertEqual(before, self._read())

    def test_a_pin_leaves_no_temporary_file_behind(self):
        _run(self.config)
        leftovers = [name for name in os.listdir(self.tmp.name)
                     if name.endswith(".tmp")]
        self.assertEqual(leftovers, [],
                         "the atomic write left %s behind" % leftovers)

    def test_a_config_that_changes_mid_pin_is_left_alone(self):
        """CC Switch writes this file too, and a half-parsed write would break Codex.

        The shim re-pins on a timer, so the pin can land exactly while CC Switch
        is rewriting the same file. Writing back the bytes this pin read would
        truncate whatever CC Switch was in the middle of adding, so the pin has
        to notice the file moved and do nothing.
        """
        real_rewrite = pin_shim_base_url.rewrite

        def rewrite_that_gets_clobbered(text, provider, from_h, to_h):
            with open(self.config, "w", encoding="utf-8") as fh:
                fh.write(text + "\n# cc switch was writing here\n")
            return real_rewrite(text, provider, from_h, to_h)

        pin_shim_base_url.rewrite = rewrite_that_gets_clobbered
        try:
            changed, detail = pin_shim_base_url.pin_once(self.config)
        finally:
            pin_shim_base_url.rewrite = real_rewrite
        self.assertFalse(changed, "the pin wrote a config CC Switch had changed")
        self.assertIn("skipped", detail)
        self.assertIn("# cc switch was writing here", self._read())
        self.assertNotIn("15722", self._read(),
                         "the refused pin still repointed the base_url")


if __name__ == "__main__":
    unittest.main()
