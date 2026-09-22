import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';
import { randomUUID } from 'node:crypto';
import { NextRequest } from 'next/server';
import { proxy } from '../proxy';
import type { SessionIdentity } from '../lib/auth';
import { db } from '../lib/db';
import { MAX_UPLOAD_BYTES, completeUpload, listSubmissions, moderateSubmission, readRequestJson,
  readReceipt, requireSameOrigin, reserveSubmission, submissionMetadata, submissionsEnabled, uploadPermission, withdrawSubmission } from '../lib/submissions';
import { publicSubmissionsPreviewUrl } from '../lib/preview';
import { totalCost, formatLatency, submissionChartRow, submissionMetrics } from '../lib/results';
import type { Submission } from '../lib/submissions';

import { browserIdentity, newBrowser, receiptToken, validReceipt, verifyBot, turnstileConfigured, requestIpHash } from '../lib/submission-access';

const url = process.env.DOLPHINBENCH_TEST_DATABASE_URL;
if (!url || new URL(url).hostname !== '127.0.0.1' || new URL(url).port !== '55439'
    || new URL(url).pathname !== '/submissions') throw new Error('Use the isolated submission test database on 127.0.0.1:55439.');
process.env.DATABASE_URL = url;
process.env.CRON_SECRET = 'local-submission-test-secret-32-characters';
process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY = 'test-site';
process.env.TURNSTILE_SECRET_KEY = 'test-secret';
const browser = newBrowser();
const sql = db();
const owner: SessionIdentity = { accountId: randomUUID(), username: 'owner', label: 'Owner', role: 'partner' };
const other: SessionIdentity = { accountId: randomUUID(), username: 'other', label: 'Other', role: 'partner' };
const admin: SessionIdentity = { accountId: randomUUID(), username: 'admin', label: 'Admin', role: 'admin' };
const metadata = { name: 'My run', harness: 'Custom harness', model: 'Custom model', memory: 'Custom memory',
  filename: 'submission.zip', size: 500, confirm: true };

test('public result metrics use complete execution measurements without inventing missing values', () => {
  const summary: NonNullable<Submission['summary']> = {
    tests: 600, passes: 300, passes_by_persona: { alex: 100, morgan: 100, riley: 100 },
    total_cost_usd: 112,
    ingestion: { total_cost_usd: 100, estimated_model_cost_usd: 99 }, grading: { estimated_model_cost_usd: 99 },
    execution: { total_cost_usd: 12, records: 600, estimated_model_cost_usd: 6, unpriced_model_responses: 0, median_duration_ms: 1500 },
  };
  assert.deepEqual(submissionMetrics(summary), { totalCost: 112, medianLatency: 1.5, p95Latency: null });
  const submission = { id: 'test', name: 'Test run', memory: 'Memory', model: 'Model', harness: 'Harness', status: 'accepted', summary };
  const point = submissionChartRow(submission)!;
  assert.equal(submissionChartRow({ ...submission, release_sha256: 'old' }, 'current'), null);
  assert.ok(submissionChartRow({ ...submission, release_sha256: 'current' }, 'current'));
  assert.equal(point.id, 'submission:test');
  assert.equal(point.submission?.name, 'Test run');
  assert.equal(point.pass_rate, .5);
  assert.equal(totalCost(point), 112);
  assert.equal(point.total_cost_usd_test_calls, 6);
  assert.equal(point.model.provider, 'Not reported');
  assert.equal(submissionChartRow({ ...submission, status: 'removed' }), null);
  assert.equal(submissionChartRow({ ...submission, summary: null }), null);
  assert.equal(submissionChartRow({ ...submission, summary: { ...summary, tests: 200 } }), null);
  for (const passes of [-1, 601, NaN, 1.5]) {
    assert.equal(submissionChartRow({ ...submission, summary: { ...summary, passes } }), null);
  }
  assert.equal(formatLatency(.001), '1 ms');
  assert.equal(formatLatency(null), 'Unavailable');
  assert.equal(submissionMetrics(null).totalCost, null);
  for (const value of [null, undefined, -1, NaN, Infinity, '6']) {
    summary.execution.total_cost_usd = value;
    assert.equal(submissionMetrics(summary).totalCost, null);
  }
  summary.total_cost_usd = 0;
  summary.ingestion.total_cost_usd = 0;
  summary.execution.total_cost_usd = 0;
  assert.equal(submissionMetrics(summary).totalCost, 0);
  summary.execution.unpriced_model_responses = 1;
  assert.equal(submissionMetrics(summary).totalCost, 0);
  summary.execution.records = 599;
  assert.equal(submissionMetrics(summary).medianLatency, null);
});

test('live results preview is local-only, public-only, and disables submission writes', () => {
  const keys = ['NODE_ENV', 'DOLPHINBENCH_LOCAL_PREVIEW', 'DOLPHINBENCH_PUBLIC_SUBMISSIONS_ORIGIN',
    'DOLPHINBENCH_SUBMISSIONS_ENABLED', 'BLOB_READ_WRITE_TOKEN'];
  const saved = keys.map(key => process.env[key]);
  try {
    Object.assign(process.env, { NODE_ENV: 'development', DOLPHINBENCH_SUBMISSIONS_ENABLED: '1', BLOB_READ_WRITE_TOKEN: 'test-only' });
    delete process.env.DOLPHINBENCH_PUBLIC_SUBMISSIONS_ORIGIN;
    assert.equal(submissionsEnabled(), true);
    process.env.DOLPHINBENCH_LOCAL_PREVIEW = '1';
    process.env.DOLPHINBENCH_PUBLIC_SUBMISSIONS_ORIGIN = 'https://example.test';
    assert.equal(publicSubmissionsPreviewUrl(new URL('http://127.0.0.1:3110/api/submissions/?view=public&page=2'))?.href,
      'https://example.test/api/submissions/?view=public&page=2');
    assert.equal(submissionsEnabled(), false);
    for (const request of ['http://localhost/api/submissions/?view=admin',
      'http://localhost/api/submissions/?view=mine', 'https://public.test/api/submissions/?view=public']) {
      assert.equal(publicSubmissionsPreviewUrl(new URL(request)), null);
    }
    Object.assign(process.env, { NODE_ENV: 'production' });
    assert.equal(publicSubmissionsPreviewUrl(new URL('http://localhost/api/submissions/?view=public')), null);
  } finally {
    keys.forEach((key, index) => { if (saved[index] === undefined) delete process.env[key]; else process.env[key] = saved[index]; });
  }
});

before(async () => {
  for (const account of [owner, other, admin]) await sql`
    INSERT INTO partner_accounts (id, username, label, role, password_hash)
    VALUES (${account.accountId}, ${account.username}, ${account.label}, ${account.role}, 'test-only')`;
});
beforeEach(async () => { await sql`DELETE FROM submissions`; });
after(async () => {
  await sql`DELETE FROM submissions`;
  await sql`DELETE FROM partner_accounts WHERE id IN ${sql([owner.accountId, other.accountId, admin.accountId])}`;
  await sql.end();
});

test('requires meaningful metadata, consent, and bounded ZIP size', () => {
  assert.equal(submissionMetadata(metadata).model, 'Custom model');
  for (const change of [{ name: '' }, { model: 'x'.repeat(201) }, { confirm: false }, { size: -1 },
    { size: MAX_UPLOAD_BYTES + 1 }, { size: 1.2 }, { filename: '../x.zip' }, { filename: 'x.exe' }]) {
    assert.throws(() => submissionMetadata({ ...metadata, ...change }));
  }
});

test('browser and receipt credentials cannot be forged or substituted', () => {
  assert.equal(browserIdentity(browser.cookie), browser.hash);
  assert.equal(browserIdentity(browser.cookie.slice(0, -1) + '!'), null);
  assert.equal(browserIdentity(newBrowser().cookie) === browser.hash, false);
  const id = randomUUID();
  assert.equal(validReceipt(id, receiptToken(id)), true);
  assert.equal(validReceipt(randomUUID(), receiptToken(id)), false);
  assert.equal(validReceipt(id, browser.cookie), false);
  const request = new Request('http://localhost/api', { headers: { 'x-forwarded-for': 'spoofed' } });
  assert.equal(requestIpHash(request), requestIpHash(new Request('http://localhost/api')));
});

test('benchmark pages are public without opening administrator access', async () => {
  for (const path of ['/', '/about/', '/dataset/', '/leaderboard/', '/leaderboard/results.json/',
    '/leaderboard/evidence/example.tar.gz/', '/personas/morgan/', '/personas/morgan/tests/001/',
    '/data/morgan.json', '/run/', '/run/guide/', '/run/template/', '/run/repo/examples/harness_template.py/',
    '/run/receipt/']) {
    const response = await proxy(new NextRequest(`https://example.test${path}`));
    assert.equal(response.headers.get('x-middleware-next'), '1', path);
    assert.equal(response.headers.get('location'), null, path);
  }
  assert.equal((await proxy(new NextRequest('https://example.test/admin/'))).headers.get('location'), 'https://example.test/login/');
  assert.equal((await proxy(new NextRequest('https://example.test/api/events/'))).status, 401);
});

test('bot verification requires success, the correct hostname and action; production rejects test keys', async () => {
  const request = new Request('https://internal.test/api', { headers: { Origin: 'https://example.test' } });
  for (const [response, expected] of [
    [{ success: true, hostname: 'example.test', action: 'submit-run' }, true],
    [{ success: false, hostname: 'example.test', action: 'submit-run' }, false],
    [{ success: true, hostname: 'other.test', action: 'submit-run' }, false],
    [{ success: true, hostname: 'example.test', action: 'other' }, false],
  ] as const) {
    assert.equal(await verifyBot(request, 'token', async () => Response.json(response)), expected);
  }
  assert.equal(await verifyBot(request, '', async () => { throw new Error('Must not contact service'); }), false);
  await assert.rejects(verifyBot(request, 'token', async () => { throw new Error('Network failed'); }));
  process.env.VERCEL_ENV = 'production';
  process.env.TURNSTILE_SECRET_KEY = '1x0000000000000000000000000000000AA';
  assert.equal(turnstileConfigured(), false);
  delete process.env.VERCEL_ENV;
  process.env.TURNSTILE_SECRET_KEY = 'test-secret';
});

test('clearing the browser cookie cannot bypass the IP upload limit', async () => {
  for (let i = 0; i < 20; i++) await reserveSubmission(newBrowser().hash, 'shared-ip', metadata);
  await assert.rejects(reserveSubmission(newBrowser().hash, 'shared-ip', metadata), /network/);
});

test('receipt access is private and permits withdrawal without an account', async () => {
  const run = await reserveSubmission(browser.hash, 'ip-one', { ...metadata, contact_email: 'researcher@example.test' });
  assert.equal((await readReceipt(run.id, run.receipt)).name, metadata.name);
  const owned = (await listSubmissions(null, 'mine', 0, browser.hash)).submissions;
  assert.equal(owned[0].receipt, run.receipt);
  assert.equal(owned[0].contact_email, null);
  assert.equal((await listSubmissions(admin, 'admin', 0)).submissions[0].contact_email, 'researcher@example.test');
  await assert.rejects(withdrawSubmission(run.id, 'invalid'), /invalid/);
  await withdrawSubmission(run.id, run.receipt);
  assert.equal((await readReceipt(run.id, run.receipt)).status, 'removed');
  assert.equal((await sql`SELECT account_id FROM submissions WHERE id = ${run.id}`)[0].account_id, null);
});

test('checks origin and bounds actual request bytes rather than trusting Content-Length', async () => {
  assert.throws(() => requireSameOrigin(new Request('https://example.test/api', { headers: { Origin: 'https://evil.test' } })));
  requireSameOrigin(new Request('https://example.test/api', { headers: { Origin: 'https://example.test' } }));
  requireSameOrigin(new Request('http://localhost:3110/api', { headers: { Host: '127.0.0.1:3110', Origin: 'http://127.0.0.1:3110' } }));
  assert.throws(() => requireSameOrigin(new Request('https://internal.test/api', { headers: { Host: 'example.test', Origin: 'https://evil.test' } })));
  await assert.rejects(readRequestJson(new Request('https://example.test/api', { method: 'POST',
    headers: { 'Content-Length': '1' }, body: ' '.repeat(17000) })), /too large/);
  await assert.rejects(readRequestJson(new Request('https://example.test/api', { method: 'POST', body: 'invalid' })), /Invalid JSON/);
});

test('concurrent reservations cannot exceed per-browser pending quota', async () => {
  const results = await Promise.allSettled(Array.from({ length: 6 }, () => reserveSubmission(browser.hash, 'ip-one', metadata)));
  assert.equal(results.filter(result => result.status === 'fulfilled').length, 2);
  assert.equal(Number((await sql`SELECT count(*) AS n FROM submissions`)[0].n), 2);
});

test('daily quota counts failed submissions too', async () => {
  for (let i = 0; i < 5; i++) {
    await reserveSubmission(browser.hash, 'ip-one', metadata);
    await sql`UPDATE submissions SET status = 'rejected'`;
  }
  await assert.rejects(reserveSubmission(browser.hash, 'ip-one', metadata), /Limit reached/);
});

test('upload tokens authorize one owner, one immutable pathname, exact size and expiry', async () => {
  const run = await reserveSubmission(browser.hash, 'ip-one', metadata);
  const permission = await uploadPermission(run.receipt, run.id, run.pathname);
  assert.equal(permission.maximumSizeInBytes, metadata.size);
  assert.equal(permission.allowOverwrite, false);
  assert.equal(permission.addRandomSuffix, false);
  await assert.rejects(uploadPermission('invalid', run.id, run.pathname), /Invalid upload receipt/);
  await assert.rejects(uploadPermission(run.receipt, run.id, 'arbitrary.zip'), /not yours/);
  await sql`UPDATE submissions SET upload_expires_at = now() - interval '1 second'`;
  await assert.rejects(uploadPermission(run.receipt, run.id, run.pathname), /expired/);
  await assert.rejects(completeUpload(run.id, undefined,
    async () => { throw new Error('Expired uploads must not contact storage'); }), /expired/);
});

test('completion authenticates storage metadata and queues once without publishing', async () => {
  const run = await reserveSubmission(browser.hash, 'ip-one', metadata);
  const blob = { pathname: run.pathname, size: 500, url: `https://fixture.private.blob.vercel-storage.com/${run.pathname}` };
  const inspect = async () => blob as any;
  await assert.rejects(readReceipt(run.id, 'invalid'), /invalid/);
  await assert.rejects(completeUpload(run.id, undefined,
    async () => ({ ...blob, size: 501 }) as any), /does not match/);
  await assert.rejects(completeUpload(run.id, undefined,
    async () => ({ ...blob, url: 'https://attacker.test/file' }) as any), /does not match/);
  await completeUpload(run.id, undefined, inspect);
  await completeUpload(run.id, undefined, async () => { throw new Error('Should not inspect twice'); });
  assert.equal((await sql`SELECT status FROM submissions WHERE id = ${run.id}`)[0].status, 'queued');
  assert.equal((await listSubmissions(null, 'public', 0)).submissions.length, 0);
});

test('lists only owner records; publication excludes raw URLs and private errors; admin removal is immediate', async () => {
  const run = await reserveSubmission(browser.hash, 'ip-one', metadata);
  assert.equal((await listSubmissions(null, 'mine', 0, 'other-browser')).submissions.length, 0);
  await assert.rejects(listSubmissions(owner, 'admin', 0), /Administrator/);
  await sql`UPDATE submissions SET status = 'accepted', blob_url = 'private-url', blob_sha256 = 'hash',
    release_sha256 = 'release-hash', validator_sha256 = 'code-hash', summary = '{"passes":0,"tests":600}', error_message = 'private-error'`;
  assert.equal((await listSubmissions(null, 'public', 0)).submissions.length, 0);
  assert.equal((await listSubmissions(null, 'mine', 0, browser.hash)).submissions.length, 1);
  await sql`UPDATE submissions SET summary = '{"passes":0,"tests":600,"total_cost_usd":3,"ingestion":{"total_cost_usd":1},"execution":{"total_cost_usd":2}}' WHERE id = ${run.id}`;
  const published = (await listSubmissions(null, 'public', 0)).submissions;
  assert.equal(published.length, 1);
  assert.equal(published[0].error_message, null);
  assert.equal('blob_url' in published[0], false);
  await assert.rejects(moderateSubmission(owner, run.id, { action: 'remove', reason: 'Suspect' }), /Administrator/);
  await moderateSubmission(admin, run.id, { action: 'remove', reason: 'Inconsistent evidence' });
  assert.equal((await listSubmissions(null, 'public', 0)).submissions.length, 0);
  assert.equal((await sql`SELECT removal_reason FROM submissions WHERE id = ${run.id}`)[0].removal_reason, 'Inconsistent evidence');
});
