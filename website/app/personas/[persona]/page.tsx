import Link from 'next/link';
import { notFound } from 'next/navigation';
import {
  getPersona,
  getPersonaData,
  getHistory,
  number,
  formatDate,
  testsHref,
  testHref,
  timelineHref,
  messageHref,
} from '@/lib/data';
import PersonaHeader from '@/components/PersonaHeader';
import { personaMetadata } from '@/lib/persona-metadata';

export async function generateMetadata({ params }: { params: Promise<{ persona: string }> }) {
  return personaMetadata((await params).persona, 'overview');
}

export default async function PersonaPage({ params }: { params: Promise<{ persona: string }> }) {
  const { persona: id } = await params;
  const persona = getPersona(id);
  if (!persona) notFound();
  const { tests } = getPersonaData(id);
  const history = getHistory(id);
  return (
    <div className="page">
      <PersonaHeader persona={persona} active="overview" />
      <dl className="mb-16 grid grid-cols-2 gap-6 border-b border-hairline py-7 md:grid-cols-4">
        {[
          ['History messages', persona.messages],
          ['User‑message tokens', persona.tokens],
          ['Facts', persona.facts],
          ['Tests', persona.tests],
        ].map(([label, value]) => (
          <div key={label}>
            <dt className="stat-label mb-1.5">{label}</dt>
            <dd className="stat-value">{number(value as number)}</dd>
          </div>
        ))}
      </dl>

      <section>
        <div className="flex items-baseline justify-between border-b border-ink pb-3">
          <h2 className="text-xl font-semibold tracking-[-0.015em]">Tests</h2>
          <Link href={testsHref(id)} className="text-link text-sm">
            All {persona.tests} <span aria-hidden="true">→</span>
          </Link>
        </div>
        {tests.slice(0, 4).map((test) => (
          <Link
            key={test.id}
            href={testHref(id, test.id)}
            className="grid grid-cols-[64px_1fr] gap-4 border-b border-hairline py-[18px] text-md leading-[1.5] transition-colors hover:bg-sand/60"
          >
            <span className="eyebrow pt-[3px] text-sm">{test.id}</span>
            <span className="line-clamp-2 min-w-0">{test.request}</span>
          </Link>
        ))}
      </section>

      <section>
        <div className="flex items-baseline justify-between border-b border-ink pb-3 pt-16">
          <h2 className="text-xl font-semibold tracking-[-0.015em]">Latest history</h2>
          <Link href={timelineHref(id)} className="text-link text-sm">
            Full history <span aria-hidden="true">→</span>
          </Link>
        </div>
        {history
          .slice(-3)
          .reverse()
          .map((message) => (
            <Link
              key={message.id}
              href={messageHref(id, message.id)}
              className="block border-b border-hairline py-[18px] text-md leading-[1.5] transition-colors hover:bg-sand/60"
            >
              <div className="eyebrow mb-2">
                {formatDate(message.date)} / {message.id}
              </div>
              <p className="line-clamp-2">{message.content}</p>
            </Link>
          ))}
      </section>

      <section>
        <h2 className="mb-3 mt-16 border-b border-ink pb-3 text-xl font-semibold tracking-[-0.015em]">
          Tools referenced by tests
        </h2>
        <ul className="flex flex-wrap gap-x-6 gap-y-2 pt-2 font-mono text-sm leading-[1.9]">
          {persona.tools.map((tool) => (
            <li className="break-all" key={tool}>
              <Link className="hover:underline" href={`${testsHref(id)}?tool=${encodeURIComponent(tool)}`}>
                {tool}
              </Link>
            </li>
          ))}
        </ul>
      </section>

      <details className="section details-plain mt-16 text-sm">
        <summary>Release provenance</summary>
        <dl className="mt-4 space-y-3">
          {[
            ['Initial profile', persona.profile_source],
            ['Checkpoint SHA-256', persona.checkpoint_sha256],
            ['History SHA-256', persona.history_sha256],
            ['Facts SHA-256', persona.facts_sha256],
          ].map(([label, value]) => (
            <div key={label}>
              <dt className="text-muted">{label}</dt>
              <dd className="break-all font-mono text-xs">{value}</dd>
            </div>
          ))}
        </dl>
      </details>
    </div>
  );
}
