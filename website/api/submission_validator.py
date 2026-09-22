"""Validate queued ZIPs on Vercel, without executing submitted code or model calls."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / '.validation'
MAX_UPLOAD_BYTES = 256 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MEMORY_BYTES = 2 * 1024 ** 3
CPU_SECONDS = 180
WALL_SECONDS = 240


def validate_child(archive: Path, runtime: Path) -> dict:
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    def deny_network(event, args):
        if event in {'socket.connect', 'socket.getaddrinfo', 'socket.bind', 'subprocess.Popen', 'os.system'}:
            raise RuntimeError('External execution is disabled during submission validation')

    sys.addaudithook(deny_network)
    info = json.loads((runtime / 'build.json').read_bytes())
    raw = (runtime / 'release.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != info['release_json_sha256']:
        raise RuntimeError('Validator release bundle changed')
    for relative, digest in info['files'].items():
        if hashlib.sha256((runtime / relative).read_bytes()).hexdigest() != digest:
            raise RuntimeError('Validator code bundle changed')
    sys.path.insert(0, str(runtime))
    from harness.submission import SubmissionError, read_zip, validate
    releases = json.loads(raw)
    del raw
    pricing = json.loads((runtime / 'pricing.json').read_bytes())
    try:
        summary = validate(*read_zip(archive), releases, pricing=pricing)
    except SubmissionError as exc:
        if isinstance(exc.__cause__, OSError):
            raise RuntimeError('Could not read staged upload') from exc
        return {'status': 'rejected', 'error_code': 'invalid_evidence', 'error_message': str(exc)[:1000]}
    return {'status': 'accepted', 'summary': summary,
            'release_sha256': info['release_sha256'], 'validator_sha256': info['validator_sha256']}


def run_validation(archive: Path, runtime: Path = RUNTIME, *, timeout: float = WALL_SECONDS) -> dict:
    # Use a fresh interpreter: no database/storage/provider credentials reach it.
    with tempfile.TemporaryDirectory(prefix='dolphinbench-validator-') as directory:
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                [sys.executable, '-I', str(Path(__file__).resolve()), '--child', str(archive.resolve()), str(runtime.resolve())],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                cwd=directory, env={'HOME': directory, 'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1'},
                start_new_session=True,
            )
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                return {'status': 'queued', 'error_code': 'validation_timeout',
                        'error_message': 'Validation exceeded its time limit. The run remains pending.'}
            if process.returncode:
                return {'status': 'queued', 'error_code': 'validation_failed',
                        'error_message': 'Validation could not finish. The run remains pending for retry.'}
            output.seek(0)
            raw = output.read(MAX_OUTPUT_BYTES + 1)
            if len(raw) > MAX_OUTPUT_BYTES:
                raise RuntimeError('Validator output exceeded its limit')
            result = json.loads(raw)
            if result.get('status') not in {'accepted', 'rejected'}:
                raise RuntimeError('Invalid validator response')
            return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('Storage download redirected')


def download_archive(blob, row: dict, target: Path, token: str) -> str:
    from urllib.parse import urlsplit
    metadata = blob.head(row['pathname'])
    url = urlsplit(metadata.url)
    if (metadata.pathname != row['pathname'] or metadata.size != row['expected_size']
            or metadata.size > MAX_UPLOAD_BYTES or url.scheme != 'https' or url.port is not None
            or url.username is not None or not re.fullmatch(r'[a-z0-9]+\.private\.blob\.vercel-storage\.com', url.hostname or '')):
        raise RuntimeError('Storage object does not match submission')
    request = urllib.request.Request(metadata.url, headers={'Authorization': f'Bearer {token}', 'Accept-Encoding': 'identity'})
    digest = hashlib.sha256()
    total = 0
    deadline = time.monotonic() + 60
    with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response, target.open('xb') as output:
        while chunk := response.read(64 * 1024):
            total += len(chunk)
            if total > min(MAX_UPLOAD_BYTES, row['expected_size']) or time.monotonic() > deadline:
                raise RuntimeError('Storage download exceeded its size or time limit')
            output.write(chunk)
            digest.update(chunk)
    if total != row['expected_size']:
        raise RuntimeError('Storage download is incomplete')
    return digest.hexdigest()


def claim(connection) -> dict | None:
    lease = uuid.uuid4()
    with connection.transaction():
        # Transaction-level locking also works with pooled Postgres connections.
        connection.execute('SELECT pg_advisory_xact_lock(8136202)')
        connection.execute("""UPDATE submissions SET status = 'queued', lease = NULL, lease_until = NULL,
          error_code = 'worker_interrupted', error_message = 'Validation was interrupted. The run remains pending.'
          WHERE status = 'validating' AND lease_until < now()""")
        if connection.execute("SELECT 1 FROM submissions WHERE status = 'validating' LIMIT 1").fetchone():
            return None
        return connection.execute("""
        UPDATE submissions SET status = 'validating', attempts = attempts + 1,
          lease = %s, lease_until = now() + interval '10 minutes', updated_at = now()
        WHERE id = (SELECT id FROM submissions WHERE attempts < 3 AND retry_after <= now()
          AND (status = 'queued' OR (status = 'validating' AND lease_until < now()))
          ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1)
        RETURNING *
        """, (lease,)).fetchone()


def finish(connection, row: dict, result: dict, digest: str | None) -> None:
    from psycopg.types.json import Jsonb
    from psycopg.errors import UniqueViolation
    try:
        connection.execute("""
          UPDATE submissions SET status = %s, summary = %s, blob_sha256 = %s,
            release_sha256 = %s, validator_sha256 = %s, error_code = %s, error_message = %s,
            lease = NULL, lease_until = NULL, retry_after = now() + interval '2 minutes', updated_at = now()
          WHERE id = %s AND status = 'validating' AND lease = %s
        """, (result['status'], Jsonb(result['summary']) if result.get('summary') else None, digest,
              result.get('release_sha256'), result.get('validator_sha256'), result.get('error_code'),
              result.get('error_message'), row['id'], row['lease']))
    except UniqueViolation:
        finish(connection, row, {'status': 'rejected', 'error_code': 'duplicate_archive',
                                'error_message': 'This exact ZIP already has an accepted submission.'}, digest)


def cleanup(connection, blob) -> None:
    connection.execute("""UPDATE submissions SET status = 'rejected', error_code = 'upload_expired',
      error_message = 'Upload expired. Submit the ZIP again.', updated_at = now()
      WHERE status = 'uploading' AND upload_expires_at < now()""")
    rows = connection.execute("""SELECT id, pathname FROM submissions WHERE NOT blob_deleted
      AND ((status = 'rejected' AND error_code = 'upload_expired') OR
           (status IN ('rejected','removed') AND updated_at < now() - interval '7 days'))
      AND upload_expires_at < now() ORDER BY created_at LIMIT 10""").fetchall()
    for row in rows:
        blob.delete(row['pathname'])
        connection.execute('UPDATE submissions SET blob_deleted = true WHERE id = %s', (row['id'],))


def process_queue() -> dict:
    import psycopg
    from psycopg.rows import dict_row
    from vercel.blob import BlobClient

    token = os.environ['BLOB_READ_WRITE_TOKEN']
    with psycopg.connect(os.environ.get('DATABASE_URL') or os.environ['POSTGRES_URL'],
                        autocommit=True, row_factory=dict_row, connect_timeout=10) as connection:
        with BlobClient(token=token) as blob:
            try:
                cleanup(connection, blob)
            except Exception as exc:
                print(f'Submission cleanup failed: {type(exc).__name__}', file=sys.stderr)
            row = claim(connection)
            if not row:
                return {'status': 'idle'}
            digest = None
            try:
                with tempfile.TemporaryDirectory(prefix='dolphinbench-upload-') as directory:
                    path = Path(directory) / 'submission.zip'
                    digest = download_archive(blob, row, path, token)
                    result = run_validation(path)
            except Exception as exc:
                print(f'Submission validation failed: {type(exc).__name__}', file=sys.stderr)
                result = {'status': 'queued', 'error_code': 'service_error',
                          'error_message': 'The validation service could not finish. Your run remains pending.'}
            finish(connection, row, result, digest)
            return {'status': result['status']}


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        secret = os.environ.get('CRON_SECRET', '')
        if not secret or not hmac.compare_digest(self.headers.get('Authorization', ''), f'Bearer {secret}'):
            self.reply(401, {'error': 'Unauthorized'})
            return
        if os.environ.get('DOLPHINBENCH_SUBMISSIONS_ENABLED') != '1':
            self.reply(503, {'error': 'Submissions are disabled'})
            return
        try:
            self.reply(200, process_queue())
        except Exception as exc:
            print(f'Submission worker failed: {type(exc).__name__}', file=sys.stderr)
            self.reply(503, {'error': 'Validation is temporarily unavailable'})

    def reply(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--child':
        print(json.dumps(validate_child(Path(sys.argv[2]), Path(sys.argv[3])), allow_nan=False))
    else:
        from http.server import HTTPServer
        HTTPServer(('127.0.0.1', int(os.environ.get('PORT', '3113'))), handler).serve_forever()
