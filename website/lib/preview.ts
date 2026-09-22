export function localPreview(): boolean {
  return (
    process.env.NODE_ENV === 'development' &&
    process.env.DOLPHINBENCH_LOCAL_PREVIEW === '1'
  );
}

export function publicSubmissionsPreviewUrl(requestUrl: URL): URL | null {
  const origin = process.env.DOLPHINBENCH_PUBLIC_SUBMISSIONS_ORIGIN;
  if (!localPreview() || !origin || !['127.0.0.1', 'localhost', '[::1]'].includes(requestUrl.hostname)
      || requestUrl.searchParams.get('view') !== 'public') return null;
  const source = new URL(origin);
  if (source.protocol !== 'https:' || source.username || source.password) {
    throw new Error('Public submissions preview requires an HTTPS origin without credentials');
  }
  const target = new URL('/api/submissions/', source.origin);
  target.searchParams.set('view', 'public');
  target.searchParams.set('page', requestUrl.searchParams.get('page') || '0');
  return target;
}

export function previewRequestAllowed(
  hostname: string,
  pathname: string,
): boolean {
  return (
    localPreview() &&
    ['127.0.0.1', 'localhost', '[::1]'].includes(hostname) &&
    publicPage(pathname)
  );
}

// Crawl, discovery, and app-icon files must stay reachable without a session.
const DISCOVERY_FILES = new Set([
  '/robots.txt', '/sitemap.xml', '/llms.txt', '/llms-full.txt', '/manifest.webmanifest', '/favicon.ico',
]);

export function publicPage(pathname: string): boolean {
  return pathname === '/' ||
      DISCOVERY_FILES.has(pathname) ||
      pathname.startsWith('/opengraph-image') ||
      pathname.startsWith('/icon') ||
      pathname.startsWith('/apple-icon') ||
      pathname.startsWith('/brand/') ||
      pathname === '/about/' ||
      pathname === '/dataset/' ||
      pathname === '/run/' ||
      pathname === '/run/guide/' ||
      pathname === '/run/template/' ||
      pathname === '/run/receipt/' ||
      pathname.startsWith('/run/repo/') ||
      pathname.startsWith('/leaderboard/') ||
      pathname.startsWith('/personas/') ||
      pathname.startsWith('/data/');
}
