import { currentSession } from '@/lib/current-session';
import { cookies } from 'next/headers';
import { NextResponse } from 'next/server';
import { publicSubmissionsPreviewUrl } from '@/lib/preview';
import { BROWSER_COOKIE, COOKIE_AGE, browserIdentity, newBrowser, requestIpHash, verifyBot } from '@/lib/submission-access';
import { MAX_UPLOAD_BYTES, listSubmissions, readRequestJson, requestFailure, requireSameOrigin,
  requireSubmissions, reserveSubmission, SubmissionRequestError, submissionMetadata, submissionsEnabled } from '@/lib/submissions';

export const dynamic = 'force-dynamic';

export async function GET(request: Request) {
  try {
    const previewUrl = publicSubmissionsPreviewUrl(new URL(request.url));
    if (previewUrl) {
      const upstream = await fetch(previewUrl, { cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(10_000) });
      if (!upstream.ok) throw new Error('Public submissions could not be loaded');
      const { submissions, hasMore } = await upstream.json();
      if (!Array.isArray(submissions) || typeof hasMore !== 'boolean') throw new Error('Invalid public submissions response');
      return Response.json({ enabled: false, submissions, hasMore }, { headers: { 'Cache-Control': 'private, no-store' } });
    }
    if (!submissionsEnabled()) return Response.json({ enabled: false, submissions: [], hasMore: false });
    const url = new URL(request.url);
    const view = url.searchParams.get('view') || 'mine';
    const hash = browserIdentity((await cookies()).get(BROWSER_COOKIE)?.value);
    const browser = view === 'mine' && !hash ? newBrowser() : null;
    const result = await listSubmissions(view === 'admin' ? await currentSession() : null, view,
      Number(url.searchParams.get('page') || 0), hash || browser?.hash || null);
    const response = NextResponse.json({ enabled: true, siteKey: process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY,
      maxUploadBytes: MAX_UPLOAD_BYTES, ...result },
      { headers: { 'Cache-Control': 'private, no-store' } });
    if (browser) response.cookies.set(BROWSER_COOKIE, browser.cookie, { httpOnly: true,
      secure: new URL(request.url).protocol === 'https:', sameSite: 'strict', path: '/', maxAge: COOKIE_AGE });
    return response;
  } catch (error) { return requestFailure(error); }
}

export async function POST(request: Request) {
  try {
    requireSubmissions();
    requireSameOrigin(request);
    const browser = browserIdentity((await cookies()).get(BROWSER_COOKIE)?.value);
    if (!browser) throw new SubmissionRequestError('Reload the submission page and try again.', 403);
    const body = await readRequestJson(request);
    submissionMetadata(body);
    if (!await verifyBot(request, body.botToken)) throw new SubmissionRequestError('Complete the bot check and try again.', 403);
    return Response.json(await reserveSubmission(browser, requestIpHash(request), body),
      { status: 201, headers: { 'Cache-Control': 'private, no-store' } });
  } catch (error) { return requestFailure(error); }
}
