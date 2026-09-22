import { randomUUID } from 'node:crypto';
import { head } from '@vercel/blob';
import { db } from './db';
import type { SessionIdentity } from './auth';
import { receiptToken, turnstileConfigured, validReceipt } from './submission-access';
import { localPreview } from './preview';

export const MAX_UPLOAD_BYTES = 256 * 1024 * 1024;
export const PAGE_SIZE = 30;
export type Submission = {
  id: string; name: string; harness: string; model: string; memory: string;
  filename: string; status: string; created_at: string; updated_at: string;
  receipt?: string; contact_email?: string | null;
  error_message: string | null; attempts: number; removal_reason: string | null;
  blob_sha256: string | null; release_sha256: string | null; validator_sha256: string | null;
  summary: { passes: number; tests: number; passes_by_persona: Record<string, number>; total_cost_usd?: number;
    ingestion: Record<string, unknown>; execution: Record<string, unknown>; grading: Record<string, unknown> } | null;
};

export class SubmissionRequestError extends Error {
  constructor(message: string, public status = 400) { super(message); }
}

export function submissionsEnabled() {
  if (localPreview() && process.env.DOLPHINBENCH_PUBLIC_SUBMISSIONS_ORIGIN) return false;
  return process.env.DOLPHINBENCH_SUBMISSIONS_ENABLED === '1'
    && turnstileConfigured() && (process.env.CRON_SECRET?.length || 0) >= 32
    && Boolean(process.env.BLOB_READ_WRITE_TOKEN
      && (process.env.DATABASE_URL || process.env.POSTGRES_URL));
}

export function requireSubmissions() {
  if (!submissionsEnabled()) throw new SubmissionRequestError('Submissions are not open yet.', 503);
}

export function requireSameOrigin(request: Request) {
  // Next can use its internal hostname in request.url; Host identifies the site the browser addressed.
  const url = new URL(request.url);
  const expected = `${url.protocol}//${request.headers.get('host') || url.host}`;
  if (request.headers.get('origin') !== expected) {
    throw new SubmissionRequestError('This request must come from the website.', 403);
  }
}

export async function readRequestJson(request: Request): Promise<any> {
  const reader = request.body?.getReader();
  if (!reader) throw new SubmissionRequestError('Missing request body.');
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > 16 * 1024) {
        await reader.cancel();
        throw new SubmissionRequestError('Request is too large.', 413);
      }
      chunks.push(value);
    }
    try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); }
    catch { throw new SubmissionRequestError('Invalid JSON request.'); }
  } finally { reader.releaseLock(); }
}

export function requestFailure(error: unknown) {
  if (error instanceof SubmissionRequestError) {
    return Response.json({ error: error.message }, { status: error.status });
  }
  // Do not put provider responses, tokens, or uploaded content in public errors.
  console.error('Submission operation failed', error instanceof Error ? error.name : 'Unknown error');
  return Response.json({ error: 'The submission service is temporarily unavailable. Try again.' }, { status: 503 });
}

export function checkId(id: string) {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(id)) {
    throw new SubmissionRequestError('Submission not found.', 404);
  }
}

export function submissionMetadata(body: any) {
  const result: Record<string, string> = {};
  for (const [key, maximum] of Object.entries({ name: 120, harness: 120, model: 200, memory: 120, filename: 200 })) {
    const value = typeof body?.[key] === 'string' ? body[key].trim() : '';
    if (!value || value.length > maximum || /[\x00-\x1f\x7f]/.test(value)) {
      throw new SubmissionRequestError(`${key}: enter between 1 and ${maximum} characters.`);
    }
    result[key] = value;
  }
  if (!result.filename.toLowerCase().endsWith('.zip') || /[/\\]/.test(result.filename)) {
    throw new SubmissionRequestError('Choose a ZIP file.');
  }
  if (!Number.isSafeInteger(body.size) || body.size <= 0 || body.size > MAX_UPLOAD_BYTES) {
    throw new SubmissionRequestError('The ZIP must be between 1 byte and 256 MiB.');
  }
  if (body.confirm !== true) throw new SubmissionRequestError('Confirm that these are your recorded run results.');
  const email = typeof body.contact_email === 'string' ? body.contact_email.trim() : '';
  if (email && (email.length > 254 || !/^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(email))) {
    throw new SubmissionRequestError('Enter a valid contact email or leave it blank.');
  }
  return { name: result.name, harness: result.harness, model: result.model,
    memory: result.memory, filename: result.filename, size: body.size as number, contactEmail: email || null };
}

export async function reserveSubmission(browser: string, ip: string, body: unknown) {
  const data = submissionMetadata(body);
  const id = randomUUID();
  const environment = process.env.VERCEL_ENV === 'production' ? 'production' : 'preview';
  const pathname = `submissions/${environment}/${id}/submission.zip`;
  await db().begin(async sql => {
    // Serialize reservations so concurrent requests cannot spend the same quota.
    await sql`SELECT pg_advisory_xact_lock(8136201)`;
    const [counts] = await sql`
      SELECT count(*) FILTER (WHERE browser_hash = ${browser} AND created_at > now() - interval '1 day')::int AS daily,
        count(*) FILTER (WHERE ip_hash = ${ip} AND created_at > now() - interval '1 day')::int AS ip_daily,
        count(*) FILTER (WHERE created_at > now() - interval '1 day')::int AS global_daily,
        count(*) FILTER (WHERE browser_hash = ${browser} AND status IN ('uploading','queued','validating')
          AND (status <> 'uploading' OR upload_expires_at > now()))::int AS pending,
        coalesce(sum(expected_size) FILTER (WHERE NOT blob_deleted), 0)::bigint AS retained_bytes
      FROM submissions
    `;
    if (counts.daily >= 5 || counts.pending >= 2) {
      throw new SubmissionRequestError('Limit reached: 5 uploads per day and 2 pending submissions per browser.', 429);
    }
    if (counts.ip_daily >= 20) throw new SubmissionRequestError('Daily upload limit reached for this network. Try again later.', 429);
    if (counts.global_daily >= 50 || Number(counts.retained_bytes) + data.size > 50 * 1024 ** 3) {
      throw new SubmissionRequestError('Uploads are temporarily at capacity. Try again later.', 429);
    }
    await sql`INSERT INTO submissions (id, browser_hash, ip_hash, contact_email, name, harness, model, memory, filename, expected_size, pathname)
      VALUES (${id}, ${browser}, ${ip}, ${data.contactEmail}, ${data.name}, ${data.harness}, ${data.model}, ${data.memory},
        ${data.filename}, ${data.size}, ${pathname})`;
  });
  return { id, pathname, receipt: receiptToken(id) };
}

export async function uploadPermission(receipt: string, id: string, pathname: string) {
  checkId(id);
  if (!validReceipt(id, receipt)) throw new SubmissionRequestError('Invalid upload receipt.', 403);
  const [row] = await db()`SELECT pathname, expected_size, upload_expires_at FROM submissions
    WHERE id = ${id} AND status = 'uploading' AND upload_expires_at > now()`;
  if (!row || row.pathname !== pathname) throw new SubmissionRequestError('Upload has expired or is not yours.', 403);
  return { maximumSizeInBytes: Number(row.expected_size), validUntil: new Date(row.upload_expires_at).getTime(),
    allowedContentTypes: ['application/zip'], addRandomSuffix: false, allowOverwrite: false,
    tokenPayload: JSON.stringify({ id }) };
}

export async function completeUpload(id: string, callbackUrl?: string, inspect = head) {
  checkId(id);
  const sql = db();
  const [row] = await sql`SELECT * FROM submissions WHERE id = ${id}`;
  if (!row) throw new SubmissionRequestError('Submission not found.', 404);
  if (row.status !== 'uploading') return;
  if (new Date(row.upload_expires_at).getTime() <= Date.now()) {
    throw new SubmissionRequestError('Upload has expired. Submit the ZIP again.', 410);
  }
  const blob = await inspect(row.pathname, { token: process.env.BLOB_READ_WRITE_TOKEN });
  const url = new URL(blob.url);
  if (blob.pathname !== row.pathname || blob.size !== Number(row.expected_size)
      || url.protocol !== 'https:' || !/^[a-z0-9]+\.private\.blob\.vercel-storage\.com$/.test(url.hostname)
      || (callbackUrl && callbackUrl !== blob.url)) {
    throw new SubmissionRequestError('Uploaded file does not match this submission.');
  }
  const updated = await sql`UPDATE submissions SET blob_url = ${blob.url}, status = 'queued', updated_at = now()
    WHERE id = ${id} AND status = 'uploading' AND upload_expires_at > now() RETURNING id`;
  if (!updated.length) {
    const [current] = await sql`SELECT status FROM submissions WHERE id = ${id}`;
    if (current?.status === 'uploading') throw new SubmissionRequestError('Upload has expired. Submit the ZIP again.', 410);
  }
}

export async function listSubmissions(account: SessionIdentity | null, view: string, page: number, browser: string | null = null) {
  if (view === 'admin' && account?.role !== 'admin') throw new SubmissionRequestError('Administrator access required.', 403);
  if (!['public', 'mine', 'admin'].includes(view) || !Number.isInteger(page) || page < 0 || page > 1000) {
    throw new SubmissionRequestError('Invalid page.');
  }
  const rows = await db()`SELECT id, name, harness, model, memory, filename, status, created_at, updated_at,
      CASE WHEN ${view} = 'public' THEN NULL ELSE error_message END AS error_message,
      attempts, removal_reason, blob_sha256, release_sha256, validator_sha256, summary,
      CASE WHEN ${view} = 'admin' THEN contact_email ELSE NULL END AS contact_email
    FROM submissions WHERE
      (${view} = 'public' AND status = 'accepted'
        AND jsonb_typeof(summary->'total_cost_usd') = 'number'
        AND jsonb_typeof(summary->'ingestion'->'total_cost_usd') = 'number'
        AND jsonb_typeof(summary->'execution'->'total_cost_usd') = 'number') OR
      (${view} = 'mine' AND browser_hash = ${browser}) OR ${view} = 'admin'
    ORDER BY created_at DESC, id DESC LIMIT ${PAGE_SIZE + 1} OFFSET ${page * PAGE_SIZE}`;
  return { submissions: rows.slice(0, PAGE_SIZE).map(row => view === 'mine'
    ? { ...row, receipt: receiptToken(row.id) } : row) as unknown as Submission[], hasMore: rows.length > PAGE_SIZE };
}

export async function readReceipt(id: string, token: string) {
  checkId(id);
  if (!validReceipt(id, token)) throw new SubmissionRequestError('Submission not found or receipt link is invalid.', 404);
  const [row] = await db()`SELECT id, name, harness, model, memory, filename, status, created_at, updated_at,
    error_message, attempts, removal_reason, blob_sha256, release_sha256, validator_sha256, summary
    FROM submissions WHERE id = ${id}`;
  if (!row) throw new SubmissionRequestError('Submission not found.', 404);
  return { ...row, receipt: token } as Submission;
}

export async function withdrawSubmission(id: string, token: string) {
  await readReceipt(id, token);
  await db()`UPDATE submissions SET status = 'removed', removal_reason = 'Withdrawn by submitter',
    lease = NULL, lease_until = NULL, updated_at = now() WHERE id = ${id} AND status <> 'removed'`;
}

export async function moderateSubmission(account: SessionIdentity, id: string, body: any) {
  if (account.role !== 'admin') throw new SubmissionRequestError('Administrator access required.', 403);
  checkId(id);
  if (body?.action === 'retry') {
    await db()`UPDATE submissions SET status = 'queued', attempts = 0, retry_after = now(),
      error_code = NULL, error_message = NULL, updated_at = now()
      WHERE id = ${id} AND status = 'queued'`;
    return;
  }
  const reason = typeof body?.reason === 'string' ? body.reason.trim() : '';
  if (body?.action !== 'remove' || !reason || reason.length > 500) {
    throw new SubmissionRequestError('Enter a removal reason (1 to 500 characters).');
  }
  await db()`UPDATE submissions SET status = 'removed', removed_by = ${account.accountId},
    removal_reason = ${reason}, lease = NULL, lease_until = NULL, updated_at = now()
    WHERE id = ${id} AND status <> 'removed'`;
}

export async function wakeValidator() {
  const origin = process.env.VERCEL_URL ? `https://${process.env.VERCEL_URL}` : process.env.DOLPHINBENCH_VALIDATOR_ORIGIN;
  if (!origin || !process.env.CRON_SECRET) return;
  try {
    const response = await fetch(new URL('/api/submission_validator/', origin), {
      method: 'POST', headers: { Authorization: `Bearer ${process.env.CRON_SECRET}`,
        ...(process.env.VERCEL_AUTOMATION_BYPASS_SECRET
          ? { 'x-vercel-protection-bypass': process.env.VERCEL_AUTOMATION_BYPASS_SECRET } : {}) },
      signal: AbortSignal.timeout(330_000), cache: 'no-store',
    });
    if (!response.ok) console.error('Validator wake failed', response.status);
  } catch { console.error('Validator wake failed; queued work remains available for retry.'); }
}
