"""Synthetic deployment regression tests; no daemon, network, or real secrets."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import types
import unittest
from unittest import mock

import deploy_release as release


def tcp_row(state, *, tx="00000000", rx="00000000", inode="0", ipv6=False):
    local = "0" * 32 if ipv6 else "00000000"
    remote = "00000000000000000000000001000000" if ipv6 else "0100007F"
    return ("0: %s:1F90 %s:FFFF %s %s:%s 00:00000000 00000000 1000 0 %s" %
            (local, remote, state, tx, rx, inode))


def write_tcp_fixture(directory, rows, ipv6=False):
    net = Path(directory) / "99" / "net"
    net.mkdir(parents=True, exist_ok=True)
    for name in ("tcp", "tcp6"):
        selected = rows if name == ("tcp6" if ipv6 else "tcp") else []
        (net / name).write_text("header\n" + "\n".join(selected))


class HelpersTest(unittest.TestCase):
    def test_coordinated_idle_explicitly_allows_only_empty_established_connections(self):
        for ipv6 in (False, True):
            with self.subTest(ipv6=ipv6), tempfile.TemporaryDirectory() as directory:
                write_tcp_fixture(directory, [tcp_row("01", inode="12345", ipv6=ipv6)], ipv6)
                self.assertEqual(release.socket_counts(99, 8080, Path(directory))[0], 1)
                self.assertEqual(release.socket_counts(99, 8080, Path(directory), coordinated_idle=True)[0], 0)

    def test_coordinated_idle_never_ignores_buffered_or_ambiguous_sockets(self):
        rows = [tcp_row("01", tx="00000001"), tcp_row("01", rx="00000001"),
                tcp_row("0A", rx="00000001"), tcp_row("05", inode="12345")]
        rows += [tcp_row(state) for state in ("02", "03", "08", "0C", "FF")]
        with tempfile.TemporaryDirectory() as directory:
            write_tcp_fixture(directory, rows)
            self.assertEqual(release.socket_counts(99, 8080, Path(directory), coordinated_idle=True)[0], len(rows))

    def test_coordinated_idle_metadata_parse_failure_remains_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            write_tcp_fixture(directory, [tcp_row("01", inode="invalid")])
            with self.assertRaisesRegex(release.ReleaseError, "service_socket_state_unavailable"):
                release.socket_counts(99, 8080, Path(directory), coordinated_idle=True)

    def test_drained_unowned_closing_sockets_are_ignored_in_both_families(self):
        for ipv6 in (False, True):
            with self.subTest(ipv6=ipv6), tempfile.TemporaryDirectory() as directory:
                rows = [tcp_row(state, ipv6=ipv6) for state in ("04", "05", "09", "0B")]
                write_tcp_fixture(directory, rows, ipv6)
                self.assertEqual(release.socket_counts(99, 8080, Path(directory)), (0, False))
                inventory = release.socket_inventory(99, 8080, Path(directory))
                self.assertEqual(sum(inventory.values()), 4)
                self.assertTrue(all(key.endswith("_inode_owned0") for key in inventory))

    def test_owned_closing_sockets_still_block_even_with_empty_queues(self):
        with tempfile.TemporaryDirectory() as directory:
            write_tcp_fixture(directory, [tcp_row(state, inode="12345") for state in ("04", "05", "09", "0B")])
            self.assertEqual(release.socket_counts(99, 8080, Path(directory))[0], 4)
            inventory = release.socket_inventory(99, 8080, Path(directory))
            self.assertTrue(all(key.endswith("_inode_owned1") for key in inventory))
            self.assertNotIn("12345", json.dumps(inventory))

    def test_closing_sockets_with_unacknowledged_or_unread_bytes_still_block(self):
        with tempfile.TemporaryDirectory() as directory:
            write_tcp_fixture(directory, [tcp_row("05", tx="00000001"), tcp_row("05", rx="00000001")])
            self.assertEqual(release.socket_counts(99, 8080, Path(directory))[0], 2)

    def test_loopback_established_ws_or_http_connections_are_never_exempt(self):
        with tempfile.TemporaryDirectory() as directory:
            write_tcp_fixture(directory, [tcp_row("01", inode="0")])
            self.assertEqual(release.socket_counts(99, 8080, Path(directory))[0], 1)
            self.assertEqual(release.socket_inventory(99, 8080, Path(directory)),
                             {"01_loopback_rx0_tx0_inode_owned0": 1})

    def test_half_open_and_close_wait_are_not_kernel_teardown_exemptions(self):
        with tempfile.TemporaryDirectory() as directory:
            write_tcp_fixture(directory, [tcp_row(state) for state in ("02", "03", "08")])
            self.assertEqual(release.socket_counts(99, 8080, Path(directory))[0], 3)

    def test_malformed_closing_metadata_fails_closed(self):
        rows = [tcp_row("05").rsplit(" ", 1)[0], tcp_row("05", tx="invalid"),
                tcp_row("05", inode="invalid")]
        for row in rows:
            with self.subTest(row=row), tempfile.TemporaryDirectory() as directory:
                write_tcp_fixture(directory, [row])
                with self.assertRaisesRegex(release.ReleaseError, "service_socket_state_unavailable"):
                    release.socket_counts(99, 8080, Path(directory))
                with self.assertRaisesRegex(release.ReleaseError, "service_socket_inventory_unavailable"):
                    release.socket_inventory(99, 8080, Path(directory))

    def create_identity_database(self, path):
        with contextlib.closing(sqlite3.connect(path)) as db:
            for statement in (
                "CREATE TABLE accounts(id INTEGER PRIMARY KEY,name TEXT,credentials TEXT,status TEXT)",
                "CREATE TABLE api_keys(id INTEGER PRIMARY KEY,key TEXT,name TEXT,enabled INT)",
                "CREATE TABLE usage_logs(id INTEGER PRIMARY KEY,model TEXT,created_at TEXT)",
                "CREATE TABLE image_generation_jobs(id INTEGER PRIMARY KEY,status TEXT,prompt TEXT)",
                "CREATE TABLE image_assets(id INTEGER PRIMARY KEY,job_id INT,filename TEXT)",
                "CREATE TABLE system_settings(id INTEGER PRIMARY KEY,site_name TEXT,admin_secret TEXT)",
                "INSERT INTO accounts VALUES(1,'retained account','old refresh token','active')",
                "INSERT INTO api_keys VALUES(1,'synthetic-api-key','retained key',1)",
                "INSERT INTO usage_logs VALUES(1,'retained-model','2026-11-17')",
                "INSERT INTO image_generation_jobs VALUES(1,'succeeded','retained prompt')",
                "INSERT INTO image_assets VALUES(1,1,'retained.png')",
                "INSERT INTO system_settings VALUES(1,'retained site','synthetic-admin-key')"):
                db.execute(statement)
            db.commit()

    def test_preserves_prior_records_allows_added_rows_and_volatile_account_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            current, baseline = Path(directory) / "current.db", Path(directory) / "baseline.db"
            self.create_identity_database(current)
            release.sqlite_backup(current, baseline)
            fingerprints = release.record_fingerprints(baseline)
            self.assertEqual(fingerprints["accounts"]["columns"], ["id", "name"])
            self.assertNotIn("old refresh token", json.dumps(fingerprints))
            with contextlib.closing(sqlite3.connect(current)) as db:
                db.execute("UPDATE accounts SET credentials='rotated refresh token',status='cooldown'")
                db.execute("INSERT INTO usage_logs VALUES(2,'newly-added','2026-11-18')")
                db.commit()
            verified = release.verify_records(current, baseline, fingerprints)
            self.assertEqual(verified["usage_logs"], {"old_count": 1, "verified": True})
            with contextlib.closing(sqlite3.connect(current)) as db:
                db.execute("DELETE FROM api_keys WHERE id=1")
                db.commit()
            with self.assertRaisesRegex(release.ReleaseError, "preexisting_stable_records_changed"):
                release.verify_records(current, baseline, fingerprints)

    def test_detects_operator_settings_change_without_exposing_setting_values(self):
        with tempfile.TemporaryDirectory() as directory:
            current, baseline = Path(directory) / "current.db", Path(directory) / "baseline.db"
            self.create_identity_database(current)
            release.sqlite_backup(current, baseline)
            fingerprints = release.record_fingerprints(baseline)
            with contextlib.closing(sqlite3.connect(current)) as db:
                db.execute("UPDATE system_settings SET admin_secret='unexpected-reset'")
                db.commit()
            with self.assertRaisesRegex(release.ReleaseError, "^preexisting_stable_records_changed$"):
                release.verify_records(current, baseline, fingerprints)

    def test_only_immutable_ghcr_releases(self):
        valid = "ghcr.io/btcfoxman/codex2api@sha256:" + "a" * 64
        self.assertEqual(release.immutable_image(valid), valid)
        for value in ("ghcr.io/btcfoxman/codex2api:latest", "repo:tag", valid + ";id"):
            with self.subTest(value=value), self.assertRaises(release.ReleaseError):
                release.immutable_image(value)

    def test_path_mapping_requires_persistent_mount_and_prevents_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            volume = root / "volume"
            volume.mkdir()
            info = {"Config": {"WorkingDir": "/app"}, "Mounts": [
                {"Destination": "/data", "Source": str(volume), "RW": True}]}
            self.assertEqual(release.mapped_path(info, "/data/main.db"), volume / "main.db")
            with self.assertRaisesRegex(release.ReleaseError, "outside_persistent"):
                release.mapped_path(info, "main.db")
            if os.name != "nt":
                (volume / "escape").symlink_to(root, target_is_directory=True)
                with self.assertRaisesRegex(release.ReleaseError, "escapes"):
                    release.mapped_path(info, "/data/escape/private.db")

    def test_socket_probe_counts_ws_half_open_and_closing_but_not_time_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            net = Path(directory) / "99" / "net"
            net.mkdir(parents=True)
            # 8080 == 1F90. Other local ports are outgoing connections.
            (net / "tcp").write_text("header\n" + "\n".join(
                "0: 00000000:%s 0100007F:FFFF %s 0 0" % (port, state)
                for port, state in [("1F90", "0A"), ("1F90", "01"),
                                    ("1F90", "03"), ("1F90", "08"),
                                    ("1F90", "06"), ("FF00", "01")]))
            (net / "tcp6").write_text("header\n0: 0000000000000000:1F90 0:0 0A 0\n")
            self.assertEqual(release.socket_counts(99, 8080, Path(directory)), (3, True))

    def test_sqlite_online_backup_includes_uncheckpointed_committed_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            source, snapshot = Path(directory) / "main.db", Path(directory) / "backup.sqlite"
            with contextlib.closing(sqlite3.connect(source)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA wal_autocheckpoint=0")
                db.execute("CREATE TABLE example(value TEXT)")
                db.execute("INSERT INTO example VALUES ('retained')")
                db.commit()
                digest = release.sqlite_backup(source, snapshot)
                self.assertEqual(len(digest), 64)
                with contextlib.closing(sqlite3.connect(snapshot)) as copied:
                    self.assertEqual(copied.execute("SELECT value FROM example").fetchall(), [("retained",)])
                self.assertEqual(db.execute("SELECT value FROM example").fetchall(), [("retained",)])

    def test_sensitive_command_failure_does_not_echo_arguments_or_stderr(self):
        failed = mock.Mock(returncode=1, stdout="secret stdout", stderr="secret stderr")
        with mock.patch("deploy_release.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(release.ReleaseError, "^command_failed$"):
                release.Runner().run(["test", "private-token"], input="private-token")


class StateTest(unittest.TestCase):
    def test_coordinated_idle_cannot_bypass_any_generation_or_queue_counter(self):
        for field in ("active_requests", "queued_image_jobs", "running_image_jobs"):
            with self.subTest(field=field):
                task = self.make_release(coordinated_idle=True)
                state = {"active_requests": 0, "queued_image_jobs": 0,
                         "running_image_jobs": 0, "blocking_service_connections": 0}
                state[field] = 1
                task.state = mock.Mock(return_value=({}, state))
                with mock.patch("deploy_release.time.monotonic", side_effect=[0, 1, 6]), mock.patch("deploy_release.time.sleep"):
                    with self.assertRaisesRegex(release.ReleaseError, "no_restart_performed"):
                        task.wait_idle()
                task.runner.run.assert_not_called()

    def test_coordinated_idle_final_busy_check_prevents_any_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / ".env").write_text("ADMIN_SECRET=synthetic-only\n")
            task = self.make_release(deploy_path=directory, coordinated_idle=True)
            task.runner.env = {"DOCKER_HOST": "unix:///var/run/docker.sock"}
            task.info = {"Id": "old", "Image": "sha256:old", "Config": {},
                         "State": {"Pid": 99}, "Mounts": [
                             {"Source": directory, "Destination": "/data", "RW": True}]}
            task.locate = mock.Mock()
            task.validate_original_config = mock.Mock()
            task.port = 8080
            task.gate_preflight = mock.Mock(return_value={"ipv6_firewall_required": False})
            task.get_json = mock.Mock(side_effect=[{"status": "ok", "build_version": "old-version"},
                {"database": {"driver": "sqlite", "location": "/data/main.db", "healthy": True}}])
            task.memory_video_risk = mock.Mock(return_value=False)
            task.pull = mock.Mock(return_value={"Id": "sha256:new"})
            task.wait_idle = mock.Mock()
            task.backup = mock.Mock()
            task.state = mock.Mock(return_value=({}, {"active_requests": 1, "running_image_jobs": 0,
                                                      "queued_image_jobs": 0, "blocking_service_connections": 0}))
            task.staged_start = mock.Mock()
            fake_fcntl = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=mock.Mock())
            with mock.patch.dict("sys.modules", {"fcntl": fake_fcntl}), \
                 mock.patch("deploy_release.docker_json", return_value=[{"Id": "sha256:old"}]), \
                 mock.patch("deploy_release.Fence"):
                with self.assertRaisesRegex(release.ReleaseError, "service_became_busy_no_restart_performed"):
                    task.deploy()
            task.staged_start.assert_not_called()
            self.assertNotIn("rollback_attempted", task.summary)

    def make_release(self, **kwargs):
        params = dict(deploy_path=".", compose_file="docker-compose.yml", service="codex2api",
                      inspect=False, image="ghcr.io/btcfoxman/codex2api@sha256:" + "a" * 64,
                      expected_version="b" * 40, idle_timeout=5, health_timeout=3, pull_timeout=3)
        params.update(kwargs)
        return release.Release(argparse.Namespace(**params), runner=mock.Mock())

    def test_all_busy_sources_must_drain_not_just_account_count(self):
        task = self.make_release()
        task.state = mock.Mock(side_effect=[({}, {"active_requests": 0, "running_image_jobs": 1}),
                                             ({}, {"active_requests": 0, "running_image_jobs": 0}),
                                             ({}, {"active_requests": 0, "service_connections": 1}),
                                             ({}, {"active_requests": 0, "running_image_jobs": 0}),
                                             ({}, {"active_requests": 0, "running_image_jobs": 0}),
                                             ({}, {"active_requests": 0, "running_image_jobs": 0})])
        with mock.patch("deploy_release.time.sleep"), mock.patch("deploy_release.time.monotonic", return_value=0):
            task.wait_idle()
        self.assertEqual(task.state.call_count, 6)

    def test_busy_timeout_never_starts_a_replacement(self):
        task = self.make_release()
        task.state = mock.Mock(return_value=({}, {"active_requests": 1}))
        with mock.patch("deploy_release.time.monotonic", side_effect=[0, 1, 6]), mock.patch("deploy_release.time.sleep"):
            with self.assertRaisesRegex(release.ReleaseError, "no_restart_performed"):
                task.wait_idle()
        task.runner.run.assert_not_called()

    def test_postgres_queue_probe_checks_every_page(self):
        task = self.make_release()
        task.get_json = mock.Mock(side_effect=[
            {"total": 2, "jobs": [{"id": 2, "status": "succeeded"}]},
            {"total": 2, "jobs": [{"id": 1, "status": "running"}]}])
        self.assertEqual(task.image_jobs({"database": {"driver": "postgres"}}),
                         {"queued": 0, "running": 1})
        self.assertEqual(task.get_json.call_count, 2)

    def test_incomplete_queue_probe_fails_closed(self):
        task = self.make_release()
        task.get_json = mock.Mock(side_effect=[{"total": 2, "jobs": [{"id": 2}]},
                                              {"total": 2, "jobs": []}])
        with self.assertRaisesRegex(release.ReleaseError, "incomplete"):
            task.image_jobs({"database": {"driver": "postgres"}})

    def test_inspect_does_not_invoke_deploy_pull_fence_or_backup(self):
        task = self.make_release(inspect=True)
        task.info = {"Config": {"Env": ["ADMIN_SECRET=do-not-print", "DATABASE_DRIVER=sqlite"]},
                     "Image": "sha256:123", "Mounts": [{"Destination": "/data"}], "State": {"Pid": 99}}
        task.port = 8080
        task.admin_secret = "do-not-print"
        task.locate = mock.Mock()
        task.get_json = mock.Mock(side_effect=[{"status": "ok", "build_version": "b" * 40},
            {"database": {"driver": "sqlite", "healthy": True}, "accounts": {"active_requests": 2}}])
        task.gate_preflight = mock.Mock(side_effect=release.ReleaseError("command_failed"))
        task.image_jobs = mock.Mock(return_value={"running": 1})
        task.memory_video_risk = mock.Mock(return_value=False)
        with mock.patch("deploy_release.socket_inventory", return_value={}):
            result = task.inspect()
        task.runner.run.assert_not_called()
        self.assertEqual(result["active_requests"], 2)
        self.assertNotIn("do-not-print", json.dumps(result))
        self.assertEqual(result["fence_check"], "command_failed")

    def test_probe_sends_admin_key_via_stdin_not_command_argument(self):
        task = self.make_release()
        task.info, task.port, task.admin_secret = {"Id": "container-id"}, 4050, "private-secret"
        task.runner.run.return_value = "{}"
        task.get_json("/api/admin/runtime-status", auth=True)
        positional, keyword = task.runner.run.call_args
        self.assertNotIn("private-secret", repr(positional))
        self.assertEqual(keyword["input"], "private-secret\n")

    def test_memory_grok_async_guard_is_conservative(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "main.db"
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE accounts(platform TEXT,credentials TEXT,status TEXT,error_message TEXT)")
                db.execute("INSERT INTO accounts VALUES ('openai','{}','active','')")
                db.commit()
                task = self.make_release()
                task.info = {"Config": {}, "Mounts": [{"Source": directory, "Destination": "/data", "RW": True}]}
                runtime = {"database": {"driver": "sqlite", "location": "/data/main.db"},
                           "cache": {"driver": "memory"}}
                self.assertFalse(task.memory_video_risk(runtime))
                db.execute("INSERT INTO accounts VALUES ('xai','{}','disabled','')")
                db.commit()
                self.assertTrue(task.memory_video_risk(runtime))
                runtime["cache"]["driver"] = "redis"
                self.assertFalse(task.memory_video_risk(runtime))

    def test_health_checks_exact_build_commit_and_database(self):
        task = self.make_release()
        task.get_json = mock.Mock(side_effect=[{"status": "ok", "build_version": "wrong"},
            {"status": "ok", "build_version": "b" * 40},
            {"database": {"healthy": True}, "output_guard_ready": True}])
        with mock.patch("deploy_release.time.monotonic", return_value=0), mock.patch("deploy_release.time.sleep"):
            task.verify_health({"Id": "new"}, "b" * 40)
        self.assertEqual(task.get_json.call_count, 3)

    def test_candidate_requires_live_output_guard_ready_not_only_health(self):
        task = self.make_release()
        task.get_json = mock.Mock(side_effect=[{"status": "ok", "build_version": "b" * 40},
            {"database": {"healthy": True}, "output_guard_ready": False}])
        with mock.patch("deploy_release.time.monotonic", side_effect=[0, 1, 4]), mock.patch("deploy_release.time.sleep"):
            with self.assertRaisesRegex(release.ReleaseError, "health_or_version"):
                task.verify_health({"Id": "new"}, "b" * 40)
        self.assertNotIn("business_guard_ready", task.summary)

    def test_rollback_can_read_an_old_runtime_without_new_guard_field(self):
        task = self.make_release()
        task.get_json = mock.Mock(side_effect=[{"status": "ok", "build_version": "old-version"},
                                              {"database": {"healthy": True}}])
        task.verify_health({"Id": "old"}, "old-version")

    def test_fence_install_failure_cleans_partial_rules(self):
        runner = mock.Mock()
        runner.run.side_effect = ["", release.ReleaseError("command_failed"), ""]
        watchdog = mock.Mock()
        host = mock.mock_open()
        host.return_value.fileno.return_value = 12
        with mock.patch("deploy_release.os.open", return_value=11), \
             mock.patch("builtins.open", host), \
             mock.patch("deploy_release.os.fstat", side_effect=lambda fd: types.SimpleNamespace(st_ino=fd)), \
             mock.patch("deploy_release.os.close"), \
             mock.patch("deploy_release.subprocess.Popen", return_value=watchdog):
            fence = release.Fence(runner, {"State": {"Pid": 99}}, 4050, True)
            with self.assertRaisesRegex(release.ReleaseError, "command_failed"):
                fence.install()
        self.assertEqual(runner.run.call_count, 3)
        self.assertIn("-D", runner.run.call_args.args[0])
        self.assertNotIn("--target", runner.run.call_args.args[0])
        self.assertIn("/fd/11", runner.run.call_args.args[0][1])
        watchdog.terminate.assert_called_once()

    def test_user_custom_entrypoint_is_rejected_before_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            task = self.make_release(deploy_path=directory)
            task.info = {"Config": {"Entrypoint": ["/custom-launcher"], "Cmd": [], "Labels": {
                "com.docker.compose.project.config_files": str(task.compose_file),
                "com.docker.compose.project.working_dir": directory,
                "com.docker.compose.service": "codex2api"}}}
            with self.assertRaisesRegex(release.ReleaseError, "unsupported_custom_entrypoint_command"):
                task.validate_original_config({"Config": {"Entrypoint": ["/usr/local/bin/codex2api"]}})
            task.runner.run.assert_not_called()

    def test_second_release_accepts_only_its_exact_durable_owned_shim(self):
        with tempfile.TemporaryDirectory() as directory:
            task = self.make_release(deploy_path=directory)
            task.compose_file.write_text("services: {}\n")
            receipt = Path(directory) / ".codex2api-release.json"
            defaults = ["/usr/local/bin/codex2api"]
            wrapped = ["/bin/sh", "-c", release.WAIT_ENTRYPOINT, "release-entrypoint",
                       "/tmp/codex-release-%s.ready" % ("a" * 32)] + defaults
            receipt.write_text(json.dumps({"services": {"codex2api": {
                "entrypoint": wrapped, "command": []}}}))
            task.info = {"Config": {"Entrypoint": wrapped, "Cmd": [], "Labels": {
                "com.docker.compose.project.config_files": "%s,%s" % (task.compose_file, receipt),
                "com.docker.compose.project.working_dir": directory,
                "com.docker.compose.service": "codex2api"}}}
            image = {"Config": {"Entrypoint": defaults, "Cmd": []}}
            task.validate_original_config(image)
            self.assertEqual(task.compose_hash, release.file_hash(task.compose_file))
            wrapped[2] = "exec arbitrary-user-shell"
            receipt.write_text(json.dumps({"services": {"codex2api": {
                "entrypoint": wrapped, "command": []}}}))
            with self.assertRaisesRegex(release.ReleaseError, "unsupported_custom_entrypoint_command"):
                task.validate_original_config(image)

    def test_failed_release_rolls_back_image_without_restoring_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / ".env").write_text("ADMIN_SECRET=synthetic-test-only\n")
            database = path / "main.db"
            database.write_text("before migration")
            task = self.make_release(deploy_path=directory)
            task.runner.env = {"DOCKER_HOST": "unix:///var/run/docker.sock"}
            task.info = {"Id": "old", "Image": "sha256:old", "Config": {},
                         "State": {"Pid": 99}, "Mounts": [
                             {"Type": "bind", "Source": directory, "Destination": "/data", "RW": True}]}
            task.locate = mock.Mock()
            task.validate_original_config = mock.Mock()
            task.port = 8080
            task.gate_preflight = mock.Mock(return_value={"ipv6_firewall_required": False})
            task.get_json = mock.Mock(side_effect=[
                {"status": "ok", "build_version": "old-version"},
                {"database": {"driver": "sqlite", "location": "/data/main.db", "healthy": True}}])
            task.pull = mock.Mock(return_value={"Id": "sha256:new"})
            task.wait_idle = mock.Mock()
            task.backup = mock.Mock()
            task.memory_video_risk = mock.Mock(return_value=False)
            task.state = mock.Mock(return_value=({}, {"active_requests": 0}))
            def staged(image, image_info, token):
                if image == task.args.image:
                    database.write_text("migrated state must survive rollback")
                return {"Id": "replacement", "Image": image_info["Id"]}, "marker"
            task.staged_start = mock.Mock(side_effect=staged)
            task.verify_health = mock.Mock(side_effect=[release.ReleaseError("new_health_failed"), None])
            fake_fcntl = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=mock.Mock())
            with mock.patch.dict("sys.modules", {"fcntl": fake_fcntl}), \
                 mock.patch("deploy_release.docker_json", return_value=[{"Id": "sha256:old"}]), \
                 mock.patch("deploy_release.Fence"):
                with self.assertRaisesRegex(release.ReleaseError, "new_health_failed"):
                    task.deploy()
            self.assertTrue(task.summary["rollback_healthy"])
            self.assertFalse(task.summary["database_restored"])
            self.assertEqual(database.read_text(), "migrated state must survive rollback")
            self.assertEqual(task.staged_start.call_count, 2)


if __name__ == "__main__":
    unittest.main()
