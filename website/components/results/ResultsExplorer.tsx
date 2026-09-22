'use client';

import Link from 'next/link';
import { RotateCcw } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { coverageLabel, dimensionValues, submissionChartRow, type ChartRow, type OfficialResults, type ResultRow } from '@/lib/results';
import { configurationPairs, defaultDirection, filterRows, sortRows, type Metric, type SortDirection, type SortKey, type View, views } from '@/lib/results-view';
import { harnessLogos, providerLogos } from '@/lib/logos';
import Breakdown from './Breakdown';
import Logo from './Logo';
import Lift from './Lift';
import ResultsTable from './ResultsTable';
import ScatterChart from './ScatterChart';

export default function ResultsExplorer(props: { showLeaderboardLink?: boolean }) {
  const [results, setResults] = useState<OfficialResults | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setFailed(false);
    fetch('/leaderboard/results.json', { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error('Results unavailable');
        const data: OfficialResults = await response.json();
        if (data.schema_version !== 2 || !Array.isArray(data.configurations)) {
          throw new Error('Invalid results');
        }
        setResults(data);
      })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [attempt]);
  if (failed) return (
    <div className="py-8 text-sm text-muted" role="alert">
      <p>Results could not be loaded.</p>
      <button className="text-link mt-3" onClick={() => setAttempt(attempt + 1)}>
        <RotateCcw size={14} aria-hidden="true" /> Retry
      </button>
    </div>
  );
  if (!results) return <p className="py-8 text-sm text-muted" role="status">Loading results...</p>;
  return <>
    {results.preview && <p role="status" className="mb-6 border-l-2 border-hairline pl-4 text-sm text-muted">
      {results.preview_note || 'Local results preview. Not published.'}
    </p>}
    <ResultsContent {...props} configurationResults={results.configurations} releaseHash={results.release_sha256} />
  </>;
}


function ResultsContent({ configurationResults, releaseHash, showLeaderboardLink = false }: {
  configurationResults: ResultRow[]; releaseHash?: string; showLeaderboardLink?: boolean;
}) {
  const [query, setQuery] = useState('');
  const [memory, setMemory] = useState('all');
  const [pair, setPair] = useState('all');
  const [sortKey, setSortKey] = useState<SortKey>('accuracy');
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc');
  const [activeId, setActiveId] = useState<string | null>(null);
  const [metric, setMetric] = useState<Metric>('cost');
  const [view, setView] = useState<View>('config');
  const [includeSubmissions, setIncludeSubmissions] = useState(false);
  const [submittedRows, setSubmittedRows] = useState<ChartRow[]>([]);
  const [submissionState, setSubmissionState] = useState<'loading' | 'ready' | 'error'>('loading');
  const [submissionAttempt, setSubmissionAttempt] = useState(0);
  useEffect(() => {
    if (!includeSubmissions) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    setSubmissionState('loading');
    async function load() {
      try {
        const rows = new Map<string, ChartRow>();
        const seen = new Set<string>();
        for (let page = 0; ; page++) {
          const response = await fetch(`/api/submissions/?view=public&page=${page}`, {
            signal: controller.signal, cache: 'no-store',
          });
          if (!response.ok) throw new Error('Submissions unavailable');
          const data = await response.json();
          if (!Array.isArray(data.submissions) || typeof data.hasMore !== 'boolean') throw new Error('Invalid submissions');
          const previousCount = seen.size;
          for (const submission of data.submissions) {
            seen.add(submission.id);
            const row = submissionChartRow(submission, releaseHash);
            if (row) rows.set(row.id, row);
          }
          if (!data.hasMore) break;
          if (seen.size === previousCount) throw new Error('Invalid submissions pagination');
        }
        if (!controller.signal.aborted) {
          setSubmittedRows([...rows.values()]); setSubmissionState('ready');
        }
      } catch {
        if (!controller.signal.aborted) {
          setSubmittedRows([]); setSubmissionState('error');
        }
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(load, 30_000);
      }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [includeSubmissions, submissionAttempt, releaseHash]);

  const configurations = useMemo(() => [...configurationResults, ...(includeSubmissions ? submittedRows : [])],
    [configurationResults, includeSubmissions, submittedRows]);
  const pairs = configurationPairs(configurations);
  const memories = dimensionValues(configurations, 'memory');
  const rows = sortRows(filterRows(configurations, { query, memory, pair }), sortKey, sortDirection);
  const officialRows = sortRows(filterRows(configurationResults, { query, memory, pair }), sortKey, sortDirection);
  const sort = (key: SortKey) => {
    if (key === sortKey) setSortDirection(current => current === 'asc' ? 'desc' : 'asc');
    else { setSortKey(key); setSortDirection(defaultDirection(key)); }
  };

  return <div className="results-explorer">
    <div className="flex flex-wrap items-baseline justify-between gap-6 border-t border-ink pb-5 pt-8">
      <div className="flex flex-wrap items-baseline gap-5">
        <h2 className="text-2xl font-semibold">Results</h2>
        <span className="text-sm text-muted">{coverageLabel(configurationResults.length)}</span>
      </div>
      <div className="flex gap-6 text-sm font-medium">
        <a href="/leaderboard/results.json" download="dolphinbench-results.json" className="text-link">Official results JSON</a>
        {showLeaderboardLink && <Link href="/leaderboard/" className="text-link">Full leaderboard <span aria-hidden="true">→</span></Link>}
      </div>
    </div>
    <p className="mb-6 text-sm text-muted">Cost includes agent and memory-system processing during ingestion and testing.</p>
    <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
      <div className="segmented min-w-0" role="group" aria-label="Configuration groups">
        <button type="button" aria-pressed={pair === 'all'} onClick={() => setPair('all')}>All configurations</button>
        {pairs.map(item => <button key={item.key} type="button" className="min-w-0 max-w-full break-words"
          aria-pressed={pair === item.key} onClick={() => setPair(item.key)}>
          <Logo src={harnessLogos[item.harness]} /><Logo src={providerLogos[item.provider]} />
          <span className="min-w-0 break-words">{item.key}</span>
        </button>)}
      </div>
      <div className="ml-auto flex max-w-full flex-wrap items-center gap-2">
        <input type="search" className="field w-40" placeholder="Search" aria-label="Search configurations" value={query} onChange={event => setQuery(event.target.value)} />
        <select className="field" aria-label="Filter by memory system" value={memory} onChange={event => setMemory(event.target.value)}>
          <option value="all">All memory systems</option>
          {memories.map(name => <option key={name}>{name}</option>)}
        </select>
      </div>
    </div>
    <div className="mb-8 flex flex-wrap items-center gap-3 text-sm">
      <label className="inline-flex items-center gap-2">
        <input type="checkbox" checked={includeSubmissions} onChange={event => {
          setIncludeSubmissions(event.target.checked); setSubmittedRows([]); setQuery(''); setMemory('all'); setPair('all'); setActiveId(null);
        }} />Include self-submitted runs
      </label>
      {includeSubmissions && submissionState === 'loading' && <span role="status" className="text-muted">Loading self-submitted runs...</span>}
      {includeSubmissions && submissionState === 'ready' && submittedRows.length === 0 && <span className="text-muted">No self-submitted runs yet.</span>}
      {includeSubmissions && submissionState === 'error' && <span role="alert" className="text-fail">
        Self-submitted runs could not be loaded.{' '}
        <button type="button" className="text-link" onClick={() => setSubmissionAttempt(value => value + 1)}>Retry self-submitted runs</button>
      </span>}
    </div>
    <ScatterChart rows={rows} allRows={configurations} metric={metric} onMetric={setMetric} activeId={activeId} onActive={setActiveId} showSubtitles={pair === 'all'} />
    {configurationResults.length ? <>
      <Breakdown rows={officialRows} view={view} onView={setView} views={views} total={600} />
      <Lift rows={configurationResults} visibleIds={new Set(officialRows.map(row => row.id))} />
      <ResultsTable rows={officialRows} total={configurationResults.length} sortKey={sortKey} sortDirection={sortDirection} onSort={sort} activeId={activeId} onActive={setActiveId} />
    </> : <p className="py-8 text-sm text-muted">No complete official results published yet.</p>}
  </div>;
}
