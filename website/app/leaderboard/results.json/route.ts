import results from '@/content/official-results.json';
import { readResultsPreview, resultsPreviewDirectory } from '@/lib/local-results';

export async function GET(request: Request) {
  const directory = resultsPreviewDirectory(request);
  return Response.json(directory ? await readResultsPreview(directory) : results, {
    headers: {
      'Cache-Control': 'no-store',
      'Content-Disposition':
        'attachment; filename="dolphinbench-results.json"',
    },
  });
}
