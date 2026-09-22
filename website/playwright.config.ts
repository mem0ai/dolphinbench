import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/browser',
  timeout: 60_000,
  workers: 1,
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:3110',
    viewport: { width: 1440, height: 1000 },
    trace: 'retain-on-failure',
  },
  webServer: process.env.PLAYWRIGHT_BASE_URL
    ? undefined
    : {
        stderr: 'ignore',
        command:
          'DOLPHINBENCH_LOCAL_PREVIEW=1 npm run dev -- --hostname 127.0.0.1 --port 3110',
        url: 'http://127.0.0.1:3110',
        reuseExistingServer: false,
        timeout: 120_000,
      },
});
