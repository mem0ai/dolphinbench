import Link from 'next/link';
import { notFound } from 'next/navigation';
import { ArrowLeft, ArrowRight, Download } from 'lucide-react';
import {
  getPersona,
  getPersonaData,
  getMessages,
  formatDate,
  personaHref,
  testsHref,
  testHref,
} from '@/lib/data';
import EventReference from '@/components/EventReference';
import AppState from '@/components/AppState';
import { testMetadata } from '@/lib/persona-metadata';

export async function generateMetadata({ params }: { params: Promise<{ persona: string; testid: string }> }) {
  const { persona, testid } = await params;
  return testMetadata(persona, testid);
}

export default async function TestDetailPage({
  params,
}: {
  params: Promise<{ persona: string; testid: string }>;
}) {
  const { persona: id, testid } = await params;
  const persona = getPersona(id);
  if (!persona) notFound();
  const { tests, facts } = getPersonaData(id);
  const index = tests.findIndex((test) => test.id === testid);
  if (index < 0) notFound();
  const test = tests[index];
  const messages = getMessages(id);
  return (
    <div className="page">
      <div className="max-w-[880px]">
        <nav
          aria-label="Breadcrumb"
          className="mb-10 flex flex-wrap items-center gap-3 text-sm text-muted"
        >
          <Link className="hover:text-ink" href="/dataset/">
            Dataset
          </Link>
          <span>/</span>
          <Link className="hover:text-ink" href={personaHref(id)}>
            {persona.name}
          </Link>
          <span>/</span>
          <Link className="hover:text-ink" href={testsHref(id)}>
            Tests
          </Link>
          <span>/</span>
          <span className="font-mono">{test.id}</span>
        </nav>
        <header className="flex flex-wrap items-start justify-between gap-5 pb-8">
          <div>
            <h1 className="page-title">Test {test.id}</h1>
            <p className="mt-3 text-sm text-muted">
              {formatDate(test.date)} / {test.fact_ids.length}{' '}
              {test.fact_ids.length === 1 ? 'fact' : 'facts'}
            </p>
          </div>
          <a
            className="command"
            href={`/data/tests/${id}/${test.id}.yaml`}
            download
          >
            <Download size={16} />
            YAML
          </a>
        </header>
        <section className="section">
          <h2 className="mb-3 text-md font-semibold">Request</h2>
          <p className="source-text text-base" data-test-request>
            {test.request}
          </p>
        </section>
        <section className="section">
          <h2 className="mb-3 text-md font-semibold">Required memory</h2>
          {test.fact_ids.map((factId) => (
            <EventReference
              key={factId}
              personaId={id}
              fact={facts[factId]}
              messages={messages}
            />
          ))}
        </section>
        <section className="section">
          <h2 className="mb-3 text-md font-semibold">Expected tool calls</h2>
          <ul className="space-y-2 font-mono text-xs">
            {test.tools.map((tool) => (
              <li className="break-all" key={tool}>
                {tool}
              </li>
            ))}
          </ul>
        </section>
        <section className="section">
          <h2 className="mb-3 text-md font-semibold">Grading</h2>
          {test.grade.config.assertions.map((assertion, i) => (
            <details key={i} className="border-b border-hairline py-3">
              <summary className="text-sm">
                <span className="ml-2">{i + 1}. </span>
                <code className="break-all text-xs">
                  {assertion.type}
                  {assertion.tool ? ` / ${assertion.tool}` : ''}
                </code>
              </summary>
              <pre className="code-block mt-4">
                {JSON.stringify(assertion, null, 2)}
              </pre>
            </details>
          ))}
          <details className="mt-5 text-sm">
            <summary>Complete grading specification</summary>
            <pre className="code-block mt-4">
              {JSON.stringify(test.grade, null, 2)}
            </pre>
          </details>
        </section>
        <AppState persona={id} testId={test.id} />
        <details className="section text-sm">
          <summary>Source file</summary>
          <p className="mt-4 font-mono text-xs">
            tests/{id}/{test.id}.yaml
          </p>
          <p className="mt-2 break-all font-mono text-xs text-muted">
            SHA-256: {test.sha256}
          </p>
        </details>
        <nav
          aria-label="Test pages"
          className="flex items-center justify-between gap-3 border-t border-hairline py-6"
        >
          {index > 0 ? (
            <Link className="command" href={testHref(id, tests[index - 1].id)}>
              <ArrowLeft size={16} />
              Test {tests[index - 1].id}
            </Link>
          ) : (
            <span />
          )}
          <Link className="text-link text-sm" href={testsHref(id)}>
            All tests
          </Link>
          {index < tests.length - 1 ? (
            <Link className="command" href={testHref(id, tests[index + 1].id)}>
              Test {tests[index + 1].id}
              <ArrowRight size={16} />
            </Link>
          ) : (
            <span />
          )}
        </nav>
      </div>
    </div>
  );
}
