import { test, expect } from '@playwright/test';

test('header, footer, and type system', async ({ page }) => {
  await page.goto('/');
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' });
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
  const banner = page.getByRole('banner');
  await expect(banner.getByRole('link', { name: 'DolphinBench', exact: true })).toHaveAttribute('href', '/');
  await expect(banner.getByRole('link', { name: 'GitHub', exact: true })).toHaveAttribute(
    'href',
    'https://github.com/mem0ai/dolphinbench',
  );
  await expect(banner.getByRole('link', { name: 'Discord', exact: true })).toHaveAttribute('href', /discord|mem0\.dev/);
  await expect(navigation.getByRole('link', { name: /^Paper/ })).toHaveAttribute('href', /^https:\/\//);
  const footer = page.getByRole('contentinfo');
  await expect(footer).toContainText('Mapping the Pareto frontier of agent memory');
  await expect(footer.getByRole('link', { name: 'mem0', exact: true })).toHaveAttribute('href', 'https://mem0.ai');
  await expect(footer.getByRole('link', { name: 'dolphinbench@mem0.ai' })).toHaveAttribute('href', 'mailto:dolphinbench@mem0.ai');
  await expect(footer.getByRole('navigation', { name: 'Footer' }).getByRole('link'))
    .toHaveText(['Leaderboard', 'Dataset', /^Paper/, /^Discord/, /^GitHub/]);
  for (const link of await banner.locator('a[href^="http"]').all()) {
    await expect(link).toHaveAttribute('target', '_blank');
    await expect(link).toHaveAttribute('rel', /noopener/);
  }
  for (const link of await footer.locator('a[href^="http"]').all()) {
    await expect(link).toHaveAttribute('target', '_blank');
    await expect(link).toHaveAttribute('rel', /noopener/);
  }
  for (const link of await footer.locator('a[href^="/"]').all()) {
    await expect(link).not.toHaveAttribute('target', '_blank');
  }
  const family = await page.evaluate(() => getComputedStyle(document.body).fontFamily);
  expect(family.toLowerCase()).toContain('fustat');
  expect(
    await page.evaluate(() => getComputedStyle(document.body).backgroundColor),
  ).toBe('rgb(255, 255, 255)');
  expect(
    await page.evaluate(() => getComputedStyle(document.body).color),
  ).toBe('rgb(10, 10, 10)');
  await page.goto('/personas/morgan/');
  await expect(
    navigation.getByRole('link', { name: 'Dataset', exact: true }),
  ).toHaveAttribute('aria-current', 'page');
  await page.setViewportSize({ width: 320, height: 760 });
  await page.goto('/');
  await expect(navigation.getByRole('link')).toHaveCount(5);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
  ).toBe(true);
  await page.setViewportSize({ width: 660, height: 900 });
  await page.goto('/');
  await expect(navigation.getByRole('link')).toHaveText([
    'Home',
    'Leaderboard',
    'Dataset',
    'Run and submit',
    /^Paper/,
  ]);
  for (const link of await navigation.getByRole('link').all()) {
    await expect(link).toBeVisible();
  }
  const headerBox = await banner.boundingBox();
  const navBox = await navigation.boundingBox();
  expect(headerBox).not.toBeNull();
  expect(navBox).not.toBeNull();
  expect(headerBox!.height).toBeGreaterThanOrEqual(navBox!.height);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
  ).toBe(true);
});
