'use client';

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Search, X, ArrowUpRight } from 'lucide-react';
import { TestSummary, testHref, testsHref } from '@/lib/types';

export default function TestBrowser({
  personaId,
  tests,
  tools,
}: {
  personaId: string;
  tests: TestSummary[];
  tools: string[];
}) {
  const searchParams = useSearchParams();
  const filters = {
    q: searchParams.get('q') || '',
    tool: searchParams.get('tool') || '',
    fact: searchParams.get('fact') || '',
  };
  const update = (next: typeof filters) => {
    const query = new URLSearchParams();
    Object.entries(next).forEach(([key, value]) => {
      if (value) query.set(key, value);
    });
    window.history.replaceState(
      null,
      '',
      `${testsHref(personaId)}${query.size ? '?' + query : ''}`,
    );
  };
  const q = filters.q.trim().toLowerCase();
  const filtered = tests.filter(
    (test) =>
      (!q || test.request.toLowerCase().includes(q) || test.id.includes(q)) &&
      (!filters.tool || test.tools.includes(filters.tool)) &&
      (!filters.fact || test.fact_ids.includes(Number(filters.fact))),
  );
  return (
    <div>
      <div className="grid items-end gap-3 py-6 sm:grid-cols-[minmax(0,1fr)_minmax(140px,240px)_100px_auto]">
        <label className="min-w-0 text-xs text-muted">
          Search tests
          <div className="relative mt-1">
            <Search
              className="absolute left-3 top-2.5 text-muted"
              size={16}
            />
            <input
              type="search"
              className="field w-full pl-9"
              value={filters.q}
              onChange={(event) =>
                update({ ...filters, q: event.target.value })
              }
            />
          </div>
        </label>
        <div className="min-w-0">
          <label htmlFor="test-tool" className="text-xs text-muted">
            Tool
          </label>
          <select
            id="test-tool"
            name="tool"
            className="field mt-1 w-full"
            value={filters.tool}
            onChange={(event) =>
              update({ ...filters, tool: event.target.value })
            }
          >
            <option value="">All tools</option>
            {tools.map((tool) => (
              <option key={tool}>{tool}</option>
            ))}
          </select>
        </div>
        <label className="text-xs text-muted">
          Fact ID
          <input
            className="field mt-1 w-full"
            type="number"
            min="1"
            value={filters.fact}
            onChange={(event) =>
              update({ ...filters, fact: event.target.value })
            }
          />
        </label>
        <button
          type="button"
          className="command w-[38px] px-0 focus-visible:border-ink focus-visible:outline-none"
          title="Reset filters"
          aria-label="Reset filters"
          onClick={() => update({ q: '', tool: '', fact: '' })}
        >
          <X size={16} />
        </button>
      </div>
      <div
        className="border-y border-hairline py-3 text-sm text-muted"
        role="status"
      >
        {filtered.length} of {tests.length} tests
      </div>
      <div className="hidden grid-cols-[45px_minmax(0,1fr)_190px_60px] gap-5 border-b border-hairline py-3 text-xs text-muted md:grid">
        <span>ID</span>
        <span>Request</span>
        <span>Tools</span>
        <span>Facts</span>
      </div>
      {filtered.map((test) => (
        <Link
          href={testHref(personaId, test.id)}
          key={test.id}
          className="group grid grid-cols-[40px_minmax(0,1fr)_16px] gap-3 border-b border-hairline py-5 transition-colors hover:bg-sand/60 md:grid-cols-[45px_minmax(0,1fr)_190px_60px] md:gap-5"
        >
          <span className="pt-0.5 font-mono text-xs text-muted">
            {test.id}
          </span>
          <span className="min-w-0 whitespace-pre-wrap break-words text-md">
            {test.request}
          </span>
          <ArrowUpRight className="text-muted md:hidden" size={15} />
          <span className="col-start-2 min-w-0 space-y-1 font-mono text-xs text-muted md:col-start-auto">
            {test.tools.map((tool) => (
              <span key={tool} className="block break-all">
                {tool}
              </span>
            ))}
          </span>
          <span className="col-start-2 text-xs text-muted md:col-start-auto">
            {test.fact_ids.length} <span className="md:hidden">facts</span>
          </span>
        </Link>
      ))}
      {filtered.length === 0 && (
        <div className="py-16 text-center">
          <p className="text-muted">No matching tests.</p>
          <button
            className="text-link mt-3 text-sm"
            onClick={() => update({ q: '', tool: '', fact: '' })}
          >
            Clear filters
          </button>
        </div>
      )}
    </div>
  );
}
