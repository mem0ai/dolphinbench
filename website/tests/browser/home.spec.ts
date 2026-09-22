import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import results from '../fixtures/complete-results.json';
import publishedResults from '../../content/official-results.json';
const resultUrl = /\/leaderboard\/results\.json\/?$/;

test.beforeEach(async ({ page }) => {
  await page.route(resultUrl, route => route.fulfill({ json: results }));
  await page.route('**/api/submissions/**', route => route.fulfill({ json: {
    enabled: false, submissions: [], hasMore: false,
  } }));
});

for (const viewport of [
  { width: 1440, height: 1000 },
  { width: 390, height: 844 },
  { width: 320, height: 760 },
]) {
  test(`home and leaderboard at ${viewport.width}px`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize(viewport);
    const errors: string[] = [];
    const consoleErrors: string[] = [];
    const hydrationWarnings: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('console', (message) => {
      if (
        message.type() === 'error' &&
        message.text().includes('tree hydrated but some attributes')
      ) {
        hydrationWarnings.push(message.text());
      }
      if (message.type() === 'error') consoleErrors.push(message.text());
    });
    await page.goto('/');
    const navigation = page.getByRole('navigation', {
      name: 'Primary navigation',
    });
    await expect(navigation.getByRole('link')).toHaveText([
      'Home',
      'Leaderboard',
      'Dataset',
      'Run and submit',
      /^Paper/,
    ]);
    await expect(
      navigation.getByRole('link', { name: 'Home', exact: true }),
    ).toHaveAttribute('aria-current', 'page');
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(
      'Mapping the Pareto frontier of agent memory',
    );
    await expect(page.locator('main')).toContainText('DolphinBench · 3 personas · 600 tasks');
    await expect(page.getByRole('link', { name: 'View leaderboard', exact: true })).toHaveAttribute('href', '/leaderboard/');
    await expect(page.locator('main')).toContainText(
      'Agents complete tasks that depend on past conversations and must recognize which earlier information matters to guide their decisions and actions.',
    );
    await expect(page.locator('main canvas')).toHaveCount(0);
    await expect(page.locator('main')).not.toContainText('SHA-256');
    await expect(page.locator('main')).not.toContainText('user-message tokens');
    await expect(page.locator('main')).not.toContainText(
      'Mem0 leads this evaluation',
    );
    await expect(page.locator('main')).not.toContainText('Morgan / 200 tasks');
    await expect(page.locator('main')).not.toContainText(
      'Cost covers token-priced agent inference only',
    );
    const headings = await page
      .locator('main h1, main h2, main h3')
      .allTextContents();
    expect(headings.every((heading) => !heading.includes('?'))).toBe(true);

    const table = page.getByRole('table', {
      name: 'Evaluation results',
    });
    await expect(table.locator('tbody tr')).toHaveCount(3);
    if (viewport.width >= 640) {
      const offsets = await table.evaluate(element => [3, 4, 5].map(index => {
        const heading = element.querySelectorAll('thead tr:last-child th')[index].querySelector('button > span')!;
        const cell = element.querySelectorAll('tbody tr:first-child > *')[index];
        const value = cell.querySelector('strong') || cell.lastChild!;
        const range = document.createRange();
        range.selectNodeContents(value);
        return Math.abs(heading.getBoundingClientRect().right - range.getBoundingClientRect().right);
      }));
      expect(offsets.every(offset => offset < 1)).toBe(true);
    }

    for (const [name, passes, rate, cost, latency] of [
      ['Mem0', 360, '60%', '$30.00', '10.0 s'],
      ['Honcho', 300, '50%', '$60.00', '20.0 s'],
      ['Built-in', 240, '40%', '$90.00', '30.0 s'],
    ] as const) {
      const row = table.getByRole('row').filter({ hasText: name });
      await expect(row).toContainText(`${passes} / 600`);
      await expect(row).toContainText(rate);
      await expect(row).toContainText(cost);
      await expect(row).toContainText(latency);
      const fixture = results.configurations.find(item => item.memory.name === name)!;
      await expect(row).toContainText(fixture.model.name);
      await expect(row).toContainText(fixture.model.provider);
      await expect(row).toContainText(fixture.harness.name);
    }
    for (const [name, p95] of [
      ['Mem0', '20.0 s'],
      ['Honcho', '40.0 s'],
      ['Built-in', '60.0 s'],
    ] as const) {
      await expect(table.getByRole('row').filter({ hasText: name })).toContainText(p95);
    }
    await expect(page.locator('main')).toContainText(
      '600 tests · 3 personas · 3 official configurations',
    );
    await expect(page.getByRole('group', { name: 'Configuration groups' })).toHaveCount(1);
    await expect(page.getByRole('heading', { name: 'Accuracy vs. cost', exact: true })).toBeVisible();
    await expect(page.getByRole('img', { name: /Accuracy versus cost/ })).toHaveCount(1);
    await expect(page.locator('main')).toContainText('Most attractive quadrant');
    await expect(page.locator('.chart-marker')).toHaveCount(3);
    await expect(page.locator('.chart-frontier')).toHaveCount(0);
    await page.getByRole('button', { name: 'Latency', exact: true }).click();
    await expect(page.getByRole('img', { name: /Accuracy versus latency/ })).toHaveCount(1);
    await page.getByRole('button', { name: 'Cost', exact: true }).click();
    expect(
      await page.evaluate(() => {
        const chart = document.querySelector('.chart-stage')!;
        const table = document.querySelector('.results-table-wrap')!;
        return Boolean(chart.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING);
      }),
    ).toBe(true);
    await expect(page.locator('main')).not.toContainText('Official ranking');
    await page.evaluate(() => scrollTo(0, 0));
    const cta = await page.getByRole('link', { name: 'View leaderboard', exact: true }).boundingBox();
    expect(cta!.y + cta!.height).toBeLessThan(viewport.height);
    if (viewport.width >= 390) {
      const bounds = await page.locator('#results').boundingBox();
      expect(bounds!.y).toBeLessThan(viewport.height - 60);
    }
    if (viewport.width === 390) {
      const strip = await page.locator('[data-timeline="strip"]').boundingBox();
      const rule = await page.locator('[data-timeline="rule"]').boundingBox();
      const request = await page.locator('[data-timeline="request"]').boundingBox();
      const caption = await page.locator('[data-timeline="caption"]').boundingBox();
      const ruleRight = rule!.x + rule!.width;
      const requestRight = request!.x + request!.width;
      const noHorizontalOverlap = ruleRight <= request!.x || requestRight <= rule!.x;
      expect(noHorizontalOverlap).toBe(true);
      expect(caption!.y).toBeGreaterThanOrEqual(strip!.y + strip!.height);
    }
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: testInfo.outputPath('home.png'),
      fullPage: true,
    });

    await page
      .getByRole('link', { name: 'Full leaderboard', exact: true })
      .click();
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(
      'Leaderboard',
    );
    await expect(page.locator('main')).toContainText(
      'Compare evaluated configurations across action accuracy, cost, and task latency.',
    );
    await expect(page.locator('main')).toContainText(
      '600 tests · 3 personas · 3 official configurations',
    );
    await expect(
      page.getByRole('heading', { name: 'Results', exact: true }),
    ).toBeVisible();
    await expect(page.locator('main')).not.toContainText('Configuration results');
    await expect(page.locator('main')).not.toContainText('Result details');
    await expect(page.locator('main')).not.toContainText('Paired results');
    await expect(page.locator('main')).not.toContainText('Metric definitions');
    for (const [name, p95] of [
      ['Mem0', '20.0 s'],
      ['Honcho', '40.0 s'],
      ['Built-in', '60.0 s'],
    ] as const) {
      await expect(
        page
          .getByRole('table', { name: 'Evaluation results' })
          .getByRole('row')
          .filter({ hasText: name }),
      ).toContainText(p95);
    }
    await expect(
      navigation.getByRole('link', { name: 'Leaderboard', exact: true }),
    ).toHaveAttribute('aria-current', 'page');
    await expect(
      page.getByRole('navigation', { name: 'Leaderboard resources' }),
    ).toContainText('MethodologyEvaluation protocolDataset');
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.evaluate(() => scrollTo(0, 0));
    await page.screenshot({
      path: testInfo.outputPath('leaderboard.png'),
      fullPage: true,
    });
    await page.getByRole('button', { name: 'Scores and evidence for Mem0, Fixture Luna, Hermes' }).click();
    const detail = page.locator('#result-synthetic-mem0');
    await expect(detail).toContainText('110 / 200');
    await expect(detail).toContainText('120 / 200');
    await expect(detail).toContainText('130 / 200');
    for (const name of ['Alex', 'Morgan', 'Riley', 'Configuration', 'Source bundle', 'Recordings', 'Grades']) {
      await expect(detail.getByRole('link', { name, exact: true })).toHaveAttribute('href', /^https:\/\/example.com\/synthetic-only\//);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);

    await page.unroute(resultUrl);
    const downloadPromise = page.waitForEvent('download');
    await page.getByRole('link', { name: 'Official results JSON' }).click();
    const download = await downloadPromise;
    expect(
      JSON.parse(fs.readFileSync((await download.path())!, 'utf8')),
    ).toEqual(publishedResults);
    await navigation
      .getByRole('link', { name: 'Dataset', exact: true })
      .click();
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Dataset');
    await expect(page.locator('main')).toContainText('600 tasks across 3 simulated users.');
    await expect(page.locator('main article')).toHaveCount(3);
    await expect(page.locator('main article').first()).toContainText('Tests200');
    await expect(page.locator('main article').first()).toContainText('Messages3,400');
    const datasetIndex = JSON.parse(fs.readFileSync(path.join(process.cwd(), 'public/data/index.json'), 'utf8'));
    await expect(page.locator('main article').first()).toContainText(`Facts${datasetIndex.personas[0].facts}`);
    await expect(page.locator('main')).toContainText('Jan 9, 2023 – Sep 11, 2026');
    await page.getByRole('link', { name: 'Morgan Chen', exact: true }).click();
    await expect(
      navigation.getByRole('link', { name: 'Dataset', exact: true }),
    ).toHaveAttribute('aria-current', 'page');
    await page
      .getByRole('navigation', { name: 'Personas', exact: true })
      .getByRole('link', { name: 'Dataset', exact: true })
      .click();
    await expect(page).toHaveURL('/dataset/');
    expect(errors).toEqual([]);
    expect(consoleErrors).toEqual([]);
    expect(hydrationWarnings).toEqual([]);
  });
}

test('configuration filters and sorting stay linked', async ({ page }) => {
  await page.goto('/leaderboard/');
  const table = page.getByRole('table', { name: 'Evaluation results' });
  await page.getByLabel('Filter by memory system').selectOption({ label: 'Honcho' });
  await expect(table.locator('tbody tr')).toHaveCount(1);
  await expect(table.locator('tbody tr')).toContainText('Honcho');
  await expect(page.getByText('1 of 3 configurations shown').first()).toBeVisible();
  await page.getByLabel('Filter by memory system').selectOption('all');
  await expect(table.locator('tbody tr')).toHaveCount(3);

  await page.getByLabel('Search configurations').fill('built');
  await expect(table.locator('tbody tr')).toHaveCount(1);
  await page.getByLabel('Search configurations').fill('nothing-matches');
  await expect(table.getByText('No configurations match these filters.')).toBeVisible();
  await page.getByLabel('Search configurations').fill('');
  await expect(table.locator('tbody tr')).toHaveCount(3);

  await expect(table.locator('tbody tr').first()).toContainText('Mem0');
  await page.getByRole('button', { name: /Total cost/ }).click();
  await expect(table.locator('tbody tr').first()).toContainText('Mem0');
  await page.getByRole('button', { name: /Total cost/ }).click();
  await expect(table.locator('tbody tr').first()).toContainText('Built-in');
  await expect(table.locator('thead th').nth(4)).toHaveAttribute('aria-sort', 'descending');
  await page.getByRole('button', { name: /Memory/ }).click();
  await expect(table.locator('tbody tr').first()).toContainText('Built-in');
});

test('why section uses the complete accepted source and original request', async ({ page }) => {
  const directory = path.join(process.cwd(), 'public/data');
  const dataset = JSON.parse(fs.readFileSync(path.join(directory, 'morgan.json'), 'utf8'));
  const history = JSON.parse(fs.readFileSync(path.join(directory, 'morgan-history.json'), 'utf8'));
  const spec = dataset.tests.find((item: { id: string }) => item.id === '018');
  const sourceId = dataset.facts[spec.fact_ids[0]].source_session_ids[0];
  const sourceIndex = history.findIndex((item: { id: string }) => item.id === sourceId);
  const source = history[sourceIndex];
  await page.goto('/');
  await expect(page.locator('[data-example-source]')).toHaveText(source.content);
  await expect(page.locator('[data-example-request]')).toHaveText(spec.request);
  const channelCheck = spec.grade.config.assertions.find((item: { type: string; value?: unknown }) =>
    item.type === 'field_equals' && typeof item.value === 'string');
  await expect(page.locator('[data-example-outcome]')).toHaveText(channelCheck.value);
  await expect(page.locator('[data-example-wrong]')).toHaveText('#eng-all');
  await expect(page.locator('#example')).toContainText(
    `${(history.length - sourceIndex - 1).toLocaleString('en-US')} unrelated messages in between`,
  );
  await expect(page.locator('#example')).toContainText(`${history.length.toLocaleString('en-US')} messages`);
  await expect(page.locator('#example')).toContainText('three years later');
  await expect(page.locator('#example')).toContainText('up to 5,128 messages each, spanning nearly five years');
  await expect(page.locator('#evaluation')).toHaveCount(1);
  expect(
    await page.evaluate(() => {
      const example = document.querySelector('#example')!;
      const evaluation = document.querySelector('#evaluation')!;
      return Boolean(example.compareDocumentPosition(evaluation) & Node.DOCUMENT_POSITION_FOLLOWING);
    }),
  ).toBe(true);
  await page.getByRole('link', { name: `message ${sourceId}` }).click();
  await expect(page.locator(`#message-${sourceId}`)).toHaveAttribute('open', '');
  await expect(page.locator(`#message-${sourceId} [data-message-content]`)).toHaveText(source.content);
  await page.goto('/');
  await page.getByRole('link', { name: 'View test 018' }).click();
  await expect(page.locator('[data-test-request]')).toHaveText(spec.request);
  await page.goto('/about/');
  await expect(page).toHaveURL('/#evaluation');
});

test('chart markers expose a tooltip on hover and focus', async ({ page }) => {
  await page.goto('/leaderboard/');
  const marker = page.locator('.chart-marker').first();
  await marker.hover();
  const tooltip = page.locator('.chart-tooltip');
  await expect(tooltip).toBeVisible();
  await expect(tooltip).toContainText('Mem0');
  await expect(tooltip).toContainText('60% acc');
  await expect(tooltip).toContainText('$30.00 total cost');
  const plotBounds = (await page.locator('.chart-plot').boundingBox())!;
  const markerBounds = (await marker.boundingBox())!;
  const tooltipBounds = (await tooltip.boundingBox())!;
  // The tooltip prefers to sit above the marker and flips below when the marker is too near the
  // top of the plot, which the fitted accuracy axis makes common. Either way it must clear the
  // point it describes and stay inside the plot.
  const markerMiddle = markerBounds.y + markerBounds.height / 2;
  if (markerMiddle - plotBounds.y >= tooltipBounds.height + 20) {
    expect(tooltipBounds.y + tooltipBounds.height).toBeLessThan(markerMiddle);
  } else {
    expect(tooltipBounds.y).toBeGreaterThan(markerMiddle);
  }
  expect(tooltipBounds.y).toBeGreaterThanOrEqual(plotBounds.y);
  expect(tooltipBounds.y + tooltipBounds.height).toBeLessThanOrEqual(plotBounds.y + plotBounds.height);
  expect(tooltipBounds.x).toBeGreaterThanOrEqual(plotBounds.x);
  expect(tooltipBounds.x + tooltipBounds.width).toBeLessThanOrEqual(plotBounds.x + plotBounds.width);
  await page.mouse.move(plotBounds.x + plotBounds.width / 2, plotBounds.y + plotBounds.height - 10);
  await expect(tooltip).toHaveCount(0);
  await marker.click();
  await expect(tooltip).toBeVisible();
  await page.mouse.move(plotBounds.x + plotBounds.width / 2, plotBounds.y + plotBounds.height - 10);
  await expect(tooltip).toHaveCount(0);
  await page.mouse.move(0, 0);
  await expect(tooltip).toHaveCount(0);
  await marker.focus();
  await expect(tooltip).toBeVisible();
  await expect(page.locator('.results-table tbody tr.is-active')).toContainText('Mem0');
  await expect(page.getByRole('button', { name: /^Official, Mem0, Fixture Luna/ })).toHaveCount(1);

  // Single-row filter must not collapse the x-axis domain to the one remaining point.
  const axisTicks = await page.locator('.chart-x-tick text').allTextContents();
  await page.getByLabel('Filter by memory system').selectOption({ label: 'Honcho' });
  await expect(page.locator('.chart-marker')).toHaveCount(1);
  await expect(page.locator('.chart-x-tick text')).toHaveText(axisTicks);
});

test('breakdown switches between configurations and memory averages', async ({ page }) => {
  await page.goto('/leaderboard/');
  const breakdown = page.locator('.breakdown');
  await expect(breakdown.getByRole('heading', { name: 'Official breakdown', exact: true })).toBeVisible();
  const views = page.getByRole('group', { name: 'Breakdown view' });
  await expect(views.getByRole('button')).toHaveText(['Configurations', 'Avg by memory', 'Avg by model', 'Avg by harness']);
  await expect(breakdown).toContainText('Tasks passed of 600 · Higher is better');
  const accuracy = breakdown.locator('[data-bar-column="accuracy"] [data-bar]');
  await expect(accuracy).toHaveCount(3);
  await expect(accuracy.first()).toContainText('Mem0');
  await expect(accuracy.first()).toContainText('Fixture Luna · Hermes');
  await expect(accuracy.first()).toContainText('60%');
  await expect(breakdown.locator('[data-bar-column="cost"] [data-bar]').first()).toContainText('$30.00');
  await expect(breakdown.locator('[data-bar-column="median"] [data-bar]').last()).toContainText('30.0 s');
  await views.getByRole('button', { name: 'Avg by memory' }).click();
  await expect(accuracy.first()).toContainText('avg of 1 config');
  await page.getByLabel('Filter by memory system').selectOption({ label: 'Honcho' });
  await expect(accuracy).toHaveCount(1);
});

const chartSubmission = {
  id: 'external', name: 'External test run', status: 'accepted',
  memory: 'Custom memory', harness: 'Custom harness', model: 'Custom model',
  summary: { tests: 600, passes: 480, passes_by_persona: { alex: 160, morgan: 160, riley: 160 },
    total_cost_usd: 8.2, ingestion: { total_cost_usd: 7 }, grading: {}, execution: {
      total_cost_usd: 1.2,
      records: 600, estimated_model_cost_usd: 1.2, unpriced_model_responses: 0, median_duration_ms: 5000,
    } },
};

for (const width of [1440, 390, 320]) {
  test(`self-submitted graph is opt-in and labeled at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    const requests: string[] = [];
    await page.route('**/api/submissions/**', route => {
      const url = new URL(route.request().url());
      requests.push(url.search);
      const laterPage = url.searchParams.get('page') === '1';
      return route.fulfill({ json: { hasMore: !laterPage, enabled: true,
        submissions: laterPage ? [{ ...chartSubmission, id: 'unpriced', name: 'Unpriced run', summary: {
          ...chartSubmission.summary, execution: { records: 600, median_duration_ms: 1000 },
        } }] : [chartSubmission],
      } });
    });
    await page.goto('/leaderboard/');
    const toggle = page.getByRole('checkbox', { name: 'Include self-submitted runs', exact: true });
    await expect(toggle).not.toBeChecked();
    await expect(page.locator('.chart-marker')).toHaveCount(3);
    await expect(page.locator('.chart-marker[data-source="self-submitted"]')).toHaveCount(0);
    const officialRings = await page.locator('.chart-frontier').count();
    await toggle.check();
    const submitted = page.locator('.chart-marker[data-source="self-submitted"]');
    await expect(submitted).toHaveCount(1);
    await expect(page.getByText('4 of 4 configurations shown')).toBeVisible();
    expect(requests.some(url => url.includes('page=1'))).toBe(true);
    expect(requests.every(url => url.includes('view=public'))).toBe(true);
    await expect(page.locator('.chart-frontier')).toHaveCount(officialRings);
    await page.getByRole('button', { name: 'Latency', exact: true }).click();
    await expect(submitted).toHaveCount(1);
    const marker = page.getByRole('button', { name: /External test run, Self-submitted/ });
    await expect(marker).toHaveAttribute('aria-label', /External test run, Self-submitted · Unverified/);
    await marker.focus();
    await marker.press('Enter');
    const tooltip = page.locator('.chart-tooltip');
    await expect(tooltip).toContainText('Self-submitted · Unverified');
    await expect(tooltip).toContainText('External test run');
    await expect(tooltip).toContainText('$8.20 total cost');
    const bounds = (await tooltip.boundingBox())!;
    expect(bounds.x).toBeGreaterThanOrEqual(0);
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
    const stage = (await page.locator('.chart-plot').boundingBox())!;
    expect(bounds.y).toBeGreaterThanOrEqual(stage.y);
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(stage.y + stage.height);
    await page.screenshot({ path: testInfo.outputPath('self-submitted-chart.png'), fullPage: true });
    await marker.press('Escape');
    await expect(tooltip).toHaveCount(0);
    const officialTable = page.getByRole('table', { name: 'Evaluation results' });
    await expect(officialTable.locator('tbody tr')).toHaveCount(3);
    await expect(officialTable).not.toContainText('Custom memory');
    await page.getByRole('button', { name: 'Latency', exact: true }).click();
    await expect(submitted).toHaveCount(1);
    await page.getByLabel('Filter by memory system').selectOption('Custom memory');
    await expect(page.locator('.chart-marker')).toHaveCount(1);
    await page.getByRole('searchbox', { name: 'Search configurations' }).fill('Unpriced run');
    await expect(submitted).toHaveCount(0);
    await toggle.uncheck();
    await expect(submitted).toHaveCount(0);
    await expect(page.locator('.chart-marker')).toHaveCount(3);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test('self-submitted graph works without official results and excludes incomplete runs', async ({ page }) => {
  await page.route(resultUrl, route => route.fulfill({ json: { schema_version: 2, configurations: [] } }));
  await page.route('**/api/submissions/**', route => route.fulfill({ json: { hasMore: false, submissions: [
    chartSubmission,
    { ...chartSubmission, id: 'removed', status: 'removed' },
    { ...chartSubmission, id: 'partial', summary: { ...chartSubmission.summary, tests: 200 } },
    { ...chartSubmission, id: 'missing', summary: { ...chartSubmission.summary, execution: {} } },
  ] } }));
  await page.goto('/leaderboard/');
  await expect(page.getByText('No complete official results published yet.')).toBeVisible();
  await page.getByRole('checkbox', { name: 'Include self-submitted runs' }).check();
  await expect(page.locator('.chart-marker')).toHaveCount(1);
  await expect(page.locator('.chart-frontier')).toHaveCount(0);
  await page.getByRole('button', { name: 'Latency', exact: true }).click();
  await expect(page.locator('.chart-marker')).toHaveCount(1);
  expect(await page.locator('.chart-plot svg').innerHTML()).not.toMatch(/NaN|Infinity/);
});

test('self-submitted graph retries failures and removes withdrawn runs on refresh', async ({ page }) => {
  await page.clock.install();
  let state = 'failure';
  await page.route('**/api/submissions/**', route => state === 'failure'
    ? route.fulfill({ status: 503 })
    : route.fulfill({ json: { hasMore: false, submissions: state === 'accepted' ? [chartSubmission] : [] } }));
  await page.goto('/leaderboard/');
  await page.getByRole('checkbox', { name: 'Include self-submitted runs' }).check();
  await expect(page.getByText('Self-submitted runs could not be loaded.', { exact: false })).toBeVisible();
  await expect(page.locator('.chart-marker[data-source="official"]')).toHaveCount(3);
  state = 'accepted';
  await page.getByRole('button', { name: 'Retry self-submitted runs', exact: true }).click();
  await page.getByRole('button', { name: 'Latency', exact: true }).click();
  await expect(page.locator('.chart-marker[data-source="self-submitted"]')).toHaveCount(1);
  state = 'removed';
  await page.clock.fastForward(30_000);
  await expect(page.locator('.chart-marker[data-source="self-submitted"]')).toHaveCount(0);
  await expect(page.locator('.chart-marker[data-source="official"]')).toHaveCount(3);
});

test('different dataset versions remain visible but do not enter the comparison', async ({ page }) => {
  await page.route(resultUrl, route => route.fulfill({ json: {
    ...results, release_sha256: publishedResults.release_sha256,
  } }));
  await page.route('**/api/submissions/**', route => route.fulfill({ json: {
    hasMore: false, submissions: [
      { ...chartSubmission, id: 'current', name: 'Current dataset run', release_sha256: publishedResults.release_sha256 },
      { ...chartSubmission, id: 'previous', name: 'Previous dataset run', release_sha256: 'previous' },
    ],
  } }));
  await page.goto('/leaderboard/');
  const table = page.getByRole('table', { name: 'Self-submitted results', exact: true });
  await expect(table.getByRole('row').filter({ hasText: 'Previous dataset run' })).toContainText('Different dataset version');
  await page.getByRole('checkbox', { name: 'Include self-submitted runs' }).check();
  await page.getByRole('button', { name: 'Latency', exact: true }).click();
  await expect(page.locator('.chart-marker[data-source="self-submitted"]')).toHaveCount(1);
  await expect(page.locator('.chart-marker[data-source="self-submitted"]')).toHaveAttribute('aria-label', /Current dataset run/);
});


test('published results and download exclude partial scores', async ({ page, request }, testInfo) => {
  await page.unroute(resultUrl);
  for (const url of ['/', '/leaderboard/']) {
    await page.goto(url);
    if (!publishedResults.configurations.length) {
      await expect(page.getByText('No complete official results published yet.')).toBeVisible();
      await expect(page.getByRole('table', { name: 'Evaluation results' })).toHaveCount(0);
      await expect(page.locator('.chart-plot svg')).toHaveCount(0);
    } else {
      const table = page.getByRole('table', { name: 'Evaluation results' });
      await expect(table.locator('tbody tr')).toHaveCount(publishedResults.configurations.length);
      await expect(page.locator('.chart-marker')).toHaveCount(publishedResults.configurations.filter(row => row.total_cost_usd !== null).length);
      for (const row of publishedResults.configurations) {
        const cost = row.total_cost_usd === null ? 'Unavailable' : `$${row.total_cost_usd.toFixed(2)}`;
        const tableRow = table.getByRole('row').filter({ hasText: row.memory.name })
          .filter({ hasText: row.model.name }).filter({ hasText: row.harness.name });
        await expect(tableRow).toContainText(cost);
        const plotted = page.getByRole('button', { name: `Official, ${row.memory.name}, ${row.model.name}, ${row.harness.name}:`, exact: false });
        if (row.total_cost_usd === null) await expect(plotted).toHaveCount(0);
        else expect(await plotted.getAttribute('aria-label')).toContain(`${cost} total cost`);
      }
    }
    await expect(page.locator('main')).not.toContainText('Partial benchmark');
  }
  for (const width of [1440, 320]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`published-${width}.png`), fullPage: true });
  }
  const response = await request.get('/leaderboard/results.json/');
  expect(response.status()).toBe(200);
  expect(response.headers()['content-disposition']).toBe('attachment; filename="dolphinbench-results.json"');
  expect(await response.json()).toEqual(publishedResults);
  for (const row of publishedResults.configurations) {
    const download = await request.get(new URL(row.evidence.configuration.url).pathname);
    expect(download.status()).toBe(200);
    expect(createHash('sha256').update(await download.body()).digest('hex'))
      .toBe(row.evidence.configuration.sha256);
  }
});

test('unavailable metrics stay out of the chart and sort last', async ({ page }) => {
  const missing = structuredClone(results);
  Object.assign(missing.configurations[0], {
    total_cost_usd: null, total_cost_usd_test_calls: null, median_latency_seconds: null, p95_latency_seconds: null,
  });
  await page.route(resultUrl, route => route.fulfill({ json: missing }));
  await page.goto('/leaderboard/');
  const table = page.getByRole('table', { name: 'Evaluation results' });
  await expect(table.locator('tbody tr').first()).toContainText('Unavailable');
  await expect(page.locator('.chart-marker')).toHaveCount(2);
  await expect(page.getByText('2 of 3 configurations shown (1 without cost data)')).toBeVisible();
  for (let i = 0; i < 2; i++) {
    await page.getByRole('button', { name: /Total cost/ }).click();
    await expect(table.locator('tbody tr').last()).toContainText('Mem0');
  }
  await page.getByLabel('Filter by memory system').selectOption('Mem0');
  await expect(page.getByText('No cost data available')).toBeVisible();
  await page.getByRole('button', { name: 'Latency', exact: true }).click();
  await expect(page.getByText('No latency data available')).toBeVisible();
  await page.getByLabel('Filter by memory system').selectOption('all');
  await expect(page.locator('.chart-marker')).toHaveCount(2);
  expect(await page.locator('.chart-plot svg').getAttribute('viewBox')).not.toMatch(/NaN|Infinity/);
});

test('failed result downloads offer a working retry', async ({ page }) => {
  let available = false;
  await page.route(resultUrl, route => {
    return available ? route.fulfill({ json: results }) : route.fulfill({ status: 503 });
  });
  await page.goto('/leaderboard/');
  await expect(page.locator('main').getByRole('alert')).toContainText('Results could not be loaded.');
  available = true;
  await page.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(page.getByRole('table', { name: 'Evaluation results' })).toBeVisible();
});

for (const width of [1440, 390, 320]) {
  test(`self-submitted results table at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    const common = { harness: 'Custom harness', memory: 'Custom memory', model: 'fixture-model', status: 'accepted',
      blob_sha256: 'a'.repeat(64), release_sha256: 'b'.repeat(64), validator_sha256: 'c'.repeat(64) };
    const summary = { tests: 600, passes: 300, passes_by_persona: { alex: 100, morgan: 100, riley: 100 },
      total_cost_usd: 112, ingestion: { total_cost_usd: 100, estimated_model_cost_usd: 99 }, grading: { estimated_model_cost_usd: 99 },
      execution: { total_cost_usd: 12, records: 600, median_duration_ms: 1500, estimated_model_cost_usd: 6, unpriced_model_responses: 0 } };
    await page.route('**/api/submissions/**', route => route.fulfill({ json: { enabled: true, hasMore: true, submissions: [
      { ...common, id: 'priced', name: 'Priced test run', summary },
      { ...common, id: 'unpriced', name: 'Unpriced test run', summary: { ...summary, execution: {
        ...summary.execution, median_duration_ms: 1, estimated_model_cost_usd: null, unpriced_model_responses: 600,
      } } },
      { ...common, id: 'legacy', name: 'L'.repeat(120), summary: { ...summary, execution: {} } },
    ] } }));
    await page.goto('/leaderboard/');
    const table = page.getByRole('table', { name: 'Self-submitted results', exact: true });
    await expect(table.getByRole('columnheader')).toHaveText([
      'Run', 'Memory', 'Model', 'Harness', 'Accuracy', 'Total cost', 'Median latency', 'p95 latency',
    ]);
    const priced = table.getByRole('row').filter({ hasText: /^Priced test run/ });
    await expect(priced).toContainText('Self-submitted · Unverified');
    await expect(priced).toContainText('50.0%');
    await expect(priced).toContainText('300 / 600');
    await expect(priced).toContainText('$112.00');
    await expect(priced).toContainText('1.5 s');
    await expect(priced).toContainText('Unavailable');
    const unpriced = table.getByRole('row').filter({ hasText: 'Unpriced test run' });
    await expect(unpriced).toContainText('1 ms');
    await expect(unpriced).not.toContainText('$0.0000');
    await expect(unpriced).toContainText('$112.00');
    await expect(table).not.toContainText('L'.repeat(120));
    const tableBounds = (await table.boundingBox())!;
    if (width === 1440) expect(tableBounds.width).toBeLessThanOrEqual(1104);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.getByRole('button', { name: 'Score details for Priced test run', exact: true }).click();
    const details = page.locator('#submission-priced');
    await expect(details).toContainText('alex100/200');
    const download = details.getByRole('link', { name: 'Download result' });
    const href = await download.getAttribute('href');
    const data = JSON.parse(decodeURIComponent(href!.split(',')[1]));
    expect(data.summary).toEqual(summary);
    expect(data.verification).toBe('self-submitted-unverified');
    await expect(page.getByRole('button', { name: 'Next submissions' })).toBeEnabled();
    await page.getByRole('region', { name: 'Self-submitted results table', exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath('self-submitted.png'), fullPage: true });
    await page.getByRole('button', { name: 'Score details for Priced test run', exact: true }).click();
    await expect(details).toHaveCount(0);
  });
}
