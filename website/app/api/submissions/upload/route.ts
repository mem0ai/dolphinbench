import { handleUpload, type HandleUploadBody } from '@vercel/blob/client';
import { after } from 'next/server';
import { completeUpload, readRequestJson, requestFailure, requireSameOrigin, requireSubmissions,
  SubmissionRequestError, uploadPermission, wakeValidator } from '@/lib/submissions';

export const maxDuration = 360;

export async function POST(request: Request) {
  try {
    requireSubmissions();
    const body = await readRequestJson(request) as HandleUploadBody;
    if (!body || !['blob.generate-client-token', 'blob.upload-completed'].includes(body.type)) {
      throw new SubmissionRequestError('Invalid upload request.');
    }
    const result = await handleUpload({
      body, request, token: process.env.BLOB_READ_WRITE_TOKEN,
      onBeforeGenerateToken: async (pathname, clientPayload) => {
        requireSameOrigin(request);
        const data = JSON.parse(clientPayload || '{}');
        return uploadPermission(data.receipt || '', data.id || '', pathname);
      },
      onUploadCompleted: async ({ blob, tokenPayload }) => {
        // handleUpload verifies Vercel's signature before invoking this callback.
        await completeUpload(JSON.parse(tokenPayload || '{}').id, blob.url);
        after(wakeValidator);
      },
    });
    return Response.json(result);
  } catch (error) { return requestFailure(error); }
}
