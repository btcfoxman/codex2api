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
            result = Path(mount["Source"]) / posixpath.relpath(path, dest)
            return result.resolve()
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
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def safe_version(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._+/-]{1,80}", value) else "hidden"


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
            if os.readlink("/proc/%s/ns/net" % pid) == os.readlink("/proc/self/ns/net"):
                raise ReleaseError("host_network_namespace_forbidden")
        except OSError:
            raise ReleaseError("network_namespace_unavailable") from None
        self.runner.run(["nsenter", "--target", str(pid), "--net", "--",
                         "iptables", "-w", "5", "-S", "INPUT"])
        busy, ipv6 = socket_counts(pid, self.port)
        if ipv6:
            self.runner.run(["nsenter", "--target", str(pid), "--net", "--",
                             "ip6tables", "-w", "5", "-S", "INPUT"])
        return {"namespace_fence_available": True,
                "service_connections": busy, "ipv6_listener": ipv6}

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
    return args


def main(argv=None):
    report = {"ok": False, "build_on_target": False}
    try:
        release = Release(parse_args(argv))
        report = release.summary
        if release.args.inspect:
            release.inspect()
        else:
            raise ReleaseError("deployment_implementation_pending")
        report["ok"] = True
    except ReleaseError as exc:
        report["error"] = str(exc)
    except Exception:
        report["error"] = "unexpected_failure_details_suppressed"
    print(json.dumps(report, sort_keys=True, ensure_ascii=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
