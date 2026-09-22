import { randomBytes } from 'node:crypto';
import bcrypt from 'bcryptjs';
import postgres from 'postgres';

const connectionString = process.env.DATABASE_URL || process.env.POSTGRES_URL;
if (!connectionString) throw new Error('DATABASE_URL is not configured.');
const sql = postgres(connectionString, { max: 1, prepare: false });

const [command, ...args] = process.argv.slice(2);
const option = (name) => {
  const index = args.indexOf(`--${name}`);
  return index >= 0 ? args[index + 1] : undefined;
};

function validUsername(username) {
  return /^[a-z0-9][a-z0-9._-]{2,63}$/i.test(username || '');
}

function generatedPassword() {
  return randomBytes(18).toString('base64url');
}

if (command === 'create') {
  const username = option('username');
  const label = option('label');
  const role = option('role') || 'partner';
  const password = option('password') || generatedPassword();
  if (!validUsername(username) || !label || !['admin', 'partner'].includes(role)) {
    throw new Error('Usage: accounts create --username NAME --label LABEL [--role admin|partner] [--password VALUE]');
  }
  const hash = await bcrypt.hash(password, 12);
  await sql`
    INSERT INTO partner_accounts (username, label, role, password_hash)
    VALUES (${username}, ${label}, ${role}, ${hash})
  `;
  console.log(JSON.stringify({ username, password, label, role }, null, 2));
  console.log('The password is shown only in this output.');
} else if (command === 'reset') {
  const username = option('username');
  if (!validUsername(username)) throw new Error('Usage: accounts reset --username NAME [--password VALUE]');
  const password = option('password') || generatedPassword();
  const hash = await bcrypt.hash(password, 12);
  const rows = await sql`
    UPDATE partner_accounts SET password_hash = ${hash}, active = true
    WHERE lower(username) = lower(${username})
    RETURNING username, label
  `;
  if (rows.length === 0) throw new Error(`Unknown account: ${username}`);
  await sql`
    DELETE FROM auth_sessions
    WHERE account_id = (SELECT id FROM partner_accounts WHERE lower(username) = lower(${username}))
  `;
  console.log(JSON.stringify({ username: rows[0].username, password, label: rows[0].label }, null, 2));
  console.log('The password is shown only in this output.');
} else if (command === 'disable') {
  const username = option('username');
  if (!validUsername(username)) throw new Error('Usage: accounts disable --username NAME');
  const rows = await sql`
    UPDATE partner_accounts SET active = false
    WHERE lower(username) = lower(${username})
    RETURNING username, label
  `;
  if (rows.length === 0) throw new Error(`Unknown account: ${username}`);
  await sql`
    DELETE FROM auth_sessions
    WHERE account_id = (SELECT id FROM partner_accounts WHERE lower(username) = lower(${username}))
  `;
  console.log(`Disabled ${rows[0].label} (${rows[0].username}).`);
} else if (command === 'enable') {
  const username = option('username');
  if (!validUsername(username)) throw new Error('Usage: accounts enable --username NAME');
  const rows = await sql`
    UPDATE partner_accounts SET active = true
    WHERE lower(username) = lower(${username})
    RETURNING username, label
  `;
  if (rows.length === 0) throw new Error(`Unknown account: ${username}`);
  console.log(`Enabled ${rows[0].label} (${rows[0].username}).`);
} else if (command === 'list') {
  const rows = await sql`
    SELECT username, label, role, active, created_at, last_login_at
    FROM partner_accounts ORDER BY created_at
  `;
  console.table(rows);
} else {
  console.log('Commands: create, reset, disable, enable, list');
  process.exitCode = 1;
}

await sql.end();
