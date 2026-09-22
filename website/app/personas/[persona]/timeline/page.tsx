import Link from 'next/link';
import { notFound } from 'next/navigation';
import { ArrowLeft, ArrowRight, Search } from 'lucide-react';
import { getPersona, getHistory, timelineHref, number } from '@/lib/data';
import PersonaHeader from '@/components/PersonaHeader';
import SessionCard from '@/components/SessionCard';
import { personaMetadata } from '@/lib/persona-metadata';

export async function generateMetadata({ params }: { params: Promise<{ persona: string }> }) {
  return personaMetadata((await params).persona, 'timeline');
}

type Query = Record<string, string | string[] | undefined>;
export default async function TimelinePage({
  params,
  searchParams,
}: {
  params: Promise<{ persona: string }>;
  searchParams: Promise<Query>;
}) {
  const { persona: id } = await params;
  const persona = getPersona(id);
  if (!persona) notFound();
  const query = await searchParams;
  const value = (key: string) =>
    typeof query[key] === 'string' ? (query[key] as string) : '';
  const q = value('q').trim();
  const from = /^\d{4}-\d{2}-\d{2}$/.test(value('from')) ? value('from') : '';
  const to = /^\d{4}-\d{2}-\d{2}$/.test(value('to')) ? value('to') : '';
  const selected = value('session');
  const history = getHistory(id);
  if (selected && !history.some((message) => message.id === selected))
    notFound();
  // Evidence deep links always locate the original message, independent of stale filters.
  const filtered = selected
    ? history
    : history.filter(
        (message) =>
          (!q ||
            message.content.toLowerCase().includes(q.toLowerCase()) ||
            message.id.includes(q)) &&
          (!from || message.date.slice(0, 10) >= from) &&
          (!to || message.date.slice(0, 10) <= to),
      );
  const size = 40;
  const pages = Math.max(1, Math.ceil(filtered.length / size));
  const requested = Number(value('page'));
  const page = selected
    ? Math.floor(
        filtered.findIndex((message) => message.id === selected) / size,
      ) + 1
    : Math.min(
        pages,
        Math.max(1, Number.isSafeInteger(requested) ? requested : 1),
      );
  const pageHref = (next: number) => {
    const search = new URLSearchParams();
    if (!selected) {
      if (q) search.set('q', q);
      if (from) search.set('from', from);
      if (to) search.set('to', to);
    }
    search.set('page', String(next));
    return `${timelineHref(id)}?${search}`;
  };
  return (
    <div className="page">
      <PersonaHeader persona={persona} active="history" />
      <form
        key={JSON.stringify([selected, q, from, to])}
        action={timelineHref(id)}
        className="grid items-end gap-3 py-6 sm:grid-cols-[minmax(0,1fr)_150px_150px_auto]"
      >
        <label className="min-w-0 text-xs text-muted">
          Search history
          <input
            name="q"
            defaultValue={selected ? '' : q}
            className="field mt-1 w-full"
            type="search"
          />
        </label>
        <label className="min-w-0 text-xs text-muted">
          From
          <input
            name="from"
            defaultValue={selected ? '' : from}
            className="field mt-1 w-full"
            type="date"
          />
        </label>
        <label className="min-w-0 text-xs text-muted">
          To
          <input
            name="to"
            defaultValue={selected ? '' : to}
            className="field mt-1 w-full"
            type="date"
          />
        </label>
        <button className="command" type="submit">
          <Search size={16} />
          Search
        </button>
      </form>
      <div className="flex flex-wrap items-center justify-between gap-3 border-y border-hairline py-3 text-sm">
        <span>
          {number(filtered.length)} messages
          {filtered.length > 0 &&
            ` / ${number((page - 1) * size + 1)}-${number(Math.min(page * size, filtered.length))}`}
        </span>
        {(q || from || to || selected) && (
          <Link className="text-link" href={timelineHref(id)}>
            Clear filters
          </Link>
        )}
      </div>
      {filtered.slice((page - 1) * size, page * size).map((message) => (
        <SessionCard
          key={message.id}
          personaId={id}
          session={message}
          open={message.id === selected}
        />
      ))}
      {filtered.length === 0 && (
        <p className="py-16 text-center text-muted" role="status">
          No matching messages.
        </p>
      )}
      <nav
        aria-label="History pages"
        className="flex items-center justify-between gap-3 py-6 text-sm"
      >
        {page > 1 ? (
          <Link className="command" href={pageHref(page - 1)}>
            <ArrowLeft size={16} />
            Previous
          </Link>
        ) : (
          <span />
        )}
        <span className="text-muted">
          Page {page} of {pages}
        </span>
        {page < pages ? (
          <Link className="command" href={pageHref(page + 1)}>
            Next
            <ArrowRight size={16} />
          </Link>
        ) : (
          <span />
        )}
      </nav>
    </div>
  );
}
