import { createHash } from 'node:crypto';
import { NextRequest, NextResponse } from 'next/server';
import { SESSION_COOKIE, verifySessionToken } from '@/lib/auth';
import { db } from '@/lib/db';

const ALLOWED_EVENTS = new Set(['page_view', 'page_time', 'click', 'select', 'search']);

type SubmittedEvent = {
  name: string;
  path?: unknown;
  target?: unknown;
  metadata?: unknown;
  clientAt?: unknown;
};

function cleanString(value: unknown, max: number) {
  return typeof value === 'string' ? value.slice(0, max) : null;
}

function cleanMetadata(value: unknown) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const result: Record<string, string | number | boolean | null> = {};
  for (const [key, item] of Object.entries(value).slice(0, 20)) {
    if (['string', 'number', 'boolean'].includes(typeof item) || item === null) {
      result[key.slice(0, 64)] = typeof item === 'string' ? item.slice(0, 500) : item as number | boolean | null;
    }
  }
  return result;
}

export async function POST(request: NextRequest) {
  const session = await verifySessionToken(request.cookies.get(SESSION_COOKIE)?.value);
  if (!session) return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });

  const body = await request.json().catch(() => null);
  const submitted: unknown[] = Array.isArray(body?.events) ? body.events.slice(0, 25) : [];
  const events = submitted.filter((event): event is SubmittedEvent => (
    !!event &&
    typeof event === 'object' &&
    'name' in event &&
    typeof event.name === 'string' &&
    ALLOWED_EVENTS.has(event.name)
  ));
  if (events.length === 0) return NextResponse.json({ ok: true });

  const forwarded = request.headers.get('x-forwarded-for')?.split(',')[0]?.trim() || null;
  const ipHash = forwarded && process.env.ACTIVITY_HASH_SALT
    ? createHash('sha256').update(`${process.env.ACTIVITY_HASH_SALT}:${forwarded}`).digest('hex')
    : null;
  const context = {
    userAgent: cleanString(request.headers.get('user-agent'), 1000),
    referrer: cleanString(request.headers.get('referer'), 1000),
    ipHash,
    country: cleanString(request.headers.get('x-vercel-ip-country'), 8),
    region: cleanString(request.headers.get('x-vercel-ip-country-region'), 32),
    city: cleanString(request.headers.get('x-vercel-ip-city'), 128),
  };

  const sql = db();
  await Promise.all(events.map((event) => sql`
    INSERT INTO activity_events (
      account_id, event_name, path, target, metadata, client_at,
      user_agent, referrer, ip_hash, country, region, city
    ) VALUES (
      ${session.accountId},
      ${event.name},
      ${cleanString(event.path, 1000)},
      ${cleanString(event.target, 500)},
      ${JSON.stringify(cleanMetadata(event.metadata))}::jsonb,
      ${cleanString(event.clientAt, 64)}::timestamptz,
      ${context.userAgent},
      ${context.referrer},
      ${context.ipHash},
      ${context.country},
      ${context.region},
      ${context.city}
    )
  `));

  return NextResponse.json({ ok: true });
}
