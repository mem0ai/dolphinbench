import { createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import { isIP } from 'node:net';

export const BROWSER_COOKIE = 'dolphinbench_submitter';
export const COOKIE_AGE = 60 * 60 * 24 * 30;

function signature(purpose: string, value: string) {
  const secret = process.env.CRON_SECRET;
  if (!secret || secret.length < 32) throw new Error('Submission signing secret is not configured');
  return createHmac('sha256', secret).update(`dolphinbench:${purpose}:${value}`).digest('base64url');
}

function equal(a: string, b: string) {
  return a.length === b.length && timingSafeEqual(Buffer.from(a), Buffer.from(b));
}

export function browserIdentity(cookie?: string) {
  if (!cookie || !/^[\w-]{43}\.[\w-]{43}$/.test(cookie)) return null;
  const [id, mac] = cookie.split('.');
  return equal(mac, signature('browser', id)) ? signature('browser-storage', id) : null;
}

export function newBrowser() {
  const id = randomBytes(32).toString('base64url');
  const cookie = `${id}.${signature('browser', id)}`;
  return { cookie, hash: browserIdentity(cookie)! };
}

export function receiptToken(id: string) { return signature('receipt', id); }

export function validReceipt(id: string, token: string) {
  return /^[\w-]{43}$/.test(token) && equal(token, receiptToken(id));
}

export function requestIpHash(request: Request) {
  // Only Vercel's own forwarding header is trusted. Local development shares one bucket.
  const ip = process.env.VERCEL === '1' ? request.headers.get('x-vercel-forwarded-for')?.trim() : '127.0.0.1';
  if (!ip || !isIP(ip)) throw new Error('Trusted client address is unavailable');
  return signature('ip', isIP(ip) === 6 ? new URL(`http://[${ip}]`).hostname : ip);
}

export function turnstileConfigured() {
  const site = process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY;
  const secret = process.env.TURNSTILE_SECRET_KEY;
  if (!site || !secret) return false;
  return process.env.VERCEL_ENV !== 'production' || ![site, secret].some(key => /^[123]x0{10,}/.test(key));
}

export async function verifyBot(request: Request, token: unknown, verify = fetch) {
  if (!turnstileConfigured()) throw new Error('Bot verification is not configured');
  if (typeof token !== 'string' || !token || token.length > 2048) return false;
  const response = await verify('https://challenges.cloudflare.com/turnstile/v0/siteverify', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ secret: process.env.TURNSTILE_SECRET_KEY, response: token }),
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new Error('Bot verification unavailable');
  const result = await response.json();
  const hostname = new URL(request.headers.get('origin') || request.url).hostname;
  const localTest = process.env.NODE_ENV === 'development' && process.env.VERCEL !== '1'
    && ['localhost', '127.0.0.1'].includes(hostname) && /^[123]x0{10,}/.test(process.env.TURNSTILE_SECRET_KEY || '');
  return result.success === true && (localTest || (result.hostname === hostname && result.action === 'submit-run'));
}
