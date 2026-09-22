import { createHash, randomBytes } from 'node:crypto';
import { db } from '@/lib/db';

export const SESSION_COOKIE = 'dolphinbench_session';
export const SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30;

export type AccountRole = 'admin' | 'partner';

export type SessionIdentity = {
  accountId: string;
  username: string;
  label: string;
  role: AccountRole;
};

function tokenHash(token: string) {
  return createHash('sha256').update(token).digest('hex');
}

export async function createSession(accountId: string) {
  const token = randomBytes(32).toString('base64url');
  const sql = db();
  await sql`
    INSERT INTO auth_sessions (token_hash, account_id, expires_at)
    VALUES (
      ${tokenHash(token)},
      ${accountId},
      now() + (${SESSION_MAX_AGE_SECONDS} * interval '1 second')
    )
  `;
  return token;
}

export async function verifySessionToken(token: string | undefined): Promise<SessionIdentity | null> {
  if (!token) return null;
  try {
    const sql = db();
    const rows = await sql`
      SELECT a.id, a.username, a.label, a.role
      FROM auth_sessions s
      JOIN partner_accounts a ON a.id = s.account_id
      WHERE s.token_hash = ${tokenHash(token)}
        AND s.expires_at > now()
        AND a.active = true
      LIMIT 1
    ` as unknown as Array<{
      id: string;
      username: string;
      label: string;
      role: AccountRole;
    }>;
    const account = rows[0];
    return account ? {
      accountId: account.id,
      username: account.username,
      label: account.label,
      role: account.role,
    } : null;
  } catch {
    return null;
  }
}

export async function revokeSession(token: string | undefined) {
  if (!token) return;
  const sql = db();
  await sql`DELETE FROM auth_sessions WHERE token_hash = ${tokenHash(token)}`;
}
