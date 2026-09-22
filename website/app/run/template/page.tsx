import RunDocument from '@/components/RunDocument';

export const metadata = {
  title: 'Python template',
  description: 'Starter Python harness for running DolphinBench with your own agent and memory system.',
  alternates: { canonical: '/run/template/' },
};

export default function TemplatePage() {
  return <RunDocument title="Python template" file="examples/harness_template.py" language="python" />;
}
