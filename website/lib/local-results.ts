import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { localPreview } from './preview';

export function resultsPreviewDirectory(request: Request): string | null {
  const host = new URL(request.url).hostname;
  return localPreview() && ['localhost', '127.0.0.1', '[::1]'].includes(host)
    ? process.env.DOLPHINBENCH_RESULTS_PREVIEW_DIR || null : null;
}

export async function readResultsPreview(directory: string) {
  return JSON.parse(await readFile(join(directory, 'preview-results.json'), 'utf8'));
}

export async function previewEvidenceFile(directory: string, name: string) {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(name)) return null;
  const files: Record<string, { path: string; sha256: string }> = JSON.parse(
    await readFile(join(directory, 'preview-files.json'), 'utf8'),
  );
  return Object.hasOwn(files, name) ? files[name] : null;
}
