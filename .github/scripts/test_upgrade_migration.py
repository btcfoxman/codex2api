#!/usr/bin/env python3
"""Exercise the real old/new SQLite migrations using synthetic data only.

Run on a CI runner after building both revisions::

    python3 scripts/test_upgrade_migration.py --old-binary /tmp/codex-old \
        --new-binary /tmp/codex-new

Each binary runs CODEX_MIGRATE_ONLY in an empty temporary working directory.
The test does not start the server, import a production database, submit a
generation, install dependencies, or use the caller's application environment.
The existing admin ImageStoragePublicURLSettingsRoundTrip test separately
covers the API-to-configuration conversion; this test covers on-disk upgrade.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile


PRESERVED_TABLES = (
    "accounts", "api_keys", "account_groups", "account_group_members",
    "system_settings", "usage_logs", "image_generation_jobs", "image_assets",
    "data_migrations",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def migrate(binary: Path, directory: Path, database: Path) -> None:
    """Run migration before main starts account workers, HTTP, or upstream sync."""
    env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "SYSTEMROOT")
           if key in os.environ}
    env.update({
        "HOME": str(directory), "TMPDIR": str(directory),
        "TMP": str(directory), "TEMP": str(directory),
        "TZ": "UTC", "DATABASE_DRIVER": "sqlite",
        "DATABASE_PATH": str(database), "CACHE_DRIVER": "memory",
        "CODEX_MIGRATE_ONLY": "1", "CODEX_BIND": "127.0.0.1",
        "CODEX_PORT": "0", "CODEX_ALLOW_ANONYMOUS": "false",
        "RESPONSE_CONTEXT_STORE_PATH": "off",
        # A second fence for packages honoring Go's standard proxy environment.
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "",
    })
    result = subprocess.run([str(binary)], cwd=directory, env=env,
                            capture_output=True, timeout=90, check=False)
    require(result.returncode == 0,
            "migration binary failed: " + result.stderr.decode("utf-8", "replace")[-6000:])
    require(database.is_file(), "migration binary did not create the fixture database")


def columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]


def insert(db: sqlite3.Connection, table: str, values: dict) -> None:
    """Keep fixtures explicit: a missing legacy column must fail this test."""
    available = columns(db, table)
    require(set(values).issubset(available), f"fixture column missing in {table}")
    names = ",".join(f'"{name}"' for name in values)
    placeholders = ",".join("?" for _ in values)
    query = f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})'
    if table == "system_settings":
        # Some old schema helpers create row 1. Preserve its other defaults.
        updates = ",".join(f'"{name}"=excluded."{name}"' for name in values if name != "id")
        query += " ON CONFLICT(id) DO UPDATE SET " + updates
    db.execute(query, tuple(values.values()))


def seed(database: Path) -> None:
    with sqlite3.connect(database) as db:
        insert(db, "accounts", {
            "id": 4101, "name": "migration fixture / 测试账号", "platform": "openai",
            "type": "oauth", "credentials": json.dumps({
                "email": "upgrade-fixture@example.invalid", "allow_duplicate": True,
                "access_token": "fixture-only-not-a-token", "workspace_id": "fixture-workspace",
                "upstream_type": "codex", "future_fixture_key": {"preserve": True},
            }), "enabled": 0, "status": "active", "note": "preserve fixture metadata",
            "created_at": "2026-10-01 01:02:03", "updated_at": "2026-10-02 04:05:06",
        })
        insert(db, "account_groups", {"id": 4201, "name": "upgrade fixture group", "channel": "codex"})
        insert(db, "account_group_members", {"account_id": 4101, "group_id": 4201})
        insert(db, "api_keys", {
            "id": 4301, "name": "upgrade fixture key", "key": "fixture-disabled-key-not-a-secret",
            "quota_limit": 123.5, "quota_used": 7.25, "total_used": 19.75,
            "reset_count": 2, "allowed_group_ids": "[4201]", "enabled": 0,
        })
        insert(db, "system_settings", {
            "id": 1, "site_name": "upgrade fixture / 设置", "admin_secret": "fixture-admin-only",
            "max_concurrency": 7, "global_rpm": 83, "test_model": "gpt-5.5",
            "model_mapping": '{"fixture-alias":"gpt-5.5"}',
            "image_storage_config": json.dumps({
                "backend": "s3", "bucket": "migration-fixture", "prefix": "images/",
                "public_base_url": "https://assets.example.invalid/images",
                "fixture_unknown_field": "preserve",
            }),
            "codex_unified_client_identity_enabled": 1,
        })
        insert(db, "usage_logs", {
            "id": 4401, "account_id": 4101, "api_key_id": 4301, "model": "gpt-image-2.5-flare",
            "endpoint": "/v1/images/generations", "channel": "codex", "status_code": 200,
            "input_tokens": 111, "output_tokens": 222, "total_tokens": 333,
            "account_billed": 0.125, "user_billed": 0.25,
            "created_at": "2026-10-03 07:08:09",
        })
        insert(db, "image_generation_jobs", {
            "id": 4501, "status": "succeeded", "prompt": "合成历史提示词 / preserve me",
            "params_json": '{"model":"gpt-image-2.5-flare","size":"1024x1024"}',
            "api_key_id": 4301, "api_key_name": "upgrade fixture key", "duration_ms": 1234,
            "created_at": "2026-10-03 07:08:09", "started_at": "2026-10-03 07:08:10",
            "completed_at": "2026-10-03 07:08:11",
        })
        insert(db, "image_assets", {
            "id": 4601, "job_id": 4501, "filename": "fixture.png",
            "storage_path": "s3://migration-fixture/images/fixture.png", "mime_type": "image/png",
            "bytes": 128, "width": 1024, "height": 1024, "model": "gpt-image-2.5-flare",
            "requested_size": "1024x1024", "actual_size": "1024x1024", "output_format": "png",
            "expires_at": 4102444800, "delete_after_read": 0,
        })


def snapshot(database: Path, old_columns: dict | None = None) -> tuple[dict, dict]:
    """Compare every pre-upgrade column, including unknown credential/settings fields."""
    with sqlite3.connect(database) as db:
        names = old_columns or {table: columns(db, table) for table in PRESERVED_TABLES}
        rows = {}
        for table, table_columns in names.items():
            require(set(table_columns).issubset(columns(db, table)), f"removed legacy columns from {table}")
            projection = ",".join(f'"{name}"' for name in table_columns)
            rows[table] = sorted(db.execute(f'SELECT {projection} FROM "{table}"').fetchall(), key=repr)
        require(db.execute("PRAGMA integrity_check").fetchone() == ("ok",), "fixture integrity check failed")
        return names, rows


def upgraded_schema(database: Path) -> list:
    with sqlite3.connect(database) as db:
        settings_columns = set(columns(db, "system_settings"))
        require({"kiro_config", "output_guard_enabled", "output_guard_max_tool_argument_bytes",
                 "output_guard_config_generation"}.issubset(settings_columns), "missing new settings columns")
        require(db.execute("SELECT kiro_config, output_guard_enabled, output_guard_config_generation "
                           "FROM system_settings WHERE id=1").fetchone() == ("{}", 0, 1),
                "unsafe upgrade defaults for Kiro/output guard")
        for table in ("key_usage_account_permissions", "key_usage_reset_attempts",
                      "key_usage_reset_leases", "key_usage_reset_cooldowns"):
            require(bool(columns(db, table)), f"missing new table {table}")
        trigger = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' "
                             "AND name='scheduler_outbox_accounts_update'").fetchone()
        require(trigger is not None and "credential_generation" in trigger[0]
                and "antigravity_quota" in trigger[0], "scheduler trigger was not upgraded")
        return db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                          "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()


def s3_config_roundtrip(database: Path, expected: str, replacement: str | None = None) -> None:
    with sqlite3.connect(database) as db:
        raw = db.execute("SELECT image_storage_config FROM system_settings WHERE id=1").fetchone()[0]
        config = json.loads(raw)
        require(config["public_base_url"] == expected, "S3 public base URL changed during upgrade")
        require(config["fixture_unknown_field"] == "preserve", "unknown S3 config field was lost")
        if replacement is not None:
            config["public_base_url"] = replacement
            db.execute("UPDATE system_settings SET image_storage_config=? WHERE id=1", (json.dumps(config),))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-binary", type=Path, required=True)
    parser.add_argument("--new-binary", type=Path, required=True)
    args = parser.parse_args()
    old, new = args.old_binary.resolve(strict=True), args.new_binary.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="codex-upgrade-fixture-") as temporary:
        directory = Path(temporary)
        database = directory / "fixture.db"
        migrate(old, directory, database)
        with sqlite3.connect(database) as db:
            require("output_guard_enabled" not in columns(db, "system_settings"),
                    "old binary is not the expected pre-upgrade revision")
        seed(database)
        original_columns, before = snapshot(database)
        migrate(new, directory, database)
        require(snapshot(database, original_columns)[1] == before, "first upgrade changed fixture rows")
        schema = upgraded_schema(database)
        s3_config_roundtrip(database, "https://assets.example.invalid/images",
                            "https://assets.example.invalid/changed-prefix")
        _, updated = snapshot(database, original_columns)
        migrate(new, directory, database)
        require(snapshot(database, original_columns)[1] == updated, "second upgrade changed fixture rows")
        require(upgraded_schema(database) == schema, "second upgrade changed schema")
        s3_config_roundtrip(database, "https://assets.example.invalid/changed-prefix")
        print(json.dumps({"upgrade_migration": "passed", "new_migration_runs": 2,
                          "preserved_tables": list(PRESERVED_TABLES), "s3_public_url_preserved": True,
                          "schema_idempotent": True, "production_data_used": False,
                          "generation_submissions": 0}))


if __name__ == "__main__":
    main()
