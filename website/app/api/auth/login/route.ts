import { createHash } from 'node:crypto';
import bcrypt from 'bcryptjs';
import { NextRequest, NextResponse } from 'next/server';
import { createSession, SESSION_COOKIE, SESSION_MAX_AGE_SECONDS, type AccountRole } from '@/lib/auth';
import { db } from '@/lib/db';

const WINDOW_MINUTES = 15;
const MAX_ATTEMPTS = 10;

function loginKey(request: NextRequest, username: string) {
  const ip = request.headers.get('x-forwarded-for')?.split(',')[0]?.trim() || 'unknown';
  return createHash('sha256').update(`${ip}:${username.toLowerCase()}`).digest('hex');
}

export async function POST(request: NextRequest) {
  const body = await request.json().catch(() => null);
  const username = typeof body?.username === 'string' ? body.username.trim() : '';
  const password = typeof body?.password === 'string' ? body.password : '';

  if (!username || !password || username.length > 64 || password.length > 256) {
    return NextResponse.json({ error: 'Invalid username or password.' }, { status: 401 });
  }

  const sql = db();
  const key = loginKey(request, username);
  const attempts = await sql`
    SELECT count(*)::int AS count
    FROM auth_login_attempts
    WHERE attempt_key = ${key}
      AND attempted_at > now() - (${WINDOW_MINUTES} * interval '1 minute')
  ` as unknown as Array<{ count: number }>;

  if ((attempts[0]?.count as number) >= MAX_ATTEMPTS) {
    return NextResponse.json({ error: 'Too many attempts. Try again later.' }, { status: 429 });
  }

  const accounts = await sql`
    SELECT id, username, label, role, password_hash
    FROM partner_accounts
    WHERE lower(username) = lower(${username}) AND active = true
    LIMIT 1
  ` as unknown as Array<{
    id: string;
    username: string;
    label: string;
    role: AccountRole;
    password_hash: string;
  }>;
  const account = accounts[0];
  const valid = account && await bcrypt.compare(password, account.password_hash);

  if (!valid) {
    await sql`INSERT INTO auth_login_attempts (attempt_key) VALUES (${key})`;
    return NextResponse.json({ error: 'Invalid username or password.' }, { status: 401 });
  }

  await Promise.all([
    sql`DELETE FROM auth_login_attempts WHERE attempt_key = ${key}`,
    sql`DELETE FROM auth_login_attempts WHERE attempted_at < now() - interval '1 day'`,
    sql`DELETE FROM auth_sessions WHERE expires_at <= now()`,
    sql`UPDATE partner_accounts SET last_login_at = now() WHERE id = ${account.id}`,
  ]);

  const token = await createSession(account.id);

  const response = NextResponse.json({ ok: true });
  response.cookies.set(SESSION_COOKIE, token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === 'production',
    sameSite: 'strict',
    path: '/',
    maxAge: SESSION_MAX_AGE_SECONDS,
  });
  return response;
}
