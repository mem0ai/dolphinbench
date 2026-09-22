import { createReadStream } from 'node:fs';
import { stat } from 'node:fs/promises';
import { Readable } from 'node:stream';
import { get, head } from '@vercel/blob';
import evidence from '@/content/official-evidence.json';
import { previewEvidenceFile, resultsPreviewDirectory } from '@/lib/local-results';

export const maxDuration = 300;

export async function GET(request: Request, context: { params: Promise<{ name: string }> }) {
  const directory = resultsPreviewDirectory(request);
  const { name } = await context.params;
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(name)) return new Response(null, { status: 404 });
  if (!directory) {
    const files: Record<string, { pathname: string; sha256: string; bytes: number }> = evidence;
    if (!Object.hasOwn(files, name)) return new Response(null, { status: 404 });
    const file = files[name];
    // Compressed GET responses can omit Content-Length; use stored metadata.
    const metadata = await head(file.pathname);
    if (metadata.size !== file.bytes) return new Response(null, { status: 503 });
    const blob = await get(file.pathname, { access: 'private' });
    if (!blob || blob.statusCode !== 200) {
      return new Response(null, { status: 503 });
    }
    return new Response(blob.stream, {
      headers: {
        'Content-Type': 'application/octet-stream',
        'Content-Length': String(file.bytes),
        'Content-Disposition': `attachment; filename="${name}"`,
        'Cache-Control': 'private, no-store',
        'X-Content-Type-Options': 'nosniff',
        'X-Content-SHA256': file.sha256,
      },
    });
  }
  const file = await previewEvidenceFile(directory, name);
  if (!file) return new Response(null, { status: 404 });
  const info = await stat(file.path);
  const stream = Readable.toWeb(createReadStream(file.path)) as ReadableStream;
  return new Response(stream, {
    headers: {
      'Content-Type': 'application/octet-stream',
      'Content-Length': String(info.size),
      'Content-Disposition': `attachment; filename="${name}"`,
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
    },
  });
}
