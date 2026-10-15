#!/usr/bin/env python3
"""Install or roll back this exact source patch without importing the gateway."""

import argparse
import ast
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import time
import uuid


STATE = ".ops-patch-state.json"
BACKUPS = ".ops-patch-backups"
LOCK = ".ops-patch.lock"
FILES = {"wb_proxy.py", "wb_webtools.py", "wb_accounts.py", "wb_ops.py", "wb_stall.py", "wb_upstream_pool.py", "wb_observe.py"}
NEW_FILES = {"wb_ops.py", "wb_stall.py", "wb_observe.py"}
DEPENDENCIES = {
    "dashboard.html", "wb_activity.py", "wb_agents.py",
    "wb_atrest.py", "wb_catalog.py", "wb_egress.py", "wb_fingerprint.py", "wb_identity.py",
    "wb_ipintel.py", "wb_modelsdev.py", "wb_pricing.py", "wb_probes.py",
    "wb_logstore.py",
    "wb_prompt.py", "wb_scheduler.py", "wb_settings.py", "wb_tasks.py",
    "wb_updates.py", "wb_webagent.py",
}
BASE_TAG = "v1.7.6"
UPSTREAM_COMMIT = "7dbe11564c0c74a1d2cdb05791b313e339dddbf5"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_digest(data):
    """Accept Git's CRLF checkout conversion; retain raw bytes in backups."""
    return digest(data.replace(b"\r\n", b"\n"))


def is_digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def safe_relative(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError("unsafe bundle path: " + repr(name))
    path = PurePosixPath(name)
    if (path.is_absolute() or path.as_posix() != name
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or any(ord(char) < 32 for char in name)):
        raise ValueError("unsafe bundle path: " + repr(name))
    return path


def checked_path(path, kind=None, allow_missing=False):
    """Reject links, junctions, non-directory ancestors, and special files."""
    path = Path(os.path.abspath(path))
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if component == path and allow_missing:
                return path
            raise
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ValueError("symlink/junction is not allowed: " + str(component))
        expected = kind if component == path else "directory"
        if expected == "directory" and not stat.S_ISDIR(info.st_mode):
            raise ValueError("expected a directory: " + str(component))
        if expected == "file" and not stat.S_ISREG(info.st_mode):
            raise ValueError("expected a regular file: " + str(component))
    return path


def read_optional(path):
    checked_path(path, "file", allow_missing=True)
    return path.read_bytes() if path.exists() else None


def atomic_write(path, data, mode=0o644):
    checked_path(path, "file", allow_missing=True)
    temporary = path.with_name(".ops-stage-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def target_lock(target):
    path = target / LOCK
    checked_path(path, "file", allow_missing=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    identity = os.fstat(fd)
    try:
        os.write(fd, (str(os.getpid()) + " " + uuid.uuid4().hex + "\n").encode("ascii"))
        os.fsync(fd)
        os.close(fd)
        fd = None
        yield
    finally:
        if fd is not None:
            os.close(fd)
        current = path.lstat()
        if ((current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino)
                or not stat.S_ISREG(current.st_mode)):
            raise RuntimeError("installer lock changed externally; it was retained")
        path.unlink()


class Bundle:
    def __init__(self, root):
        self.root = checked_path(root, "directory")
        manifest = checked_path(self.root / "MANIFEST.sha256", "file")
        listed = set()
        for line in manifest.read_text(encoding="utf-8").splitlines():
            expected, name = line.split("  ", 1)
            relative = safe_relative(name)
            if name in listed or not is_digest(expected):
                raise ValueError("invalid checksum entry: " + name)
            listed.add(name)
            path = checked_path(self.root / relative, "file")
            if digest(path.read_bytes()) != expected:
                raise ValueError("bundle checksum mismatch: " + name)
        if "BASELINE.json" not in listed:
            raise ValueError("BASELINE.json must be covered by MANIFEST.sha256")
        self.meta = json.loads((self.root / "BASELINE.json").read_text(encoding="utf-8"))
        if not isinstance(self.meta, dict):
            raise ValueError("invalid patch metadata")
        self.files = self.meta.get("files", {})
        self.dependencies = self.meta.get("compatibility", {})
        if (not isinstance(self.files, dict) or set(self.files) != FILES
                or not isinstance(self.dependencies, dict) or set(self.dependencies) != DEPENDENCIES
                or not isinstance(self.meta.get("patch_id"), str) or not self.meta["patch_id"].strip()
                or self.meta.get("base_tag") != BASE_TAG
                or self.meta.get("upstream_commit") != UPSTREAM_COMMIT):
            raise ValueError("invalid patch metadata or unsupported upstream baseline")
        if not all(is_digest(value) for value in self.dependencies.values()):
            raise ValueError("invalid dependency checksums")
        if {name for name in listed if name.startswith("files/")} != {"files/" + name for name in FILES}:
            raise ValueError("payload scope must contain exactly the seven patch modules")
        self.payload = {}
        for name, entry in self.files.items():
            if (not isinstance(entry, dict) or not is_digest(entry.get("patched_sha256"))
                    or (name in NEW_FILES and entry.get("base_sha256", "missing") is not None)
                    or (name not in NEW_FILES and not is_digest(entry.get("base_sha256")))
                    or entry.get("base_sha256") == entry.get("patched_sha256")):
                raise ValueError("invalid source checksum metadata: " + name)
            payload_name = "files/" + name
            data = (self.root / payload_name).read_bytes()
            if source_digest(data) != entry["patched_sha256"]:
                raise ValueError("payload/source checksum mismatch: " + name)
            ast.parse(data.decode("utf-8"), filename=name)
            self.payload[name] = data

    def target(self, directory):
        target = checked_path(directory, "directory")
        for name in FILES | DEPENDENCIES | {STATE, LOCK}:
            checked_path(target / name, "file", allow_missing=name not in DEPENDENCIES)
        checked_path(target / BACKUPS, "directory", allow_missing=True)
        unexpected = {
            path.name for path in target.iterdir()
            if path.name.lower().startswith("wb_") and path.name.lower().endswith(".py")
            and path.name not in FILES | DEPENDENCIES
        }
        if unexpected:
            raise ValueError("unknown gateway modules: " + ", ".join(sorted(unexpected)))
        for name, expected in self.dependencies.items():
            if source_digest((target / name).read_bytes()) != expected:
                raise ValueError("unchanged dependency is not exact " + BASE_TAG + ": " + name)
        return target

    def status(self, target):
        result = {}
        for name, entry in self.files.items():
            data = read_optional(target / name)
            actual = source_digest(data) if data is not None else None
            if actual == entry["base_sha256"]:
                result[name] = "base"
            elif actual == entry["patched_sha256"]:
                result[name] = "patched"
            else:
                result[name] = "unknown"
        return result

    @staticmethod
    def unlocked(target):
        if (target / LOCK).exists():
            raise FileExistsError("installer lock already exists: " + str(target / LOCK))

    def commit(self, target, replacement, before, modes):
        if set(replacement) != set(before) or not set(replacement) <= FILES | {STATE}:
            raise ValueError("transaction is outside the patch file scope")
        attempted = []
        order = sorted(set(replacement) - {STATE}) + ([STATE] if STATE in replacement else [])
        try:
            for name in order:
                path = target / name
                if read_optional(path) != before[name]:
                    raise RuntimeError(name + " changed externally; not overwritten")
                attempted.append(name)
                data = replacement[name]
                if data is None:
                    path.unlink()
                else:
                    atomic_write(path, data, modes.get(name, 0o644))
        except BaseException as failure:
            errors = []
            for name in reversed(attempted):
                path = target / name
                original = before[name]
                try:
                    current = read_optional(path)
                    if current == original:
                        continue
                    if current != replacement[name]:
                        errors.append(name + " changed externally; not overwritten")
                        continue
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_write(path, original, modes.get(name, 0o644))
                except (OSError, ValueError) as error:
                    errors.append(name + ": " + str(error))
            if errors:
                raise RuntimeError("write failed; automatic recovery incomplete: " + "; ".join(errors)) from failure
            raise

    def apply(self, directory, dry_run=False):
        target = self.target(directory)
        self.unlocked(target)
        status = self.status(target)
        if set(status.values()) == {"patched"}:
            return "already applied; no files changed"
        if set(status.values()) != {"base"}:
            raise ValueError("unknown or mixed source; use clean " + BASE_TAG + ": " + json.dumps(status))
        if (target / STATE).exists():
            raise ValueError("existing patch state; inspect it before applying")
        if dry_run:
            return "dry-run OK: exact " + BASE_TAG + "; no files changed"
        with target_lock(target):
            self.target(target)
            if set(self.status(target).values()) != {"base"} or (target / STATE).exists():
                raise ValueError("target changed during validation")
            original, modes, state_files = {}, {}, {}
            backup_name = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:12]
            backup_parent = target / BACKUPS
            backup_parent.mkdir(mode=0o700, exist_ok=True)
            checked_path(backup_parent, "directory")
            backup = backup_parent / backup_name
            backup.mkdir(mode=0o700)
            for name in sorted(self.files):
                path = target / name
                original[name] = read_optional(path)
                actual = source_digest(original[name]) if original[name] is not None else None
                if actual != self.files[name]["base_sha256"]:
                    raise ValueError("target changed while taking backup: " + name)
                modes[name] = stat.S_IMODE(path.stat().st_mode) if original[name] is not None else 0o644
                if original[name] is not None:
                    atomic_write(backup / name, original[name], modes[name])
                state_files[name] = {
                    "sha256": digest(original[name]) if original[name] is not None else None,
                    "mode": modes[name],
                }
            state = {
                "patch_id": self.meta["patch_id"], "base_tag": BASE_TAG,
                "backup": (PurePosixPath(BACKUPS) / backup_name).as_posix(), "files": state_files,
            }
            replacement = dict(self.payload)
            replacement[STATE] = (json.dumps(state, indent=2) + "\n").encode("utf-8")
            original[STATE] = None
            modes[STATE] = 0o600
            self.target(target)
            self.commit(target, replacement, original, modes)
        return "applied " + self.meta["patch_id"] + "; backup: " + str(backup)

    def rollback_plan(self, target):
        state_data = (target / STATE).read_bytes()
        state = json.loads(state_data.decode("utf-8"))
        if (not isinstance(state, dict) or state.get("patch_id") != self.meta["patch_id"]
                or state.get("base_tag") != BASE_TAG or not isinstance(state.get("files"), dict)
                or set(state["files"]) != FILES):
            raise ValueError("backup belongs to a different patch")
        if set(self.status(target).values()) != {"patched"}:
            raise ValueError("patched files changed; rollback refuses to overwrite them")
        relative = safe_relative(state.get("backup"))
        if len(relative.parts) != 2 or relative.parts[0] != BACKUPS:
            raise ValueError("invalid backup directory")
        backup = checked_path(target / relative, "directory")
        restore, before, modes = {}, {}, {}
        for name, entry in state["files"].items():
            if (not isinstance(entry, dict) or "sha256" not in entry
                    or (entry["sha256"] is not None and not is_digest(entry["sha256"]))
                    or (entry["sha256"] is None) != (self.files[name]["base_sha256"] is None)):
                raise ValueError("invalid backup absence/checksum marker: " + name)
            if type(entry.get("mode")) is not int or not 0 <= entry["mode"] <= 0o7777:
                raise ValueError("invalid backup mode: " + name)
            data = read_optional(backup / name)
            if entry["sha256"] is None:
                if data is not None:
                    raise ValueError("unexpected backup for a new module: " + name)
            elif data is None or digest(data) != entry["sha256"] or source_digest(data) != self.files[name]["base_sha256"]:
                raise ValueError("backup checksum mismatch: " + name)
            restore[name], before[name], modes[name] = data, (target / name).read_bytes(), entry["mode"]
        restore[STATE], before[STATE], modes[STATE] = None, state_data, 0o600
        return backup, restore, before, modes

    def rollback(self, directory, dry_run=False):
        target = self.target(directory)
        self.unlocked(target)
        backup, restore, before, modes = self.rollback_plan(target)
        if dry_run:
            return "rollback dry-run OK; no files changed"
        with target_lock(target):
            self.target(target)
            if ((target / STATE).read_bytes() != before[STATE]
                    or set(self.status(target).values()) != {"patched"}):
                raise ValueError("target or patch state changed during rollback validation")
            # Revalidate backup bytes while locked, before deleting any new module.
            current_backup, current_restore, current_before, current_modes = self.rollback_plan(target)
            if current_backup != backup or current_restore != restore or current_before != before or current_modes != modes:
                raise ValueError("backup or target changed during rollback validation")
            self.commit(target, restore, before, modes)
        return "rolled back; original backup retained at " + str(backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", help="existing official gateway source directory (not accounts/usage)")
    parser.add_argument("--dry-run", action="store_true", help="validate without modifying the target")
    parser.add_argument("--rollback", action="store_true", help="restore this bundle's saved originals")
    parser.add_argument("--verify-package", action="store_true", help="verify package checksums only")
    args = parser.parse_args()
    try:
        bundle = Bundle(Path(__file__).parent)
        if args.verify_package:
            if args.target or args.rollback or args.dry_run:
                parser.error("--verify-package cannot be combined with target actions")
            print("package checksums OK: " + bundle.meta["patch_id"])
            return 0
        if not args.target:
            parser.error("--target is required")
        action = bundle.rollback if args.rollback else bundle.apply
        print(action(args.target, dry_run=args.dry_run))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, SyntaxError, TypeError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
