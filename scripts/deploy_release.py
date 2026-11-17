#!/usr/bin/env python3
"""Pull a CI-built immutable release; never build or install on the target.

--inspect performs read-only discovery. Deployment deliberately fails closed if
the existing instance cannot be fenced, drained, or backed up safely.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid


class ReleaseError(Exception):
    """An allowlisted diagnostic code, never raw command output."""


class Runner:
    def __init__(self):
        self.env = os.environ.copy()

    def run(self, args, *, input=None, timeout=30):
        try:
            result = subprocess.run(args, input=input, capture_output=True,
                                    text=True, timeout=timeout, env=self.env,
                                    check=False)
        except subprocess.TimeoutExpired:
            raise ReleaseError("command_timeout") from None
        except OSError:
            raise ReleaseError("command_unavailable") from None
        if result.returncode:
            raise ReleaseError("command_failed")
        return result.stdout


def docker_json(runner, *args):
    try:
        return json.loads(runner.run(["docker", *args]))
    except (ValueError, TypeError):
        raise ReleaseError("invalid_docker_response") from None


def container_env(info):
    return dict(item.split("=", 1) for item in info.get("Config", {}).get("Env", [])
                if "=" in item)


def immutable_image(value):
    if not re.fullmatch(r"ghcr\.io/[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}", value):
        raise ReleaseError("immutable_ghcr_digest_required")
    return value


def socket_counts(pid, port, proc_root=Path("/proc")):
    """Count service sockets, including half-open and closing live sockets."""
    busy = 0
    ipv6_listener = False
    for name in ("tcp", "tcp6"):
        path = proc_root / str(pid) / "net" / name
        if not path.exists() and name == "tcp6":
            continue
        try:
            lines = path.read_text().splitlines()[1:]
            for line in lines:
                fields = line.split()
                if len(fields) < 4 or int(fields[1].rsplit(":", 1)[1], 16) != port:
                    continue
                state = fields[3]
                if name == "tcp6" and state == "0A":
                    ipv6_listener = True
                if state not in ("0A", "06", "07"):
                    busy += 1
        except (OSError, ValueError):
            raise ReleaseError("service_socket_state_unavailable") from None
    return busy, ipv6_listener


def mapped_path(info, value):
    if not value or value == ":memory:" or value.startswith("file:"):
        raise ReleaseError("persistent_database_path_required")
    path = posixpath.normpath(posixpath.join(
        info.get("Config", {}).get("WorkingDir") or "/", value))
    mounts = sorted(info.get("Mounts", []),
                    key=lambda item: len(item.get("Destination", "")), reverse=True)
    for mount in mounts:
        dest = posixpath.normpath(mount.get("Destination", ""))
        if dest == "/":
            continue
        if path == dest or path.startswith(dest + "/"):
            if not mount.get("Source") or not mount.get("RW", False):
                raise ReleaseError("database_mount_not_writable")
            root = Path(mount["Source"]).resolve()
            result = (root / posixpath.relpath(path, dest)).resolve()
            if not result.is_relative_to(root):
                raise ReleaseError("database_path_escapes_persistent_mount")
            return result
    raise ReleaseError("database_outside_persistent_mount")


def sqlite_backup(source, destination, timeout=90):
    """SQLite online backup includes committed WAL; never copy a live .db."""
    deadline = time.monotonic() + timeout
    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise ReleaseError("sqlite_backup_timeout")
    try:
        with contextlib.closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True,
                                               timeout=5)) as old:
            with contextlib.closing(sqlite3.connect(destination)) as snapshot:
                old.backup(snapshot, pages=256, progress=progress, sleep=0.05)
                if snapshot.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
                    raise ReleaseError("sqlite_backup_integrity_failed")
        os.chmod(destination, 0o600)
    except sqlite3.Error:
        raise ReleaseError("sqlite_backup_failed") from None
    return file_hash(destination)


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# Credentials/quota/status synchronization is intentionally absent from the
# account projection. These are durable identities and operator configuration,
# not volatile runtime snapshots or newly added migration columns.
STABLE_RECORDS = {
    "accounts": ("id", "name", "platform", "type", "proxy_url", "note", "created_at"),
    "api_keys": ("id", "key", "name", "quota_limit", "allowed_group_ids", "expires_at", "enabled", "created_at"),
    "usage_logs": ("id", "account_id", "api_key_id", "channel", "endpoint", "model", "status_code",
                   "total_tokens", "input_tokens", "output_tokens", "created_at"),
    "image_generation_jobs": ("id", "status", "prompt", "params_json", "api_key_id", "created_at", "completed_at"),
    "image_assets": ("id", "job_id", "filename", "storage_path", "proxy_url", "model", "bytes", "created_at"),
    "system_settings": ("id", "site_name", "site_logo", "background_config", "max_concurrency", "global_rpm",
        "proxy_url", "admin_secret", "image_storage_config", "model_mapping", "codex_model_mapping",
        "reasoning_effort_models", "max_retries", "max_rate_limit_retries", "scheduler_engine", "scheduler_mode",
        "public_key_usage_page_enabled", "public_image_studio_page_enabled", "public_account_portal_page_enabled",
        "prompt_filter_config", "continuous_retry_policy", "usage_log_mode", "image_s3_public_base_url"),
}


def record_fingerprints(snapshot, timeout=90):
    deadline = time.monotonic() + timeout
    result = {}
    try:
        with contextlib.closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            for table, candidates in STABLE_RECORDS.items():
                present = {row[1] for row in db.execute('PRAGMA table_info("%s")' % table)}
                columns = [name for name in candidates if name in present]
                if "id" not in columns:
                    raise ReleaseError("baseline_required_table_or_identity_missing")
                where = " WHERE status IN ('succeeded','failed')" if table == "image_generation_jobs" else ""
                projection = ",".join('"%s"' % name for name in columns)
                digest, count = hashlib.sha256(), 0
                for row in db.execute('SELECT %s FROM "%s"%s ORDER BY id' % (projection, table, where)):
                    encoded = json.dumps(list(row), ensure_ascii=True, separators=(",", ":"),
                                         default=lambda value: {"bytes": value.hex()}).encode()
                    digest.update(encoded + b"\n")
                    count += 1
                result[table] = {"columns": columns, "count": count, "sha256": digest.hexdigest()}
    except sqlite3.Error:
        raise ReleaseError("stable_record_fingerprint_unavailable") from None
    return result


def verify_records(current, baseline, fingerprints, timeout=90):
    """Compare only preexisting identities; new records are valid additions."""
    deadline = time.monotonic() + timeout
    result = {}
    try:
        with contextlib.closing(sqlite3.connect(current.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("ATTACH DATABASE ? AS release_baseline", (baseline.as_uri() + "?mode=ro",))
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            for table, metadata in fingerprints.items():
                # Names come only from the fixed projection above, never from a
                # task prompt, remote API response, or a user-supplied SQL value.
                columns = metadata["columns"]
                if table not in STABLE_RECORDS or any(name not in STABLE_RECORDS[table] for name in columns):
                    raise ReleaseError("baseline_projection_invalid")
                predicate = 'n.id IS NULL OR ' + " OR ".join(
                    'n."%s" IS NOT b."%s"' % (name, name) for name in columns)
                where = "b.status IN ('succeeded','failed') AND " if table == "image_generation_jobs" else ""
                query = ('SELECT COUNT(*) FROM release_baseline."%s" b LEFT JOIN main."%s" n '
                         'ON n.id=b.id WHERE %s(%s)') % (table, table, where, predicate)
                changed = db.execute(query).fetchone()[0]
                if changed:
                    raise ReleaseError("preexisting_stable_records_changed")
                result[table] = {"old_count": metadata["count"], "verified": True}
    except sqlite3.Error:
        raise ReleaseError("stable_record_verification_unavailable") from None
    return result


def safe_version(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._+/-]{1,80}", value) else "hidden"


class Fence:
    """Only mutate the dedicated container's network namespace, never the host.

    Existing sockets remain untouched. An independent, bounded watchdog removes
    the exact rules if the SSH session or its Python process is lost.
    """
    def __init__(self, runner, info, port, ipv6, lifetime=900):
        self.runner, self.pid, self.port = runner, info["State"]["Pid"], port
        self.fd = os.open("/proc/%d/ns/net" % self.pid, os.O_RDONLY)
        try:
            with open("/proc/self/ns/net", "rb") as host:
                if os.fstat(self.fd).st_ino == os.fstat(host.fileno()).st_ino:
                    raise ReleaseError("host_network_namespace_forbidden")
        except BaseException:
            os.close(self.fd)
            raise
        self.token = "codex-release-" + uuid.uuid4().hex
        self.commands = []
        self.watchdog = None
        self.tools = ["iptables"] + (["ip6tables"] if ipv6 else [])
        self.lifetime = lifetime

    def command(self, tool, action):
        rule = ["!", "-i", "lo", "-p", "tcp", "--dport", str(self.port),
                "-m", "conntrack", "--ctstate", "NEW", "-m", "comment",
                "--comment", self.token, "-j", "REJECT", "--reject-with", "tcp-reset"]
        return ["nsenter", "--net=/proc/%d/fd/%d" % (os.getpid(), self.fd), "--", tool, "-w", "5",
                action, "INPUT"] + (["1"] if action == "-I" else []) + rule

    def install(self):
        try:
            # Start cleanup before installing any rule; use -c because workflow
            # executes this module from stdin without a __file__.
            code = ("import os,sys,time,subprocess,json; "
                    "fd,delay,commands=json.loads(sys.argv[1]); "
                    "time.sleep(delay); "
                    "[(c.__setitem__(1,'--net=/proc/%d/fd/%d'%(os.getpid(),fd))) for c in commands]; "
                    "[(subprocess.run(c,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
                    "timeout=15)) for c in commands]")
            cleanup = [self.command(tool, "-D") for tool in self.tools]
            self.watchdog = subprocess.Popen(
                [sys.executable, "-c", code,
                 json.dumps([self.fd, self.lifetime, cleanup])],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(self.fd,))
            for tool in self.tools:
                self.runner.run(self.command(tool, "-I"))
                self.commands.append(self.command(tool, "-D"))
        except BaseException:
            self.close()
            raise
        return self

    def close(self):
        failed = False
        for command in reversed(self.commands):
            try:
                self.runner.run(command)
            except ReleaseError:
                failed = True
        if self.watchdog is not None and not failed:
            self.watchdog.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.watchdog.wait(timeout=3)
        self.commands.clear()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if failed:
            raise ReleaseError("fence_cleanup_pending_watchdog")


def mount_signature(info):
    return sorted((item.get("Type"), item.get("Source"), item.get("Destination"), item.get("RW"))
                  for item in info.get("Mounts", []))


WAIT_ENTRYPOINT = ('n=0; while [ ! -e "$1" ]; do n=$((n+1)); '
                   '[ "$n" -lt 240 ] || exit 74; sleep 1; done; shift; exec "$@"')


class Release:
    def __init__(self, args, runner=None):
        self.args = args
        self.runner = runner or Runner()
        self.path = Path(args.deploy_path).resolve()
        self.compose_file = self.path / args.compose_file
        self.service = args.service
        self.summary = {"operation": "inspect" if args.inspect else "deploy",
                        "environment": "SSH-JP", "build_on_target": False,
                        "database_restored": False}
        self.info = None
        self.admin_secret = ""

    def compose(self, *args, override=None, timeout=30):
        command = ["docker", "compose", "--project-directory", str(self.path),
                   "-f", str(self.compose_file)]
        if override:
            command += ["-f", str(override)]
        return self.runner.run(command + list(args), timeout=timeout)

    def locate(self):
        if not self.compose_file.is_file():
            raise ReleaseError("existing_compose_file_required")
        ids = self.compose("ps", "-q", self.service).split()
        if len(ids) != 1:
            raise ReleaseError("exactly_one_existing_service_required")
        self.info = docker_json(self.runner, "inspect", ids[0])[0]
        if not self.info.get("State", {}).get("Running"):
            raise ReleaseError("existing_service_not_running")
        self.admin_secret = (os.getenv("CODEX_DEPLOY_ADMIN_SECRET") or
                             container_env(self.info).get("ADMIN_SECRET", ""))
        if "\n" in self.admin_secret or "\r" in self.admin_secret:
            raise ReleaseError("invalid_admin_secret_encoding")
        env = container_env(self.info)
        try:
            self.port = int(env.get("CODEX_PORT", "8080"))
        except ValueError:
            raise ReleaseError("service_port_unavailable") from None
        if not 0 < self.port < 65536:
            raise ReleaseError("invalid_service_port")

    def get_json(self, path, *, auth=False, info=None):
        info = info or self.info
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        if auth:
            if not self.admin_secret:
                raise ReleaseError("admin_secret_unavailable")
            command = ["docker", "exec", "-i", info["Id"], "/bin/sh", "-c",
                       'IFS= read -r deploy_key; exec wget -T 5 -q -O - '
                       '--header="Connection: close" --header="X-Admin-Key: $deploy_key" "$1"',
                       "release-probe", url]
            payload = self.admin_secret + "\n"
        else:
            command = ["docker", "exec", info["Id"], "wget", "-T", "5", "-q", "-O", "-",
                       "--header=Connection: close", url]
            payload = None
        try:
            raw = self.runner.run(command, input=payload, timeout=12)
            if len(raw) > 16 << 20:
                raise ReleaseError("probe_response_too_large")
            return json.loads(raw)
        except ValueError:
            raise ReleaseError("invalid_probe_response") from None

    def gate_preflight(self):
        info = self.info
        mode = info.get("HostConfig", {}).get("NetworkMode", "")
        if mode == "host" or mode.startswith("container:"):
            raise ReleaseError("shared_or_host_network_not_supported")
        pid = info["State"]["Pid"]
        try:
            namespace_fd = os.open("/proc/%s/ns/net" % pid, os.O_RDONLY)
        except OSError:
            raise ReleaseError("network_namespace_unavailable") from None
        try:
            with open("/proc/self/ns/net", "rb") as host:
                if os.fstat(namespace_fd).st_ino == os.fstat(host.fileno()).st_ino:
                    raise ReleaseError("host_network_namespace_forbidden")
            prefix = ["nsenter", "--net=/proc/%d/fd/%d" % (os.getpid(), namespace_fd), "--"]
            try:
                self.runner.run(prefix + ["iptables", "-w", "5", "-S", "INPUT"])
            except ReleaseError:
                raise ReleaseError("namespace_ipv4_firewall_permission_or_tool_unavailable") from None
            busy, ipv6 = socket_counts(pid, self.port)
            ipv6_firewall = Path("/proc/%d/net/tcp6" % pid).exists()
            if ipv6_firewall:
                try:
                    self.runner.run(prefix + ["ip6tables", "-w", "5", "-S", "INPUT"])
                except ReleaseError:
                    raise ReleaseError("namespace_ipv6_firewall_permission_or_tool_unavailable") from None
        finally:
            os.close(namespace_fd)
        return {"namespace_fence_available": True, "ipv6_firewall_required": ipv6_firewall,
                "service_connections": busy, "ipv6_listener": ipv6}

    def state(self, info=None):
        expected = info or self.info
        current = docker_json(self.runner, "inspect", expected["Id"])[0]
        if (not current.get("State", {}).get("Running") or
            current.get("State", {}).get("Pid") != expected.get("State", {}).get("Pid")):
            raise ReleaseError("container_changed_during_idle_verification")
        runtime = self.get_json("/api/admin/runtime-status", auth=True, info=info)
        active = runtime.get("accounts", {}).get("active_requests")
        if type(active) is not int or active < 0:
            raise ReleaseError("active_request_state_unavailable")
        counts = self.image_jobs(runtime)
        sockets, _ = socket_counts((info or self.info)["State"]["Pid"], self.port)
        return runtime, {"active_requests": active, "service_connections": sockets,
                         "queued_image_jobs": counts.get("queued", 0),
                         "running_image_jobs": counts.get("running", 0)}

    def wait_idle(self):
        deadline, streak = time.monotonic() + self.args.idle_timeout, 0
        while time.monotonic() < deadline:
            _, state = self.state()
            self.summary["last_idle_check"] = state
            if all(value == 0 for value in state.values()):
                streak += 1
                if streak >= 3:
                    return
            else:
                streak = 0
            time.sleep(1)
        raise ReleaseError("service_not_idle_no_restart_performed")

    def pull(self):
        image = immutable_image(self.args.image)
        user, token = os.getenv("GHCR_USERNAME"), os.getenv("GHCR_TOKEN")
        if token:
            if not user:
                raise ReleaseError("registry_username_required")
            self.runner.run(["docker", "login", "ghcr.io", "--username", user,
                             "--password-stdin"], input=token + "\n", timeout=30)
        for attempt in range(2):
            try:
                self.runner.run(["docker", "pull", "--quiet", image],
                                timeout=self.args.pull_timeout)
                break
            except ReleaseError:
                if attempt == 1:
                    raise ReleaseError("immutable_image_pull_failed") from None
        info = docker_json(self.runner, "image", "inspect", image)[0]
        if image not in info.get("RepoDigests", []):
            raise ReleaseError("pulled_digest_could_not_be_verified")
        if info.get("Os") != "linux":
            raise ReleaseError("linux_image_required")
        self.summary["release_image"] = image
        self.summary["pull_route"] = "existing_dockerd_configuration"
        return info

    def backup(self, runtime):
        driver = runtime.get("database", {}).get("driver")
        # Do not pretend that a SQLite snapshot backs up PostgreSQL. Inspect
        # identifies this before fencing; support needs the target's existing
        # pg_dump tooling and an explicit consistent-backup procedure.
        if driver != "sqlite":
            raise ReleaseError("postgres_consistent_backup_procedure_required")
        main = mapped_path(self.info, runtime["database"].get("location", ""))
        if not main.is_file():
            raise ReleaseError("persistent_sqlite_file_missing")
        env = container_env(self.info)
        files = {"main": main}
        context_path = env.get("RESPONSE_CONTEXT_STORE_PATH", "").strip()
        if context_path and context_path.lower() not in ("off", "none", "false", "0"):
            context_file = mapped_path(self.info, context_path)
        else:
            context_file = main.parent / "response-context.db"
        if context_file.is_file() and context_file != main:
            files["response_context"] = context_file
        root = self.path / ".release-backups"
        if root.resolve().parent != self.path:
            raise ReleaseError("backup_directory_escapes_deployment_path")
        root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)
        backup_dir = root / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:12])
        backup_dir.mkdir(mode=0o700)
        needed = sum(item.stat().st_size + (Path(str(item) + "-wal").stat().st_size
                     if Path(str(item) + "-wal").is_file() else 0)
                     for item in files.values()) + (64 << 20)
        if shutil.disk_usage(backup_dir).free < needed:
            raise ReleaseError("insufficient_backup_disk_space")
        snapshots = {}
        for label, source in files.items():
            snapshots[label] = sqlite_backup(source, backup_dir / (label + ".sqlite"))
        self.database_baseline = backup_dir / "main.sqlite"
        self.stable_fingerprints = record_fingerprints(self.database_baseline)
        record = {"old_image": self.info["Image"], "new_image": self.args.image,
                  "snapshot_sha256": snapshots, "stable_record_fingerprints": self.stable_fingerprints,
                  "automatic_database_restore": False}
        manifest = backup_dir / "manifest.json"
        manifest.write_text(json.dumps(record, sort_keys=True) + "\n")
        os.chmod(manifest, 0o600)
        self.summary.update(backup_id=backup_dir.name, sqlite_snapshots=len(snapshots),
                            snapshot_sha256=snapshots)

    def staged_start(self, image, image_info, token):
        if file_hash(self.compose_file) != self.compose_hash:
            raise ReleaseError("compose_file_changed_during_release")
        entry = image_info.get("Config", {}).get("Entrypoint") or []
        command = image_info.get("Config", {}).get("Cmd") or []
        if not isinstance(entry, list) or not isinstance(command, list) or not entry + command:
            raise ReleaseError("unsupported_image_entrypoint")
        marker = "/tmp/codex-release-%s.ready" % token
        override = self.path / ".codex2api-release.json"
        payload = {"services": {self.service: {"image": image, "pull_policy": "never",
                   "entrypoint": ["/bin/sh", "-c", WAIT_ENTRYPOINT, "release-entrypoint", marker] + entry + command,
                   "command": []}}}
        override.write_text(json.dumps(payload))
        os.chmod(override, 0o600)
        try:
            self.compose("up", "-d", "--no-build", "--no-deps", self.service,
                         override=override, timeout=90)
            ids = self.compose("ps", "-q", self.service).split()
            if len(ids) != 1:
                raise ReleaseError("replacement_service_unavailable")
            info = docker_json(self.runner, "inspect", ids[0])[0]
            if not info.get("State", {}).get("Running") or info.get("Image") != image_info["Id"]:
                raise ReleaseError("replacement_image_mismatch")
            if mount_signature(info) != mount_signature(self.info):
                raise ReleaseError("persistent_mounts_changed")
            new_env = container_env(info)
            if any(new_env.get(key) != value for key, value in container_env(self.info).items()):
                raise ReleaseError("existing_container_environment_changed")
            if file_hash(self.path / ".env") != self.env_hash:
                raise ReleaseError("application_env_changed_during_release")
            return info, marker
        finally:
            # This receipt is intentionally durable: manual compose operations
            # must include it to keep the verified immutable image selection.
            self.summary["release_receipt"] = override.name

    def validate_original_config(self, old_image):
        labels = self.info.get("Config", {}).get("Labels", {}) or {}
        files = [str(Path(item).resolve()) for item in
                 labels.get("com.docker.compose.project.config_files", "").split(",") if item]
        receipt = self.path / ".codex2api-release.json"
        base = str(self.compose_file.resolve())
        if files not in ([base], [base, str(receipt)]):
            raise ReleaseError("unsupported_compose_file_identity")
        if Path(labels.get("com.docker.compose.project.working_dir", "")).resolve() != self.path:
            raise ReleaseError("unsupported_compose_project_directory")
        if labels.get("com.docker.compose.service") != self.service:
            raise ReleaseError("unsupported_compose_service_identity")
        original = old_image.get("Config", {})
        actual = self.info.get("Config", {})
        entry, command = original.get("Entrypoint") or [], original.get("Cmd") or []
        if files == [base]:
            if (actual.get("Entrypoint") or []) != entry or (actual.get("Cmd") or []) != command:
                raise ReleaseError("unsupported_custom_entrypoint_command")
        else:
            try:
                managed = json.loads(receipt.read_text())["services"][self.service]
                wrapped = managed["entrypoint"]
            except (OSError, ValueError, KeyError, TypeError):
                raise ReleaseError("managed_release_receipt_invalid") from None
            if (not isinstance(wrapped, list) or len(wrapped) < 5 or
                wrapped[:4] != ["/bin/sh", "-c", WAIT_ENTRYPOINT, "release-entrypoint"] or
                not re.fullmatch(r"/tmp/codex-release-[0-9a-f]{32}\.ready", wrapped[4]) or
                wrapped[5:] != entry + command or actual.get("Entrypoint") != wrapped or
                (actual.get("Cmd") or []) != [] or managed.get("command") != []):
                raise ReleaseError("unsupported_custom_entrypoint_command")
        self.compose_hash = file_hash(self.compose_file)

    def verify_health(self, info, expected):
        deadline = time.monotonic() + self.args.health_timeout
        while time.monotonic() < deadline:
            try:
                health = self.get_json("/health", info=info)
                if health.get("status") == "ok" and health.get("build_version") == expected:
                    runtime = self.get_json("/api/admin/runtime-status", info=info, auth=True)
                    guard = runtime.get("output_guard_ready")
                    candidate = expected == self.args.expected_version
                    guard_ready = guard is True if candidate else guard is not False
                    if runtime.get("database", {}).get("healthy") is True and guard_ready:
                        if candidate:
                            self.summary["business_guard_ready"] = True
                        return
            except ReleaseError:
                pass
            time.sleep(2)
        raise ReleaseError("release_health_or_version_check_failed")

    def deploy(self):
        import fcntl  # Target is Linux; inspect remains importable on Windows.
        immutable_image(self.args.image)
        if not re.fullmatch(r"[0-9a-f]{40}", self.args.expected_version):
            raise ReleaseError("full_release_commit_sha_required")
        self.locate()
        if not (self.path / ".env").is_file():
            raise ReleaseError("existing_application_env_required")
        self.env_hash = file_hash(self.path / ".env")
        preflight = self.gate_preflight()
        old_health = self.get_json("/health")
        if old_health.get("status") != "ok" or not old_health.get("build_version"):
            raise ReleaseError("existing_service_health_or_version_unavailable")
        runtime = self.get_json("/api/admin/runtime-status", auth=True)
        if runtime.get("database", {}).get("healthy") is not True:
            raise ReleaseError("existing_database_health_not_confirmed")
        if runtime.get("database", {}).get("driver") != "sqlite":
            raise ReleaseError("postgres_consistent_backup_procedure_required")
        if self.memory_video_risk(runtime):
            raise ReleaseError("in_memory_grok_async_jobs_cannot_be_verified")
        # Resolve now, before restricting intake or changing the running image.
        mapped_path(self.info, runtime["database"].get("location", ""))
        token = uuid.uuid4().hex
        old_image = docker_json(self.runner, "image", "inspect", self.info["Image"])[0]
        self.validate_original_config(old_image)
        endpoint = self.runner.env.get("DOCKER_HOST", "")
        if not endpoint:
            context = docker_json(self.runner, "context", "inspect")[0]
            endpoint = context.get("Endpoints", {}).get("docker", {}).get("Host", "")
        if not endpoint.startswith("unix://"):
            raise ReleaseError("local_unix_docker_endpoint_required")
        # A temporary auth directory must not silently switch a saved Docker
        # context to another daemon. Pin the verified local socket first.
        self.runner.env["DOCKER_HOST"] = endpoint
        self.runner.env.pop("DOCKER_CONTEXT", None)
        old_tag = "codex2api-rollback:" + token
        fences = []
        switched = False
        with open(self.path / ".codex2api-deploy.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ReleaseError("another_release_is_running") from None
            with tempfile.TemporaryDirectory(prefix="codex2api-registry-") as registry_config:
                self.runner.env["DOCKER_CONFIG"] = registry_config
                target_image = self.pull()
                self.runner.run(["docker", "image", "tag", old_image["Id"], old_tag])
                self.summary["rollback_image"] = old_tag
                try:
                    fence = Fence(self.runner, self.info, self.port,
                                  preflight["ipv6_firewall_required"],
                                  self.args.idle_timeout + self.args.health_timeout + 240).install()
                    fences.append(fence)
                    self.wait_idle()
                    self.backup(runtime)
                    _, last = self.state()
                    if any(last.values()):
                        raise ReleaseError("service_became_busy_no_restart_performed")
                    if file_hash(self.path / ".env") != self.env_hash:
                        raise ReleaseError("application_env_changed_during_release")
                    # New image starts in a shell wait loop. Install its namespace
                    # fence BEFORE releasing the application entrypoint, so no
                    # client writes can occur before health/version verification.
                    switched = True
                    new, marker = self.staged_start(self.args.image, target_image, token)
                    fences.append(Fence(self.runner, new, self.port,
                                        preflight["ipv6_firewall_required"],
                                        self.args.health_timeout + 120).install())
                    self.runner.run(["docker", "exec", new["Id"], "touch", marker])
                    self.verify_health(new, self.args.expected_version)
                    new_runtime = self.get_json("/api/admin/runtime-status", info=new, auth=True)
                    current_db = mapped_path(new, new_runtime.get("database", {}).get("location", ""))
                    self.summary["preserved_records"] = verify_records(
                        current_db, self.database_baseline, self.stable_fingerprints)
                    self.summary.update(deployed=True, deployed_version=self.args.expected_version,
                                        image_id=new["Image"], preserved_env=True,
                                        preserved_mounts=True, database_restored=False)
                except BaseException:
                    if switched:
                        self.summary["rollback_attempted"] = True
                        try:
                            restored, marker = self.staged_start(old_tag, old_image, uuid.uuid4().hex)
                            fences.append(Fence(self.runner, restored, self.port,
                                                preflight["ipv6_firewall_required"],
                                                self.args.health_timeout + 120).install())
                            self.runner.run(["docker", "exec", restored["Id"], "touch", marker])
                            self.verify_health(restored, old_health["build_version"])
                            self.summary["rollback_healthy"] = True
                        except BaseException:
                            self.summary["rollback_healthy"] = False
                    raise
                finally:
                    failures = []
                    for fence in reversed(fences):
                        try:
                            fence.close()
                        except ReleaseError as exc:
                            failures.append(str(exc))
                    if failures:
                        self.summary["fence_cleanup"] = "watchdog_pending"
                    else:
                        self.summary["fence_cleanup"] = "complete"
                    if failures:
                        raise ReleaseError("fence_cleanup_pending_watchdog")
        return self.summary

    def image_jobs(self, runtime):
        if runtime.get("database", {}).get("driver") == "sqlite":
            source = mapped_path(self.info, runtime["database"]["location"])
            try:
                with contextlib.closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True,
                                                       timeout=3)) as db:
                    rows = db.execute("SELECT status,COUNT(*) FROM image_generation_jobs "
                                      "WHERE status IN ('queued','running') GROUP BY status").fetchall()
            except sqlite3.Error:
                raise ReleaseError("image_queue_state_unavailable") from None
            return dict(rows)
        # Original API has no status filter: inspect the complete bounded list,
        # never assume only the newest page may contain a running job.
        counts = {"queued": 0, "running": 0}
        total = None
        seen = set()
        for page in range(1, 51):
            result = self.get_json("/api/admin/images/jobs?summary=1&page=%d&page_size=100" % page,
                                   auth=True)
            current_total = result.get("total")
            jobs = result.get("jobs")
            if not isinstance(current_total, int) or not isinstance(jobs, list):
                raise ReleaseError("image_queue_response_invalid")
            if total is None:
                total = current_total
            if total != current_total or total > 5000:
                raise ReleaseError("image_queue_cannot_be_bounded")
            for job in jobs:
                ident = job.get("id")
                if ident in seen:
                    raise ReleaseError("image_queue_changed_during_probe")
                seen.add(ident)
                status = job.get("status")
                if status in counts:
                    counts[status] += 1
            if len(seen) >= total:
                return counts
            if not jobs:
                raise ReleaseError("image_queue_incomplete")
        raise ReleaseError("image_queue_cannot_be_bounded")

    def memory_video_risk(self, runtime):
        """Old MemoryTokenCache cannot inventory its async Grok bindings."""
        driver = runtime.get("cache", {}).get("driver")
        if driver not in ("memory", "redis"):
            raise ReleaseError("runtime_cache_driver_unavailable")
        if driver != "memory":
            return False
        if runtime.get("database", {}).get("driver") != "sqlite":
            raise ReleaseError("in_memory_async_video_state_cannot_be_verified")
        source = mapped_path(self.info, runtime["database"].get("location", ""))
        try:
            with contextlib.closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True,
                                                   timeout=3)) as db:
                count = db.execute("SELECT COUNT(*) FROM accounts WHERE status<>'deleted' "
                    "AND COALESCE(error_message,'')<>'deleted' AND "
                    "(LOWER(COALESCE(platform,''))='xai' OR "
                    "LOWER(COALESCE(json_extract(credentials,'$.upstream_type'),''))='grok')").fetchone()[0]
        except sqlite3.Error:
            raise ReleaseError("in_memory_async_video_state_cannot_be_verified") from None
        return count > 0

    def inspect(self):
        self.locate()
        env = container_env(self.info)
        self.summary.update({"container_running": True,
                             "current_image_id": self.info.get("Image", ""),
                             "persistent_mounts": [m.get("Destination") for m in self.info.get("Mounts", [])],
                             "admin_secret_available": bool(self.admin_secret),
                             "database_driver_env": env.get("DATABASE_DRIVER", "postgres"),
                             "pull_route": "existing_dockerd_configuration"})
        for name, probe in (("health", lambda: self.get_json("/health")),
                            ("runtime", lambda: self.get_json("/api/admin/runtime-status", auth=True)),
                            ("fence", self.gate_preflight)):
            try:
                result = probe()
                if name == "health":
                    self.summary.update(health_ok=result.get("status") == "ok",
                                        current_version=safe_version(result.get("build_version")))
                elif name == "runtime":
                    self.summary.update(database_driver=result.get("database", {}).get("driver"),
                                        database_healthy=result.get("database", {}).get("healthy"),
                                        active_requests=result.get("accounts", {}).get("active_requests"))
                    try:
                        counts = self.image_jobs(result)
                        self.summary["image_jobs"] = counts
                    except ReleaseError as exc:
                        self.summary["image_jobs_check"] = str(exc)
                    try:
                        self.summary["grok_memory_async_risk"] = self.memory_video_risk(result)
                    except ReleaseError as exc:
                        self.summary["grok_memory_async_check"] = str(exc)
                else:
                    self.summary.update(result)
            except ReleaseError as exc:
                self.summary[name + "_check"] = str(exc)
        return self.summary


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", action="store_true",
                        default=os.getenv("DEPLOY_OPERATION", "deploy") == "inspect")
    parser.add_argument("--deploy-path", default=os.getenv("DEPLOY_PATH", ""))
    parser.add_argument("--compose-file", default=os.getenv("DEPLOY_COMPOSE_FILE") or "docker-compose.yml")
    parser.add_argument("--service", default=os.getenv("DEPLOY_SERVICE") or "codex2api")
    parser.add_argument("--image", default=os.getenv("RELEASE_IMAGE", ""))
    parser.add_argument("--expected-version", default=os.getenv("RELEASE_SHA", ""))
    parser.add_argument("--idle-timeout", type=int, default=180)
    parser.add_argument("--health-timeout", type=int, default=300)
    parser.add_argument("--pull-timeout", type=int, default=180)
    args = parser.parse_args(argv)
    if not args.deploy_path:
        raise ReleaseError("deploy_path_required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.service):
        raise ReleaseError("invalid_service_name")
    if any(not 0 < getattr(args, name) <= 3600
           for name in ("idle_timeout", "health_timeout", "pull_timeout")):
        raise ReleaseError("invalid_deployment_timeout")
    return args


def main(argv=None):
    report = {"ok": False, "build_on_target": False}
    try:
        release = Release(parse_args(argv))
        report = release.summary
        if release.args.inspect:
            release.inspect()
        else:
            def interrupted(_signum, _frame):
                raise ReleaseError("deployment_interrupted")
            for name in ("SIGTERM", "SIGHUP", "SIGINT"):
                if hasattr(signal, name):
                    signal.signal(getattr(signal, name), interrupted)
            release.deploy()
        report["ok"] = True
    except ReleaseError as exc:
        report["error"] = str(exc)
    except Exception:
        report["error"] = "unexpected_failure_details_suppressed"
    print(json.dumps(report, sort_keys=True, ensure_ascii=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
