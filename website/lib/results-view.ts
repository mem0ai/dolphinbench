import {
  colorFor,
  totalCost,
  dimensionValue,
  personaNames,
  type Dimension,
  type ChartRow,
  type Persona,
  type ResultRow,
} from './results';

export type Metric = 'cost' | 'latency';
export type SortKey = 'memory' | 'model' | 'harness' | 'accuracy' | 'cost' | 'latency' | 'p95';
export type SortDirection = 'asc' | 'desc';
export type View = 'config' | Dimension;
export type Pair = { key: string; harness: string; model: string; provider: string };
export type Filters = { query: string; memory: string; pair: string };

export const pairKey = (row: ChartRow) => `${row.harness.name} + ${row.model.name}`;

export function configurationPairs(rows: ChartRow[]): Pair[] {
  const pairs = new Map<string, Pair>();
  rows.forEach((row) => {
    const key = pairKey(row);
    if (!pairs.has(key))
      pairs.set(key, { key, harness: row.harness.name, model: row.model.name, provider: row.model.provider });
  });
  return Array.from(pairs.values());
}

export function filterRows<T extends ChartRow>(rows: T[], filters: Filters): T[] {
  const query = filters.query.trim().toLowerCase();
  return rows.filter(
    (row) =>
      (filters.memory === 'all' || row.memory.name === filters.memory) &&
      (filters.pair === 'all' || pairKey(row) === filters.pair) &&
      (!query ||
        `${row.memory.name} ${row.model.name} ${row.model.provider} ${row.harness.name} ${row.submission?.name || ''}`
          .toLowerCase()
          .includes(query)),
  );
}

const sortValue: Record<SortKey, (row: ChartRow) => string | number | null> = {
  memory: (row) => row.memory.name,
  model: (row) => row.model.name,
  harness: (row) => row.harness.name,
  accuracy: (row) => row.pass_rate,
  cost: (row) => totalCost(row),
  latency: (row) => row.median_latency_seconds,
  p95: (row) => row.p95_latency_seconds,
};

export const defaultDirection = (key: SortKey): SortDirection => (key === 'accuracy' ? 'desc' : 'asc');

export function sortRows<T extends ChartRow>(rows: T[], key: SortKey, direction: SortDirection): T[] {
  const value = sortValue[key];
  return [...rows].sort((a, b) => {
    const x = value(a);
    const y = value(b);
    if (x === null || y === null) return x === y ? 0 : x === null ? 1 : -1;
    const order = typeof x === 'string' ? x.localeCompare(String(y)) : x - Number(y);
    return direction === 'asc' ? order : -order;
  });
}

export const metricValue = (row: ChartRow, metric: Metric) =>
  metric === 'cost' ? totalCost(row) : row.median_latency_seconds;

// Upper-left staircase: walking left to right, keep every point whose pass count exceeds the best so far.
export function paretoFront(rows: ChartRow[], metric: Metric) {
  let best = -1;
  return rows.filter(row => !row.submission && metricValue(row, metric) !== null)
    .sort((a, b) => metricValue(a, metric)! - metricValue(b, metric)! || b.passes - a.passes)
    .filter((row) => {
      if (row.passes <= best) return false;
      best = row.passes;
      return true;
    });
}

// Every view is always offered, even when a dimension has a single value, so the
// controls do not change shape as configurations are added.
export const views: View[] = ['config', 'memory', 'model', 'harness'];

export type BreakdownItem = {
  label: string;
  sub: string;
  color: string;
  memory?: string;
  accuracy: number;
  cost: number | null;
  median: number | null;
};

export function breakdownItems(rows: ChartRow[], view: View): BreakdownItem[] {
  rows = rows.filter(row => !row.submission);
  if (view === 'config')
    return rows.map((row) => ({
      label: row.memory.name,
      memory: row.memory.name,
      sub: `${row.model.name} · ${row.harness.name}`,
      color: colorFor(row, 'memory'),
      accuracy: row.pass_rate * 100,
      cost: totalCost(row),
      median: row.median_latency_seconds,
    }));
  const groups = new Map<string, ChartRow[]>();
  rows.forEach((row) => {
    const key = dimensionValue(row, view);
    groups.set(key, [...(groups.get(key) ?? []), row]);
  });
  return Array.from(groups.entries()).map(([label, group]) => {
    const average = (value: (row: ChartRow) => number | null) => {
      const values = group.map(value);
      return values.some(value => value === null) ? null : values.reduce<number>((sum, value) => sum + value!, 0) / group.length;
    };
    return {
      label,
      memory: view === 'memory' ? label : undefined,
      sub: `avg of ${group.length} config${group.length === 1 ? '' : 's'}`,
      color: colorFor(group[0], view),
      accuracy: average((row) => row.pass_rate * 100)!,
      cost: average(totalCost),
      median: average((row) => row.median_latency_seconds),
    };
  });
}

export type BarKey = 'accuracy' | 'cost' | 'median';
export type Bar = { item: BreakdownItem; width: string; value: string };

const barFormat: Record<BarKey, (value: number) => string> = {
  accuracy: (value) => `${Number(value.toFixed(1))}%`,
  cost: (value) => `$${value.toFixed(2)}`,
  median: (value) => `${value.toFixed(1)} s`,
};

export function barList(items: BreakdownItem[], key: BarKey): Bar[] {
  const ascending = key !== 'accuracy';
  const sorted = [...items].sort((a, b) => {
    const x = a[key], y = b[key];
    return x === null || y === null ? x === y ? 0 : x === null ? 1 : -1 : ascending ? x - y : y - x;
  });
  const max = Math.max(...sorted.map((item) => item[key] ?? 0), 0) || 1;
  return sorted.map((item) => ({
    item,
    width: `${(((item[key] ?? 0) / max) * 100).toFixed(1)}%`,
    value: item[key] === null ? 'Unavailable' : barFormat[key](item[key]!),
  }));
}

export type LiftItem = {
  id: string;
  memory: string;
  pair: string;
  color: string;
  overall: number;
  personas: Record<Persona, number>;
};

const BUILTIN = 'builtin';
export const personaIds = Object.keys(personaNames) as Persona[];

// Accuracy change, in percentage points, of each memory system over the Built-in run
// (no external memory) with the same harness and model. Pairs without a Built-in run
// have no baseline and produce no items.
export function liftOverBuiltin(rows: ResultRow[]): LiftItem[] {
  const groups = new Map<string, ResultRow[]>();
  rows.forEach((row) => {
    const key = pairKey(row);
    groups.set(key, [...(groups.get(key) ?? []), row]);
  });
  const points = (passes: number, total: number) => (passes / total) * 100;
  return Array.from(groups.entries()).flatMap(([pair, group]) => {
    const baseline = group.find((row) => row.memory.id === BUILTIN);
    if (!baseline) return [];
    return group
      .filter((row) => row !== baseline)
      .map((row) => ({
        id: row.id,
        memory: row.memory.name,
        pair,
        color: colorFor(row, 'memory'),
        overall: points(row.passes, row.total) - points(baseline.passes, baseline.total),
        personas: Object.fromEntries(personaIds.map((persona) => [
          persona,
          points(row.personas[persona].passes, row.personas[persona].total)
            - points(baseline.personas[persona].passes, baseline.personas[persona].total),
        ])) as Record<Persona, number>,
      }))
      .sort((a, b) => b.overall - a.overall);
  });
}

export const formatPoints = (value: number) => `${value > 0 ? '+' : ''}${Number(value.toFixed(1))} pp`;
