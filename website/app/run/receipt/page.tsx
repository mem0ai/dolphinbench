import { SubmissionReceipt } from '@/components/Submissions';

export const metadata = {
  title: 'Submission receipt', referrer: 'no-referrer' as const,
  robots: { index: false, follow: false },
};

export default function ReceiptPage() {
  return <div className="container-x pb-24 pt-14 sm:pt-20">
    <h1 className="page-title mb-4">Submission receipt</h1>
    <p className="mb-8 max-w-[40em] text-base text-muted">Keep this link private. Anyone with it can view or withdraw this submission.</p>
    <SubmissionReceipt />
  </div>;
}
