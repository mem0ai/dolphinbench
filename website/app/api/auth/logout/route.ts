import { NextRequest, NextResponse } from 'next/server';
import { revokeSession, SESSION_COOKIE } from '@/lib/auth';

export async function POST(request: NextRequest) {
  await revokeSession(request.cookies.get(SESSION_COOKIE)?.value);
  const response = NextResponse.redirect(new URL('/login/', request.url));
  response.cookies.set(SESSION_COOKIE, '', {
    httpOnly: true,
    secure: process.env.NODE_ENV === 'production',
    sameSite: 'strict',
    path: '/',
    maxAge: 0,
  });
  return response;
}
