import type { Submission } from './submissions';

export type Artifact = { url: string; sha256: string };
export type Persona = 'alex' | 'morgan' | 'riley';
export const personaNames: Record<Persona, string> = {
  alex: 'Alex', morgan: 'Morgan', riley: 'Riley',
};
export type ResultRow = {
  id: string;
  memory: { id: string; name: string };
  harness: { id: string; name: string };
  model: { id: string; name: string; provider: string };
  personas: Record<Persona, { total: number; passes: number; source: Artifact }>;
  evidence: Record<'configuration' | 'source' | 'recordings' | 'grades', Artifact>;
  total: number;
  passes: number;
  pass_rate: number;
  total_cost_usd_test_calls: number | null;
  total_cost_usd?: number | null;
  total_cost_scope?: string;
  median_latency_seconds: number | null;
  p95_latency_seconds: number | null;
  agent_inference_cost_scope?: string;
  runs_url?: string;
};
export type OfficialResults = { schema_version: 2; configurations: ResultRow[]; release_sha256?: string; preview?: boolean; preview_note?: string };
export type ChartRow = Omit<ResultRow, 'personas' | 'evidence'> & {
  submission?: { name: string };
};
export const coverageLabel = (count: number) => `600 tests · 3 personas · ${count} official configuration${count === 1 ? '' : 's'}`;
export const percent = (rate: number) => `${Number((rate * 100).toFixed(1))}%`;
export const totalCost = (row: Pick<ResultRow, 'total_cost_usd'>) =>
  measurement(row.total_cost_usd);
export const formatTotalCost = (row: Pick<ResultRow, 'total_cost_usd'>) => {
  const cost = totalCost(row);
  return cost === null ? 'Unavailable' : `$${cost.toFixed(2)}`;
};
export const formatLatency = (seconds: number | null) =>
  seconds === null ? 'Unavailable' : seconds > 0 && seconds < 1
    ? `${Number((seconds * 1000).toFixed(1))} ms` : `${seconds.toFixed(1)} s`;

function measurement(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

export function submissionMetrics(summary: Submission['summary']) {
  const execution = summary?.execution;
  const ingestionCost = measurement(summary?.ingestion?.total_cost_usd);
  const executionCost = measurement(execution?.total_cost_usd);
  const cost = measurement(summary?.total_cost_usd);
  const median = measurement(execution?.median_duration_ms);
  const p95 = measurement(execution?.p95_duration_ms);
  const complete = summary?.tests === 600 && execution?.records === summary.tests;
  return {
    totalCost: complete && ingestionCost !== null && executionCost !== null
      && cost !== null && cost === ingestionCost + executionCost ? cost : null,
    medianLatency: complete && median !== null ? median / 1000 : null,
    p95Latency: complete && p95 !== null ? p95 / 1000 : null,
  };
}

export function submissionChartRow(row: Pick<Submission, 'id' | 'name' | 'memory' | 'model' | 'harness' | 'status' | 'summary'> & { release_sha256?: string | null }, releaseHash?: string): ChartRow | null {
  if (releaseHash && row.release_sha256 !== releaseHash) return null;
  const summary = row.summary;
  if (row.status !== 'accepted' || summary?.tests !== 600
      || !Number.isInteger(summary.passes) || summary.passes < 0 || summary.passes > 600) return null;
  const metrics = submissionMetrics(summary);
  if (metrics.totalCost === null) return null;
  return {
    id: `submission:${row.id}`, submission: { name: row.name },
    memory: { id: row.memory.toLowerCase(), name: row.memory },
    model: { id: row.model, name: row.model, provider: 'Not reported' },
    harness: { id: row.harness, name: row.harness },
    total: summary.tests, passes: summary.passes, pass_rate: summary.passes / summary.tests,
    total_cost_usd_test_calls: measurement(summary.execution.estimated_model_cost_usd),
    total_cost_usd: metrics.totalCost,
    median_latency_seconds: metrics.medianLatency, p95_latency_seconds: metrics.p95Latency,
  };
}

export type Dimension = 'memory' | 'model' | 'harness';
const seriesPalettes: Record<Dimension, Record<string, string>> = {
  memory: { Mem0: '#3AA7E0', Honcho: '#FF8A5B', Hindsight: '#7B61FF', Supermemory: '#2FA98C', 'Built-in': '#A8A39A' },
  model: { 'GPT 5.6 Luna': '#1F4E79', 'MiniMax M2.5': '#D97B4A', 'MiniMax M3': '#D97B4A', 'Claude Sonnet 4.6': '#C9A227', 'Claude Sonnet 5': '#C9A227' },
  harness: { Hermes: '#3AA7E0', 'Claude Code': '#D97B4A' },
};
const extraPalette = ['#1F4E79', '#D97B4A', '#C9A227', '#7B61FF', '#2FA98C', '#B3261E', '#3AA7E0', '#FF8A5B'];
export const dimensionValue = (row: ChartRow, dimension: Dimension) => row[dimension].name;
export const dimensionValues = (rows: ChartRow[], dimension: Dimension) =>
  Array.from(new Set(rows.map(row => dimensionValue(row, dimension))));
export function colorFor(row: ChartRow, dimension: Dimension) {
  const value = dimensionValue(row, dimension);
  const hash = value.split('').reduce((sum, char) => sum + char.charCodeAt(0), 0);
  return seriesPalettes[dimension][value] ?? extraPalette[hash % extraPalette.length];
}
