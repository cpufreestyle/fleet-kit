#!/usr/bin/env python3
"""Register an "opencodex" provider in CC Switch (claude app_type).

Adds one provider row pointing Claude Code at the local opencodex gateway
(http://127.0.0.1:10100, Anthropic /v1/messages verified). Idempotent: if a
claude provider named "opencodex" already exists the script is a no-op.

Takes one timestamped sqlite backup before the first write (kit precedent,
same as the fleetkit-anthropic / freellmapi backups in ~/.cc-switch). CC Switch
caches the database in memory -- restart the app afterwards.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

DB = Path(os.path.expanduser("~/.cc-switch/cc-switch.db"))
NAME = "opencodex"
APP_TYPE = "claude"
BASE_URL = os.environ.get("OPENCODEX_BASE_URL", "http://127.0.0.1:10100")

SETTINGS_CONFIG = {
    "env": {
        "ANTHROPIC_BASE_URL": BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": "dummy",
        "ANTHROPIC_MODEL": "stepfun/step-5-preview",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "workbuddy-gpt/hy4-preview",
        "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": "workbuddy-gpt/hy4-preview",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "stepfun/step-5-preview",
        "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": "stepfun/step-5-preview",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "workbuddy/glm-5.2",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME": "workbuddy/glm-5.2",
        "ANTHROPIC_DEFAULT_FABLE_MODEL": "workbuddy-gpt/gpt-5.6-luna",
        "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME": "workbuddy-gpt/gpt-5.6-luna",
        "CLAUDE_CODE_DISABLE_VERSION_CHECK": "true",
        "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
        "CLIO_ONBOARDING_COMPLETED": "true",
    }
}
META = {"commonConfigEnabled": True, "endpointAutoSelect": True, "apiFormat": "anthropic"}


def main() -> int:
    if not DB.exists():
        print(f"db missing: {DB}")
        return 1
    con = sqlite3.connect(DB)
    try:
        row = con.execute(
            "SELECT id FROM providers WHERE app_type=? AND name=?", (APP_TYPE, NAME)
        ).fetchone()
        if row:
            print(f"provider exists: {APP_TYPE}/{NAME} id={row[0]} -- no-op")
            return 0

        ts = time.strftime("%Y%m%d-%H%M%S")
        bak = DB.with_name(f"{DB.name}.bak-before-opencodex-{ts}")
        src = sqlite3.connect(DB)
        dst = sqlite3.connect(bak)
        src.backup(dst)
        dst.close()
        src.close()
        print(f"backup: {bak}")

        pid = str(uuid.uuid4())
        columns = (
            "id, app_type, name, settings_config, website_url, category, "
            "created_at, sort_index, notes, icon, icon_color, meta, "
            "is_current, in_failover_queue, cost_multiplier, provider_type"
        )
        placeholders = ", ".join("?" for _ in range(16))
        values = (
            pid,
            APP_TYPE,
            NAME,
            json.dumps(SETTINGS_CONFIG, ensure_ascii=False),
            None,
            "",
            int(time.time() * 1000),
            1,
            None,
            "",
            "",
            "{}",
            0,
            0,
            "1.0",
            "custom",
        )
        con.execute(f"INSERT INTO providers ({columns}) VALUES ({placeholders})", values)
        con.execute(
            "UPDATE providers SET meta=? WHERE id=? AND app_type=?",
            (json.dumps(META, ensure_ascii=False), pid, APP_TYPE),
        )
        con.commit()

        check = con.execute(
            "SELECT name, settings_config, meta FROM providers WHERE id=? AND app_type=?",
            (pid, APP_TYPE),
        ).fetchone()
        print(f"inserted: {APP_TYPE}/{check[0]} id={pid}")
        print(f"  base_url: {json.loads(check[1])['env']['ANTHROPIC_BASE_URL']}")
        print(f"  meta: {check[2]}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
