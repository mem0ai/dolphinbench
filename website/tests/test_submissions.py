"""Exercise the actual bounded validator and its durable Postgres transitions."""

import copy
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'website'))
from api import submission_validator as worker
from harness import submission
from tests.unit.submissions import test_submission as fixtures


class ValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from scripts.build_run_docs import build_validator
        build_validator()
        cls.directory = tempfile.TemporaryDirectory()
        cls.runtime = Path(cls.directory.name) / 'runtime'
        shutil.copytree(worker.RUNTIME, cls.runtime)
        cls.releases, cls.ingestion, cls.tests = fixtures.fixture()
        raw = json.dumps(cls.releases).encode()
        (cls.runtime / 'release.json').write_bytes(raw)
        info = json.loads((cls.runtime / 'build.json').read_text())
        info['release_json_sha256'] = hashlib.sha256(raw).hexdigest()
        (cls.runtime / 'build.json').write_text(json.dumps(info))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def archive(self, directory, tests=None):
        path = Path(directory) / 'submission.zip'
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('ingestion.json', json.dumps(self.ingestion))
            archive.writestr('tests.json', json.dumps(tests or self.tests))
        return path

    def test_valid_recording_is_accepted_and_inconsistent_grades_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.archive(directory)
            result = worker.run_validation(path, self.runtime)
            self.assertEqual(result['status'], 'accepted')
            self.assertEqual(result['summary']['passes'], 600)
            tests = copy.deepcopy(self.tests)
            tests['tests'][0]['grading'][0]['passed'] = False
            result = worker.run_validation(self.archive(directory, tests), self.runtime)
            self.assertEqual(result['status'], 'rejected')
            self.assertIn('supplied grades disagree', result['error_message'])

    def test_timeouts_and_broken_runtime_remain_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.archive(directory)
            self.assertEqual(worker.run_validation(path, self.runtime, timeout=.001)['status'], 'queued')
            self.assertEqual(worker.run_validation(path, Path(directory) / 'missing')['status'], 'queued')

    def test_download_checks_storage_identity_and_actual_bytes(self):
        row = {'pathname': 'submissions/preview/test/submission.zip', 'expected_size': 4}
        metadata = SimpleNamespace(pathname=row['pathname'], size=4,
                                   url='https://test.private.blob.vercel-storage.com/submission.zip')
        blob = Mock()
        blob.head.return_value = metadata
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'submission.zip'
            with patch.object(worker.urllib.request, 'build_opener') as opener:
                opener.return_value.open.return_value = io.BytesIO(b'test')
                self.assertEqual(worker.download_archive(blob, row, target, 'test-secret'), hashlib.sha256(b'test').hexdigest())
                request = opener.return_value.open.call_args.args[0]
                self.assertEqual(request.get_header('Authorization'), 'Bearer test-secret')
                self.assertEqual(target.read_bytes(), b'test')
                for payload in (b'too long', b'bad'):
                    target.unlink()
                    opener.return_value.open.return_value = io.BytesIO(payload)
                    with self.assertRaises(RuntimeError):
                        worker.download_archive(blob, row, target, 'test-secret')
                for url in ('https://attacker.test/file', 'http://test.private.blob.vercel-storage.com/file',
                            'https://test.private.blob.vercel-storage.com:444/file'):
                    metadata.url = url
                    opener.reset_mock()
                    with self.assertRaises(RuntimeError):
                        worker.download_archive(blob, row, target, 'test-secret')
                    opener.assert_not_called()

    def test_child_has_no_credentials_and_enforces_memory_cpu_and_network_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / 'runtime'
            shutil.copytree(self.runtime, runtime)
            target = runtime / 'graders/__init__.py'
            original = target.read_text()
            for extra, expected in (
                ("\nimport os, resource\nassert 'AZURE_OPENAI_API_KEY' not in os.environ\n"
                 "assert 'BLOB_READ_WRITE_TOKEN' not in os.environ\n"
                 f"assert resource.getrlimit(resource.RLIMIT_AS)[0] == {worker.MEMORY_BYTES}\n"
                 f"assert resource.getrlimit(resource.RLIMIT_CPU)[0] == {worker.CPU_SECONDS}\n", 'accepted'),
                ("\nimport socket\nsocket.create_connection(('127.0.0.1', 9))\n", 'queued'),
                (f"\nbytearray({worker.MEMORY_BYTES * 2})\n", 'queued'),
            ):
                with self.subTest(expected=expected, extra=extra[:30]):
                    target.write_text(original + extra)
                    info = json.loads((runtime / 'build.json').read_text())
                    info['files']['graders/__init__.py'] = hashlib.sha256(target.read_bytes()).hexdigest()
                    (runtime / 'build.json').write_text(json.dumps(info))
                    with patch.dict(os.environ, {'AZURE_OPENAI_API_KEY': 'test-provider-secret', 'BLOB_READ_WRITE_TOKEN': 'test-blob-secret'}):
                        result = worker.run_validation(self.archive(directory), runtime)
                    self.assertEqual(result['status'], expected)

    def test_full_published_release_validates_under_process_limits(self):
        original = submission.write_zip
        def check(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            checked = worker.run_validation(path)
            self.assertEqual(checked['status'], 'accepted', checked)
            self.assertEqual(checked['summary']['tests'], 600)
            self.assertEqual(checked['summary']['ingestion']['records'], 13539)
            return result
        with patch.object(submission, 'write_zip', side_effect=check):
            fixtures.ExporterTests('test_full_released_history_and_tests_round_trip_with_synthetic_responses').debug()


@unittest.skipUnless(os.environ.get('DOLPHINBENCH_TEST_DATABASE_URL'), 'Isolated submission database not configured')
class QueueTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        from urllib.parse import urlsplit
        url = os.environ['DOLPHINBENCH_TEST_DATABASE_URL']
        parsed = urlsplit(url)
        if (parsed.hostname, parsed.port, parsed.path) != ('127.0.0.1', 55439, '/submissions'):
            raise RuntimeError('Refusing to use a non-test database')
        self.connection = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        self.connection.execute('DELETE FROM submissions')
        self.owner = uuid.uuid4()
        self.connection.execute("INSERT INTO partner_accounts (id, username, label, password_hash) VALUES (%s,%s,'Test','test')",
                                (self.owner, str(self.owner)))

    def tearDown(self):
        self.connection.execute('DELETE FROM submissions')
        self.connection.execute('DELETE FROM partner_accounts WHERE id = %s', (self.owner,))
        self.connection.close()

    def queued(self):
        identity = uuid.uuid4()
        self.connection.execute("""INSERT INTO submissions
          (id, account_id, name, harness, model, memory, filename, expected_size, pathname, status, blob_url)
          VALUES (%s,%s,'Run','Harness','Model','Memory','submission.zip',10,%s,'queued','private-url')""",
                                (identity, self.owner, f'submissions/preview/{identity}/submission.zip'))
        return identity

    def test_only_one_job_is_claimed_and_expired_leases_are_recoverable(self):
        self.queued(); self.queued()
        first = worker.claim(self.connection)
        self.assertIsNotNone(first)
        self.assertIsNone(worker.claim(self.connection))
        self.connection.execute("UPDATE submissions SET lease_until = now() - interval '1 second' WHERE id = %s", (first['id'],))
        second = worker.claim(self.connection)
        self.assertEqual(second['id'], first['id'])
        self.assertNotEqual(second['lease'], first['lease'])
        worker.finish(self.connection, first, {'status': 'rejected', 'error_message': 'Stale worker'}, None)
        self.assertEqual(self.connection.execute('SELECT status FROM submissions WHERE id = %s', (first['id'],)).fetchone()['status'], 'validating')

    def test_duplicate_archives_and_admin_removal_cannot_publish_again(self):
        result = {'status': 'accepted', 'summary': {'passes': 0, 'tests': 600}, 'release_sha256': 'release', 'validator_sha256': 'code'}
        self.queued()
        first = worker.claim(self.connection)
        worker.finish(self.connection, first, result, 'same-hash')
        self.queued()
        second = worker.claim(self.connection)
        worker.finish(self.connection, second, result, 'same-hash')
        self.assertEqual(self.connection.execute('SELECT status FROM submissions WHERE id = %s', (second['id'],)).fetchone()['status'], 'rejected')
        self.queued()
        third = worker.claim(self.connection)
        self.connection.execute("UPDATE submissions SET status = 'removed', lease = NULL WHERE id = %s", (third['id'],))
        worker.finish(self.connection, third, result, 'different-hash')
        self.assertEqual(self.connection.execute('SELECT status FROM submissions WHERE id = %s', (third['id'],)).fetchone()['status'], 'removed')

    def test_three_interrupted_attempts_stay_pending_without_automatic_relaunch(self):
        self.queued()
        for attempt in range(3):
            row = worker.claim(self.connection)
            self.assertEqual(row['attempts'], attempt + 1)
            self.connection.execute("UPDATE submissions SET lease_until = now() - interval '1 second'")
        self.assertIsNone(worker.claim(self.connection))
        self.assertEqual(self.connection.execute('SELECT status FROM submissions').fetchone()['status'], 'queued')

    def test_cleanup_preserves_accepted_archives_and_waits_for_upload_tokens_to_expire(self):
        from unittest.mock import Mock
        identity = self.queued()
        self.connection.execute("UPDATE submissions SET status = 'removed', updated_at = now() - interval '8 days'")
        blob = Mock()
        worker.cleanup(self.connection, blob)
        blob.delete.assert_not_called()
        self.connection.execute("UPDATE submissions SET upload_expires_at = now() - interval '1 second'")
        worker.cleanup(self.connection, blob)
        blob.delete.assert_called_once()
        self.assertTrue(self.connection.execute('SELECT blob_deleted FROM submissions WHERE id = %s', (identity,)).fetchone()['blob_deleted'])


if __name__ == '__main__':
    unittest.main()
