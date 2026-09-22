import Link from 'next/link';
import { data, formatDate, number, personaHref } from '@/lib/data';
import StructuredData, { datasetSchema } from '@/components/StructuredData';

export const metadata = {
  title: 'Dataset',
  description: `The complete DolphinBench release: ${data.tests} tasks across ${data.personas.length} simulated users, ` +
    `${data.messages.toLocaleString('en-US')} history messages, and every test's source evidence and grading checks.`,
  alternates: { canonical: '/dataset/' },
};

export default function DatasetPage() {
  return (
    <div className="container-x pb-28 pt-14 sm:pt-20">
      <StructuredData data={[datasetSchema(data)]} />
      <header className="pb-14">
        <h1 className="page-title mb-5">Dataset</h1>
        <p className="text-lg text-muted">
          {data.tests} tasks across {data.personas.length} simulated users.
        </p>
      </header>
      <section aria-label="Personas" className="border-t border-ink">
        {data.personas.map((persona, index) => (
          <article
            key={persona.id}
            className="relative grid gap-6 border-b border-hairline py-9 transition-colors hover:bg-sand/60 md:grid-cols-[64px_minmax(0,1.4fr)_minmax(0,1fr)] md:gap-8"
          >
            <span className="eyebrow pt-2">0{index + 1}</span>
            <div className="flex min-w-0 flex-col">
              <Link
                href={personaHref(persona.id)}
                className="stretched-link mb-1.5 text-4xl font-semibold leading-[1.1] tracking-[-0.025em]"
              >
                {persona.name} <span className="font-normal text-faint" aria-hidden="true">→</span>
              </Link>
              <span className="mb-4 text-base text-muted">
                {persona.role} / {persona.organization}
              </span>
              <span className="max-w-[34em] text-md leading-[1.5]">{persona.summary}</span>
              <span className="eyebrow mt-4">
                {formatDate(persona.start)} – {formatDate(persona.end)}
              </span>
            </div>
            <dl className="grid grid-cols-3 gap-4 content-start pt-2">
              {[
                ['Tests', persona.tests],
                ['Messages', persona.messages],
                ['Facts', persona.facts],
              ].map(([label, value]) => (
                <div key={label} className="flex flex-col gap-1">
                  <dt className="text-xs text-muted">{label}</dt>
                  <dd className="text-[22px] font-semibold tracking-[-0.02em]">{number(value as number)}</dd>
                </div>
              ))}
            </dl>
          </article>
        ))}
      </section>
    </div>
  );
}
