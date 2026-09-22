import { test, expect } from '@playwright/test';
import { chartAxis, formatAxisTick, percentAxis } from '@/lib/chart-axis';
import results from '../../content/official-results.json';

test('cost uses standard logarithmic ticks and proportional ratios', () => {
  const axis = chartAxis([61.48, 148.02, 1132.75], true);
  expect(axis.log).toBe(true);
  expect(axis.ticks).toEqual([50, 100, 200, 500, 1000, 2000]);
  expect(axis.position(100) - axis.position(50)).toBeCloseTo(axis.position(200) - axis.position(100));
  expect(formatAxisTick(1000)).toBe('1,000');
  expect(formatAxisTick(0.002)).toBe('0.002');
});

test('latency uses equal rounded intervals starting at zero', () => {
  const axis = chartAxis([32.19, 55.31], false);
  expect(axis.log).toBe(false);
  expect(axis.ticks).toEqual([0, 10, 20, 30, 40, 50, 60, 70]);
  expect(axis.position(10) - axis.position(0)).toBeCloseTo(axis.position(20) - axis.position(10));
});

test('empty, zero, single-value and widely spaced results have finite axes', () => {
  for (const values of [[], [0], [0, 100], [100], [0.001, 0.003], [0.01, 1000000]]) {
    for (const log of [true, false]) {
      const axis = chartAxis(values, log);
      expect(axis.ticks.length).toBeGreaterThan(1);
      expect(axis.ticks.length).toBeLessThanOrEqual(10);
      expect(axis.log).toBe(log && values.length > 0 && Math.min(...values) > 0);
      for (const value of values) {
        expect(Number.isFinite(axis.position(value))).toBe(true);
        expect(axis.position(value)).toBeGreaterThanOrEqual(0);
        expect(axis.position(value)).toBeLessThanOrEqual(1);
      }
    }
  }
});

test('accuracy axis fits the results instead of always spanning 0 to 100', () => {
  // The published spread sits well inside 0-100%, so the axis crops to it.
  const axis = percentAxis(results.configurations.map(row => row.pass_rate * 100));
  expect(axis.min).toBeGreaterThan(0);
  expect(axis.max).toBeLessThan(100);
  expect(axis.ticks[0]).toBe(axis.min);
  expect(axis.ticks[axis.ticks.length - 1]).toBe(axis.max);
  expect(axis.position(axis.min)).toBe(0);
  expect(axis.position(axis.max)).toBe(1);
  // Every plotted value still lands inside the axis.
  for (const row of results.configurations) {
    const position = axis.position(row.pass_rate * 100);
    expect(position).toBeGreaterThanOrEqual(0);
    expect(position).toBeLessThanOrEqual(1);
  }
});

test('accuracy axis stays finite and readable for empty, single and extreme inputs', () => {
  for (const values of [[], [0], [50], [100], [100, 100], [0, 100], [33.3333, 33.5], [95, 99]]) {
    const axis = percentAxis(values);
    expect(axis.max).toBeGreaterThan(axis.min);
    expect(axis.min).toBeGreaterThanOrEqual(0);
    expect(axis.max).toBeLessThanOrEqual(100);
    expect(axis.ticks.length).toBeGreaterThan(1);
    expect(axis.ticks.length).toBeLessThanOrEqual(12);
    expect(axis.ticks[0]).toBe(axis.min);
    expect(axis.ticks[axis.ticks.length - 1]).toBe(axis.max);
    for (const value of values) {
      expect(Number.isFinite(axis.position(value))).toBe(true);
      expect(axis.position(value)).toBeGreaterThanOrEqual(0);
      expect(axis.position(value)).toBeLessThanOrEqual(1);
    }
  }
});

test('published charts use readable axes in both modes', async ({ page }, testInfo) => {
  await page.route(/\/leaderboard\/results\.json\/?$/, route => route.fulfill({ json: results }));
  await page.route('**/api/submissions/**', route => route.fulfill({ json: { enabled: false, submissions: [], hasMore: false } }));
  await page.goto('/leaderboard');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const metric of ['Cost', 'Latency']) {
      await page.getByRole('group', { name: 'Chart metric' }).getByRole('button', { name: metric, exact: true }).click();
      const values = results.configurations.map(row => metric === 'Cost' ? row.total_cost_usd : row.median_latency_seconds)
        .filter((value): value is number => value !== null);
      await expect(page.locator('.chart-marker')).toHaveCount(values.length);
      const axis = chartAxis(values, metric === 'Cost');
      await expect(page.locator('.chart-x-tick text')).toHaveText(axis.ticks.map(value => `${metric === 'Cost' ? '$' : ''}${formatAxisTick(value)}`));
      const yAxis = percentAxis(results.configurations.map(row => row.pass_rate * 100));
      await expect(page.locator('.chart-y-tick text')).toHaveText(yAxis.ticks.map(tick => `${formatAxisTick(tick)}%`));
      expect(await page.locator('.chart-plot svg').innerHTML()).not.toMatch(/NaN|Infinity/);
      await page.locator('.chart-stage').screenshot({ path: testInfo.outputPath(`${metric}-${width}.png`) });
    }
  }
});
