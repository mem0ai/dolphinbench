'use client';

import { Fragment, useState } from 'react';
import { ArrowDown, ArrowUp, ArrowUpDown, ChevronDown } from 'lucide-react';
import { colorFor, formatTotalCost, formatLatency, percent, personaNames, type Persona, type ResultRow } from '@/lib/results';
import type { SortDirection, SortKey } from '@/lib/results-view';
import { memoryLogos } from '@/lib/logos';
import Logo from './Logo';

const columns: readonly [SortKey, string, boolean][] = [
  ['memory', 'Memory', false],
  ['model', 'Model / provider', false],
  ['harness', 'Harness', false],
  ['accuracy', 'Accuracy', true],
  ['cost', 'Total cost', true],
  ['latency', 'Median latency', true],
  ['p95', 'p95 latency', true],
];

export default function ResultsTable({
  rows,
  total,
  sortKey,
  sortDirection,
  onSort,
  activeId,
  onActive,
}: {
  rows: ResultRow[];
  total: number;
  sortKey: SortKey;
  sortDirection: SortDirection;
  onSort: (key: SortKey) => void;
  activeId: string | null;
  onActive: (id: string | null) => void;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const icon = (key: SortKey) => {
    const Icon = sortKey === key ? sortDirection === 'asc' ? ArrowUp : ArrowDown : ArrowUpDown;
    return <Icon size={13} aria-hidden="true" />;
  };
  const ariaSort = (key: SortKey) =>
    sortKey === key ? (sortDirection === 'asc' ? 'ascending' : 'descending') : 'none';
  return (
    <div className="results-table-wrap">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-4">
        <h3 className="text-md font-semibold">Official results</h3>
        <span className="text-sm text-muted">
          {rows.length} of {total} configurations shown
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="results-table" aria-label="Evaluation results">
          <thead>
            <tr>
              {columns.map(([key, label, right]) => (
                <th key={key} scope="col" aria-sort={ariaSort(key)} className={right ? 'text-right' : undefined}>
                  <button type="button" onClick={() => onSort(key)}>
                    <span>{label}</span>{icon(key)}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <Fragment key={row.id}><tr
                className={activeId === row.id ? 'is-active' : undefined}
                onMouseEnter={() => onActive(row.id)}
                onMouseLeave={() => onActive(null)}
              >
                <th scope="row">
                  <span className="inline-flex items-center gap-2.5 font-semibold">
                    <span
                      className="size-2 shrink-0 rounded-full border border-ink/20"
                      style={{ background: colorFor(row, 'memory') }}
                      aria-hidden="true"
                    />
                    <Logo src={memoryLogos[row.memory.name]} size={18} className="rounded" />
                    {row.memory.name}
                    <button type="button" className="result-expand" title="Scores and evidence"
                      aria-label={`Scores and evidence for ${row.memory.name}, ${row.model.name}, ${row.harness.name}`}
                      aria-expanded={expanded === row.id} aria-controls={`result-${encodeURIComponent(row.id)}`}
                      onClick={() => setExpanded(expanded === row.id ? null : row.id)}><ChevronDown size={16} aria-hidden="true" /></button>
                  </span>
                </th>
                <td>
                  <span className="block">{row.model.name}</span>
                  <span className="block text-sm text-muted">{row.model.provider}</span>
                </td>
                <td>
                  <span className="block">{row.harness.name}</span>
                </td>
                <td className="text-right">
                  <span className="block font-semibold">{percent(row.pass_rate)}</span>
                  <span className="block font-mono text-xs text-muted">
                    {row.passes} / {row.total}
                  </span>
                  {row.runs_url && <a className="text-link text-sm" href={row.runs_url}>View evidence</a>}
                </td>
                <td className="text-right font-mono text-sm">{formatTotalCost(row)}</td>
                <td className="text-right font-mono text-sm">{formatLatency(row.median_latency_seconds)}</td>
                <td className="text-right font-mono text-sm">{formatLatency(row.p95_latency_seconds)}</td>
              </tr>
              {expanded === row.id && <tr id={`result-${encodeURIComponent(row.id)}`}><td colSpan={columns.length}>
                <dl className="persona-scores">{(Object.keys(personaNames) as Persona[]).map(persona => <div key={persona}>
                  <dt><a className="text-link" href={row.runs_url ? `${row.runs_url}/${persona}` : row.personas[persona].source.url}>{personaNames[persona]}</a></dt>
                  <dd>{row.personas[persona].passes} / 200 <span className="text-muted">({percent(row.personas[persona].passes / 200)})</span></dd>
                </div>)}</dl>
                <nav className="result-evidence" aria-label={`Evidence for ${row.memory.name}, ${row.model.name}, ${row.harness.name}`}>
                  {([['configuration', 'Configuration'], ['source', 'Source bundle'], ['recordings', 'Recordings'], ['grades', 'Grades']] as const).map(([key, label]) =>
                    <a key={key} className="text-link" href={row.evidence[key].url}>{label}</a>)}
                </nav>
                {row.agent_inference_cost_scope && <p className="mt-3 text-xs text-muted">{row.agent_inference_cost_scope}</p>}
              </td></tr>}
              </Fragment>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={columns.length} className="py-10 text-sm text-muted">
                  No configurations match these filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <p className="pt-3 text-sm text-muted">A task passes when every required check passes.</p>
    </div>
  );
}
