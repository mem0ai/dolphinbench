import postgres from 'postgres';
import { readFile } from 'node:fs/promises';

const connectionString = process.env.DATABASE_URL || process.env.POSTGRES_URL;
if (!connectionString) throw new Error('DATABASE_URL is not configured.');
const sql = postgres(connectionString, { max: 1, prepare: false });

await sql`
  CREATE TABLE IF NOT EXISTS partner_accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username text NOT NULL,
    label text NOT NULL,
    role text NOT NULL DEFAULT 'partner' CHECK (role IN ('admin', 'partner')),
    password_hash text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
  )
`;
await sql`CREATE UNIQUE INDEX IF NOT EXISTS partner_accounts_username_lower_idx ON partner_accounts (lower(username))`;

await sql`
  CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash text PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES partner_accounts(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
  )
`;
await sql`CREATE INDEX IF NOT EXISTS auth_sessions_account_idx ON auth_sessions (account_id)`;
await sql`CREATE INDEX IF NOT EXISTS auth_sessions_expiry_idx ON auth_sessions (expires_at)`;

await sql`
  CREATE TABLE IF NOT EXISTS activity_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES partner_accounts(id) ON DELETE CASCADE,
    event_name text NOT NULL,
    path text,
    target text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    client_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    user_agent text,
    referrer text,
    ip_hash text,
    country text,
    region text,
    city text
  )
`;
await sql`CREATE INDEX IF NOT EXISTS activity_events_account_created_idx ON activity_events (account_id, created_at DESC)`;
await sql`CREATE INDEX IF NOT EXISTS activity_events_created_idx ON activity_events (created_at DESC)`;

await sql`
  CREATE TABLE IF NOT EXISTS auth_login_attempts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    attempt_key text NOT NULL,
    attempted_at timestamptz NOT NULL DEFAULT now()
  )
`;
await sql`CREATE INDEX IF NOT EXISTS auth_login_attempts_key_time_idx ON auth_login_attempts (attempt_key, attempted_at DESC)`;

await sql.unsafe(await readFile(new URL('../submissions.sql', import.meta.url), 'utf8'));
console.log('Database schema is ready.');
await sql.end();
