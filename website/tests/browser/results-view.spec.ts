import { test, expect } from '@playwright/test';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { brotliCompressSync } from 'node:zlib';
import { MockAgent, getGlobalDispatcher, setGlobalDispatcher } from 'undici';
import { GET as downloadEvidence } from '@/app/leaderboard/evidence/[name]/route';
import officialEvidence from '@/content/official-evidence.json';
import { previewEvidenceFile, resultsPreviewDirectory } from '@/lib/local-results';
import {
  colorFor,
  coverageLabel,
  dimensionValues,
  percent,
  totalCost,
  formatTotalCost,
  submissionChartRow,
  type ResultRow,
} from '@/lib/results';
import {
  barList,
  breakdownItems,
  configurationPairs,
  filterRows,
  formatPoints,
  liftOverBuiltin,
  paretoFront,
  sortRows,
  views,
} from '@/lib/results-view';

import fixture from '../fixtures/complete-results.json';
const configurationResults = fixture.configurations.map(row => ({
  ...row, model: fixture.configurations[0].model, harness: fixture.configurations[0].harness,
}));
const synthetic = (
  memory: string,
  passes: number,
  cost: number,
  median: number,
): ResultRow =>
  ({
    ...configurationResults[0],
    id: `${memory}-${passes}`,
    memory: { id: memory.toLowerCase(), name: memory },
    passes,
    pass_rate: passes / 600,
    total_cost_usd: cost,
    median_latency_seconds: median,
  }) as ResultRow;

test('coverage label is computed from the report', () => {
  expect(coverageLabel(3)).toBe(
    '600 tests · 3 personas · 3 official configurations',
  );
  expect(coverageLabel(1)).toContain('1 official configuration');
  expect(percent(424 / 600)).toBe('70.7%');
  expect(percent(417 / 600)).toBe('69.5%');
  expect(barList(breakdownItems([synthetic('Mem0', 424, .01, 30)], 'config'), 'accuracy')[0].value).toBe('70.7%');
});

test('total cost never falls back to evaluation-only cost', () => {
  expect(totalCost(configurationResults[0])).toBe(30);
  expect(formatTotalCost(configurationResults[0])).toBe('$30.00');
  for (const value of [undefined, null, -1, NaN, Infinity]) {
    expect(totalCost({ total_cost_usd: value })).toBeNull();
  }
  expect(formatTotalCost({ total_cost_usd: 0 })).toBe('$0.00');
  const row = submissionChartRow({ id: 'partial-cost', name: 'Test', memory: 'Test',
    model: 'Test', harness: 'Test', status: 'accepted', summary: {
      tests: 600, passes: 300, passes_by_persona: { alex: 100, morgan: 100, riley: 100 },
      ingestion: {}, grading: {}, execution: { records: 600, estimated_model_cost_usd: 6,
        unpriced_model_responses: 0, median_duration_ms: 1000 },
    } })!;
  expect(row).toBeNull();
});

test('evidence downloads use stored size even when compression removes Content-Length', async () => {
  const dispatcher = getGlobalDispatcher();
  const agent = new MockAgent();
  agent.disableNetConnect();
  const token = process.env.BLOB_READ_WRITE_TOKEN;
  const api = process.env.VERCEL_BLOB_API_URL;
  process.env.BLOB_READ_WRITE_TOKEN = 'vercel_blob_rw_teststore_testsecret';
  process.env.VERCEL_BLOB_API_URL = 'https://blob-api.example.test';
  setGlobalDispatcher(agent);
  try {
    const name = 'builtin-sources.json';
    const file = officialEvidence[name];
    const payload = Buffer.alloc(file.bytes, 'a');
    const storage = agent.get('https://teststore.private.blob.vercel-storage.com');
    const metadataApi = agent.get('https://blob-api.example.test');
    const metadata = { pathname: file.pathname, size: file.bytes,
      url: `https://teststore.private.blob.vercel-storage.com/${file.pathname}`,
      uploadedAt: '2026-09-16T00:00:00Z', contentType: 'application/json' };
    metadataApi.intercept({ path: `/?${new URLSearchParams({ url: file.pathname })}`, method: 'GET' })
      .reply(200, metadata, { headers: { 'content-type': 'application/json' } });
    storage.intercept({ path: `/${file.pathname}`, method: 'GET' })
      .reply(200, brotliCompressSync(payload), { headers: {
        'content-type': 'application/json', 'content-encoding': 'br',
      } });
    const request = new Request(`https://dolphinbench.ai/leaderboard/evidence/${name}/`);
    const context = { params: Promise.resolve({ name }) };
    const response = await downloadEvidence(request, context);
    expect(response.status).toBe(200);
    expect(response.headers.get('content-length')).toBe(String(file.bytes));
    expect(response.headers.get('x-content-sha256')).toBe(file.sha256);
    expect(Buffer.from(await response.arrayBuffer())).toEqual(payload);

    metadataApi.intercept({ path: `/?${new URLSearchParams({ url: file.pathname })}`, method: 'GET' })
      .reply(200, { ...metadata, size: file.bytes + 1 }, { headers: { 'content-type': 'application/json' } });
    expect((await downloadEvidence(request, context)).status).toBe(503);
    expect((await downloadEvidence(request, { params: Promise.resolve({ name: 'unknown.zip' }) })).status).toBe(404);
    agent.assertNoPendingInterceptors();
  } finally {
    setGlobalDispatcher(dispatcher);
    if (token === undefined) delete process.env.BLOB_READ_WRITE_TOKEN;
    else process.env.BLOB_READ_WRITE_TOKEN = token;
    if (api === undefined) delete process.env.VERCEL_BLOB_API_URL;
    else process.env.VERCEL_BLOB_API_URL = api;
    await agent.close();
  }
});

test('local evidence preview cannot expose arbitrary files or run in production', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'results-preview-'));
  const keys = ['NODE_ENV', 'DOLPHINBENCH_LOCAL_PREVIEW', 'DOLPHINBENCH_RESULTS_PREVIEW_DIR'];
  const saved = keys.map(key => process.env[key]);
  try {
    Object.assign(process.env, { NODE_ENV: 'development', DOLPHINBENCH_LOCAL_PREVIEW: '1', DOLPHINBENCH_RESULTS_PREVIEW_DIR: directory });
    await writeFile(join(directory, 'preview-files.json'), JSON.stringify({
      'evidence.json': { path: join(directory, 'evidence.json'), sha256: 'a'.repeat(64) },
    }));
    expect(resultsPreviewDirectory(new Request('http://localhost/leaderboard/'))).toBe(directory);
    expect(resultsPreviewDirectory(new Request('https://public.example/leaderboard/'))).toBeNull();
    expect(await previewEvidenceFile(directory, 'evidence.json')).toHaveProperty('sha256');
    for (const name of ['../.env', '%2e%2e', 'missing.json', 'constructor', '__proto__', '/etc/passwd']) {
      expect(await previewEvidenceFile(directory, name)).toBeNull();
    }
    Object.assign(process.env, { NODE_ENV: 'production' });
    expect(resultsPreviewDirectory(new Request('http://localhost/leaderboard/'))).toBeNull();
  } finally {
    keys.forEach((key, i) => { if (saved[i] === undefined) delete process.env[key]; else process.env[key] = saved[i]; });
    await rm(directory, { recursive: true });
  }
});

test('pairs, dimension values, and colors come from the data', () => {
  expect(configurationPairs(configurationResults)).toEqual([
    { key: 'Hermes + Fixture Luna', harness: 'Hermes', model: 'Fixture Luna', provider: 'OpenAI' },
  ]);
  expect(dimensionValues(configurationResults, 'memory')).toEqual(['Mem0', 'Honcho', 'Built-in']);
  expect(colorFor(configurationResults[0], 'memory')).toBe('#3AA7E0');
  expect(colorFor(configurationResults[2], 'memory')).toBe('#A8A39A');
  expect(colorFor(configurationResults[0], 'harness')).toBe('#3AA7E0');
  expect(colorFor(synthetic('Unknown', 1, 1, 1), 'memory')).toMatch(/^#/);
  // "Foo" and "Bar" hash to different indices mod 8 (the extra-palette size), so two
  // different unrecognized names must not collide onto the same fallback color.
  expect(colorFor(synthetic('Foo', 1, 1, 1), 'memory')).not.toBe(
    colorFor(synthetic('Bar', 1, 1, 1), 'memory'),
  );
});

test('filters and sorting', () => {
  const none = { query: '', memory: 'all', pair: 'all' };
  expect(filterRows(configurationResults, none)).toHaveLength(3);
  expect(filterRows(configurationResults, { ...none, query: 'honcho' }).map((r) => r.memory.name)).toEqual(['Honcho']);
  expect(filterRows(configurationResults, { ...none, memory: 'Built-in' })).toHaveLength(1);
  expect(filterRows(configurationResults, { ...none, pair: 'Hermes + Fixture Luna' })).toHaveLength(3);
  expect(filterRows(configurationResults, { ...none, pair: 'Nope' })).toHaveLength(0);
  expect(sortRows(configurationResults, 'accuracy', 'desc')[0].memory.name).toBe('Mem0');
  expect(sortRows(configurationResults, 'cost', 'desc')[0].memory.name).toBe('Built-in');
  expect(sortRows(configurationResults, 'p95', 'asc')[0].memory.name).toBe('Mem0');
  expect(sortRows(configurationResults, 'memory', 'asc').map((r) => r.memory.name)).toEqual(['Built-in', 'Honcho', 'Mem0']);
});

test('pareto front keeps the upper-left staircase', () => {
  const rows = [
    synthetic('A', 100, 0.01, 40),
    synthetic('B', 120, 0.02, 30),
    synthetic('C', 110, 0.03, 20),
    synthetic('D', 130, 0.04, 50),
  ];
  expect(paretoFront(rows, 'cost').map((r) => r.memory.name)).toEqual(['A', 'B', 'D']);
  expect(paretoFront(rows, 'latency').map((r) => r.memory.name)).toEqual(['C', 'B', 'D']);
  expect(paretoFront(configurationResults, 'cost').map((r) => r.memory.name)).toEqual(['Mem0']);

  // Equal-cost ties must keep the higher-pass row, not whichever sorts first.
  const tied = [synthetic('Low', 100, 0.02, 40), synthetic('High', 120, 0.02, 40)];
  expect(paretoFront(tied, 'cost').map((r) => r.memory.name)).toEqual(['High']);
});

test('breakdown views and bars', () => {
  expect(views).toEqual(['config', 'memory', 'model', 'harness']);
  const config = breakdownItems(configurationResults, 'config');
  expect(config[0]).toMatchObject({ label: 'Mem0', memory: 'Mem0', sub: 'Fixture Luna · Hermes' });
  expect(config[0].accuracy).toBeCloseTo(60);
  const byMemory = breakdownItems(configurationResults, 'memory');
  expect(byMemory.map((i) => i.sub)).toEqual(['avg of 1 config', 'avg of 1 config', 'avg of 1 config']);
  const byHarness = breakdownItems(configurationResults, 'harness');
  expect(byHarness).toHaveLength(1);
  expect(byHarness[0]).toMatchObject({ label: 'Hermes', sub: 'avg of 3 configs' });
  expect(byHarness[0].memory).toBeUndefined();
  const accuracy = barList(config, 'accuracy');
  expect(accuracy[0]).toMatchObject({ width: '100.0%', value: '60%' });
  expect(accuracy[2].item.label).toBe('Built-in');
  const cost = barList(config, 'cost');
  expect(cost[0].item.label).toBe('Mem0');
  expect(cost[0].value).toBe('$30.00');
  expect(barList(config, 'median')[0].value).toBe('10.0 s');
});


test('missing metrics stay unavailable and self-submissions never enter official averages', () => {
  const rows = [{ ...configurationResults[0], total_cost_usd: null, median_latency_seconds: null }, configurationResults[1]];
  expect(sortRows(rows, 'cost', 'asc').at(-1)?.memory.name).toBe('Mem0');
  expect(sortRows(rows, 'cost', 'desc').at(-1)?.memory.name).toBe('Mem0');
  expect(paretoFront(rows, 'cost')).toHaveLength(1);
  const grouped = breakdownItems(rows, 'harness');
  expect(grouped[0].cost).toBeNull();
  expect(barList(grouped, 'cost')[0]).toMatchObject({ width: '0.0%', value: 'Unavailable' });
  const submitted = { ...configurationResults[0], id: 'submitted', passes: 600, submission: { name: 'Unverified' } };
  expect(breakdownItems([...configurationResults, submitted], 'config')).toHaveLength(3);
  expect(paretoFront([submitted], 'cost')).toHaveLength(0);
});

test('lift over built-in memory is measured against the same harness and model', () => {
  const withPersonas = (row: ResultRow, passes: [number, number, number]): ResultRow => ({
    ...row,
    passes: passes[0] + passes[1] + passes[2],
    pass_rate: (passes[0] + passes[1] + passes[2]) / 600,
    personas: {
      alex: { ...row.personas.alex, passes: passes[0] },
      morgan: { ...row.personas.morgan, passes: passes[1] },
      riley: { ...row.personas.riley, passes: passes[2] },
    },
  });
  const builtin = withPersonas({ ...synthetic('Built-in', 300, 0.01, 30), memory: { id: 'builtin', name: 'Built-in' } }, [100, 100, 100]);
  const mem0 = withPersonas(synthetic('Mem0', 360, 0.01, 30), [130, 110, 120]);
  const honcho = withPersonas(synthetic('Honcho', 270, 0.01, 30), [80, 100, 90]);
  const items = liftOverBuiltin([builtin, mem0, honcho]);
  expect(items.map((item) => item.memory)).toEqual(['Mem0', 'Honcho']);
  expect(items[0].overall).toBeCloseTo(10);
  expect(items[0].personas.alex).toBeCloseTo(15);
  expect(items[0].personas.morgan).toBeCloseTo(5);
  expect(items[0].personas.riley).toBeCloseTo(10);
  expect(items[1].overall).toBeCloseTo(-5);
  expect(items[1].personas.alex).toBeCloseTo(-10);
  expect(formatPoints(items[0].overall)).toBe('+10 pp');
  expect(formatPoints(items[1].personas.alex)).toBe('-10 pp');
  // No Built-in run for the pair: nothing to compare against.
  expect(liftOverBuiltin([mem0, honcho])).toEqual([]);
  // Baselines only apply within a pair.
  const otherPair = { ...builtin, id: 'other', harness: { id: 'claude-code', name: 'Claude Code' } };
  expect(liftOverBuiltin([otherPair, mem0])).toEqual([]);
});
