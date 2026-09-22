import { after } from 'next/server';
import { currentSession } from '@/lib/current-session';
import { completeUpload, moderateSubmission, readReceipt, readRequestJson, requestFailure, requireSameOrigin,
  requireSubmissions, SubmissionRequestError, wakeValidator, withdrawSubmission } from '@/lib/submissions';

export const maxDuration = 360;
type Context = { params: Promise<{ id: string }> };
const receipt = (request: Request) => request.headers.get('authorization')?.replace(/^Bearer /, '') || '';

export async function GET(request: Request, context: Context) {
  try {
    requireSubmissions();
    return Response.json(await readReceipt((await context.params).id, receipt(request)),
      { headers: { 'Cache-Control': 'private, no-store' } });
  } catch (error) { return requestFailure(error); }
}

export async function POST(request: Request, context: Context) {
  try {
    requireSubmissions();
    requireSameOrigin(request);
    const id = (await context.params).id;
    await readReceipt(id, receipt(request));
    await completeUpload(id);
    after(wakeValidator);
    return Response.json({ ok: true });
  } catch (error) { return requestFailure(error); }
}

export async function PATCH(request: Request, context: Context) {
  try {
    requireSubmissions();
    requireSameOrigin(request);
    const id = (await context.params).id;
    const body = await readRequestJson(request);
    if (body?.action === 'withdraw') {
      await withdrawSubmission(id, receipt(request));
    } else {
      const account = await currentSession();
      if (!account) throw new SubmissionRequestError('Administrator access required.', 401);
      await moderateSubmission(account, id, body);
    }
    after(wakeValidator);
    return Response.json({ ok: true });
  } catch (error) { return requestFailure(error); }
}
