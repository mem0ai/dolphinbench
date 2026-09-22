import { test, expect } from '@playwright/test';
import results from '../fixtures/complete-results.json';

const resultUrl = /\/leaderboard\/results\.json\/?$/;

test.beforeEach(async ({ page }) => {
  await page.route(resultUrl, route => route.fulfill({ json: results }));
  await page.route('**/api/submissions/**', route => route.fulfill({ json: { enabled: false, submissions: [], hasMore: false } }));
});

const meta = (page: import('@playwright/test').Page, selector: string) =>
  page.locator(`head ${selector}`).getAttribute('content');

test('home page carries canonical, Open Graph, Twitter, and structured data', async ({ page, request }) => {
  await page.goto('/');
  await expect(page).toHaveTitle('DolphinBench: Mapping the Pareto frontier of agent memory');
  expect(await meta(page, 'meta[name="description"]')).toMatch(/open benchmark from Mem0/);
  await expect(page.locator('head link[rel="canonical"]')).toHaveAttribute('href', 'https://dolphinbench.ai/');
  expect(await meta(page, 'meta[property="og:title"]')).toBe('DolphinBench: Mapping the Pareto frontier of agent memory');
  expect(await meta(page, 'meta[property="og:site_name"]')).toBe('DolphinBench');
  expect(await meta(page, 'meta[property="og:url"]')).toBe('https://dolphinbench.ai/');
  expect(await meta(page, 'meta[name="twitter:card"]')).toBe('summary_large_image');
  expect(await meta(page, 'meta[name="robots"]')).toMatch(/index, follow/);

  const image = await meta(page, 'meta[property="og:image"]');
  expect(image).toMatch(/\/opengraph-image/);
  expect(await meta(page, 'meta[property="og:image:width"]')).toBe('1200');
  expect(await meta(page, 'meta[property="og:image:height"]')).toBe('630');
  expect(await meta(page, 'meta[name="twitter:image"]')).toMatch(/\/opengraph-image/);
  const response = await request.get(new URL(image!).pathname + new URL(image!).search);
  expect(response.status()).toBe(200);
  expect(response.headers()['content-type']).toBe('image/png');
  const png = await response.body();
  expect(png.subarray(1, 4).toString()).toBe('PNG');
  expect(png.readUInt32BE(16)).toBe(1200);
  expect(png.readUInt32BE(20)).toBe(630);

  for (const [selector, dimension] of [['link[rel="icon"][type="image/png"]', 64], ['link[rel="apple-touch-icon"]', 180]] as const) {
    const href = await page.locator(`head ${selector}`).first().getAttribute('href');
    expect(href).toMatch(/\/(icon|apple-icon)/);
    const icon = await request.get(new URL(href!, 'http://placeholder').pathname + new URL(href!, 'http://placeholder').search);
    expect(icon.status()).toBe(200);
    expect(icon.headers()['content-type']).toBe('image/png');
    expect((await icon.body()).readUInt32BE(16)).toBe(dimension);
  }

  await expect(page.locator('head link[rel="icon"][href^="/favicon.ico"]')).toHaveCount(1);
  const favicon = await request.get('/favicon.ico');
  expect(favicon.status()).toBe(200);
  expect(favicon.headers()['content-type']).toMatch(/image\/(x-icon|vnd\.microsoft\.icon)/);
  expect((await favicon.body()).readUInt16LE(2)).toBe(1);

  await expect(page.locator('head link[rel="manifest"]')).toHaveAttribute('href', '/manifest.webmanifest');
  const manifestResponse = await request.get('/manifest.webmanifest');
  expect(manifestResponse.status()).toBe(200);
  const manifest = await manifestResponse.json();
  expect(manifest.short_name).toBe('DolphinBench');
  expect(manifest.icons.map((icon: { src: string }) => icon.src)).toContain('/brand/dolphinbench-icon-512.png');
  const brandIcon = await request.get('/brand/dolphinbench-icon-512.png');
  expect(brandIcon.status()).toBe(200);
  expect((await brandIcon.body()).readUInt32BE(16)).toBe(512);

  const schemas = await page.locator('script[type="application/ld+json"]').allTextContents();
  const types = schemas.map(text => JSON.parse(text)['@type']);
  expect(types.sort()).toEqual(['Dataset', 'Organization', 'WebSite']);
  const website = JSON.parse(schemas.find(text => text.includes('"WebSite"'))!);
  expect(website.image).toBe('https://dolphinbench.ai/brand/dolphinbench-icon-512.png');
  const dataset = JSON.parse(schemas.find(text => text.includes('"Dataset"'))!);
  expect(dataset.description).toContain('600 tasks across 3 simulated users');
  expect(dataset.distribution[0].contentUrl).toBe('https://dolphinbench.ai/leaderboard/results.json');
});

test('inner pages describe themselves and private pages stay out of the index', async ({ page }) => {
  await page.goto('/personas/morgan/');
  await expect(page).toHaveTitle('Morgan Chen | DolphinBench');
  expect(await meta(page, 'meta[name="description"]')).toContain('Founder & CEO at Scaffold');
  await expect(page.locator('head link[rel="canonical"]')).toHaveAttribute('href', 'https://dolphinbench.ai/personas/morgan/');
  await page.goto('/personas/morgan/tests/018/');
  await expect(page).toHaveTitle('Test 018 for Morgan Chen | DolphinBench');
  expect(await meta(page, 'meta[name="description"]')).toContain('Tools: send_discord_message');
  await page.goto('/leaderboard/');
  await expect(page.locator('head link[rel="canonical"]')).toHaveAttribute('href', 'https://dolphinbench.ai/leaderboard/');
  await page.goto('/login/');
  expect(await meta(page, 'meta[name="robots"]')).toBe('noindex, nofollow');
});

test('the Google tag loads on public pages and stays off the private receipt link', async ({ page }) => {
  // next/script injects the tag after hydration, so it lands in the body rather than the head.
  const tag = page.locator('script[src*="googletagmanager.com/gtag/js"]');
  const dataLayer = () => page.evaluate(() => (window as { dataLayer?: unknown[] }).dataLayer?.length ?? 0);
  // Never let a test run report into the property.
  await page.route('**://*.googletagmanager.com/**', route => route.abort());

  await page.goto('/');
  await expect(tag).toHaveAttribute('src', /[?&]id=G-DS880BMQ34(&|$)/);
  await expect.poll(dataLayer).toBeGreaterThan(0);
  await page.goto('/leaderboard/');
  await expect(tag).toHaveCount(1);

  // gtag reports the full URL, and the receipt fragment is a capability token.
  await page.goto('/run/receipt/#00000000-0000-4000-8000-000000000000.' + 'x'.repeat(43));
  await expect(page.locator('body')).toContainText('Submission receipt');
  await expect(tag).toHaveCount(0);
  expect(await dataLayer()).toBe(0);
});

test('robots, sitemap, and llms.txt are served without a session', async ({ request }) => {
  const robots = await request.get('/robots.txt');
  expect(robots.status()).toBe(200);
  const robotsText = await robots.text();
  expect(robotsText).toContain('Allow: /');
  expect(robotsText).toContain('Disallow: /admin/');
  expect(robotsText).toContain('Sitemap: https://dolphinbench.ai/sitemap.xml');

  const sitemap = await request.get('/sitemap.xml');
  expect(sitemap.status()).toBe(200);
  const xml = await sitemap.text();
  for (const path of ['/', '/leaderboard/', '/dataset/', '/run/', '/personas/morgan/', '/personas/riley/tests/', '/personas/alex/tests/001/']) {
    expect(xml).toContain(`<loc>https://dolphinbench.ai${path}</loc>`);
  }
  expect(xml).not.toContain('/admin/');
  expect(xml.match(/<url>/g)!.length).toBeGreaterThan(600);

  const llms = await request.get('/llms.txt');
  expect(llms.status()).toBe(200);
  expect(llms.headers()['content-type']).toMatch(/text\/markdown/);
  const index = await llms.text();
  expect(index.startsWith('# DolphinBench\n\n> ')).toBe(true);
  for (const line of ['## Leaderboard', '## Dataset', '## Run and submit', '## Contact', 'dolphinbench@mem0.ai',
      'https://dolphinbench.ai/leaderboard/results.json', 'https://dolphinbench.ai/personas/morgan/', 'https://dolphinbench.ai/llms-full.txt']) {
    expect(index).toContain(line);
  }

  const full = await request.get('/llms-full.txt');
  expect(full.status()).toBe(200);
  const text = await full.text();
  expect(text).toContain('# Current official results');
  expect(text).toContain('# Harness integration guide');
  expect(text).toContain('| Memory | Model | Harness | Accuracy |');
});
