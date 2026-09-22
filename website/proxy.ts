import { NextRequest, NextResponse } from 'next/server';
import { SESSION_COOKIE, verifySessionToken } from '@/lib/auth';
import { publicPage } from '@/lib/preview';

const PUBLIC_PATHS = new Set(['/login/', '/api/auth/login/']);

export async function proxy(req: NextRequest) {
  const path = req.nextUrl.pathname;
  // Submission endpoints enforce bot verification, receipt access, or admin authentication themselves.
  if (path === '/api/submissions/' || path.startsWith('/api/submissions/') ||
      ['/api/submission_validator', '/api/submission_validator/'].includes(path)) {
    return NextResponse.next();
  }
  if (publicPage(path)) return NextResponse.next();
  const session = await verifySessionToken(req.cookies.get(SESSION_COOKIE)?.value);

  if (PUBLIC_PATHS.has(path)) {
    if (session && path === '/login/') return NextResponse.redirect(new URL('/', req.url));
    return NextResponse.next();
  }

  if (session) return NextResponse.next();
  if (path.startsWith('/api/')) return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });

  const login = new URL('/login/', req.url);
  return NextResponse.redirect(login);
}

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'],
};
