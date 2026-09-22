import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';

for (const width of [1440, 390, 320]) {
  test('participation guide at ' + width + 'px', async ({ page, context }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/submissions/**', route => route.fulfill({ json: {
      enabled: false, submissions: [], hasMore: false,
    } }));
    await page.goto('/run/');
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Run and submit');
    await expect(page.getByRole('navigation', { name: 'Primary navigation' })
      .getByRole('link', { name: 'Run and submit' })).toHaveAttribute('aria-current', 'page');
    await expect(page.locator('main .run-section h2')).toHaveText(['Setup', 'Run', 'Package', 'Submit']);
    if (width >= 1024) {
      const steps = page.getByRole('navigation', { name: 'Run and submit sections' });
      await expect(steps.getByRole('link')).toHaveCount(4);
      await expect(steps.getByRole('link').first()).toContainText('01');
    }
    await expect(page.locator('#setup').getByText('01', { exact: true })).toBeVisible();
    await expect(page.locator('main')).not.toContainText('end-to-end reproduction');
    await expect(page.locator('main')).not.toContainText('Sign in');
    await expect(page.locator('#upload')).toContainText('exactly ingestion.json and tests.json at the ZIP root');
    await expect(page.locator('#upload')).toContainText('256 MiB');
    await expect(page.locator('#run')).toContainText('read-only');
    await expect(page.locator('#run')).toContainText('including failures');
    await expect(page.locator('#submission')).toContainText('python -m harness.runner package');
    await expect(page.locator('#setup')).toContainText('python -m harness.runner init');
    await expect(page.locator('#setup')).toContainText('Connect your existing agent in my_harness.py');
    await expect(page.locator('#setup')).not.toContainText('my_harness:BenchmarkHarness');
    await expect(page.locator('#setup')).not.toContainText('Run configuration');
    await expect(page.getByRole('link', { name: 'Harness integration guide', exact: true }))
      .toHaveAttribute('href', '/run/guide/');
    for (const file of ['docs/DRIVER_CONTRACT.md', 'examples/harness_template.py', 'examples/mcp_connection.py', 'harness/submission.py']) {
      expect((await page.request.get('/run/repo/' + file + '/')).status()).toBe(200);
    }
    expect((await page.request.get('/run/repo/.env/')).status()).toBe(404);
    await expect(page.locator('input[type=file]')).toBeDisabled();
    await page.evaluate(() => scrollTo(0, 0));
    expect((await page.locator('main pre').first().boundingBox())!.y).toBeLessThan(900);
    if (width < 1024) {
      await page.getByLabel('Jump to section').selectOption('run');
    } else {
      await page.getByRole('navigation', { name: 'Run and submit sections' })
        .getByRole('link', { name: 'Run' }).click();
    }
    await expect(page).toHaveURL(/#run$/);
    const runHeading = await page.locator('#run-heading').boundingBox();
    expect(runHeading!.y).toBeGreaterThanOrEqual(100);
    expect(runHeading!.y).toBeLessThan(220);
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.getByRole('button', { name: 'Copy Get the repository', exact: true }).click();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toContain('github.com/mem0ai/dolphinbench.git');
    await page.screenshot({ path: testInfo.outputPath('run.png'), fullPage: true });
    await page.getByText('Required format and examples', { exact: true }).click();
    await expect(page.locator('#format')).toHaveAttribute('open', '');
    await expect(page.locator('#format')).toContainText('judge_settings');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await page.evaluate(() => Array.from(document.querySelectorAll('main pre')).every(element =>
      element.scrollWidth <= element.clientWidth))).toBe(true);
    await page.screenshot({ path: testInfo.outputPath('run-expanded.png'), fullPage: true });
    expect(errors).toEqual([]);
  });
}

for (const width of [1440, 390, 320]) {
  test(`integration documents and source copying at ${width}px`, async ({ page, context }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.route('**/api/submissions/**', route => route.fulfill({ json: { enabled: false, submissions: [] } }));
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    for (const [name, route, file] of [
      ['Harness integration guide', 'guide', 'docs/DRIVER_CONTRACT.md'],
      ['Python template', 'template', 'examples/harness_template.py'],
    ]) {
      const source = readFileSync('../' + file, 'utf8');
      await page.goto('/run/');
      await page.getByRole('button', { name: `Copy ${name} for agent`, exact: false }).click();
      expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(source);
      await page.getByRole('link', { name, exact: true }).click();
      await expect(page).toHaveURL(new RegExp(`/run/${route}/$`));
      await expect(page.getByRole('heading', { level: 1 })).toHaveText(name);
      await expect(page).toHaveTitle(new RegExp(name));
      await page.getByRole('button', { name: new RegExp(`Copy ${name} for agent`, 'i') }).click();
      expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(source);
      await expect(page.locator('[role=status]')).toHaveText('Copied');
      await expect(page.locator('article .hljs-keyword').first()).toBeVisible();
      if (route === 'guide') {
        await expect(page.locator('article table')).toHaveCount(1);
        await expect(page.locator('article').getByRole('link', { name: 'Python template', exact: true }))
          .toHaveAttribute('href', '/run/template/');
      } else {
        expect(await page.locator('article pre').textContent()).toBe(source);
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      expect(await page.evaluate(() => Array.from(document.querySelectorAll('article pre')).every(element =>
        element.scrollWidth <= element.clientWidth))).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`${route}.png`), fullPage: true });
      await page.getByRole('link', { name: 'Run and submit', exact: true }).last().click();
      await expect(page).toHaveURL(/\/run\/#setup$/);
    }
    expect(errors).toEqual([]);
  });
}

for (const width of [1440, 390, 320]) {
  test(`submission form and evidence statuses at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('https://challenges.cloudflare.com/turnstile/v0/api.js**', route => route.fulfill({
      contentType: 'application/javascript', body: `window.turnstile = {
        render: function(el, options) {
          el.dataset.widgetSize = options.size;
          const timer = setTimeout(function() { options.callback('browser-test-token'); }, 10);
          const frame = document.createElement('iframe');
          frame.id = 'verification-' + timer;
          frame.title = 'Browser verification';
          frame.style.cssText = options.size === 'flexible'
            ? 'width:100%;min-width:300px;height:65px;border:0;display:block'
            : 'width:150px;height:140px;border:0;display:block';
          el.appendChild(frame);
          return String(timer);
        },
        remove: function(id) {
          clearTimeout(Number(id));
          document.getElementById('verification-' + id)?.remove();
        }
      };`,
    }));
    const common = { harness: 'Custom harness', model: 'Custom model', memory: 'Custom memory', attempts: 0 };
    await page.route('**/api/submissions/**', route => {
      if (route.request().method() === 'POST') return route.fulfill({ status: 429, json: { error: 'Limit reached: 5 uploads per day and 2 pending submissions per browser.' } });
      return route.fulfill({ json: { enabled: true, siteKey: 'test-site', hasMore: false, maxUploadBytes: 256 * 1024 ** 2, submissions: [
        { ...common, id: 'pending', name: 'Pending run', status: 'queued', error_message: 'Validation could not finish. The run remains pending for retry.' },
        { ...common, id: 'rejected', name: 'Rejected run', status: 'rejected', error_message: 'ZIP must contain ingestion.json and tests.json only.' },
        { ...common, id: 'accepted', name: 'A'.repeat(120), status: 'accepted', blob_sha256: 'a'.repeat(64), release_sha256: 'b'.repeat(64),
          summary: { passes: 300, tests: 600, passes_by_persona: { morgan: 100, alex: 100, riley: 100 } } },
      ] } });
    });
    await page.goto('/run/#upload');
    await expect(page.getByLabel('Submission ZIP')).toBeEnabled();
    const verification = page.locator('[data-widget-size="flexible"] iframe');
    await expect(verification).toBeVisible();
    const verificationBounds = (await verification.boundingBox())!;
    expect(verificationBounds.height).toBe(65);
    expect(verificationBounds.width).toBeGreaterThanOrEqual(300);
    expect(verificationBounds.x).toBeGreaterThanOrEqual(0);
    expect(verificationBounds.x + verificationBounds.width).toBeLessThanOrEqual(width);
    await page.locator('#upload').evaluate(element => element.scrollIntoView({ block: 'start' }));
    await page.screenshot({ path: testInfo.outputPath('upload-empty.png') });
    await page.getByLabel('Run name', { exact: true }).fill('My run');
    await page.getByLabel('Harness', { exact: true }).fill('Custom harness');
    await page.getByLabel('Model', { exact: true }).fill('Custom model');
    await page.getByLabel('Memory system', { exact: true }).fill('Custom memory');
    const picker = page.waitForEvent('filechooser');
    await page.getByLabel('Submission ZIP').click();
    await (await picker).setFiles({ name: 'submission.zip', mimeType: 'application/zip', buffer: Buffer.from('test') });
    await expect(page.getByText('submission.zip', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Remove selected ZIP' }).click();
    await expect(page.getByText('Drag and drop your ZIP here', { exact: true })).toBeVisible();
    const dropped = await page.evaluateHandle(() => {
      const transfer = new DataTransfer();
      transfer.items.add(new File(['test'], 'submission.zip', { type: 'application/zip' }));
      return transfer;
    });
    await page.getByLabel('Submission ZIP').dispatchEvent('dragenter', { dataTransfer: dropped });
    await expect(page.getByText('Drop your ZIP here', { exact: true })).toBeVisible();
    await page.getByLabel('Submission ZIP').dispatchEvent('drop', { dataTransfer: dropped });
    await dropped.dispose();
    await expect(page.getByText('submission.zip', { exact: true })).toBeVisible();
    await page.getByRole('checkbox').check();
    const request = page.waitForRequest(request => request.method() === 'POST' && new URL(request.url()).pathname === '/api/submissions/');
    await page.getByRole('button', { name: 'Submit run', exact: true }).click();
    expect((await request).postDataJSON()).toEqual({ name: 'My run', harness: 'Custom harness', model: 'Custom model', memory: 'Custom memory', filename: 'submission.zip', size: 4, confirm: true, contact_email: '', botToken: 'browser-test-token' });
    await expect(page.locator('#upload').getByRole('alert')).toContainText('Limit reached');
    await expect(page.locator('#upload article').getByText('Self-submitted · Unverified', { exact: true })).toBeVisible();
    await expect(page.locator('#upload article').last()).toContainText('300/600');
    await expect(page.getByText('ZIP must contain ingestion.json and tests.json only.')).toBeVisible();
    await page.getByText('Score details', { exact: true }).click();
    await expect(page.getByRole('link', { name: 'Download result' })).toHaveAttribute('download', 'accepted.json');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.locator('#upload').screenshot({ path: testInfo.outputPath('submission-states.png') });
    await page.locator('#upload').evaluate(element => element.scrollIntoView({ block: 'start' }));
    await page.screenshot({ path: testInfo.outputPath('upload-viewport.png') });
    expect(errors).toEqual([]);
  });
}


test('anonymous receipt access, bot check, withdrawal, and admin-only moderation', async ({ page, context, browser }, testInfo) => {
  const url = process.env.DOLPHINBENCH_TEST_DATABASE_URL;
  test.skip(!url, 'Requires the isolated local database and official Turnstile test keys.');
  expect(new URL(url!).host).toBe('127.0.0.1:55439');
  expect(new URL(url!).pathname).toBe('/submissions');
  const { default: postgres } = await import('postgres');
  const { randomUUID, randomBytes, createHash } = await import('node:crypto');
  const sql = postgres(url!, { max: 1 });
  const admin = randomUUID();
  const token = randomBytes(32).toString('base64url');
  const origin = new URL(String(testInfo.project.use.baseURL)).origin;
  const submitted: string[] = [];
  const other = await browser.newContext({ baseURL: origin });
  try {
    const opened = await context.request.get('/api/submissions/');
    expect(opened.status()).toBe(200);
    expect((await opened.json()).enabled).toBe(true);
    expect((await context.request.get('/api/submissions/?view=admin')).status()).toBe(403);
    expect((await context.request.post('/api/submissions/', { headers: { Origin: 'https://attacker.test' }, data: {} })).status()).toBe(403);
    const data = { name: 'Browser API run', harness: 'Custom harness', model: 'Custom model',
      memory: 'Custom memory', filename: 'submission.zip', size: 4, confirm: true };
    expect((await context.request.post('/api/submissions/', { headers: { Origin: origin }, data })).status()).toBe(403);
    const response = await context.request.post('/api/submissions/', { headers: { Origin: origin },
      data: { ...data, botToken: 'XXXX.DUMMY.TOKEN.XXXX' } });
    expect(response.status(), await response.text()).toBe(201);
    const run = await response.json();
    submitted.push(run.id);
    expect((await (await other.request.get('/api/submissions/?view=mine')).json()).submissions).toEqual([]);
    expect((await other.request.get('/api/submissions/' + run.id + '/')).status()).toBe(404);
    expect((await other.request.post('/api/submissions/upload/', { headers: { Origin: origin }, data: {
      type: 'blob.generate-client-token', payload: { pathname: run.pathname, clientPayload: JSON.stringify({ id: run.id, receipt: 'invalid' }) },
    } })).status()).toBe(403);
    expect((await context.request.patch('/api/submissions/' + run.id + '/', { headers: { Origin: origin },
      data: { action: 'remove', reason: 'Not an admin' } })).status()).toBe(401);
    expect((await context.request.get('/api/submissions/' + run.id + '/', { headers: { Authorization: 'Bearer ' + run.receipt } })).status()).toBe(200);
    const receiptPage = await other.newPage();
    await receiptPage.setViewportSize({ width: 390, height: 844 });
    const requested: string[] = [];
    receiptPage.on('request', request => requested.push(request.url()));
    await receiptPage.goto('/run/receipt/#' + run.id + '.' + run.receipt);
    await expect(receiptPage.getByText('Browser API run', { exact: true })).toBeVisible();
    await receiptPage.getByRole('button', { name: 'Withdraw', exact: true }).click();
    await receiptPage.getByRole('dialog').screenshot({ path: testInfo.outputPath('withdraw.png') });
    await receiptPage.getByRole('button', { name: 'Withdraw run', exact: true }).click();
    await expect(receiptPage.getByText('Removal reason: Withdrawn by submitter')).toBeVisible();
    expect(requested.every(request => !request.includes(run.receipt))).toBe(true);
    expect(requested.every(request => !request.includes('/api/events'))).toBe(true);
    await sql`INSERT INTO partner_accounts (id, username, label, role, password_hash)
      VALUES (${admin}, ${admin}, 'Browser admin', 'admin', 'test-only')`;
    await sql`INSERT INTO auth_sessions (token_hash, account_id, expires_at)
      VALUES (${createHash('sha256').update(token).digest('hex')}, ${admin}, now() + interval '1 hour')`;
    await context.addCookies([{ name: 'dolphinbench_session', value: token, url: origin }]);
    await page.goto('/admin/');
    await expect(page.getByText('Browser API run', { exact: true })).toBeVisible();
    expect((await (await context.request.get('/api/submissions/?view=public')).json()).submissions).toEqual([]);
    await sql`UPDATE submissions SET status = 'queued' WHERE id = ${run.id}`;
    await page.getByRole('button', { name: 'Refresh submissions' }).click();
    await page.getByRole('button', { name: 'Remove', exact: true }).click();
    await page.getByLabel('Reason', { exact: true }).fill('Test moderation');
    await page.getByRole('button', { name: 'Remove run', exact: true }).click();
    await expect(page.getByText('Removal reason: Test moderation', { exact: true })).toBeVisible();
  } finally {
    if (submitted.length) await sql`DELETE FROM submissions WHERE id IN ${sql(submitted)}`;
    await sql`DELETE FROM partner_accounts WHERE id = ${admin}`;
    await sql.end();
    await other.close();
  }
});
