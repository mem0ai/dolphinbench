import type { ReactNode } from 'react';
import Link from 'next/link';
import RunCode from '@/components/RunCode';
import RunNavigation from '@/components/RunNavigation';
import Submissions from '@/components/Submissions';
import examples from '@/content/submission-examples.json';
import { contactEmail, discordUrl, repositoryUrl } from '@/lib/site';
import CopyText from '@/components/CopyText';
import { runRepoFileContents } from '@/lib/run-repo-files';

export const metadata = {
  title: 'Run and submit',
  description: 'Connect your harness, model, and memory system, run all 600 DolphinBench tasks, and submit a packaged run to the leaderboard.',
  alternates: { canonical: '/run/' },
};

const guide = '/run/guide/';
const sections = [
  ['setup', 'Setup'],
  ['run', 'Run'],
  ['submission', 'Package'],
  ['upload', 'Submit'],
] as const;

function Section({ id, number, title, children }: {
  id: string; number: string; title: string; children: ReactNode;
}) {
  return (
    <section id={id} className="run-section" aria-labelledby={`${id}-heading`}>
      <span className="run-number" aria-hidden="true">{number}</span>
      <div className="min-w-0">
        <h2 id={`${id}-heading`}>{title}</h2>
        {children}
      </div>
    </section>
  );
}

export default function RunPage() {
  return (
    <div className="run-page">
      <header className="run-header">
        <h1 className="page-title mb-6">Run and submit</h1>
        <p>
          Evaluate your harness, model, and memory system on DolphinBench&apos;s 600 tasks across three personas.
        </p>
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-base">
          <Link href="/dataset/" className="text-link">Explore the dataset</Link>
          <a href="#upload" className="text-link">Already have a run? Submit it</a>
        </div>
      </header>

      <div className="run-layout">
        <RunNavigation sections={sections} />
        <div className="run-guide min-w-0">
          <Section id="setup" number="01" title="Setup">
            <RunCode label="Get the repository">{[
              'git clone ' + repositoryUrl + '.git dolphinbench',
              'cd dolphinbench',
              'python3 -m venv .venv',
              'source .venv/bin/activate',
              'pip install -r requirements.txt',
              'python -m harness.runner init',
            ].join('\n')}</RunCode>
            <p>This creates <code>my_harness.py</code> and a matching <code>run.yaml</code>. Connect your existing agent in <code>my_harness.py</code> using the integration guide and starter Python file below.</p>
            <div className="my-4 flex flex-wrap gap-x-6 gap-y-2 text-sm">
              <span className="inline-flex items-center gap-1"><Link href={guide}>Harness integration guide</Link>
                <CopyText text={runRepoFileContents['docs/DRIVER_CONTRACT.md'].content} label="Copy harness integration guide for agent" /></span>
              <span className="inline-flex items-center gap-1"><Link href="/run/template/">Python template</Link>
                <CopyText text={runRepoFileContents['examples/harness_template.py'].content} label="Copy Python template for agent" /></span>
            </div>
          </Section>

          <Section id="run" number="02" title="Run">
            <p>After implementing your harness, check the setup, process the histories, then evaluate the tests. Ingestion and evaluation incur model and grading costs.</p>
            <RunCode label="Run the benchmark">{[
              'python -m harness.runner prepare',
              'python -m harness.runner ingest --confirm-paid-calls',
              'python -m harness.runner evaluate --confirm-paid-calls',
            ].join('\n')}</RunCode>
            <ul className="run-list">
              <li>Start with separate empty memory for each persona and process every history message in order.</li>
              <li>Run each test in a fresh conversation with isolated app state and read-only access to the completed memory.</li>
              <li>Keep the release and grading rules unchanged. Keep facts and expected answers outside the agent&apos;s context.</li>
              <li>Record complete interactions, API-reported usage, and all 600 results, including failures.</li>
            </ul>
          </Section>

          <Section id="submission" number="03" title="Package">
            <p>Generate the required submission ZIP from your completed run. This command validates the recorded evidence without model calls:</p>
            <RunCode label="Package your submission">{'python -m harness.runner package'}</RunCode>
            <p>The ZIP is saved to <code>tmp/my-agent/submission.zip</code>. It must contain exactly these two files at its root:</p>
            <dl className="run-fields">
              <div><dt><code>ingestion.json</code></dt><dd>Recorded interactions for the complete history of all three personas.</dd></div>
              <div><dt><code>tests.json</code></dt><dd>All 600 test interactions and their grading evidence, including failures.</dd></div>
            </dl>
            <details id="format" className="run-details">
              <summary>Required format and examples</summary>
              <p>The examples below show one history interaction and one test. Actual submissions must contain the complete run, with real settings, messages, token usage, timings, and recorded judge responses.</p>
              <RunCode label="ingestion.json example" language="json">{JSON.stringify(examples.ingestion, null, 2)}</RunCode>
              <RunCode label="tests.json example" language="json">{JSON.stringify(examples.tests, null, 2)}</RunCode>
              <p>Keep credentials outside the archive. See the <a href={guide}>recording requirements</a> and <a href="/run/repo/harness/submission.py/">validator source</a> for the full format.</p>
              <RunCode label="Check an existing ZIP">{'python -m harness.export_submission --release . --check tmp/my-agent/submission.zip'}</RunCode>
            </details>
          </Section>

          <Section id="upload" number="04" title="Submit">
            <p><a href="#submission">Package your run in the required format</a> before uploading: exactly <code>ingestion.json</code> and <code>tests.json</code> at the ZIP root, covering the complete history and all 600 tests. Maximum upload: 256 MiB.</p>
            <p>Valid submissions appear as <strong>Self-submitted · Unverified</strong>. Scores and configuration are public; raw ZIPs and contact emails stay private.</p>
            <Submissions />
            <p className="mt-6 text-sm text-muted">
              Questions about running or submitting? Email <a href={`mailto:${contactEmail}`}>{contactEmail}</a> or ask in{' '}
              <a href={discordUrl}>Discord</a>.
            </p>
          </Section>
        </div>
      </div>
    </div>
  );
}
