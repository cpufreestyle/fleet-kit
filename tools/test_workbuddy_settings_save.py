"""bridge-settings.json writes must keep every other key, and never half-write.

route_model and image_route_model are two fields of the same file, so they
started life as two identical copies of read-modify-write. The copy that
survives (_save_setting) has to keep the properties both copies had, because
the admin endpoints depend on them:

  * a save preserves unrelated keys -- the file is shared with anything else
    that stashes state there, and dropping a neighbour is silent data loss;
  * a corrupt or missing file reads as "nothing set yet" instead of raising,
    so a bad edit does not wedge the whole dashboard;
  * the temp file is renamed over the target, so a crash never leaves a
    half-written settings file for the next import to parse;
  * concurrent picks do not lose each other's write.

The test points SETTINGS_PATH at a temp file, because the real one is live
state: a test that wrote to it would change which model a running bridge
routes to.
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

BRIDGE_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), os.pardir, "bridges", "workbuddy"))
sys.path.insert(0, BRIDGE_DIR)

spec = importlib.util.spec_from_file_location(
    "workbuddy_core", os.path.join(BRIDGE_DIR, "core.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


class SettingsSaveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "bridge-settings.json"
        self._real = core.SETTINGS_PATH
        core.SETTINGS_PATH = self.path
        self.addCleanup(setattr, core, "SETTINGS_PATH", self._real)

    def _read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_a_save_keeps_unrelated_keys(self):
        self.path.write_text(json.dumps({"route_model": "hy3", "foreign": 42}),
                             encoding="utf-8")
        core._save_image_route_model("hunyuan-image-v3.0-art")
        data = self._read()
        self.assertEqual(data["foreign"], 42)
        self.assertEqual(data["image_route_model"], "hunyuan-image-v3.0-art")

    def test_a_missing_file_reads_as_nothing_set(self):
        core._save_route_model("hy4-preview")
        self.assertEqual(self._read(), {"route_model": "hy4-preview"})

    def test_a_corrupt_file_is_overwritten_not_fatal(self):
        self.path.write_text("{not json", encoding="utf-8")
        core._save_route_model("deepseek-v4.1-flash")
        self.assertEqual(self._read()["route_model"], "deepseek-v4.1-flash")

    def test_a_json_list_is_not_treated_as_the_settings_dict(self):
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        core._save_route_model("glm-5.3")
        self.assertEqual(self._read()["route_model"], "glm-5.3")

    def test_the_write_leaves_no_temporary_file_behind(self):
        core._save_route_model("hy3")
        leftovers = [n for n in os.listdir(self.tmp.name) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_concurrent_picks_keep_both_writes(self):
        """Two admin picks at once must not drop one another's field."""
        self.path.write_text("{}", encoding="utf-8")
        errors = []

        def save(model, field):
            try:
                for _ in range(25):
                    core._save_setting(field, model)
            except Exception as exc:  # pragma: no cover - surfaced by assert
                errors.append(exc)

        threads = [threading.Thread(target=save, args=("hy4-preview", "route_model")),
                   threading.Thread(target=save, args=("seedream-4.0", "image_route_model"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        data = self._read()
        self.assertEqual(data["route_model"], "hy4-preview")
        self.assertEqual(data["image_route_model"], "seedream-4.0")

    def test_the_loader_round_trips_what_the_saver_wrote(self):
        core._save_route_model("hy4-preview")
        self.assertEqual(core._load_route_model(), "hy4-preview")
        core._save_image_route_model("hunyuan-image-v3.0-art")
        self.assertEqual(core._load_image_route_model(), "hunyuan-image-v3.0-art")


if __name__ == "__main__":
    unittest.main()
