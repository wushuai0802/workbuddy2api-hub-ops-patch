"""Offline installer regression tests; every gateway/data file is synthetic."""

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ops_patch_installer", ROOT / "apply.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="workbuddy-installer-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / "package"
        self.target = self.root / "gateway"
        (self.package / "files").mkdir(parents=True)
        self.target.mkdir()
        self.base = {
            name: ("# synthetic " + name + "\nraise RuntimeError('gateway must not be imported')\n").encode()
            for name in installer.FILES - installer.NEW_FILES
        }
        self.payload = {
            name: ("# patched synthetic " + name + "\nraise RuntimeError('gateway must not be imported')\n").encode()
            for name in installer.FILES
        }
        self.meta = {
            "patch_id": installer.BASE_TAG + "-ops.synthetic", "base_tag": installer.BASE_TAG,
            "upstream_commit": installer.UPSTREAM_COMMIT,
            "files": {
                name: {
                    "base_sha256": installer.source_digest(self.base[name]) if name in self.base else None,
                    "patched_sha256": installer.source_digest(self.payload[name]),
                }
                for name in sorted(installer.FILES)
            },
            "compatibility": {},
        }
        for name, data in self.base.items():
            (self.target / name).write_bytes(data)
        for name, data in self.payload.items():
            (self.package / "files" / name).write_bytes(data)
        for name in installer.DEPENDENCIES:
            data = ("# synthetic unchanged " + name + "\n").encode()
            (self.target / name).write_bytes(data)
            self.meta["compatibility"][name] = installer.source_digest(data)
        (self.target / "accounts").mkdir()
        (self.target / "usage").mkdir()
        (self.target / "accounts" / "synthetic.json").write_bytes(b'{"synthetic-secret":"retain-exactly"}\r\n')
        (self.target / "usage" / "synthetic.jsonl").write_bytes(b'{"synthetic-usage":1}\r\n')
        (self.target / ".env").write_bytes(b"SYNTHETIC_SETTING=retain-exactly\r\n")
        self.write_meta()
        self.rehash_package()
        self.bundle = installer.Bundle(self.package)

    def write_meta(self):
        (self.package / "BASELINE.json").write_text(json.dumps(self.meta, indent=2) + "\n", encoding="utf-8")

    def rehash_package(self):
        files = [path for path in self.package.rglob("*") if path.is_file() and path.name != "MANIFEST.sha256"]
        lines = [installer.digest(path.read_bytes()) + "  " + path.relative_to(self.package).as_posix() for path in sorted(files)]
        (self.package / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def snapshot(self, ignore_backups=False):
        result = {}
        for path in self.target.rglob("*"):
            name = path.relative_to(self.target).as_posix()
            if ignore_backups and name.split("/")[0] == installer.BACKUPS:
                continue
            result[name] = ("link", str(path.readlink())) if path.is_symlink() else ("file", path.read_bytes()) if path.is_file() else ("dir",)
        return result

    def symlink(self, path, destination, directory=False):
        try:
            path.symlink_to(destination, target_is_directory=directory)
        except (OSError, NotImplementedError) as error:
            self.skipTest("OS does not permit test symlinks: " + str(error))

    def test_package_verification_never_imports_gateway(self):
        original = __import__

        def checked_import(name, *args, **kwargs):
            self.assertFalse(name.startswith("wb_"), "installer tried to import gateway code")
            return original(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=checked_import):
            self.assertEqual(installer.Bundle(self.package).payload, self.payload)
            self.bundle.apply(self.target, dry_run=True)

    def test_dry_run_creates_no_lock_backup_or_state(self):
        before = self.snapshot()
        self.assertIn("dry-run OK", self.bundle.apply(self.target, dry_run=True))
        self.assertEqual(self.snapshot(), before)

    def test_apply_and_rollback_preserve_data_and_exact_crlf_bytes(self):
        for name in self.base:
            (self.target / name).write_bytes(self.base[name].replace(b"\n", b"\r\n"))
        before = self.snapshot()
        self.bundle.apply(self.target)
        self.assertEqual(self.bundle.status(self.target), dict.fromkeys(installer.FILES, "patched"))
        state = json.loads((self.target / installer.STATE).read_text())
        backup = self.target / state["backup"]
        for name in self.base:
            self.assertEqual((backup / name).read_bytes(), before[name][1])
            self.assertEqual(state["files"][name]["sha256"], installer.digest(before[name][1]))
        for name in installer.NEW_FILES:
            self.assertIsNone(state["files"][name]["sha256"])
            self.assertFalse((backup / name).exists())
        installed = self.snapshot()
        self.assertIn("rollback dry-run OK", self.bundle.rollback(self.target, dry_run=True))
        self.assertEqual(self.snapshot(), installed)
        self.bundle.rollback(self.target)
        self.assertEqual(self.snapshot(ignore_backups=True), before)
        self.assertTrue(backup.is_dir(), "original backups must be retained")

    def test_already_applied_is_idempotent(self):
        self.bundle.apply(self.target)
        before = self.snapshot()
        self.assertIn("already applied", self.bundle.apply(self.target))
        self.assertEqual(self.snapshot(), before)

    def test_payload_tamper_fails_package_verification(self):
        (self.package / "files" / "wb_proxy.py").write_bytes(b"# tampered\n")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            installer.Bundle(self.package)

    def test_rehashed_payload_still_must_match_source_metadata(self):
        (self.package / "files" / "wb_proxy.py").write_bytes(b"# tampered\n")
        self.rehash_package()
        with self.assertRaisesRegex(ValueError, "payload/source checksum mismatch"):
            installer.Bundle(self.package)

    def test_payload_syntax_is_checked_without_import(self):
        data = b"def invalid(:\n"
        (self.package / "files" / "wb_proxy.py").write_bytes(data)
        self.meta["files"]["wb_proxy.py"]["patched_sha256"] = installer.source_digest(data)
        self.write_meta()
        self.rehash_package()
        with self.assertRaises(SyntaxError):
            installer.Bundle(self.package)

    def test_metadata_cannot_expand_write_scope_to_settings(self):
        self.meta["files"][".env"] = {"base_sha256": None, "patched_sha256": installer.digest(b"x")}
        self.write_meta()
        self.rehash_package()
        with self.assertRaisesRegex(ValueError, "invalid patch metadata"):
            installer.Bundle(self.package)

    def test_metadata_must_guard_all_unchanged_modules(self):
        del self.meta["compatibility"]["wb_activity.py"]
        self.write_meta()
        self.rehash_package()
        with self.assertRaisesRegex(ValueError, "invalid patch metadata"):
            installer.Bundle(self.package)

    def test_metadata_cannot_mark_existing_module_as_new(self):
        self.meta["files"]["wb_proxy.py"]["base_sha256"] = None
        self.write_meta()
        self.rehash_package()
        with self.assertRaisesRegex(ValueError, "invalid source checksum metadata"):
            installer.Bundle(self.package)

    def test_extra_payload_module_is_refused(self):
        (self.package / "files" / "wb_extra.py").write_bytes(b"# unexpected\n")
        self.rehash_package()
        with self.assertRaisesRegex(ValueError, "payload scope"):
            installer.Bundle(self.package)

    def test_manifest_unsafe_paths_and_duplicates_are_refused(self):
        for name in ("../escape", "files/../wb_proxy.py", "./BASELINE.json", "C:/outside.py", "files\\wb_proxy.py", "files//wb_proxy.py"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unsafe bundle path"):
                installer.safe_relative(name)
        manifest = self.package / "MANIFEST.sha256"
        manifest.write_bytes(manifest.read_bytes() * 2)
        with self.assertRaisesRegex(ValueError, "invalid checksum entry"):
            installer.Bundle(self.package)

    def test_unknown_or_mixed_target_is_not_modified(self):
        for data in (b"# user custom source\n", self.payload["wb_proxy.py"]):
            with self.subTest(data=data):
                (self.target / "wb_proxy.py").write_bytes(data)
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, "unknown or mixed source"):
                    self.bundle.apply(self.target)
                self.assertEqual(self.snapshot(), before)

    def test_changed_dependency_is_not_modified(self):
        for name in ("wb_updates.py", "wb_egress.py"):
            with self.subTest(name=name):
                path = self.target / name
                original = path.read_bytes()
                path.write_bytes(b"# different version\n")
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, "unchanged dependency"):
                    self.bundle.apply(self.target)
                self.assertEqual(self.snapshot(), before)
                path.write_bytes(original)

    def test_existing_new_module_and_extra_module_are_refused(self):
        for name in ("wb_ops.py", "wb_custom.py"):
            with self.subTest(name=name):
                path = self.target / name
                path.write_bytes(b"# user code\n")
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    self.bundle.apply(self.target)
                self.assertEqual(self.snapshot(), before)
                path.unlink()

    def test_existing_state_on_base_source_is_refused(self):
        (self.target / installer.STATE).write_bytes(b"{}\n")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "existing patch state"):
            self.bundle.apply(self.target)
        self.assertEqual(self.snapshot(), before)

    def test_target_directory_link_is_refused(self):
        linked = self.root / "linked-gateway"
        self.symlink(linked, self.target, directory=True)
        with self.assertRaisesRegex(ValueError, "symlink/junction"):
            self.bundle.apply(linked)

    def test_target_ancestor_junction_or_link_is_refused(self):
        real = self.root / 'real'
        real.mkdir()
        self.target.rename(real / 'gateway')
        self.target = real / 'gateway'
        alias = self.root / 'alias'
        if os.name == 'nt':
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(real)],
                           check=True, capture_output=True)
        else:
            alias.symlink_to(real, target_is_directory=True)
        try:
            before = self.snapshot()
            with self.assertRaisesRegex(ValueError, 'symlink/junction'):
                self.bundle.apply(alias / 'gateway', dry_run=True)
            self.assertEqual(self.snapshot(), before)
        finally:
            if os.name == 'nt':
                os.rmdir(alias)
            else:
                alias.unlink()

    def test_target_module_and_dangling_links_are_refused(self):
        path = self.target / "wb_proxy.py"
        outside = self.root / "outside.py"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        self.symlink(path, outside)
        with self.assertRaisesRegex(ValueError, "symlink/junction"):
            self.bundle.apply(self.target)
        path.unlink()
        path.write_bytes(self.base["wb_proxy.py"])
        self.symlink(self.target / "wb_stall.py", self.root / "absent.py")
        with self.assertRaisesRegex(ValueError, "symlink/junction"):
            self.bundle.apply(self.target)

    def test_payload_directory_link_is_refused(self):
        files = self.package / "files"
        outside = self.root / "outside-payload"
        files.rename(outside)
        self.symlink(files, outside, directory=True)
        with self.assertRaisesRegex(ValueError, "symlink/junction"):
            installer.Bundle(self.package)

    def test_backup_directory_and_file_links_are_refused(self):
        self.bundle.apply(self.target)
        state = json.loads((self.target / installer.STATE).read_text())
        backup = self.target / state["backup"]
        outside = self.root / "outside-backup"
        backup.rename(outside)
        self.symlink(backup, outside, directory=True)
        with self.assertRaisesRegex(ValueError, "symlink/junction"):
            self.bundle.rollback(self.target)
        backup.unlink()
        outside.rename(backup)
        saved_file = backup / "wb_proxy.py"
        outside_file = self.root / "outside-saved.py"
        saved_file.rename(outside_file)
        self.symlink(saved_file, outside_file)
        with self.assertRaisesRegex(ValueError, "symlink/junction"):
            self.bundle.rollback(self.target)

    def test_backup_tamper_and_target_edits_are_not_overwritten(self):
        self.bundle.apply(self.target)
        state = json.loads((self.target / installer.STATE).read_text())
        saved = self.target / state["backup"] / "wb_proxy.py"
        original = saved.read_bytes()
        saved.write_bytes(b"# changed backup\n")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "backup checksum mismatch"):
            self.bundle.rollback(self.target)
        self.assertEqual(self.snapshot(), before)
        saved.write_bytes(original)
        (self.target / "wb_stall.py").write_bytes(b"# changed local module\n")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "patched files changed"):
            self.bundle.rollback(self.target)
        self.assertEqual(self.snapshot(), before)

    def test_lock_refuses_concurrent_install_and_is_not_deleted(self):
        with installer.target_lock(self.target):
            before = self.snapshot()
            for dry_run in (False, True):
                with self.subTest(dry_run=dry_run), self.assertRaises(FileExistsError):
                    self.bundle.apply(self.target, dry_run=dry_run)
                self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.target / installer.LOCK).exists())

    def test_source_write_failure_restores_the_entire_transaction(self):
        before = self.snapshot()
        real_write = installer.atomic_write
        failed = False

        def failing_write(path, data, mode=0o644):
            nonlocal failed
            if path == self.target / "wb_proxy.py" and not failed:
                failed = True
                raise OSError("synthetic write failure")
            return real_write(path, data, mode)

        with mock.patch.object(installer, "atomic_write", side_effect=failing_write):
            with self.assertRaisesRegex(OSError, "synthetic write failure"):
                self.bundle.apply(self.target)
        self.assertEqual(self.snapshot(ignore_backups=True), before)

    def test_state_write_failure_restores_source_and_removes_new_modules(self):
        before = self.snapshot()
        real_write = installer.atomic_write

        def failing_write(path, data, mode=0o644):
            if path == self.target / installer.STATE:
                raise OSError("synthetic state failure")
            return real_write(path, data, mode)

        with mock.patch.object(installer, "atomic_write", side_effect=failing_write):
            with self.assertRaisesRegex(OSError, "synthetic state failure"):
                self.bundle.apply(self.target)
        self.assertEqual(self.snapshot(ignore_backups=True), before)

    def test_state_delete_failure_restores_the_patched_transaction(self):
        self.bundle.apply(self.target)
        before = self.snapshot()
        real_unlink = Path.unlink

        def failing_unlink(path, *args, **kwargs):
            if path == self.target / installer.STATE:
                raise OSError("synthetic state deletion failure")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", failing_unlink):
            with self.assertRaisesRegex(OSError, "synthetic state deletion failure"):
                self.bundle.rollback(self.target)
        self.assertEqual(self.snapshot(), before)

    def test_recovery_does_not_overwrite_an_external_change(self):
        real_write = installer.atomic_write

        def failing_write(path, data, mode=0o644):
            if path == self.target / "wb_proxy.py":
                (self.target / "wb_ops.py").write_bytes(b"# externally edited during install\n")
                raise OSError("synthetic write failure")
            return real_write(path, data, mode)

        with mock.patch.object(installer, "atomic_write", side_effect=failing_write):
            with self.assertRaisesRegex(RuntimeError, "recovery incomplete.*changed externally"):
                self.bundle.apply(self.target)
        self.assertEqual((self.target / "wb_ops.py").read_bytes(), b"# externally edited during install\n")
        self.assertEqual((self.target / "wb_proxy.py").read_bytes(), self.base["wb_proxy.py"])
        self.assertFalse((self.target / installer.STATE).exists())

    def test_dependencies_are_revalidated_after_lock_acquisition(self):
        real_lock = installer.target_lock

        @contextmanager
        def changing_lock(target):
            with real_lock(target):
                (target / "wb_updates.py").write_bytes(b"# changed before lock\n")
                yield

        with mock.patch.object(installer, "target_lock", changing_lock):
            with self.assertRaisesRegex(ValueError, "unchanged dependency"):
                self.bundle.apply(self.target)
        for name, data in self.base.items():
            self.assertEqual((self.target / name).read_bytes(), data)
        self.assertFalse((self.target / installer.BACKUPS).exists())

    def test_backup_snapshot_rejects_a_source_changed_after_validation(self):
        real_read = installer.read_optional
        reads = 0
        changed = b"# external user edit before backup\n"

        def changing_read(path):
            nonlocal reads
            if path == self.target / "wb_proxy.py":
                reads += 1
                if reads == 3:
                    path.write_bytes(changed)
            return real_read(path)

        with mock.patch.object(installer, "read_optional", side_effect=changing_read):
            with self.assertRaisesRegex(ValueError, "target changed while taking backup"):
                self.bundle.apply(self.target)
        self.assertEqual((self.target / "wb_proxy.py").read_bytes(), changed)
        self.assertFalse((self.target / installer.STATE).exists())
        self.assertTrue(all(not (self.target / name).exists() for name in installer.NEW_FILES))

    def test_rollback_revalidates_state_after_lock_acquisition(self):
        self.bundle.apply(self.target)
        real_lock = installer.target_lock

        @contextmanager
        def changing_lock(target):
            with real_lock(target):
                path = target / installer.STATE
                path.write_bytes(path.read_bytes() + b"\n")
                yield

        with mock.patch.object(installer, "target_lock", changing_lock):
            with self.assertRaisesRegex(ValueError, "patch state changed"):
                self.bundle.rollback(self.target)
        self.assertEqual(self.bundle.status(self.target), dict.fromkeys(installer.FILES, "patched"))

    def test_rollback_rejects_backup_corrupted_during_lock_acquisition(self):
        self.bundle.apply(self.target)
        before = self.snapshot(ignore_backups=True)
        state = json.loads((self.target / installer.STATE).read_text())
        real_lock = installer.target_lock

        @contextmanager
        def corrupting_lock(target):
            with real_lock(target):
                (target / state['backup'] / 'wb_proxy.py').write_bytes(b'# damaged backup\n')
                yield

        with mock.patch.object(installer, 'target_lock', corrupting_lock):
            with self.assertRaisesRegex(ValueError, 'backup checksum mismatch'):
                self.bundle.rollback(self.target)
        self.assertEqual(self.snapshot(ignore_backups=True), before)

    def test_external_edit_immediately_before_commit_is_preserved(self):
        real_commit = self.bundle.commit
        edited = b'# external edit before commit\n'

        def intervening(target, replacement, before, modes):
            (target / 'wb_proxy.py').write_bytes(edited)
            return real_commit(target, replacement, before, modes)

        with mock.patch.object(self.bundle, 'commit', side_effect=intervening):
            with self.assertRaisesRegex(RuntimeError, 'changed externally'):
                self.bundle.apply(self.target)
        self.assertEqual((self.target / 'wb_proxy.py').read_bytes(), edited)
        self.assertFalse((self.target / installer.STATE).exists())
        self.assertTrue(all(not (self.target / name).exists() for name in installer.NEW_FILES))

    def test_transaction_cannot_write_outside_patch_scope(self):
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "outside the patch file scope"):
            self.bundle.commit(self.target, {".env": b"new"}, {".env": b"old"}, {})
        self.assertEqual(self.snapshot(), before)

    def test_cli_verify_dry_run_apply_and_rollback(self):
        shutil.copyfile(ROOT / "apply.py", self.package / "apply.py")
        self.rehash_package()
        before = self.snapshot()
        for arguments in (("--verify-package",), ("--target", str(self.target), "--dry-run")):
            result = subprocess.run([sys.executable, str(self.package / "apply.py"), *arguments], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.snapshot(), before)
        for arguments in (("--target", str(self.target)), ("--target", str(self.target), "--rollback")):
            result = subprocess.run([sys.executable, str(self.package / "apply.py"), *arguments], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(ignore_backups=True), before)


if __name__ == "__main__":
    unittest.main()
