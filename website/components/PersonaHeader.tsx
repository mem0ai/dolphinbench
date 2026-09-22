import Link from 'next/link';
import { data, Persona, personaHref, testsHref, timelineHref } from '@/lib/data';

export default function PersonaHeader({
  persona,
  active,
}: {
  persona: Persona;
  active: 'overview' | 'tests' | 'history';
}) {
  const index = data.personas.findIndex((item) => item.id === persona.id) + 1;
  return (
    <header>
      <nav aria-label="Personas" className="mb-10 flex flex-wrap gap-x-5 gap-y-2 text-sm">
        <Link href="/dataset/" className="text-muted transition-colors hover:text-ink">Dataset</Link>
        {data.personas.map((item) => (
          <Link
            key={item.id}
            href={personaHref(item.id)}
            aria-current={item.id === persona.id ? 'true' : undefined}
            className={item.id === persona.id ? 'font-semibold text-ink' : 'text-muted transition-colors hover:text-ink'}
          >
            {item.name}
          </Link>
        ))}
      </nav>
      <div className="grid items-end gap-6 border-b border-ink pb-10 md:grid-cols-2 md:gap-12">
        <div>
          <p className="eyebrow mb-4">
            0{index} / {persona.id}
          </p>
          <h1 className="page-title mb-3">{persona.name}</h1>
          <p className="text-md text-muted">
            {persona.role} / {persona.organization} <span className="text-faint">(initial profile)</span>
          </p>
        </div>
        <p className="max-w-[32em] text-lg leading-[1.5]">{persona.summary}</p>
      </div>
      <nav aria-label="Persona views" className="sub-nav mt-6">
        {(
          [
            ['overview', 'Overview', personaHref(persona.id)],
            ['tests', `Tests (${persona.tests})`, testsHref(persona.id)],
            ['history', 'History', timelineHref(persona.id)],
          ] as const
        ).map(([id, label, href]) => (
          <Link key={id} href={href} aria-current={id === active ? 'page' : undefined}>
            {label}
          </Link>
        ))}
      </nav>
    </header>
  );
}
