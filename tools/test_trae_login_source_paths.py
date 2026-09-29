"""Trae must find the IDE login state where it actually lives.

``storage_candidates()`` built its base from ``app_support_dirs("Trae")`` and
then appended the edition name, producing

    ~/Library/Application Support/Trae/Trae CN/User/globalStorage/storage.json

The real layout is one level shallower -- the edition directory *is* the
directory under Application Support:

    ~/Library/Application Support/Trae CN/User/globalStorage/storage.json

Because the candidate list is filtered by ``path.exists()``, every desktop
candidate was dropped. Only the ~/.trae2codex/creds.json cache kept the bridge
alive, so a fresh machine (or a stale cache, or a token the cached refresh
could not renew) reported HTTP 401 "Trae login state not found" while the user
was logged into Trae CN.

``read_desktop_auth`` had the mirror-image mistake for ``product.json``: it walked
four levels up from storage.json expecting to land in the .app bundle, but that
lands in Application Support, so ``app_version`` was always empty and every
request carried the hardcoded fallback version.

These tests build a fake install tree and pin both paths. The cli JWT sources
(~/.trae-cn/trae-jwt-token) are deliberately not listed: their payload carries no
token and no refresh token, and using the JWT as a bearer returns zero models.
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

BRIDGE_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), os.pardir, "bridges", "trae"))
sys.path.insert(0, BRIDGE_DIR)

spec = importlib.util.spec_from_file_location(
    "trae_login_paths", os.path.join(BRIDGE_DIR, "trae_bridge.py"))
tb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tb)

AUTH_KEY = tb.AUTH_STORAGE_KEY


def _write_install(root, edition="Trae CN"):
    """Materialise <root>/<edition>/User/globalStorage/storage.json."""
    gs = os.path.join(root, edition, "User", "globalStorage")
    os.makedirs(gs)
    auth = {
        "token": "access-tok",
        "refreshToken": "refresh-tok",
        "userId": "uid-1",
        "host": "https://api.trae.cn",
    }
    storage = {
        AUTH_KEY: json.dumps(auth),
        "telemetry.machineId": "machine-1",
        "iCubeLastVersion": "2.3.87416",
    }
    with open(os.path.join(gs, "storage.json"), "w", encoding="utf-8") as fh:
        json.dump(storage, fh)
    return os.path.join(gs, "storage.json")


class TraeLoginSourcePathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        # 隔离真实凭证缓存：load_store() 读 CREDS_FILE，而缓存优先于 IDE。
        cache = mock.patch.object(tb, "CREDS_FILE",
                                 os.path.join(self.root, "creds.json"))
        cache.start()
        self.addCleanup(cache.stop)
        # app_support_dirs("Trae") is the only thing the bridge asks of _platform,
        # so redirecting it redirects every desktop candidate.
        patcher = mock.patch.object(tb._platform, "app_support_dirs",
                                     lambda *parts: [os.path.join(self.root, "Trae")])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_app_support_root_is_not_inside_a_trae_directory(self):
        root = tb.app_support_root()
        self.assertEqual(str(root), self.root)
        self.assertFalse(str(root).rstrip("/").endswith("Trae"))

    def test_desktop_edition_path_has_no_extra_trae_level(self):
        expected = os.path.join(self.root, "Trae CN", "User", "globalStorage", "storage.json")
        base = tb.app_support_root()
        self.assertEqual(str(base / "Trae CN" / "User" / "globalStorage" / "storage.json"),
                         expected)

    def test_candidates_include_the_written_desktop_install(self):
        _write_install(self.root)
        cands = tb.storage_candidates()
        self.assertTrue(cands, "an existing desktop install must be discovered")
        editions = {c["edition"] for c in cands}
        self.assertIn("Trae CN", editions)
        for c in cands:
            self.assertEqual(c["source"], "desktop")
            path = str(c["path"])
            self.assertFalse(os.path.join("Trae", "Trae CN") in path
                             or ("/Trae/" in path and path.endswith("/storage.json")),
                             "path must not carry the extra Trae/ level: " + path)

    def test_resolve_credential_reads_the_desktop_login_state(self):
        _write_install(self.root)
        result = asyncio.run(tb.resolve_credential(allow_refresh=False))
        self.assertEqual(result["edition"], "Trae CN")
        self.assertEqual(result["source"], "desktop")
        self.assertEqual(result["access_token"], "access-tok")
        self.assertEqual(result["region"], "cn")

    def test_no_cli_sources_are_advertised(self):
        _write_install(self.root)
        paths = {str(c["path"]) for c in tb.storage_candidates()}
        self.assertFalse([p for p in paths if "trae-jwt-token" in p],
                         "cli JWT files must not be offered as a login source")


if __name__ == "__main__":
    unittest.main()
