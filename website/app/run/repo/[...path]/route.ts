import { runRepoFileContents } from '@/lib/run-repo-files';

export async function GET(_request: Request, context: { params: Promise<{ path: string[] }> }) {
  const file = (await context.params).path.join('/');
  const bundled = runRepoFileContents[file];
  if (!bundled) {
    return new Response('Not found', { status: 404 });
  }
  return new Response(bundled.content, { headers: {
    'Content-Type': 'text/plain; charset=utf-8',
    'X-Content-Type-Options': 'nosniff',
    'Cache-Control': 'no-store',
    'ETag': `"${bundled.sha256}"`,
  } });
}
