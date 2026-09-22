import RunDocument from '@/components/RunDocument';

export const metadata = {
  title: 'Harness integration guide',
  description: 'How to connect an existing agent and memory system to the DolphinBench runner.',
  alternates: { canonical: '/run/guide/' },
};

export default function GuidePage() {
  return <RunDocument title="Harness integration guide" file="docs/DRIVER_CONTRACT.md" />;
}
