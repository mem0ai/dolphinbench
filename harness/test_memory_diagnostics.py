import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from harness.memory_diagnostics import Recorder, instrument_httpx, wrap_method


class MemoryDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'events.jsonl'
        self.recorder = Recorder(self.path)
        self.addCleanup(__import__('os').close, self.recorder.fd)

    def rows(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_wrapper_preserves_return_exception_and_hides_contents(self):
        expected = {'summary': 'private memory'}
        class Manager:
            def get_prefetch_context(self, query):
                return expected
            def fail(self):
                raise ValueError('private credential')
        wrap_method(Manager, 'get_prefetch_context', self.recorder)
        wrap_method(Manager, 'fail', self.recorder)
        self.assertIs(Manager().get_prefetch_context('private query'), expected)
        with self.assertRaisesRegex(ValueError, 'private credential'):
            Manager().fail()
        text = self.path.read_text()
        self.assertNotIn('private', text)
        self.assertEqual(self.rows()[1]['field_chars'], {'summary': 14})
        self.assertEqual(self.rows()[-1]['error_type'], 'ValueError')

    def test_http_keeps_payload_timeout_and_records_status_without_secrets(self):
        captured = []
        def handler(request):
            captured.append(request)
            return httpx.Response(503, text='private response')
        original = httpx.Client.send
        with patch.object(httpx.Client, 'send', original):
            instrument_httpx(httpx, self.recorder)
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                response = client.post('http://honcho/v3/workspaces/private/peers/private/chat?secret=private',
                                       json={'query': 'private'}, headers={'Authorization': 'private'}, timeout=7)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(captured[0].content), {'query': 'private'})
        self.assertEqual(captured[0].extensions['timeout']['read'], 7)
        self.assertIn('x-dolphinbench-diagnostic-id', captured[0].headers)
        self.assertNotIn('private', self.path.read_text())
        self.assertEqual(self.rows()[-1]['status'], 503)

    def test_non_honcho_http_is_not_modified_or_logged(self):
        captured = []
        def handler(request):
            captured.append(request)
            return httpx.Response(200)
        original = httpx.Client.send
        with patch.object(httpx.Client, 'send', original):
            instrument_httpx(httpx, self.recorder)
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                client.get('http://model/v1/responses')
        self.assertNotIn('x-dolphinbench-diagnostic-id', captured[0].headers)
        self.assertEqual(self.rows(), [])


if __name__ == '__main__':
    unittest.main()
