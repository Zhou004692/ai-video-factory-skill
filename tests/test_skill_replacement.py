from __future__ import annotations

import os
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import distribute_skill
from runtime_lock import RuntimeStateLock, RuntimeStateBusy


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob('*') if p.is_file()}


class SkillReplacementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {
            'FACTORY_RUNTIME_HOME': str(self.root / 'runtime'),
            'FACTORY_ENV_FILE': '', 'FACTORY_DATA_DIR': '', 'FACTORY_DB_PATH': '',
        })
        self.env.start()
        self.output = self.root / 'installed'
        self.target = distribute_skill.build_distribution(ROOT, self.output)
        (self.target / 'VERSION').write_text('old-version', encoding='utf-8')
        self.before = tree_bytes(self.target)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_copy_failure_keeps_previous_skill(self):
        with mock.patch.object(distribute_skill.shutil, 'copy2',
                               side_effect=OSError('injected copy failure')):
            with self.assertRaisesRegex(OSError, 'injected copy'):
                distribute_skill.build_distribution(ROOT, self.output)
        self.assertEqual(tree_bytes(self.target), self.before)

    def test_failed_audit_keeps_previous_skill(self):
        with mock.patch.object(distribute_skill, 'audit_distribution',
                               return_value={'ok': False, 'findings': [{'rule': 'test'}]}):
            with self.assertRaises(ValueError):
                distribute_skill.build_distribution(ROOT, self.output)
        self.assertEqual(tree_bytes(self.target), self.before)

    def test_promotion_failure_restores_previous_skill(self):
        original = os.replace

        def fail_promotion(source, destination):
            if Path(destination) == self.target and Path(source).name == 'next':
                raise OSError('injected promotion failure')
            return original(source, destination)

        with mock.patch.object(distribute_skill.os, 'replace', side_effect=fail_promotion):
            with self.assertRaisesRegex(OSError, 'injected promotion'):
                distribute_skill.build_distribution(ROOT, self.output)
        self.assertEqual(tree_bytes(self.target), self.before)

    def test_live_external_runtime_blocks_replacement_even_without_legacy_state(self):
        lock = RuntimeStateLock(self.root / 'runtime' / '.runtime.lock',
                                purpose='management').acquire()
        try:
            with self.assertRaises(RuntimeStateBusy):
                distribute_skill.build_distribution(ROOT, self.output)
        finally:
            lock.release()
        self.assertEqual(tree_bytes(self.target), self.before)

    def test_source_inside_target_is_rejected_before_change(self):
        with self.assertRaisesRegex(ValueError, 'source|来源'):
            distribute_skill.build_distribution(self.target, self.output)
        self.assertEqual(tree_bytes(self.target), self.before)

    def test_output_inside_distributable_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'output'):
            distribute_skill.build_distribution(self.target, self.target / 'scripts/factory/output')

    def test_incomplete_source_does_not_replace_current_skill(self):
        empty_source = self.root / 'empty-source'
        empty_source.mkdir()
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            distribute_skill.build_distribution(empty_source, self.output)
        self.assertEqual(tree_bytes(self.target), self.before)

    def test_process_state_overrides_inside_replaced_skill_are_rejected(self):
        for key in ('FACTORY_RUNTIME_HOME', 'FACTORY_ENV_FILE', 'FACTORY_DATA_DIR', 'FACTORY_DB_PATH'):
            with self.subTest(key=key), mock.patch.dict(os.environ, {
                    key: str(self.target / 'custom-state')}):
                with self.assertRaisesRegex(RuntimeError, key):
                    distribute_skill.build_distribution(ROOT, self.output)
            self.assertEqual(tree_bytes(self.target), self.before)

    def test_failed_rollback_keeps_recoverable_previous_directory(self):
        original = os.replace

        def fail_publish_and_restore(source, destination):
            if Path(destination) == self.target:
                raise OSError('injected rename failure')
            return original(source, destination)

        with mock.patch.object(distribute_skill.os, 'replace', side_effect=fail_publish_and_restore):
            with self.assertRaises(OSError):
                distribute_skill.build_distribution(ROOT, self.output)
        previous = list(self.output.glob('.skill-replacement-*/previous'))
        self.assertEqual(len(previous), 1)
        self.assertEqual(tree_bytes(previous[0]), self.before)
        distribute_skill.build_distribution(ROOT, self.output)
        self.assertTrue((self.target / 'scripts/bootstrap.py').is_file())

    def test_interrupted_rename_is_recovered_before_validating_next_source(self):
        workspace = self.output / '.skill-replacement-interrupted'
        workspace.mkdir()
        (workspace / 'replacement.json').write_text(
            json.dumps({'target': str(self.target)}), encoding='utf-8')
        os.replace(self.target, workspace / 'previous')
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            distribute_skill.build_distribution(self.root / 'missing-source', self.output)
        self.assertEqual(tree_bytes(self.target), self.before)

    def test_ambiguous_interrupted_backups_are_preserved_for_diagnosis(self):
        for name in ('first', 'second'):
            workspace = self.output / f'.skill-replacement-{name}'
            workspace.mkdir()
            (workspace / 'replacement.json').write_text(
                json.dumps({'target': str(self.target)}), encoding='utf-8')
            (workspace / 'previous').mkdir()
            (workspace / 'previous/SKILL.md').write_text(name)
        original = self.root / 'retained-original'
        os.replace(self.target, original)
        with self.assertRaisesRegex(RuntimeError, 'Multiple interrupted'):
            distribute_skill.build_distribution(ROOT, self.output)
        self.assertFalse(self.target.exists())
        self.assertEqual(len(list(self.output.glob('.skill-replacement-*/previous/SKILL.md'))), 2)

    def test_archive_write_failure_preserves_previous_archive(self):
        version = (ROOT / 'VERSION').read_text().strip()
        archive = self.output / f'ai-video-factory-{version}.zip'
        archive.write_bytes(b'previous-complete-archive')
        with mock.patch.object(distribute_skill.zipfile.ZipFile, 'write',
                               side_effect=OSError('injected archive write failure')):
            with self.assertRaisesRegex(OSError, 'injected archive'):
                distribute_skill.build_archive(ROOT, self.output)
        self.assertEqual(archive.read_bytes(), b'previous-complete-archive')


if __name__ == '__main__':
    unittest.main()
