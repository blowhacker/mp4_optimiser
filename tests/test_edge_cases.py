import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import mp4_optimiser as m
from test_mp4_optimiser import make_info


class EdgeCaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / 'movie.mp4'
        self.source.write_bytes(b'x' * 100)

    def process(self, **kwargs):
        options = dict(source=self.source, source_root=self.root,
                       destination_root=self.root, replace_originals=True,
                       dry_run=False, keep_trickplay=True,
                       summary_log=self.root / 'summary.csv')
        options.update(kwargs)
        return m.process_file(**options)

    def encode(self, command):
        Path(command[-1]).write_bytes(b'y' * 50)
        return mock.Mock(returncode=0)

    def test_dry_run_preserves_stale_temp_and_does_not_create_output_dirs(self):
        stale = m.make_temp_path(self.source)
        stale.write_bytes(b'previous partial output')
        destination = self.root / 'new' / 'nested'
        with mock.patch.object(m, 'probe', return_value=make_info()):
            self.assertEqual(self.process(dry_run=True)[0], 'dry-run')
            self.assertEqual(self.process(dry_run=True, replace_originals=False,
                                          destination_root=destination)[0], 'dry-run')
        self.assertEqual(stale.read_bytes(), b'previous partial output')
        self.assertFalse(destination.exists())
        self.assertEqual(m.discover_videos(self.root, self.root), [self.source.resolve()])

    def test_source_changed_during_encode_is_preserved(self):
        def encode(command):
            self.encode(command)
            self.source.write_bytes(b'new original')
            return mock.Mock(returncode=0)
        with mock.patch.object(m, 'probe', return_value=make_info()), \
             mock.patch.object(m.subprocess, 'run', side_effect=encode):
            with self.assertRaisesRegex(RuntimeError, 'source changed'):
                self.process()
        self.assertEqual(self.source.read_bytes(), b'new original')
        self.assertEqual(list(self.root.glob('*.__optimising__*')), [])

    def test_exact_cutoff_is_accepted_and_permissions_preserved(self):
        def encode(command):
            Path(command[-1]).write_bytes(b'y' * 85)
            return mock.Mock(returncode=0)
        self.source.chmod(0o640)
        with mock.patch.object(m, 'probe', side_effect=[make_info(), make_info(codec='hevc')]), \
             mock.patch.object(m.subprocess, 'run', side_effect=encode):
            self.assertEqual(self.process(), ('converted', 15))
        self.assertEqual(self.source.stat().st_mode & 0o777, 0o640)

    def test_failed_csv_does_not_report_installed_output_as_failed_encode(self):
        with mock.patch.object(m, 'probe', side_effect=[make_info(), make_info(codec='hevc')]), \
             mock.patch.object(m.subprocess, 'run', side_effect=self.encode), \
             mock.patch.object(m, 'append_summary_log', side_effect=OSError('disk full')):
            self.assertEqual(self.process(), ('converted_with_warnings', 50))
        self.assertEqual(self.source.read_bytes(), b'y' * 50)

    def test_validation_failure_preserves_original_and_cleans_temp(self):
        with mock.patch.object(m, 'probe', side_effect=[make_info(), make_info(codec='hevc', duration=0)]), \
             mock.patch.object(m.subprocess, 'run', side_effect=self.encode):
            self.assertEqual(self.process(), ('failed', 0))
        self.assertEqual(self.source.read_bytes(), b'x' * 100)
        self.assertEqual(list(self.root.glob('*.__optimising__*')), [])

    def test_install_refuses_dangling_symlink(self):
        temp = self.root / 'temp.mp4'
        temp.write_bytes(b'encoded')
        target = self.root / 'output.mp4'
        target.symlink_to(self.root / 'missing')
        with self.assertRaises(FileExistsError):
            m.install_output(self.source, target, temp, False)
        self.assertTrue(target.is_symlink())
        self.assertEqual(self.source.read_bytes(), b'x' * 100)

    def test_video_loss_and_invalid_durations_are_rejected(self):
        for duration in (0, float('nan'), float('inf')):
            self.assertTrue(m.validate_output(make_info(), make_info(codec='hevc', duration=duration)))
        errors = m.validate_output(make_info(video_streams=[{}, {}]), make_info(codec='hevc'))
        self.assertIn('video streams: 2 -> 1', errors)

    def test_replaced_file_with_preserved_size_and_mtime_invalidates_cache(self):
        store = m.StateStore(self.root / 'state.sqlite')
        self.addCleanup(store.close)
        fingerprint = m.source_fingerprint(self.source)
        store.cache_rejection(self.source, fingerprint, store.signature, 95, 5)
        replacement = self.root / 'replacement'
        replacement.write_bytes(b'z' * 100)
        os.utime(replacement, ns=(self.source.stat().st_atime_ns, fingerprint['mtime_ns']))
        replacement.replace(self.source)
        self.assertIsNone(store.cached_rejection(self.source, m.source_fingerprint(self.source), store.signature))

    def test_interruption_is_recorded_with_started_attempt(self):
        script_dir = self.root / 'scripts'
        script_dir.mkdir()
        with mock.patch.object(sys, 'argv', ['optimiser', str(self.root), '--replace-originals']), \
             mock.patch.object(m, '__file__', str(script_dir / 'mp4_optimiser.py')), \
             mock.patch.object(m, 'require_binary'), \
             mock.patch.object(m, 'run_capture', return_value=mock.Mock(stdout='test encoder')), \
             mock.patch.object(m, 'process_file', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                m.main()
        self.assertFalse((self.root / 'mp4_optimiser.sqlite').exists())
        with closing(sqlite3.connect(script_dir / 'mp4_optimiser.sqlite')) as connection:
            self.assertEqual(connection.execute('SELECT status FROM runs').fetchone()[0], 'interrupted')
            outcomes = connection.execute('SELECT outcome FROM attempts ORDER BY id').fetchall()
            self.assertEqual(outcomes, [('started',), ('interrupted',)])

    def test_rejection_reused_and_force_reencodes(self):
        store = m.StateStore(self.root / 'state.sqlite')
        self.addCleanup(store.close)
        run_id = store.start_run(self.root, self.root, {})
        def encode(command):
            Path(command[-1]).write_bytes(b'y' * 95)
            return mock.Mock(returncode=0)
        with mock.patch.object(m, 'probe', return_value=make_info()), \
             mock.patch.object(m.subprocess, 'run', side_effect=encode) as runner:
            for force in (False, False, True):
                self.process(state_store=store, run_id=run_id, force=force)
            self.assertEqual(runner.call_count, 2)

    def test_unsafe_video_policies_are_skipped(self):
        for video in ({'color_transfer': 'smpte2084'}, {'field_order': 'tt'},
                      {'disposition': {'attached_pic': 1}}):
            self.assertFalse(m.should_encode(make_info(video_streams=[video]))[0])
        self.assertFalse(m.should_encode(make_info(data_streams=[{}]))[0])

    def test_encoder_identity_invalidates_policy(self):
        self.assertNotEqual(m.policy_signature('encoder v1'), m.policy_signature('encoder v2'))

    def test_interrupted_encode_cleans_temp(self):
        def encode(command):
            self.encode(command)
            raise KeyboardInterrupt
        with mock.patch.object(m, 'probe', return_value=make_info()), \
             mock.patch.object(m.subprocess, 'run', side_effect=encode):
            with self.assertRaises(KeyboardInterrupt):
                self.process()
        self.assertEqual(self.source.read_bytes(), b'x' * 100)
        self.assertEqual(list(self.root.glob('*.__optimising__*')), [])

    def test_same_destination_cli_is_rejected_before_processing(self):
        with mock.patch.object(sys, 'argv', ['optimiser', str(self.root), str(self.root)]), \
             mock.patch.object(m, 'require_binary'), mock.patch.object(m, 'process_file') as process:
            with self.assertRaises(SystemExit) as error:
                m.main()
        self.assertEqual(error.exception.code, 2)
        process.assert_not_called()

    def test_legacy_database_migrates_and_old_cache_is_not_reused(self):
        path = self.root / 'legacy.sqlite'
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript('''
                CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at TEXT,
                    finished_at TEXT, parameters_json TEXT, source_root TEXT,
                    destination_root TEXT, videos_discovered INTEGER, converted INTEGER,
                    skipped INTEGER, failed INTEGER, bytes_saved INTEGER, runtime_seconds REAL);
                CREATE TABLE rejection_cache (source_path TEXT PRIMARY KEY,
                    source_size INTEGER, source_mtime_ns INTEGER, policy_signature TEXT,
                    rejected_at TEXT, output_size INTEGER, saving_percent REAL);
            ''')
            fingerprint = m.source_fingerprint(self.source)
            connection.execute('INSERT INTO rejection_cache VALUES (?, ?, ?, ?, ?, ?, ?)',
                               (str(self.source), 100, fingerprint['mtime_ns'],
                                m.policy_signature(), 'yesterday', 95, 5))
            connection.commit()
        store = m.StateStore(path)
        self.addCleanup(store.close)
        self.assertIsNone(store.cached_rejection(self.source, fingerprint, store.signature))
        run_id = store.start_run(self.root, self.root, {})
        self.assertEqual(store.connection.execute('SELECT status FROM runs WHERE id=?',
                                                  (run_id,)).fetchone()[0], 'running')


if __name__ == '__main__':
    unittest.main()
