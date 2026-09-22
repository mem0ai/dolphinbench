import { test, expect, Page } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import type { Release, PersonaData, HistoryMessage } from '../../lib/types';
import { formatDate, formatTime } from '../../lib/types';
import { localPreview, previewRequestAllowed } from '../../lib/preview';

const read = <T>(name: string): T =>
  JSON.parse(
    fs.readFileSync(path.join(process.cwd(), 'public/data', name), 'utf8'),
  );
const release = read<Release>('index.json');

async function noOverflow(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
}

for (const viewport of [
  { width: 1440, height: 1000 },
  { width: 390, height: 844 },
  { width: 320, height: 760 },
]) {
  test(`dataset and full evidence at ${viewport.width}px`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize(viewport);
    page.setDefaultTimeout(10_000);
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto('/dataset/');
    await expect(
      page.getByRole('heading', { name: 'Dataset', exact: true }),
    ).toBeVisible();
    await expect(page.locator('article')).toHaveCount(3);
    await expect(page.locator('main')).not.toContainText(release.release_sha256);
    await expect(page.getByRole('link', { name: 'Release details', exact: true })).toHaveCount(0);
    await expect(page.locator('main canvas')).toHaveCount(0);
    await expect(page.getByText('Messages / month')).toHaveCount(0);
    await expect(page.getByText('Monthly counts')).toHaveCount(0);
    await noOverflow(page);
    await page.screenshot({
      path: testInfo.outputPath('dataset.png'),
      fullPage: true,
    });
    for (const persona of release.personas) {
      await page.getByRole('link', { name: persona.name, exact: true }).click();
      await expect(
        page.getByRole('heading', { name: persona.name, exact: true }),
      ).toBeVisible();
      await expect(page.getByText('History activity')).toHaveCount(0);
      await expect(page.getByRole('navigation', { name: 'Personas', exact: true })).toContainText('Dataset');
      await expect(page.locator('main')).toContainText(`${persona.role} / ${persona.organization}`);
      await expect(page.locator('main')).toContainText('(initial profile)');
      await expect(page.locator('main dl').first()).toContainText(
        `History messages${persona.messages.toLocaleString('en-US')}`,
      );
      await expect(page.locator('main')).toContainText('Tools referenced by tests');
      await expect(page.getByRole('link', { name: 'All 200', exact: true })).toHaveAttribute(
        'href',
        `/personas/${persona.id}/tests/`,
      );
      await expect(page.getByRole('link', { name: 'Full history', exact: true })).toHaveAttribute(
        'href',
        `/personas/${persona.id}/timeline/`,
      );
      await noOverflow(page);
      await page
        .getByRole('link', { name: 'Tests (200)', exact: true })
        .click();
      await expect(page.getByRole('status')).toHaveText('200 of 200 tests');
      await noOverflow(page);
      await page.getByLabel('Search tests').fill('no-result-9138119');
      await expect(page.getByRole('status')).toHaveText('0 of 200 tests');
      await page.getByRole('button', { name: 'Reset filters' }).click();
      const dataset = read<PersonaData>(`${persona.id}.json`);
      await page
        .getByLabel('Tool', { exact: true })
        .selectOption(persona.tools[0]);
      const count = dataset.tests.filter((test) =>
        test.tools.includes(persona.tools[0]),
      ).length;
      await expect(page.getByRole('status')).toHaveText(
        `${count} of 200 tests`,
      );
      await page.reload();
      await expect(page.getByLabel('Tool', { exact: true })).toHaveValue(
        persona.tools[0],
      );
      await page.getByRole('button', { name: 'Reset filters' }).click();
      const first = dataset.tests[0];
      await page.goto(`/personas/${persona.id}/tests/${first.id}/`);
      await expect(page.locator('[data-test-request]')).toHaveText(
        first.request,
      );
      const state = page.locator('details').filter({ has: page.locator('summary', { hasText: /^App state$/ }) });
      await expect(state.locator('pre')).toHaveCount(0);
      await state.locator('summary').click();
      await expect(state.locator('pre')).not.toBeEmpty();
      expect((await state.locator('pre').boundingBox())!.height).toBeLessThanOrEqual(448);
      await expect(state.getByRole('link', { name: 'Download JSON' })).toHaveAttribute(
        'href', `/data/tests/${persona.id}/${first.id}-state.json`,
      );
      await state.locator('summary').click();
      const fact = dataset.facts[first.fact_ids[0]];
      const sourceId = fact.source_session_ids[0];
      const evidence = page.locator(`#fact-${fact.id}`);
      await evidence
        .locator('summary')
        .filter({ hasText: sourceId })
        .first()
        .click();
      const source = read<HistoryMessage[]>(`${persona.id}-history.json`).find(
        (message) => message.id === sourceId,
      )!;
      await expect(evidence).toContainText(source.content);
      await page
        .getByText('Complete grading specification', { exact: true })
        .click();
      await page.getByText('App state', { exact: true }).click();
      await noOverflow(page);
      await page.evaluate(() => window.scrollTo(0, 0));
      if (persona.id === 'morgan')
        await page.screenshot({
          path: testInfo.outputPath('test-evidence.png'),
          fullPage: true,
        });
      const download = page.waitForEvent('download');
      await page.getByRole('link', { name: 'YAML', exact: true }).click();
      const file = await download;
      expect(fs.readFileSync((await file.path())!)).toEqual(
        fs.readFileSync(
          path.join(
            process.cwd(),
            `public/data/tests/${persona.id}/${first.id}.yaml`,
          ),
        ),
      );
      await evidence
        .getByRole('link', {
          name: `Message ${sourceId} in history`,
          exact: true,
        })
        .first()
        .click();
      const message = page.locator(`#message-${sourceId}`);
      await expect(message).toHaveAttribute('open', '');
      await expect(message.locator('[data-message-content]')).toHaveText(
        source.content,
      );
      await noOverflow(page);
      await page.goto('/dataset/');
    }
    expect(errors).toEqual([]);
    await page.goto('/about/');
    await expect(page).toHaveURL('/#evaluation');
    await noOverflow(page);
  });
}

test('history search, dates, paging, deep links, and missing IDs', async ({
  page,
  request,
}, testInfo) => {
  const persona = release.personas[0];
  const history = read<HistoryMessage[]>(`${persona.id}-history.json`);
  await page.goto(`/personas/${persona.id}/timeline/`);
  await page.getByLabel('Search history').fill('no-result-9138119');
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await expect(page.getByRole('status')).toHaveText('No matching messages.');
  await page.getByRole('link', { name: 'Clear filters' }).click();
  await expect(page.getByLabel('Search history')).toHaveValue('');
  await expect(page.locator('details[id^="message-"]')).toHaveCount(40);
  const date = history[0].date.slice(0, 10);
  await page.getByLabel('From', { exact: true }).fill(date);
  await page.getByLabel('To', { exact: true }).fill(date);
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  const matching = history.filter(
    (message) => message.date.slice(0, 10) === date,
  );
  await expect(page.locator('details[id^="message-"]')).toHaveCount(
    Math.min(40, matching.length),
  );
  const last = history.at(-1)!;
  await page.goto(
    `/personas/${persona.id}/timeline/?session=${last.id}#message-${last.id}`,
  );
  await expect(
    page.locator(`#message-${last.id} [data-message-content]`),
  ).toHaveText(last.content);
  await page.screenshot({
    path: testInfo.outputPath('history-deep-link.png'),
    fullPage: true,
  });
  await page.getByRole('link', { name: 'Previous', exact: true }).click();
  await expect(page).not.toHaveURL(/session=/);
  for (const route of [
    '/personas/unknown/',
    '/personas/morgan/tests/999/',
    '/personas/morgan/timeline/?session=999999',
  ]) {
    expect((await request.get(route)).status()).toBe(404);
  }
  expect((await request.get('/admin/', { maxRedirects: 0 })).status()).toBe(
    307,
  );
});

test('all 600 test routes resolve their required facts', async ({
  request,
}) => {
  test.setTimeout(600_000);
  let count = 0;
  for (const persona of release.personas) {
    const dataset = read<PersonaData>(`${persona.id}.json`);
    for (let start = 0; start < dataset.tests.length; start += 4) {
      await Promise.all(
        dataset.tests.slice(start, start + 4).map(async (spec) => {
          const response = await request.get(
            `/personas/${persona.id}/tests/${spec.id}/`,
          );
          expect(response.status()).toBe(200);
          const html = await response.text();
          expect(html).toContain('data-test-request');
          for (const factId of spec.fact_ids)
            expect(html).toContain(`id="fact-${factId}"`);
          await response.dispose();
          count++;
        }),
      );
    }
    console.log(`Verified ${persona.id}: ${dataset.tests.length} test routes`);
  }
  expect(count).toBe(600);
});

test('local preview cannot enable production or private routes', () => {
  const previous = {
    NODE_ENV: process.env.NODE_ENV,
    DOLPHINBENCH_LOCAL_PREVIEW: process.env.DOLPHINBENCH_LOCAL_PREVIEW,
  };
  try {
    Object.assign(process.env, {
      NODE_ENV: 'development',
      DOLPHINBENCH_LOCAL_PREVIEW: '1',
    });
    expect(localPreview()).toBe(true);
    expect(previewRequestAllowed('127.0.0.1', '/')).toBe(true);
    for (const route of ['/dataset/', '/run/', '/run/repo/docs/DRIVER_CONTRACT.md/', '/leaderboard/', '/leaderboard/results.json',
        '/robots.txt', '/sitemap.xml', '/llms.txt', '/llms-full.txt', '/opengraph-image', '/icon', '/apple-icon',
        '/favicon.ico', '/manifest.webmanifest', '/brand/dolphinbench-icon-512.png'])
      expect(previewRequestAllowed('localhost', route)).toBe(true);
    for (const route of ['/admin/', '/api/auth/login/'])
      expect(previewRequestAllowed('localhost', route)).toBe(false);
    expect(previewRequestAllowed('example.com', '/')).toBe(false);
    expect(previewRequestAllowed('example.com', '/run/repo/docs/DRIVER_CONTRACT.md/')).toBe(false);
    expect(
      previewRequestAllowed('localhost.example.com', '/data/index.json'),
    ).toBe(false);
    Object.assign(process.env, { NODE_ENV: 'production' });
    expect(localPreview()).toBe(false);
    expect(previewRequestAllowed('127.0.0.1', '/')).toBe(false);
    expect(previewRequestAllowed('127.0.0.1', '/run/repo/docs/DRIVER_CONTRACT.md/')).toBe(false);
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});

test('back navigation retains test search and tool filters', async ({
  page,
}) => {
  await page.goto('/personas/morgan/tests/?tool=place_order');
  await page.getByLabel('Search tests').fill('out early');
  await expect(page).toHaveURL(/q=out\+early/);
  await expect(page.getByRole('status')).toHaveText('1 of 200 tests');
  const count = await page.getByRole('status').textContent();
  await page.locator('main a[href="/personas/morgan/tests/001/"]').click();
  await expect(
    page.getByRole('heading', { name: 'Test 001', exact: true }),
  ).toBeVisible();
  await page.goBack();
  await expect(page.getByLabel('Search tests')).toHaveValue('out early');
  await expect(page.getByLabel('Tool', { exact: true })).toHaveValue(
    'place_order',
  );
  await expect(page.getByRole('status')).toHaveText(count!);
});

test('source dates are independent of viewer timezone', async ({ browser, baseURL }) => {
  const date = '2025-01-01T23:30:00-08:00';
  expect(formatDate(date)).toBe('Jan 1, 2025');
  expect(formatTime(date)).toBe('23:30 UTC-08:00');
  const history = read<HistoryMessage[]>('morgan-history.json');
  const first = history[0];
  const displays: string[] = [];
  for (const timezoneId of ['America/Los_Angeles', 'Asia/Tokyo']) {
    const context = await browser.newContext({ timezoneId });
    const page = await context.newPage();
    await page.goto(
      `${baseURL}/personas/morgan/timeline/?session=${first.id}`,
    );
    displays.push(
      await page.locator(`#message-${first.id} summary`).innerText(),
    );
    await context.close();
  }
  expect(displays[0]).toBe(displays[1]);
});
