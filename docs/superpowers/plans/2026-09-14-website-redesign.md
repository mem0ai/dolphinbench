# Website Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restyle the DolphinBench website (`website/`) to match the Claude Design handoff `DolphinBench v2.dc.html`, keeping every route, data source, auth path, and behavior.

**Architecture:** The Next.js 16 App Router app stays as it is; only the visual layer changes. A new design system (Fustat + JetBrains Mono, ink/paper/sand palette) goes into `tailwind.config.ts` and `styles/globals.css`. The results block is rebuilt as small components under `components/results/` fed by pure helpers in `lib/results-view.ts` (unit-tested through Playwright, which resolves the `@/` alias). Every number on the site is derived from `content/morgan-results.json` or `public/data/`.

**Tech Stack:** Next.js 16.3 (App Router, Turbopack), React 18, Tailwind 3.4, TypeScript 5, Playwright 1.56, Python 3.12 (data build only).

**Spec:** `docs/superpowers/specs/2026-09-14-website-redesign-design.md`

## Global Constraints

- All work happens in `website/` unless a path says otherwise. Run all commands from `website/`.
- Real data only: never hard-code results, persona stats, message text, or counts. Read them from `lib/results.ts`, `lib/data.ts`, or `public/data/`.
- Palette (exact): ink `#0A0A0A`, paper `#FFFFFF`, sand `#F5F3EE`, muted `#6E6A62`, faint `#A8A39A`, highlight `#A9DDF5`, pass `#1E7A62`, quadrant `#2FA98C` at 8%, fail `#B3261E`, hover-ink `#2A2A2A`, hairline `rgba(10,10,10,0.1)`, control border `rgba(10,10,10,0.15)`, link underline `rgba(10,10,10,0.3)`.
- Series colors: memory Mem0 `#3AA7E0`, Honcho `#FF8A5B`, Hindsight `#7B61FF`, Supermemory `#2FA98C`, Built-in `#A8A39A`; model GPT 5.6 Luna `#1F4E79`, MiniMax M2.5 `#D97B4A`, Claude Sonnet 4.6 `#C9A227`; harness Hermes `#3AA7E0`, Claude Code `#D97B4A`.
- Fonts: Fustat 400/500/600/700 self-hosted from `assets/fonts/`; JetBrains Mono 400/500 from `next/font/google`. Container `max-width: 1120px`, padding 32px (20px below 640px).
- Light theme only. Below 768px, grids stack, nav wraps under the wordmark, the results table scrolls in its own `overflow-x: auto` box. The page body never scrolls horizontally.
- Logos are served from static imports under `assets/logos/`; no runtime requests to third-party favicon services.
- Every "→" arrow lives in `<span aria-hidden="true">→</span>` so accessible link names stay exact ("Full leaderboard", "View leaderboard").
- Do not commit `.claude/launch.json` or `website/next-env.d.ts` (dev-server side effects). Do not deploy. Do not run `db:setup` or `accounts`.
- Before writing any Next.js-specific code, read the relevant guide under `node_modules/next/dist/docs/` (this Next.js version differs from older ones; `website/AGENTS.md` says so).
- Commit after each task with the trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Verification commands

- Type check: `npm run typecheck`
- Playwright, full: `npm run test:e2e` (starts its own server on port 3110; takes several minutes because of the 600-route test)
- Playwright, one file against the already-running preview on 3108: `PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/<file> --reporter=line`
- Preview server: `npm run preview` (http://127.0.0.1:3108). It hot-reloads. If it is not running, start it in the background first.
- Python tests: `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`
- Production build: `npm run build`

## File structure

| Path | Responsibility |
| --- | --- |
| `assets/fonts/Fustat-*.ttf` | Self-hosted brand font (copied from the handoff) |
| `assets/logos/{mem0.svg,honcho.png,nousresearch.png,openai.png}` | Memory / harness / provider marks |
| `tailwind.config.ts` | Color tokens, type scale, fonts |
| `styles/globals.css` | Base styles and shared component classes (`.container-x`, `.page-title`, `.text-link`, `.pill-button`, `.segmented`, `.field`, `.code-block`, results/chart/run classes) |
| `app/layout.tsx` | Fonts, favicon, footer |
| `components/Header.tsx` | Sticky nav |
| `lib/site.ts` | Repository URLs and name |
| `lib/results.ts` | Official results rows, coverage label, dimension colors |
| `lib/results-view.ts` | Pure filter / sort / Pareto / breakdown helpers (no React, no images) |
| `lib/logos.ts` | Static logo imports keyed by display name (components only) |
| `components/results/Logo.tsx` | Small logo image |
| `components/results/ResultsTable.tsx` | Sortable table |
| `components/results/ScatterChart.tsx` | Accuracy vs. cost/latency SVG |
| `components/results/Breakdown.tsx` | Three bar-chart columns |
| `components/results/ResultsExplorer.tsx` | State, header, controls; composes the three above |
| `lib/example.ts` | Server-only derivation of the worked example (test 018) and dataset facts |
| `components/WhyDolphinBench.tsx` | Sand band on Home (replaces `WorkedExample.tsx`) |
| `components/Evaluation.tsx` | "How results are produced" section |
| `app/page.tsx`, `app/leaderboard/page.tsx`, `app/dataset/page.tsx` | Page layouts |
| `components/PersonaHeader.tsx`, `app/personas/**` | Persona pages |
| `components/TestBrowser.tsx`, `components/EventReference.tsx`, `components/SessionCard.tsx` | Dataset detail components |
| `app/run/page.tsx`, `components/RunCode.tsx`, `components/RunNavigation.tsx` | Run page |
| `app/login/page.tsx`, `app/admin/page.tsx` | Auth pages |
| `tests/browser/*.spec.ts` | Playwright coverage |

---

### Task 1: Design system foundation, header, footer

**Files:**
- Create: `assets/fonts/Fustat-Regular.ttf`, `Fustat-Medium.ttf`, `Fustat-SemiBold.ttf`, `Fustat-Bold.ttf`
- Create: `assets/logos/mem0.svg`, `assets/logos/honcho.png`, `assets/logos/nousresearch.png`, `assets/logos/openai.png`
- Modify: `tailwind.config.ts`
- Modify: `styles/globals.css` (lines 1–8 and the top of `@layer components`)
- Modify: `app/layout.tsx`
- Modify: `components/Header.tsx`
- Modify: `lib/site.ts`
- Create: `tests/browser/chrome.spec.ts`

**Interfaces:**
- Produces: Tailwind color names `ink paper sand muted faint highlight pass fail hover-ink hairline control`; CSS classes `.container-x .page-title .section-title .eyebrow .text-link .pill-button .marker .field .segmented .code-block .inline-code .stat-label .stat-value .details-plain`; `repositoryName` in `lib/site.ts`.
- Legacy Tailwind names (`bg surface surface-hover border border-strong fg fg-secondary fg-muted accent accent-hover success warning error`) stay as aliases until Task 10 removes them.

- [ ] **Step 1: Copy fonts and logos into the project**

The handoff bundle is unzipped at `/private/tmp/claude-501/-Users-deshraj-Projects-mem0-org-dolphinbench--claude-worktrees-local-website-benchmark-ce1c55/9a2800ac-69b2-4bc0-b38f-0d9311d43e64/scratchpad/handoff/website-redesign-benchmark/project` and the approved favicon downloads are in `.../scratchpad/logos`.

```bash
H=/private/tmp/claude-501/-Users-deshraj-Projects-mem0-org-dolphinbench--claude-worktrees-local-website-benchmark-ce1c55/9a2800ac-69b2-4bc0-b38f-0d9311d43e64/scratchpad
mkdir -p assets/fonts assets/logos
cp "$H"/handoff/website-redesign-benchmark/project/_ds/*/fonts/Fustat-*.ttf assets/fonts/
cp "$H"/handoff/website-redesign-benchmark/project/assets/mem0-logo.svg assets/logos/mem0.svg
cp "$H"/logos/honcho.dev.png assets/logos/honcho.png
cp "$H"/logos/nousresearch.com.png assets/logos/nousresearch.png
cp "$H"/logos/openai.com.png assets/logos/openai.png
ls -la assets/fonts assets/logos
```

If the scratchpad is gone, re-unzip `"/Users/deshraj/Downloads/Website redesign benchmark-handoff.zip"` and re-download the three PNGs (the user approved this one-time download): `curl -sSL -o assets/logos/honcho.png "https://www.google.com/s2/favicons?domain=honcho.dev&sz=64"` and likewise for `nousresearch.com` and `openai.com`.

Expected: four TTFs of ~96 KB each, one SVG, three 64×64 PNGs.

- [ ] **Step 2: Write the failing chrome test**

Create `tests/browser/chrome.spec.ts`:

```ts
import { test, expect } from '@playwright/test';

test('header, footer, and type system', async ({ page }) => {
  await page.goto('/');
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' });
  await expect(navigation.getByRole('link')).toHaveText([
    'Home',
    'Leaderboard',
    'Dataset',
    'Run and submit',
  ]);
  await expect(
    navigation.getByRole('link', { name: 'Home', exact: true }),
  ).toHaveAttribute('aria-current', 'page');
  const banner = page.getByRole('banner');
  await expect(banner.getByRole('link', { name: 'DolphinBench' })).toHaveAttribute('href', '/');
  await expect(banner.getByRole('link', { name: /mem0ai\/enact/ })).toHaveAttribute(
    'href',
    'https://github.com/mem0ai/enact',
  );
  const footer = page.getByRole('contentinfo');
  await expect(footer).toContainText('Evaluating long-term agent memory through action');
  await expect(footer.getByRole('link')).toHaveText(['Leaderboard', 'Dataset', 'GitHub']);
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
  await expect(navigation.getByRole('link')).toHaveCount(4);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
  ).toBe(true);
});
```

- [ ] **Step 3: Run it to confirm it fails**

Run: `PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/chrome.spec.ts --reporter=line`
Expected: FAIL on the `mem0ai/enact` link (the old header has an icon-only link) and on the font family.

- [ ] **Step 4: Replace `tailwind.config.ts`**

```ts
import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}', './lib/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: '#0A0A0A',
        paper: '#FFFFFF',
        sand: '#F5F3EE',
        muted: '#6E6A62',
        faint: '#A8A39A',
        highlight: '#A9DDF5',
        pass: '#1E7A62',
        fail: '#B3261E',
        'hover-ink': '#2A2A2A',
        hairline: 'rgba(10,10,10,0.1)',
        control: 'rgba(10,10,10,0.15)',
        // Legacy aliases; removed in the cleanup task once no page uses them.
        bg: '#FFFFFF',
        surface: '#FFFFFF',
        'surface-hover': '#F5F3EE',
        border: 'rgba(10,10,10,0.1)',
        'border-strong': 'rgba(10,10,10,0.3)',
        fg: '#0A0A0A',
        'fg-secondary': '#0A0A0A',
        'fg-muted': '#6E6A62',
        accent: '#0A0A0A',
        'accent-hover': '#2A2A2A',
        'accent-soft': '#0A0A0A',
        success: '#1E7A62',
        warning: '#9a5700',
        error: '#B3261E',
      },
      fontFamily: {
        sans: ['var(--font-sans)', 'system-ui', 'sans-serif'],
        mono: ['var(--font-mono)', 'ui-monospace', 'Menlo', 'monospace'],
      },
      fontSize: {
        xs: ['12px', { lineHeight: '1.5' }],
        sm: ['13px', { lineHeight: '1.5' }],
        base: ['15px', { lineHeight: '1.5' }],
        md: ['16px', { lineHeight: '1.6' }],
        lg: ['18px', { lineHeight: '1.5' }],
        xl: ['19px', { lineHeight: '1.5' }],
        '2xl': ['24px', { lineHeight: '1.25' }],
        '3xl': ['28px', { lineHeight: '1.2' }],
        '4xl': ['32px', { lineHeight: '1.1' }],
      },
      borderRadius: { DEFAULT: '6px', md: '6px', lg: '10px' },
      transitionDuration: { DEFAULT: '150ms' },
    },
  },
  plugins: [],
};

export default config;
```

- [ ] **Step 5: Replace the top of `styles/globals.css`**

Replace everything from line 1 through the line `@layer components {` (inclusive) with the block below. Keep every existing rule after it for now (they are removed page by page).

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

:root {
  color-scheme: light;
}

@layer base {
  html {
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }
  body {
    @apply bg-paper text-ink;
  }
  ::selection {
    background: #a9ddf5;
    color: #0a0a0a;
  }
  input::placeholder {
    color: #a8a39a;
  }
  input:focus,
  select:focus,
  textarea:focus {
    outline: none;
    border-color: #0a0a0a;
  }
  select {
    appearance: none;
    -webkit-appearance: none;
  }
  h1,
  h2,
  h3,
  h4 {
    text-wrap: balance;
  }
  p {
    text-wrap: pretty;
  }
}

@layer components {
  .container-x {
    @apply mx-auto w-full max-w-[1120px] px-5 sm:px-8;
  }
  .page-title {
    font-size: clamp(40px, 5vw, 64px);
    @apply font-semibold leading-none tracking-[-0.035em];
  }
  .section-title {
    font-size: clamp(34px, 4.2vw, 56px);
    @apply font-semibold leading-[1.02] tracking-[-0.035em];
  }
  .eyebrow {
    @apply font-mono text-xs text-muted;
  }
  .text-link {
    @apply border-b border-ink/30 pb-px font-medium transition-colors hover:border-ink;
  }
  .pill-button {
    @apply inline-flex items-center gap-2.5 rounded-full bg-ink px-[22px] py-[13px] text-base font-medium text-paper transition-colors hover:bg-hover-ink;
  }
  .marker {
    background: linear-gradient(transparent 62%, #a9ddf5 62%, #a9ddf5 92%, transparent 92%);
  }
  .field {
    @apply h-[38px] rounded-md border border-control bg-paper px-3 text-sm text-ink;
  }
  select.field {
    padding-right: 30px;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'><path d='M2 4l4 4 4-4' fill='none' stroke='%230A0A0A' stroke-width='1.5'/></svg>");
    background-repeat: no-repeat;
    background-position: right 10px center;
  }
  .segmented {
    @apply inline-flex flex-wrap overflow-hidden rounded-md border border-control text-sm;
  }
  .segmented > button {
    @apply inline-flex items-center gap-1.5 bg-paper px-3.5 py-2 text-ink transition-colors hover:bg-sand;
  }
  .segmented > button + button {
    @apply border-l border-control;
  }
  .segmented > button[aria-pressed='true'] {
    @apply bg-ink text-paper hover:bg-ink;
  }
  .code-block {
    @apply overflow-hidden rounded-md border border-hairline bg-sand;
  }
  .code-block pre {
    @apply overflow-x-auto px-4 py-3.5 font-mono text-sm leading-[1.75];
  }
  .inline-code {
    @apply rounded bg-sand px-1.5 py-px font-mono text-[0.9em];
  }
  .stat-label {
    @apply text-sm text-muted;
  }
  .stat-value {
    @apply text-3xl font-semibold tracking-[-0.025em];
  }
  .details-plain > summary {
    @apply cursor-pointer list-none text-sm text-muted hover:text-ink;
  }
  .details-plain > summary::-webkit-details-marker {
    display: none;
  }
```

- [ ] **Step 6: Add the repository name to `lib/site.ts`**

```ts
export const repositoryUrl = 'https://github.com/mem0ai/enact';
export const repositoryName = 'mem0ai/enact';
export const sourceBranch = 'website/dataset-browser';
export const evaluationUrl = `${repositoryUrl}/blob/${sourceBranch}/docs/CANONICAL_EVALUATION.md`;
export const methodologyUrl = `${repositoryUrl}/blob/${sourceBranch}/docs/methodology.md`;
```

- [ ] **Step 7: Replace `app/layout.tsx`**

Read `node_modules/next/dist/docs/01-app/01-getting-started/13-fonts.md` first. The `localFont` result's variable name becomes the generated font-family name, so the variable must be called `fustat`.

```tsx
import type { Metadata } from 'next';
import localFont from 'next/font/local';
import { JetBrains_Mono } from 'next/font/google';
import '../styles/globals.css';
import Header from '@/components/Header';
import ActivityTracker from '@/components/ActivityTracker';
import { localPreview } from '@/lib/preview';
import { repositoryUrl } from '@/lib/site';

const fustat = localFont({
  src: [
    { path: '../assets/fonts/Fustat-Regular.ttf', weight: '400' },
    { path: '../assets/fonts/Fustat-Medium.ttf', weight: '500' },
    { path: '../assets/fonts/Fustat-SemiBold.ttf', weight: '600' },
    { path: '../assets/fonts/Fustat-Bold.ttf', weight: '700' },
  ],
  variable: '--font-sans',
  display: 'swap',
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ['latin'],
  weight: ['400', '500'],
  variable: '--font-mono',
  display: 'swap',
});

const favicon =
  'data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🐬</text></svg>';

export const metadata: Metadata = {
  title: { default: 'DolphinBench', template: '%s | DolphinBench' },
  description:
    'DolphinBench compares agent configurations on tasks that depend on past conversations.',
  icons: { icon: { url: favicon, type: 'image/svg+xml' } },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${fustat.variable} ${jetbrainsMono.variable}`}>
      <body className="min-h-screen bg-paper font-sans text-ink antialiased">
        {!localPreview() && <ActivityTracker />}
        <Header />
        <main id="main-content">{children}</main>
        <footer className="border-t border-hairline bg-sand">
          <div className="container-x flex flex-wrap items-center justify-between gap-4 py-8 text-sm text-muted">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-md leading-none" aria-hidden="true">🐬</span>
              <span className="font-semibold text-ink">DolphinBench</span>
              <span>· Evaluating long-term agent memory through action</span>
            </div>
            <div className="flex gap-6">
              <a href="/leaderboard/" className="transition-colors hover:text-ink">Leaderboard</a>
              <a href="/dataset/" className="transition-colors hover:text-ink">Dataset</a>
              <a href={repositoryUrl} className="transition-colors hover:text-ink">GitHub</a>
            </div>
          </div>
        </footer>
      </body>
    </html>
  );
}
```

- [ ] **Step 8: Replace `components/Header.tsx`**

```tsx
'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Github } from 'lucide-react';
import { repositoryName, repositoryUrl } from '@/lib/site';

const links = [
  ['/', 'Home'],
  ['/leaderboard/', 'Leaderboard'],
  ['/dataset/', 'Dataset'],
  ['/run/', 'Run and submit'],
] as const;

export default function Header() {
  const pathname = usePathname();
  const current = pathname.startsWith('/personas/') ? '/dataset/' : pathname;
  return (
    <header className="sticky top-0 z-40 border-b border-hairline bg-paper/85 backdrop-blur-md">
      <div className="container-x flex flex-wrap items-center justify-between gap-x-6 gap-y-0 py-3 sm:h-[60px] sm:flex-nowrap sm:py-0">
        <Link
          href="/"
          className="inline-flex items-center gap-2 text-base font-semibold tracking-[-0.01em] text-ink"
        >
          <span className="text-lg leading-none" aria-hidden="true">🐬</span>
          DolphinBench
        </Link>
        <nav
          aria-label="Primary navigation"
          className="order-3 flex w-full flex-wrap items-center gap-x-5 text-sm sm:order-none sm:w-auto sm:gap-x-7"
        >
          {links.map(([href, label]) => {
            const active = href === '/' ? current === '/' : current.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                aria-current={active ? 'page' : undefined}
                className={`border-b-[1.5px] py-2 transition-colors hover:text-ink sm:py-5 ${
                  active ? 'border-ink text-ink' : 'border-transparent text-muted'
                }`}
              >
                {label}
              </Link>
            );
          })}
        </nav>
        <a
          href={repositoryUrl}
          className="inline-flex items-center gap-2 text-sm text-muted transition-colors hover:text-ink"
          title="GitHub repository"
        >
          <Github size={16} aria-hidden="true" />
          <span className="hidden sm:inline">{repositoryName}</span>
          <span className="sr-only sm:hidden">{repositoryName}</span>
        </a>
      </div>
    </header>
  );
}
```

- [ ] **Step 9: Type check and run the chrome test**

Run: `npm run typecheck && PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/chrome.spec.ts --reporter=line`
Expected: typecheck clean; 1 passed. If the font assertion fails, run `page.evaluate(() => getComputedStyle(document.body).fontFamily)` in the browser pane and confirm the family is `__fustat_<hash>`; the variable name in `layout.tsx` must be `fustat`.

- [ ] **Step 10: Confirm the old suites still pass**

Run: `PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/home.spec.ts tests/browser/run.spec.ts --reporter=line`
Expected: all pass (the header nav names, aria-current, and copy behavior are unchanged). If `run.spec.ts` fails only on the `#setup-heading` y range, that is fixed in Task 9 — note it and continue.

- [ ] **Step 11: Commit**

```bash
git add assets tailwind.config.ts styles/globals.css app/layout.tsx components/Header.tsx lib/site.ts tests/browser/chrome.spec.ts
git commit -m "Add redesign tokens, fonts, header, and footer

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Results data helpers

**Files:**
- Modify: `lib/results.ts`
- Create: `lib/results-view.ts`
- Create: `tests/browser/results-view.spec.ts`

**Interfaces:**
- Produces from `lib/results.ts`: `benchmarkCoverage`, `coverageLabel(configurations: number): string`, `configurationResults: ResultRow[]`, `type ResultRow`, `type Dimension = 'memory' | 'model' | 'harness'`, `dimensionValue(row, dimension): string`, `dimensionValues(rows, dimension): string[]`, `colorFor(row, dimension): string`, `percent`, `costPerTask`, `formatCostPerTask`, `formatLatency`, `officialResults`, `systemNames`, `type MemorySystem`.
- Produces from `lib/results-view.ts`: `type Metric = 'cost' | 'latency'`, `type SortKey = 'memory' | 'model' | 'harness' | 'accuracy' | 'cost' | 'latency' | 'p95'`, `type SortDirection`, `type View = 'config' | Dimension`, `type Pair = { key: string; harness: string; model: string; provider: string }`, `pairKey(row)`, `configurationPairs(rows): Pair[]`, `type Filters = { query: string; memory: string; pair: string }`, `filterRows(rows, filters)`, `sortRows(rows, key, direction)`, `defaultDirection(key)`, `metricValue(row, metric)`, `paretoFront(rows, metric)`, `availableViews(rows): View[]`, `type BreakdownItem = { label: string; sub: string; color: string; memory?: string; accuracy: number; cost: number; median: number }`, `breakdownItems(rows, view): BreakdownItem[]`, `type BarKey = 'accuracy' | 'cost' | 'median'`, `type Bar = { item: BreakdownItem; width: string; value: string }`, `barList(items, key): Bar[]` (accuracy sorts descending, cost and median ascending).
- `lib/results-view.ts` must never import images or React (Playwright imports it directly).

- [ ] **Step 1: Write the failing helper tests**

Create `tests/browser/results-view.spec.ts`:

```ts
import { test, expect } from '@playwright/test';
import {
  colorFor,
  configurationResults,
  coverageLabel,
  dimensionValues,
  type ResultRow,
} from '@/lib/results';
import {
  availableViews,
  barList,
  breakdownItems,
  configurationPairs,
  filterRows,
  paretoFront,
  sortRows,
} from '@/lib/results-view';

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
    pass_rate: passes / 200,
    total_cost_usd_test_calls: cost * 200,
    median_latency_seconds: median,
  }) as ResultRow;

test('coverage label is computed from the report', () => {
  expect(coverageLabel(3)).toBe(
    'Partial benchmark · 1 of 3 personas · 200 of 600 tests · 3 configurations',
  );
  expect(coverageLabel(1)).toContain('1 configuration');
});

test('pairs, dimension values, and colors come from the data', () => {
  expect(configurationPairs(configurationResults)).toEqual([
    { key: 'Hermes + GPT 5.6 Luna', harness: 'Hermes', model: 'GPT 5.6 Luna', provider: 'OpenAI' },
  ]);
  expect(dimensionValues(configurationResults, 'memory')).toEqual(['Mem0', 'Honcho', 'Built-in']);
  expect(colorFor(configurationResults[0], 'memory')).toBe('#3AA7E0');
  expect(colorFor(configurationResults[2], 'memory')).toBe('#A8A39A');
  expect(colorFor(configurationResults[0], 'harness')).toBe('#3AA7E0');
  expect(colorFor(synthetic('Unknown', 1, 1, 1), 'memory')).toMatch(/^#/);
});

test('filters and sorting', () => {
  const none = { query: '', memory: 'all', pair: 'all' };
  expect(filterRows(configurationResults, none)).toHaveLength(3);
  expect(filterRows(configurationResults, { ...none, query: 'honcho' }).map((r) => r.memory.name)).toEqual(['Honcho']);
  expect(filterRows(configurationResults, { ...none, memory: 'Built-in' })).toHaveLength(1);
  expect(filterRows(configurationResults, { ...none, pair: 'Hermes + GPT 5.6 Luna' })).toHaveLength(3);
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
});

test('breakdown views and bars', () => {
  expect(availableViews(configurationResults)).toEqual(['config', 'memory']);
  const config = breakdownItems(configurationResults, 'config');
  expect(config[0]).toMatchObject({ label: 'Mem0', memory: 'Mem0', sub: 'GPT 5.6 Luna · Hermes' });
  expect(config[0].accuracy).toBeCloseTo(69);
  const byMemory = breakdownItems(configurationResults, 'memory');
  expect(byMemory.map((i) => i.sub)).toEqual(['avg of 1 config', 'avg of 1 config', 'avg of 1 config']);
  const byHarness = breakdownItems(configurationResults, 'harness');
  expect(byHarness).toHaveLength(1);
  expect(byHarness[0]).toMatchObject({ label: 'Hermes', sub: 'avg of 3 configs' });
  expect(byHarness[0].memory).toBeUndefined();
  const accuracy = barList(config, 'accuracy');
  expect(accuracy[0]).toMatchObject({ width: '100.0%', value: '69%' });
  expect(accuracy[2].item.label).toBe('Built-in');
  const cost = barList(config, 'cost');
  expect(cost[0].item.label).toBe('Mem0');
  expect(cost[0].value).toBe('$0.0054');
  expect(barList(config, 'median')[0].value).toBe('38.1 s');
});
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/results-view.spec.ts --reporter=line`
Expected: FAIL — `@/lib/results-view` cannot be resolved.

- [ ] **Step 3: Replace `lib/results.ts`**

```ts
import results from '@/content/morgan-results.json';

export const officialResults = results;
export const benchmarkCoverage = {
  completedPersonas: 1,
  totalPersonas: 3,
  completedTests: Math.max(...Object.values(results.summary).map((summary) => summary.total)),
  totalTests: 600,
};
export function coverageLabel(configurations: number) {
  const { completedPersonas, totalPersonas, completedTests, totalTests } = benchmarkCoverage;
  const partial = completedPersonas < totalPersonas || completedTests < totalTests;
  return [
    partial ? 'Partial benchmark' : 'Full benchmark',
    `${completedPersonas} of ${totalPersonas} personas`,
    `${completedTests} of ${totalTests} tests`,
    `${configurations} configuration${configurations === 1 ? '' : 's'}`,
  ].join(' · ');
}
export type MemorySystem = keyof typeof results.summary;
export const systemNames: Record<MemorySystem, string> = {
  mem0: 'Mem0',
  honcho: 'Honcho',
  builtin: 'Built-in',
};

const modelProviders: Record<string, string> = {
  'gpt-5.6-luna': 'OpenAI',
};

function formatModelName(modelId: string) {
  return modelId
    .split('-')
    .map((part, index) =>
      index === 0 && part.toLowerCase() === 'gpt'
        ? 'GPT'
        : part.charAt(0).toUpperCase() + part.slice(1),
    )
    .join(' ');
}

export const configurationResults = (Object.keys(results.summary) as MemorySystem[])
  .map((memoryId) => ({
    id: `${memoryId}:${results.model_id}:${results.agent.toLowerCase()}`,
    memory: { id: memoryId, name: systemNames[memoryId] },
    model: {
      id: results.model_id,
      name: formatModelName(results.model_id),
      provider: modelProviders[results.model_id] ?? 'Model provider',
    },
    harness: { id: results.agent.toLowerCase(), name: results.agent },
    ...results.summary[memoryId],
  }))
  .sort((a, b) => b.pass_rate - a.pass_rate);
export type ResultRow = (typeof configurationResults)[number];

export const percent = (rate: number) => `${Math.round(rate * 100)}%`;
export const costPerTask = (row: ResultRow) => row.total_cost_usd_test_calls / row.total;
export const formatCostPerTask = (row: ResultRow) => `$${costPerTask(row).toFixed(4)}`;
export const formatLatency = (seconds: number) => `${seconds.toFixed(1)} s`;

export type Dimension = 'memory' | 'model' | 'harness';
const seriesPalettes: Record<Dimension, Record<string, string>> = {
  memory: {
    Mem0: '#3AA7E0',
    Honcho: '#FF8A5B',
    Hindsight: '#7B61FF',
    Supermemory: '#2FA98C',
    'Built-in': '#A8A39A',
  },
  model: { 'GPT 5.6 Luna': '#1F4E79', 'MiniMax M2.5': '#D97B4A', 'Claude Sonnet 4.6': '#C9A227' },
  harness: { Hermes: '#3AA7E0', 'Claude Code': '#D97B4A' },
};
const extraPalette = ['#1F4E79', '#D97B4A', '#C9A227', '#7B61FF', '#2FA98C', '#B3261E', '#3AA7E0', '#FF8A5B'];

export const dimensionValue = (row: ResultRow, dimension: Dimension) =>
  dimension === 'memory' ? row.memory.name : dimension === 'model' ? row.model.name : row.harness.name;
export const dimensionValues = (rows: ResultRow[], dimension: Dimension) =>
  Array.from(new Set(rows.map((row) => dimensionValue(row, dimension))));
export function colorFor(row: ResultRow, dimension: Dimension) {
  const value = dimensionValue(row, dimension);
  const known = seriesPalettes[dimension][value];
  if (known) return known;
  const index = dimensionValues([...configurationResults, row], dimension).indexOf(value);
  return extraPalette[index % extraPalette.length];
}
```

- [ ] **Step 4: Create `lib/results-view.ts`**

```ts
import {
  colorFor,
  costPerTask,
  dimensionValue,
  type Dimension,
  type ResultRow,
} from './results';

export type Metric = 'cost' | 'latency';
export type SortKey = 'memory' | 'model' | 'harness' | 'accuracy' | 'cost' | 'latency' | 'p95';
export type SortDirection = 'asc' | 'desc';
export type View = 'config' | Dimension;
export type Pair = { key: string; harness: string; model: string; provider: string };
export type Filters = { query: string; memory: string; pair: string };

export const pairKey = (row: ResultRow) => `${row.harness.name} + ${row.model.name}`;

export function configurationPairs(rows: ResultRow[]): Pair[] {
  const pairs = new Map<string, Pair>();
  rows.forEach((row) => {
    const key = pairKey(row);
    if (!pairs.has(key))
      pairs.set(key, { key, harness: row.harness.name, model: row.model.name, provider: row.model.provider });
  });
  return Array.from(pairs.values());
}

export function filterRows(rows: ResultRow[], filters: Filters) {
  const query = filters.query.trim().toLowerCase();
  return rows.filter(
    (row) =>
      (filters.memory === 'all' || row.memory.name === filters.memory) &&
      (filters.pair === 'all' || pairKey(row) === filters.pair) &&
      (!query ||
        `${row.memory.name} ${row.model.name} ${row.model.provider} ${row.harness.name}`
          .toLowerCase()
          .includes(query)),
  );
}

const sortValue: Record<SortKey, (row: ResultRow) => string | number> = {
  memory: (row) => row.memory.name,
  model: (row) => row.model.name,
  harness: (row) => row.harness.name,
  accuracy: (row) => row.pass_rate,
  cost: (row) => costPerTask(row),
  latency: (row) => row.median_latency_seconds,
  p95: (row) => row.p95_latency_seconds,
};

export const defaultDirection = (key: SortKey): SortDirection => (key === 'accuracy' ? 'desc' : 'asc');

export function sortRows(rows: ResultRow[], key: SortKey, direction: SortDirection) {
  const value = sortValue[key];
  return [...rows].sort((a, b) => {
    const x = value(a);
    const y = value(b);
    const order = typeof x === 'string' ? x.localeCompare(String(y)) : x - Number(y);
    return direction === 'asc' ? order : -order;
  });
}

export const metricValue = (row: ResultRow, metric: Metric) =>
  metric === 'cost' ? costPerTask(row) : row.median_latency_seconds;

// Upper-left staircase: walking left to right, keep every point that beats the best accuracy so far.
export function paretoFront(rows: ResultRow[], metric: Metric) {
  let best = -1;
  return [...rows]
    .sort((a, b) => metricValue(a, metric) - metricValue(b, metric))
    .filter((row) => {
      if (row.passes <= best) return false;
      best = row.passes;
      return true;
    });
}

export function availableViews(rows: ResultRow[]): View[] {
  const dimensions: Dimension[] = ['memory', 'model', 'harness'];
  return [
    'config',
    ...dimensions.filter((dimension) => new Set(rows.map((row) => dimensionValue(row, dimension))).size > 1),
  ];
}

export type BreakdownItem = {
  label: string;
  sub: string;
  color: string;
  memory?: string;
  accuracy: number;
  cost: number;
  median: number;
};

export function breakdownItems(rows: ResultRow[], view: View): BreakdownItem[] {
  if (view === 'config')
    return rows.map((row) => ({
      label: row.memory.name,
      memory: row.memory.name,
      sub: `${row.model.name} · ${row.harness.name}`,
      color: colorFor(row, 'memory'),
      accuracy: row.pass_rate * 100,
      cost: costPerTask(row),
      median: row.median_latency_seconds,
    }));
  const groups = new Map<string, ResultRow[]>();
  rows.forEach((row) => {
    const key = dimensionValue(row, view);
    groups.set(key, [...(groups.get(key) ?? []), row]);
  });
  return Array.from(groups.entries()).map(([label, group]) => {
    const average = (value: (row: ResultRow) => number) =>
      group.reduce((sum, row) => sum + value(row), 0) / group.length;
    return {
      label,
      memory: view === 'memory' ? label : undefined,
      sub: `avg of ${group.length} config${group.length === 1 ? '' : 's'}`,
      color: colorFor(group[0], view),
      accuracy: average((row) => row.pass_rate * 100),
      cost: average(costPerTask),
      median: average((row) => row.median_latency_seconds),
    };
  });
}

export type BarKey = 'accuracy' | 'cost' | 'median';
export type Bar = { item: BreakdownItem; width: string; value: string };

const barFormat: Record<BarKey, (value: number) => string> = {
  accuracy: (value) => `${Math.round(value)}%`,
  cost: (value) => `$${value.toFixed(4)}`,
  median: (value) => `${value.toFixed(1)} s`,
};

export function barList(items: BreakdownItem[], key: BarKey): Bar[] {
  const ascending = key !== 'accuracy';
  const sorted = [...items].sort((a, b) => (ascending ? a[key] - b[key] : b[key] - a[key]));
  const max = Math.max(...sorted.map((item) => item[key]), 0) || 1;
  return sorted.map((item) => ({
    item,
    width: `${((item[key] / max) * 100).toFixed(1)}%`,
    value: barFormat[key](item[key]),
  }));
}
```

- [ ] **Step 5: Run the helper tests and the type check**

Run: `npm run typecheck && PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/results-view.spec.ts --reporter=line`
Expected: typecheck reports errors in `components/ResultsExplorer.tsx` (it imports the removed `systemColors`) — fix by adding this shim at the top of `lib/results.ts` exports **temporarily**: `export const systemColors: Record<MemorySystem, string> = { mem0: '#3AA7E0', honcho: '#FF8A5B', builtin: '#A8A39A' };` (Task 3 deletes it with the old component). Then: typecheck clean; 5 passed.

- [ ] **Step 6: Commit**

```bash
git add lib/results.ts lib/results-view.ts tests/browser/results-view.spec.ts
git commit -m "Add results view helpers with coverage, pareto, and breakdown logic

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Results explorer shell, controls, and table

**Files:**
- Create: `lib/logos.ts`, `components/results/Logo.tsx`, `components/results/ResultsTable.tsx`, `components/results/ResultsExplorer.tsx`
- Delete: `components/ResultsExplorer.tsx`
- Modify: `app/page.tsx` (import path), `app/leaderboard/page.tsx` (import path, remove duplicate scope/JSON link), `styles/globals.css` (remove legacy results/chart classes, add table classes), `lib/results.ts` (remove the `systemColors` shim)
- Modify: `tests/browser/home.spec.ts`

**Interfaces:**
- Consumes: everything from Task 2.
- Produces: `ResultsExplorer({ showLeaderboardLink?: boolean })`; `ResultsTable({ rows, total, sortKey, sortDirection, onSort, activeId, onActive })`; `Logo({ src?: string; size?: number; className?: string })`; `lib/logos.ts` exports `memoryLogos`, `harnessLogos`, `providerLogos` (`Record<string, string>` keyed by display name).
- Tasks 4 and 5 insert `<ScatterChart>` and `<Breakdown>` into `ResultsExplorer` between the controls and the table.

- [ ] **Step 1: Update `tests/browser/home.spec.ts` for the new results block**

Make these exact edits (the rest of the file stays for now; Tasks 4–7 change more):

1. In the per-viewport test, delete the two chart `img` assertions (`/Accuracy versus cost/` and `/Accuracy versus latency/`), the `Latency` button click between them, and the `.results-table-wrap`/`.pareto-explorer` `compareDocumentPosition` block. Keep the `Official ranking` assertion.
2. After the `for` loop over rows on Home, add p95 assertions on Home too:

```ts
    for (const [name, p95] of [
      ['Mem0', '63.8 s'],
      ['Honcho', '69.9 s'],
      ['Built-in', '83.1 s'],
    ] as const) {
      await expect(table.getByRole('row').filter({ hasText: name })).toContainText(p95);
    }
    await expect(page.locator('main')).toContainText(
      'Partial benchmark · 1 of 3 personas · 200 of 600 tests · 3 configurations',
    );
    await expect(page.getByRole('group', { name: 'Configuration groups' })).toHaveCount(0);
```

3. In the leaderboard section replace

```ts
    await expect(page.locator('main')).toContainText(
      'Partial benchmark1 of 3 personas200 of 600 tests',
    );
    await expect(
      page.getByRole('heading', { name: 'Accuracy vs. cost', exact: true }),
    ).toBeVisible();
```

with

```ts
    await expect(page.locator('main')).toContainText(
      'Partial benchmark · 1 of 3 personas · 200 of 600 tests · 3 configurations',
    );
```

and delete the second `compareDocumentPosition` block (chart before table) on the leaderboard.

4. Replace the whole `configuration filters, selection, sorting, and chart grouping stay linked` test with:

```ts
test('configuration filters and sorting stay linked', async ({ page }) => {
  await page.goto('/leaderboard/');
  const table = page.getByRole('table', { name: 'Evaluation results' });
  await page.getByLabel('Filter by memory system').selectOption({ label: 'Honcho' });
  await expect(table.locator('tbody tr')).toHaveCount(1);
  await expect(table.locator('tbody tr')).toContainText('Honcho');
  await expect(page.getByText('1 of 3 configurations shown')).toBeVisible();
  await page.getByLabel('Filter by memory system').selectOption('all');
  await expect(table.locator('tbody tr')).toHaveCount(3);

  await page.getByLabel('Search configurations').fill('built');
  await expect(table.locator('tbody tr')).toHaveCount(1);
  await page.getByLabel('Search configurations').fill('nothing-matches');
  await expect(page.getByText('No configurations match these filters.')).toBeVisible();
  await page.getByLabel('Search configurations').fill('');
  await expect(table.locator('tbody tr')).toHaveCount(3);

  await expect(table.locator('tbody tr').first()).toContainText('Mem0');
  await page.getByRole('button', { name: /Cost \/ task/ }).click();
  await expect(table.locator('tbody tr').first()).toContainText('Mem0');
  await page.getByRole('button', { name: /Cost \/ task/ }).click();
  await expect(table.locator('tbody tr').first()).toContainText('Built-in');
  await expect(table.locator('thead th').nth(4)).toHaveAttribute('aria-sort', 'descending');
  await page.getByRole('button', { name: /Memory/ }).click();
  await expect(table.locator('tbody tr').first()).toContainText('Built-in');
});
```

- [ ] **Step 2: Run the home spec to confirm the new assertions fail**

Run: `PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/home.spec.ts --reporter=line`
Expected: FAIL on the coverage text and on `Search configurations`.

- [ ] **Step 3: Create `lib/logos.ts` and `components/results/Logo.tsx`**

`lib/logos.ts`:

```ts
import mem0 from '@/assets/logos/mem0.svg';
import honcho from '@/assets/logos/honcho.png';
import nous from '@/assets/logos/nousresearch.png';
import openai from '@/assets/logos/openai.png';

// Keyed by the display names in lib/results.ts. Missing keys render a monogram instead.
export const memoryLogos: Record<string, string> = { Mem0: mem0.src, Honcho: honcho.src };
export const harnessLogos: Record<string, string> = { Hermes: nous.src };
export const providerLogos: Record<string, string> = { OpenAI: openai.src };
```

`components/results/Logo.tsx`:

```tsx
export default function Logo({
  src,
  size = 14,
  className = '',
}: {
  src?: string;
  size?: number;
  className?: string;
}) {
  if (!src) return null;
  return (
    <img
      src={src}
      alt=""
      width={size}
      height={size}
      className={`shrink-0 rounded-[3px] object-contain ${className}`}
    />
  );
}
```

- [ ] **Step 4: Create `components/results/ResultsTable.tsx`**

```tsx
'use client';

import { colorFor, formatCostPerTask, formatLatency, percent, type ResultRow } from '@/lib/results';
import type { SortDirection, SortKey } from '@/lib/results-view';
import { memoryLogos } from '@/lib/logos';
import Logo from './Logo';

const columns: readonly [SortKey, string, boolean][] = [
  ['memory', 'Memory', false],
  ['model', 'Model / provider', false],
  ['harness', 'Harness', false],
  ['accuracy', 'Accuracy', true],
  ['cost', 'Cost / task', true],
  ['latency', 'Median latency', true],
  ['p95', 'p95 latency', true],
];

export default function ResultsTable({
  rows,
  total,
  sortKey,
  sortDirection,
  onSort,
  activeId,
  onActive,
}: {
  rows: ResultRow[];
  total: number;
  sortKey: SortKey;
  sortDirection: SortDirection;
  onSort: (key: SortKey) => void;
  activeId: string | null;
  onActive: (id: string | null) => void;
}) {
  const icon = (key: SortKey) => (sortKey === key ? (sortDirection === 'asc' ? '↑' : '↓') : '↕');
  const ariaSort = (key: SortKey) =>
    sortKey === key ? (sortDirection === 'asc' ? 'ascending' : 'descending') : 'none';
  return (
    <div className="results-table-wrap">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-4">
        <h3 className="text-md font-semibold tracking-[-0.01em]">All configurations</h3>
        <span className="text-sm text-muted">
          {rows.length} of {total} configurations shown
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="results-table" aria-label="Evaluation results">
          <thead>
            <tr>
              {columns.map(([key, label, right]) => (
                <th key={key} scope="col" aria-sort={ariaSort(key)} className={right ? 'text-right' : undefined}>
                  <button type="button" onClick={() => onSort(key)}>
                    {label} <span aria-hidden="true">{icon(key)}</span>
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.id}
                className={activeId === row.id ? 'is-active' : undefined}
                onMouseEnter={() => onActive(row.id)}
                onMouseLeave={() => onActive(null)}
              >
                <th scope="row">
                  <span className="inline-flex items-center gap-2.5 font-semibold">
                    <span
                      className="size-2 shrink-0 rounded-full border border-ink/20"
                      style={{ background: colorFor(row, 'memory') }}
                      aria-hidden="true"
                    />
                    <Logo src={memoryLogos[row.memory.name]} size={18} className="rounded" />
                    {row.memory.name}
                  </span>
                </th>
                <td>
                  <span className="block">{row.model.name}</span>
                  <span className="block text-sm text-muted">{row.model.provider}</span>
                </td>
                <td>{row.harness.name}</td>
                <td className="text-right">
                  <span className="block font-semibold">{percent(row.pass_rate)}</span>
                  <span className="block font-mono text-xs text-muted">
                    {row.passes} / {row.total}
                  </span>
                </td>
                <td className="text-right font-mono text-sm">{formatCostPerTask(row)}</td>
                <td className="text-right font-mono text-sm">{formatLatency(row.median_latency_seconds)}</td>
                <td className="text-right font-mono text-sm">{formatLatency(row.p95_latency_seconds)}</td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={columns.length} className="py-10 text-sm text-muted">
                  No configurations match these filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <p className="pt-3 text-sm text-muted">A task passes when every required check passes.</p>
    </div>
  );
}
```

- [ ] **Step 5: Create `components/results/ResultsExplorer.tsx`**

```tsx
'use client';

import Link from 'next/link';
import { useMemo, useState } from 'react';
import { configurationResults, coverageLabel, dimensionValues } from '@/lib/results';
import {
  configurationPairs,
  defaultDirection,
  filterRows,
  sortRows,
  type SortDirection,
  type SortKey,
} from '@/lib/results-view';
import { harnessLogos, providerLogos } from '@/lib/logos';
import Logo from './Logo';
import ResultsTable from './ResultsTable';

export default function ResultsExplorer({ showLeaderboardLink = false }: { showLeaderboardLink?: boolean }) {
  const [query, setQuery] = useState('');
  const [memory, setMemory] = useState('all');
  const [pair, setPair] = useState('all');
  const [sortKey, setSortKey] = useState<SortKey>('accuracy');
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc');
  const [activeId, setActiveId] = useState<string | null>(null);

  const pairs = configurationPairs(configurationResults);
  const memories = dimensionValues(configurationResults, 'memory');
  const rows = useMemo(
    () => sortRows(filterRows(configurationResults, { query, memory, pair }), sortKey, sortDirection),
    [query, memory, pair, sortKey, sortDirection],
  );

  const sort = (key: SortKey) => {
    if (key === sortKey) setSortDirection((current) => (current === 'asc' ? 'desc' : 'asc'));
    else {
      setSortKey(key);
      setSortDirection(defaultDirection(key));
    }
  };

  return (
    <div className="results-explorer">
      <div className="flex flex-wrap items-baseline justify-between gap-6 border-t border-ink pb-5 pt-8">
        <div className="flex flex-wrap items-baseline gap-5">
          <h2 className="text-2xl font-semibold tracking-[-0.02em]">Results</h2>
          <span className="text-sm text-muted">{coverageLabel(configurationResults.length)}</span>
        </div>
        <div className="flex gap-6 text-sm font-medium">
          <a href="/leaderboard/results.json" download="dolphinbench-results.json" className="text-link">
            Results JSON
          </a>
          {showLeaderboardLink && (
            <Link href="/leaderboard/" className="text-link">
              Full leaderboard <span aria-hidden="true">→</span>
            </Link>
          )}
        </div>
      </div>

      <div className="mb-10 flex flex-wrap items-center justify-between gap-3">
        {pairs.length > 1 && (
          <div className="segmented" role="group" aria-label="Configuration groups">
            <button type="button" aria-pressed={pair === 'all'} onClick={() => setPair('all')}>
              All configurations
            </button>
            {pairs.map((item) => (
              <button
                key={item.key}
                type="button"
                aria-pressed={pair === item.key}
                onClick={() => setPair(item.key)}
              >
                <Logo src={harnessLogos[item.harness]} />
                <Logo src={providerLogos[item.provider]} />
                {item.key}
              </button>
            ))}
          </div>
        )}
        <div className="ml-auto flex items-center gap-2">
          <input
            type="search"
            className="field w-40"
            placeholder="Search"
            aria-label="Search configurations"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <select
            className="field"
            aria-label="Filter by memory system"
            value={memory}
            onChange={(event) => setMemory(event.target.value)}
          >
            <option value="all">All memory systems</option>
            {memories.map((name) => (
              <option key={name}>{name}</option>
            ))}
          </select>
        </div>
      </div>

      <ResultsTable
        rows={rows}
        total={configurationResults.length}
        sortKey={sortKey}
        sortDirection={sortDirection}
        onSort={sort}
        activeId={activeId}
        onActive={setActiveId}
      />
    </div>
  );
}
```

- [ ] **Step 6: Wire the pages, delete the old component, remove the shim**

- `git rm components/ResultsExplorer.tsx`
- In `lib/results.ts` delete the temporary `systemColors` export from Task 2.
- `app/page.tsx`: change the import to `import ResultsExplorer from '@/components/results/ResultsExplorer';` and the usage to `<ResultsExplorer showLeaderboardLink />`. Delete the `section-heading` div (the "Results" heading and the "Full leaderboard" link) inside `#results`, since the explorer renders them. Keep `<section id="results" ...>`.
- `app/leaderboard/page.tsx`: change the import likewise; render `<ResultsExplorer />`; delete the `<p className="benchmark-scope">…</p>` block and the `Results JSON` `<a>` (the explorer renders both). Remove the now-unused `Download` import and the `benchmarkCoverage` import.

- [ ] **Step 7: Replace the results/chart CSS in `styles/globals.css`**

Delete every rule whose selector starts with `.results-`, `.configuration-`, `.sort-button`, `.mobile-cell-label`, `.pareto-`, `.metric-switch`, `.chart-`, `.frontier-key`, `.benchmark-scope`, `.leaderboard-resources` (including their `@media` variants). Leave `.section-heading` for Task 6 (the old worked example still uses it). Add inside `@layer components`:

```css
  .results-table {
    @apply w-full min-w-[720px] border-collapse text-base;
  }
  .results-table th,
  .results-table td {
    @apply px-2 first:pl-0 last:pr-0;
  }
  .results-table thead th {
    @apply border-b border-ink pb-2.5 text-left text-xs font-normal text-muted;
  }
  .results-table thead th button {
    @apply inline-flex gap-1.5 text-muted transition-colors hover:text-ink;
  }
  .results-table thead th.text-right button {
    @apply w-full justify-end;
  }
  .results-table thead th[aria-sort='ascending'] button,
  .results-table thead th[aria-sort='descending'] button {
    @apply text-ink;
  }
  .results-table tbody th,
  .results-table tbody td {
    @apply border-b border-hairline py-5 text-left align-middle;
  }
  .results-table tbody tr.is-active th,
  .results-table tbody tr.is-active td {
    @apply bg-sand;
  }
```

- [ ] **Step 8: Type check, run home + chrome specs**

Run: `npm run typecheck && PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/home.spec.ts tests/browser/chrome.spec.ts tests/browser/results-view.spec.ts --reporter=line`
Expected: all pass. Then open http://127.0.0.1:3108/leaderboard/ in the browser pane at desktop and 375px width; confirm the table scrolls inside its box and the page has no horizontal scroll.

- [ ] **Step 9: Commit**

```bash
git add -A lib components app styles tests
git commit -m "Rebuild results explorer shell and table in the new design

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Scatter chart

**Files:**
- Create: `components/results/ScatterChart.tsx`
- Modify: `components/results/ResultsExplorer.tsx`
- Modify: `styles/globals.css`
- Modify: `tests/browser/home.spec.ts`

**Interfaces:**
- Consumes: `metricValue`, `paretoFront`, `type Metric` (Task 2); `colorFor`, `percent`, `formatCostPerTask`, `formatLatency` (Task 2); `memoryLogos`, `harnessLogos` (Task 3).
- Produces: `ScatterChart({ rows, allRows, metric, onMetric, activeId, onActive, showSubtitles })`.

- [ ] **Step 1: Add chart assertions to `tests/browser/home.spec.ts`**

In the per-viewport test, right after the `Configuration groups` `toHaveCount(0)` line added in Task 3, add:

```ts
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
```

Append a new test at the end of the file:

```ts
test('chart markers expose a tooltip on hover and focus', async ({ page }) => {
  await page.goto('/leaderboard/');
  const marker = page.locator('.chart-marker').first();
  await marker.hover();
  const tooltip = page.locator('.chart-tooltip');
  await expect(tooltip).toBeVisible();
  await expect(tooltip).toContainText('Mem0');
  await expect(tooltip).toContainText('69% acc');
  await expect(tooltip).toContainText('$0.0054 / task');
  await page.mouse.move(0, 0);
  await expect(tooltip).toHaveCount(0);
  await marker.focus();
  await expect(tooltip).toBeVisible();
  await expect(page.locator('.results-table tbody tr.is-active')).toContainText('Mem0');
});
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/home.spec.ts --reporter=line`
Expected: FAIL on `Accuracy vs. cost`.

- [ ] **Step 3: Create `components/results/ScatterChart.tsx`**

```tsx
'use client';

import { useId } from 'react';
import { colorFor, formatCostPerTask, formatLatency, percent, type ResultRow } from '@/lib/results';
import { metricValue, paretoFront, type Metric } from '@/lib/results-view';
import { harnessLogos, memoryLogos } from '@/lib/logos';

const X0 = 56;
const X1 = 792;
const Y0 = 16;
const Y1 = 440;
const WIDTH = 800;
const HEIGHT = 482;
const R = 13;

type Anchor = 'start' | 'end' | 'middle';
type Slot = { x: number; y: number; a: Anchor };
type Box = { x0: number; x1: number; y0: number; y1: number };

export default function ScatterChart({
  rows,
  allRows,
  metric,
  onMetric,
  activeId,
  onActive,
  showSubtitles,
}: {
  rows: ResultRow[];
  allRows: ResultRow[];
  metric: Metric;
  onMetric: (metric: Metric) => void;
  activeId: string | null;
  onActive: (id: string | null) => void;
  showSubtitles: boolean;
}) {
  const clipBase = useId().replace(/:/g, '');
  const isCost = metric === 'cost';
  const xValue = (row: ResultRow) => metricValue(row, metric);
  const xs = (rows.length ? rows : allRows).map(xValue);
  const accuracies = (rows.length ? rows : allRows).map((row) => row.pass_rate * 100);
  let xmin = Math.min(...xs);
  let xmax = Math.max(...xs);
  const pad = (xmax - xmin || 1) * 0.15;
  xmin = Math.max(0, xmin - pad);
  xmax += pad;
  const yMin = Math.min(30, Math.floor(Math.min(...accuracies) / 10) * 10);
  const yMax = Math.max(80, Math.ceil(Math.max(...accuracies) / 10) * 10);
  const sx = (value: number) => X0 + ((value - xmin) / (xmax - xmin)) * (X1 - X0);
  const sy = (value: number) => Y1 - ((value - yMin) / (yMax - yMin)) * (Y1 - Y0);
  const yTicks: number[] = [];
  for (let tick = yMin + 5; tick < yMax; tick += 10) yTicks.push(tick);
  const front = paretoFront(rows, metric);
  const frontierPath = front
    .map((row, index) => `${index ? 'L' : 'M'}${sx(xValue(row)).toFixed(1)} ${sy(row.pass_rate * 100).toFixed(1)}`)
    .join(' ');
  const midX = (X0 + X1) / 2;
  const midY = (Y0 + Y1) / 2;
  const formatX = (value: number) => (isCost ? `$${value.toFixed(4)}` : `${value.toFixed(1)} s`);

  // Label placement: try slots around each marker, avoiding other labels and markers.
  const points = rows.map((row) => ({ row, cx: sx(xValue(row)), cy: sy(row.pass_rate * 100) }));
  const dense = points.length > 8;
  const placed: Box[] = [];
  const labels = points.map((point) => {
    const { row, cx, cy } = point;
    const right = cx > midX;
    const title = dense ? percent(row.pass_rate) : `${row.memory.name} · ${percent(row.pass_rate)}`;
    const sub = dense || !showSubtitles ? '' : `${row.model.name} · ${row.harness.name}`;
    const width = Math.max(title.length * 6.6, sub.length * 6.2) + 4;
    const lineHeight = dense ? 15 : 28;
    const base: Slot[] = right
      ? [{ x: cx - 20, y: cy - 3, a: 'end' }, { x: cx + 20, y: cy - 3, a: 'start' }]
      : [{ x: cx + 20, y: cy - 3, a: 'start' }, { x: cx - 20, y: cy - 3, a: 'end' }];
    const slots: Slot[] = [...base];
    const step = dense ? 17 : 30;
    [-1, 1, -2, 2, -3, 3, -4, 4].forEach((k) => base.forEach((b) => slots.push({ x: b.x, y: b.y + k * step, a: b.a })));
    [-1, 1, -2, 2].forEach((k) =>
      base.forEach((b) => slots.push({ x: b.x + (b.a === 'start' ? 40 : -40), y: b.y + k * step, a: b.a })),
    );
    slots.push({ x: cx, y: cy - 22, a: 'middle' }, { x: cx, y: cy + 24, a: 'middle' });
    const box = (slot: Slot): Box => {
      const x0 = slot.a === 'end' ? slot.x - width : slot.a === 'middle' ? slot.x - width / 2 : slot.x;
      return { x0, x1: x0 + width, y0: slot.y - 11, y1: slot.y - 11 + lineHeight };
    };
    const inPlot = (b: Box) => b.x0 >= 2 && b.x1 <= WIDTH - 2 && b.y0 >= Y0 - 14 && b.y1 <= Y1 + 8;
    const hits = (b: Box) =>
      placed.some((q) => b.x0 < q.x1 && b.x1 > q.x0 && b.y0 < q.y1 && b.y1 > q.y0) ||
      points.some(
        (o) => o !== point && o.cx > b.x0 - 14 && o.cx < b.x1 + 14 && o.cy > b.y0 - 14 && o.cy < b.y1 + 14,
      );
    const slot = slots.find((s) => inPlot(box(s)) && !hits(box(s))) ?? slots.find((s) => inPlot(box(s))) ?? slots[0];
    const show = !dense || front.includes(row);
    const b = box(slot);
    if (show) placed.push(b);
    const offset = Math.abs(slot.y - (cy - 3)) > 6 || slot.a === 'middle';
    const ax = slot.a === 'end' ? b.x1 + 4 : slot.a === 'start' ? b.x0 - 4 : cx;
    const ay = slot.a === 'middle' ? (slot.y < cy ? b.y1 : b.y0) : (b.y0 + b.y1) / 2;
    return { slot, title, sub, show, offset, ax, ay };
  });

  const active = points.find((point) => point.row.id === activeId);

  return (
    <div className="chart-stage">
      <div className="mb-2 flex flex-wrap items-end justify-between gap-4 border-b border-ink pb-5">
        <div>
          <h3 className="mb-1 text-md font-semibold tracking-[-0.01em]">Accuracy vs. {metric}</h3>
          <p className="text-xs text-muted">
            Each marker is the memory system’s logo; the small badge is the harness. Top‑left (green) is best; the
            dashed line is the Pareto frontier.
          </p>
        </div>
        <div className="segmented" role="group" aria-label="Chart metric">
          <button type="button" aria-pressed={isCost} onClick={() => onMetric('cost')}>
            Cost
          </button>
          <button type="button" aria-pressed={!isCost} onClick={() => onMetric('latency')}>
            Latency
          </button>
        </div>
      </div>
      <div className="relative mb-16 min-h-[200px]">
        <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          className="block h-auto w-full font-mono"
          role="img"
          aria-label={`Accuracy versus ${metric}. Higher accuracy and lower ${metric} are better.`}
        >
          <rect x={X0} y={Y0} width={midX - X0} height={midY - Y0} fill="#2FA98C" opacity={0.08} />
          <text x={X0 + 10} y={Y0 + 10} fill="#1E7A62" fontSize={11} fontWeight={600} dominantBaseline="hanging" className="font-sans">
            Most attractive quadrant
          </text>
          {yTicks.map((tick) => (
            <g key={tick}>
              <line x1={X0} x2={X1} y1={sy(tick)} y2={sy(tick)} stroke="rgba(10,10,10,0.08)" />
              <text x={X0 - 10} y={sy(tick)} fill="#6E6A62" fontSize={11} textAnchor="end" dominantBaseline="middle">
                {tick}%
              </text>
            </g>
          ))}
          {[0, 1, 2, 3, 4].map((i) => {
            const value = xmin + ((xmax - xmin) * i) / 4;
            const x = sx(value);
            return (
              <g key={i}>
                <line x1={x} x2={x} y1={Y0} y2={Y1} stroke="rgba(10,10,10,0.08)" />
                <text x={x} y={460} fill="#6E6A62" fontSize={11} textAnchor={i === 4 ? 'end' : i === 0 ? 'start' : 'middle'}>
                  {formatX(value)}
                </text>
              </g>
            );
          })}
          <line x1={X0} x2={X1} y1={Y1} y2={Y1} stroke="rgba(10,10,10,0.3)" />
          <text x={X1} y={478} fill="#6E6A62" fontSize={11} textAnchor="end">
            {isCost ? 'Cost per task (USD) →' : 'Median latency (s) →'}
          </text>
          <text x={X0} y={6} fill="#6E6A62" fontSize={11} dominantBaseline="hanging">
            Accuracy ↑
          </text>
          {front.length > 1 && (
            <path d={frontierPath} className="chart-frontier" fill="none" stroke="#1E7A62" strokeWidth={1.5} strokeDasharray="5 4" />
          )}
          {points.map((point, index) => {
            const { row, cx, cy } = point;
            const label = labels[index];
            const ring = colorFor(row, 'memory');
            const hovered = activeId === row.id;
            const logo = memoryLogos[row.memory.name];
            const harnessLogo = harnessLogos[row.harness.name];
            const clipId = `${clipBase}-${index}`;
            return (
              <g key={row.id}>
                {label.show && label.offset && (
                  <line x1={cx} y1={cy} x2={label.ax} y2={label.ay} stroke="rgba(10,10,10,0.35)" strokeWidth={1} />
                )}
                <g
                  className="chart-marker"
                  tabIndex={0}
                  style={{ cursor: 'pointer', outline: 'none' }}
                  onMouseEnter={() => onActive(row.id)}
                  onMouseLeave={() => onActive(null)}
                  onFocus={() => onActive(row.id)}
                  onBlur={() => onActive(null)}
                >
                  <title>
                    {`${row.memory.name}, ${row.model.name}, ${row.harness.name}: ${percent(row.pass_rate)} accuracy, ${formatCostPerTask(row)} per task, ${formatLatency(row.median_latency_seconds)} median latency`}
                  </title>
                  <defs>
                    <clipPath id={clipId}>
                      <circle cx={cx} cy={cy} r={R - 2.5} />
                    </clipPath>
                  </defs>
                  {hovered && <circle cx={cx} cy={cy} r={R + 5} fill={ring} opacity={0.18} />}
                  <circle cx={cx} cy={cy} r={R} fill="#FFFFFF" stroke={ring} strokeWidth={hovered ? 2.5 : 2} />
                  {logo ? (
                    <image
                      href={logo}
                      x={cx - (R - 4)}
                      y={cy - (R - 4)}
                      width={(R - 4) * 2}
                      height={(R - 4) * 2}
                      clipPath={`url(#${clipId})`}
                      preserveAspectRatio="xMidYMid meet"
                    />
                  ) : (
                    <text x={cx} y={cy + 0.5} fill="#6E6A62" fontSize={11} fontWeight={600} textAnchor="middle" dominantBaseline="middle" className="font-sans">
                      {row.memory.name.charAt(0)}
                    </text>
                  )}
                  <circle cx={cx + R - 3} cy={cy + R - 3} r={6.5} fill="#FFFFFF" stroke="rgba(10,10,10,0.25)" strokeWidth={1} />
                  {harnessLogo ? (
                    <image href={harnessLogo} x={cx + R - 3 - 4.5} y={cy + R - 3 - 4.5} width={9} height={9} preserveAspectRatio="xMidYMid meet" />
                  ) : (
                    <text x={cx + R - 3} y={cy + R - 3 + 0.5} fill="#6E6A62" fontSize={7} fontWeight={600} textAnchor="middle" dominantBaseline="middle" className="font-sans">
                      {row.harness.name.charAt(0)}
                    </text>
                  )}
                </g>
                {label.show && (
                  <text x={label.slot.x} y={label.slot.y} fill="#0A0A0A" fontSize={12} fontWeight={600} textAnchor={label.slot.a} className="font-sans">
                    {label.title}
                  </text>
                )}
                {label.show && label.sub && (
                  <text x={label.slot.x} y={label.slot.y + 14} fill="#6E6A62" fontSize={10} textAnchor={label.slot.a}>
                    {label.sub}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
        {active && (
          <div
            className="chart-tooltip"
            style={{ left: `${(active.cx / WIDTH) * 100}%`, top: `${(active.cy / HEIGHT) * 100}%` }}
          >
            <div className="mb-0.5 text-sm font-semibold">{active.row.memory.name}</div>
            <div className="text-faint">
              {active.row.harness.name} + {active.row.model.name} ({active.row.model.provider})
            </div>
            <div className="mt-1.5 flex gap-3.5 font-mono">
              <span>{percent(active.row.pass_rate)} acc</span>
              <span>{formatCostPerTask(active.row)} / task</span>
              <span>{formatLatency(active.row.median_latency_seconds)} median</span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Add the tooltip CSS**

In `styles/globals.css` inside `@layer components`:

```css
  .chart-tooltip {
    @apply pointer-events-none absolute z-10 whitespace-nowrap rounded-lg bg-ink px-3 py-2.5 text-xs leading-normal text-paper;
    transform: translate(-50%, calc(-100% - 18px));
    box-shadow: 0 8px 24px rgba(10, 10, 10, 0.18);
  }
  .chart-marker:focus-visible circle:first-of-type {
    stroke-width: 3;
  }
```

- [ ] **Step 5: Insert the chart into `ResultsExplorer`**

Add state `const [metric, setMetric] = useState<Metric>('cost');` (import `type Metric` from `@/lib/results-view`) and render between the controls `div` and `<ResultsTable>`:

```tsx
      <ScatterChart
        rows={rows}
        allRows={configurationResults}
        metric={metric}
        onMetric={setMetric}
        activeId={activeId}
        onActive={setActiveId}
        showSubtitles={pair === 'all'}
      />
```

with `import ScatterChart from './ScatterChart';`.

- [ ] **Step 6: Type check and test**

Run: `npm run typecheck && PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/home.spec.ts --reporter=line`
Expected: all pass. Then look at the chart in the browser pane: three logo markers with harness badges, labels not overlapping, green quadrant top-left, tooltip on hover.

- [ ] **Step 7: Commit**

```bash
git add components/results/ScatterChart.tsx components/results/ResultsExplorer.tsx styles/globals.css tests/browser/home.spec.ts
git commit -m "Add accuracy scatter chart with logo markers and Pareto frontier

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Breakdown bar charts

**Files:**
- Create: `components/results/Breakdown.tsx`
- Modify: `components/results/ResultsExplorer.tsx`
- Modify: `tests/browser/home.spec.ts`

**Interfaces:**
- Consumes: `availableViews`, `breakdownItems`, `barList`, `type View`, `type BarKey` (Task 2); `memoryLogos`, `Logo` (Task 3).
- Produces: `Breakdown({ rows, view, onView, views, total })`.

- [ ] **Step 1: Add breakdown assertions**

Append to `tests/browser/home.spec.ts`:

```ts
test('breakdown switches between configurations and memory averages', async ({ page }) => {
  await page.goto('/leaderboard/');
  const breakdown = page.locator('.breakdown');
  await expect(breakdown.getByRole('heading', { name: 'Breakdown', exact: true })).toBeVisible();
  const views = page.getByRole('group', { name: 'Breakdown view' });
  await expect(views.getByRole('button')).toHaveText(['Configurations', 'Avg by memory']);
  await expect(breakdown).toContainText('Tasks passed of 200 · Higher is better');
  const accuracy = breakdown.locator('[data-bar-column="accuracy"] [data-bar]');
  await expect(accuracy).toHaveCount(3);
  await expect(accuracy.first()).toContainText('Mem0');
  await expect(accuracy.first()).toContainText('GPT 5.6 Luna · Hermes');
  await expect(accuracy.first()).toContainText('69%');
  await expect(breakdown.locator('[data-bar-column="cost"] [data-bar]').first()).toContainText('$0.0054');
  await expect(breakdown.locator('[data-bar-column="median"] [data-bar]').last()).toContainText('47.4 s');
  await views.getByRole('button', { name: 'Avg by memory' }).click();
  await expect(accuracy.first()).toContainText('avg of 1 config');
  await page.getByLabel('Filter by memory system').selectOption({ label: 'Honcho' });
  await expect(accuracy).toHaveCount(1);
});
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/home.spec.ts -g breakdown --reporter=line`
Expected: FAIL — no `Breakdown` heading.

- [ ] **Step 3: Create `components/results/Breakdown.tsx`**

```tsx
'use client';

import { barList, breakdownItems, type BarKey, type View } from '@/lib/results-view';
import type { ResultRow } from '@/lib/results';
import { memoryLogos } from '@/lib/logos';
import Logo from './Logo';

const viewLabels: Record<View, string> = {
  config: 'Configurations',
  memory: 'Avg by memory',
  model: 'Avg by model',
  harness: 'Avg by harness',
};

export default function Breakdown({
  rows,
  view,
  onView,
  views,
  total,
}: {
  rows: ResultRow[];
  view: View;
  onView: (view: View) => void;
  views: View[];
  total: number;
}) {
  const items = breakdownItems(rows, view);
  const columns: [BarKey, string, string][] = [
    ['accuracy', 'Accuracy', `Tasks passed of ${total} · Higher is better`],
    ['cost', 'Cost per task', 'USD, averaged over tasks · Lower is better'],
    ['median', 'Median latency', 'Seconds per task · Lower is better'],
  ];
  return (
    <div className="breakdown mb-16">
      <div className="mb-5 flex flex-wrap items-center justify-between gap-4">
        <h3 className="text-md font-semibold tracking-[-0.01em]">Breakdown</h3>
        {views.length > 1 && (
          <div className="segmented" role="group" aria-label="Breakdown view">
            {views.map((option) => (
              <button key={option} type="button" aria-pressed={view === option} onClick={() => onView(option)}>
                {viewLabels[option]}
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="grid gap-10 md:grid-cols-3">
        {columns.map(([key, title, subtitle]) => (
          <div key={key} data-bar-column={key}>
            <div className="mb-2 border-b border-ink pb-3">
              <div className="text-md font-semibold tracking-[-0.01em]">{title}</div>
              <div className="mt-0.5 text-xs text-muted">{subtitle}</div>
            </div>
            {barList(items, key).map(({ item, width, value }) => (
              <div
                key={`${item.label}-${item.sub}`}
                data-bar
                className="grid grid-cols-[minmax(0,1fr)_56px] items-center gap-3 border-b border-hairline py-[9px]"
              >
                <div className="min-w-0">
                  <div className="mb-[5px] flex justify-between gap-2 text-sm leading-[1.3]">
                    <span className="inline-flex min-w-0 items-center gap-1.5 truncate font-semibold">
                      <Logo src={item.memory ? memoryLogos[item.memory] : undefined} />
                      {item.label}
                    </span>
                    <span className="truncate text-xs text-muted">{item.sub}</span>
                  </div>
                  <div className="h-2.5 overflow-hidden rounded-sm bg-ink/[0.06]">
                    <div className="h-full rounded-sm" style={{ width, background: item.color }} />
                  </div>
                </div>
                <span className="text-right font-mono text-sm">{value}</span>
              </div>
            ))}
            {items.length === 0 && <p className="py-4 text-sm text-muted">No configurations match these filters.</p>}
          </div>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Insert into `ResultsExplorer`**

Add `const [view, setView] = useState<View>('config');` and `const views = availableViews(configurationResults);` (import `availableViews`, `type View` from `@/lib/results-view`; `import Breakdown from './Breakdown';`). Render between `<ScatterChart>` and `<ResultsTable>`:

```tsx
      <Breakdown
        rows={rows}
        view={view}
        onView={setView}
        views={views}
        total={Math.max(...configurationResults.map((row) => row.total))}
      />
```

- [ ] **Step 5: Type check and test**

Run: `npm run typecheck && PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/home.spec.ts --reporter=line`
Expected: all pass. Check the three columns render side by side at desktop and stack at 375px.

- [ ] **Step 6: Commit**

```bash
git add components/results/Breakdown.tsx components/results/ResultsExplorer.tsx tests/browser/home.spec.ts
git commit -m "Add results breakdown bar charts

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Home page — hero, Why DolphinBench, Evaluation

**Files:**
- Create: `lib/example.ts`, `components/WhyDolphinBench.tsx`, `components/Evaluation.tsx`
- Delete: `components/WorkedExample.tsx`
- Modify: `app/page.tsx`, `styles/globals.css` (remove `.home-*`, `.example-*`, `.outcome-value`, `.memory-highlight`, `.evaluation-summary`, `.section-heading` rules; `.inline-link`, `.section-label`, `.primary-command` stay until Task 10 because the leaderboard and run pages still use them)
- Modify: `tests/browser/home.spec.ts`

**Interfaces:**
- Consumes: `ResultsExplorer` (Task 3–5), `data`, `getHistory`, `getPersonaData`, `formatDate`, `number`, `messageHref`, `testHref` from `@/lib/data`.
- Produces: `getWorkedExample()` and `datasetFacts()` in `lib/example.ts` (server-only).

- [ ] **Step 1: Update the home assertions in `tests/browser/home.spec.ts`**

1. Replace `await expect(page.getByRole('heading', { level: 1 })).toHaveText('DolphinBench');` with:

```ts
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(
      'Evaluating long‑term agent memory through action.',
    );
    await expect(page.locator('main')).toContainText('DolphinBench — 3 personas · 600 tasks');
    await expect(page.getByRole('link', { name: 'View leaderboard', exact: true })).toHaveAttribute('href', '/leaderboard/');
```

(The H1 uses a non-breaking hyphen U+2011 in "long‑term", copied from the handoff.)

2. Replace the `#results` bounding-box check

```ts
    await page.evaluate(() => scrollTo(0, 0));
    const bounds = await page.locator('#results').boundingBox();
    expect(bounds!.y).toBeLessThan(viewport.height - 60);
```

with

```ts
    await page.evaluate(() => scrollTo(0, 0));
    const cta = await page.getByRole('link', { name: 'View leaderboard', exact: true }).boundingBox();
    expect(cta!.y + cta!.height).toBeLessThan(viewport.height);
    if (viewport.width >= 1024) {
      const bounds = await page.locator('#results').boundingBox();
      expect(bounds!.y).toBeLessThan(viewport.height - 60);
    }
```

3. Replace the whole `worked example uses the complete accepted source and original request` test with:

```ts
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
  await expect(page.locator('[data-example-outcome]')).toHaveText(spec.grade.config.assertions[0].value);
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
```

- [ ] **Step 2: Run to confirm failure**

Run: `PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/home.spec.ts --reporter=line`
Expected: FAIL on the H1 text.

- [ ] **Step 3: Create `lib/example.ts`**

```ts
import 'server-only';
import { data, getHistory, getPersonaData } from './data';

const WORDS = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten'];
export const numberWord = (value: number) => WORDS[value] ?? String(value);
const YEAR = 365.25 * 86_400_000;

function monthsBetween(start: string, end: string) {
  const a = new Date(start);
  const b = new Date(end);
  return (b.getUTCFullYear() - a.getUTCFullYear()) * 12 + (b.getUTCMonth() - a.getUTCMonth());
}

// The Home example: a rule stated once in Morgan's history and a later test whose grader checks a field value.
export function getWorkedExample(personaId = 'morgan', testId = '018') {
  const persona = data.personas.find((item) => item.id === personaId);
  if (!persona) throw new Error(`Unknown persona ${personaId}`);
  const dataset = getPersonaData(personaId);
  const test = dataset.tests.find((item) => item.id === testId);
  if (!test) throw new Error(`Missing example test ${personaId}/${testId}`);
  const fact = dataset.facts[test.fact_ids[0]];
  const sourceId = fact.source_session_ids[0];
  const history = getHistory(personaId);
  const sourceIndex = history.findIndex((message) => message.id === sourceId);
  if (sourceIndex < 0) throw new Error(`Missing source message ${sourceId}`);
  const source = history[sourceIndex];
  const assertion = test.grade.config.assertions.find(
    (item) => item.type === 'field_equals' && typeof item.value === 'string' && item.tool && item.path,
  );
  if (!assertion) throw new Error(`Example test ${testId} needs a field_equals assertion`);
  const value = assertion.value as string;
  const wrongValue = fact.statement.match(/#[\w-]+/g)?.find((candidate) => candidate !== value);
  if (!wrongValue) throw new Error(`Example fact ${fact.id} does not name an alternative channel`);
  const years = Math.floor((Date.parse(test.date) - Date.parse(source.date)) / YEAR);
  return {
    persona,
    test,
    fact,
    source,
    value,
    wrongValue,
    tool: assertion.tool!,
    field: assertion.path!.replace(/^args\./, ''),
    messageCount: history.length,
    between: history.length - sourceIndex - 1,
    markerPercent: ((sourceIndex + 1) / history.length) * 100,
    firstMessage: history[0],
    lastMessage: history[history.length - 1],
    months: monthsBetween(history[0].date, history[history.length - 1].date),
    yearsWord: numberWord(years),
  };
}

export function datasetFacts() {
  const maxMessages = Math.max(...data.personas.map((persona) => persona.messages));
  const spanYears = Math.max(
    ...data.personas.map((persona) => (Date.parse(persona.end) - Date.parse(persona.start)) / YEAR),
  );
  const rounded = Math.round(spanYears);
  return {
    personas: data.personas.length,
    tests: data.tests,
    maxMessages,
    spanLabel: `${spanYears < rounded ? 'nearly' : 'over'} ${numberWord(rounded)} years`,
  };
}
```

- [ ] **Step 4: Create `components/WhyDolphinBench.tsx`**

```tsx
import Link from 'next/link';
import { formatDate, messageHref, number, testHref } from '@/lib/data';
import { datasetFacts, getWorkedExample } from '@/lib/example';

function ToolCall({
  tool,
  field,
  value,
  wrong,
}: {
  tool: string;
  field: string;
  value: string;
  wrong?: boolean;
}) {
  return (
    <div className={`font-mono text-sm leading-[1.8] ${wrong ? 'text-muted' : ''}`}>
      <div>{tool}(</div>
      <div className="pl-[18px]">
        <span className="text-muted">{field}</span> ={' '}
        {wrong ? (
          <span className="text-fail line-through">
            &quot;<span data-example-wrong>{value}</span>&quot;
          </span>
        ) : (
          <span className="marker">
            &quot;<span data-example-outcome>{value}</span>&quot;
          </span>
        )}
        ,
      </div>
      <div className="pl-[18px]">
        <span className="text-muted">content</span> = &quot;…&quot;
      </div>
      <div>)</div>
    </div>
  );
}

const monthYear = (iso: string) => formatDate(iso, { day: undefined, month: 'short', year: 'numeric' });

export default function WhyDolphinBench() {
  const example = getWorkedExample();
  const facts = datasetFacts();
  const { source, test, value, wrongValue, persona } = example;
  const firstName = persona.name.split(' ')[0];
  const parts = source.content.split(value);
  return (
    <section id="example" className="border-y border-hairline bg-sand">
      <div className="container-x py-16 sm:py-24">
        <div className="mb-14 grid items-end gap-8 md:grid-cols-[minmax(0,1.2fr)_minmax(0,0.8fr)] md:gap-12">
          <div>
            <p className="eyebrow mb-4">Why DolphinBench</p>
            <h2 className="section-title">
              Memory isn’t recalling a fact. It’s doing the <span className="marker">right thing</span>{' '}
              {example.yearsWord} years later.
            </h2>
          </div>
          <p className="text-[17px] leading-[1.55]">
            Most memory benchmarks ask a question and check the answer. DolphinBench gives the agent real tools and a
            request, then grades what it <em>does</em>. The only way to pass is to have retained the right rule — and
            to apply it.
          </p>
        </div>

        <div className="relative pb-20 pt-9">
          <div className="mb-2.5 flex justify-between gap-4 font-mono text-[11px] text-muted">
            <span>
              {monthYear(example.firstMessage.date)} · message {example.firstMessage.id}
            </span>
            <span className="hidden sm:inline">
              {number(example.messageCount)} messages · {example.months} months
            </span>
            <span>
              {monthYear(example.lastMessage.date)} · message {example.lastMessage.id}
            </span>
          </div>
          <div
            className="relative h-7 rounded-sm"
            style={{ background: 'repeating-linear-gradient(90deg, rgba(10,10,10,0.28) 0 1px, transparent 1px 5px)' }}
          >
            <div className="absolute -bottom-2 -top-2 w-[3px] rounded-sm bg-ink" style={{ left: `${example.markerPercent}%` }} />
            <div className="absolute -bottom-2 -top-2 right-0 w-[3px] rounded-sm bg-pass" />
            <div
              className="absolute right-0 top-1/2 h-px opacity-50"
              style={{ left: `${example.markerPercent}%`, background: 'linear-gradient(90deg, #0A0A0A, #1E7A62)' }}
            />
            <div className="absolute -top-0.5 whitespace-nowrap font-mono text-[11px]" style={{ left: `calc(${example.markerPercent}% + 14px)` }}>
              #{source.id} · the rule
            </div>
            <div className="absolute -top-0.5 right-3 whitespace-nowrap font-mono text-[11px] text-pass">
              test {test.id} · the request
            </div>
          </div>
          <p className="absolute bottom-8 right-0 text-center text-sm text-muted" style={{ left: `${example.markerPercent}%` }}>
            {number(example.between)} unrelated messages in between — fundraising, hiring, a dog named Kibo, a condo tour
          </p>
        </div>

        <div className="mb-8 grid gap-8 md:grid-cols-2">
          <article className="rounded-lg border border-hairline bg-paper px-7 py-6">
            <div className="mb-3.5 flex flex-wrap items-baseline justify-between gap-2">
              <span className="text-sm font-semibold">What {firstName} said once</span>
              <span className="eyebrow">
                <time dateTime={source.date}>{formatDate(source.date)}</time> ·{' '}
                <Link href={messageHref(persona.id, source.id)} className="transition-colors hover:text-ink">
                  message {source.id}
                </Link>
              </span>
            </div>
            <blockquote className="text-lg leading-[1.5]">
              “
              <span data-example-source>
                {parts.map((part, index) => (
                  <span key={index}>
                    {index > 0 && <code className="inline-code">{value}</code>}
                    {part}
                  </span>
                ))}
              </span>
              ”
            </blockquote>
          </article>
          <article className="rounded-lg border border-hairline bg-paper px-7 py-6">
            <div className="mb-3.5 flex flex-wrap items-baseline justify-between gap-2">
              <span className="text-sm font-semibold">What {firstName} asks today</span>
              <span className="eyebrow">
                <time dateTime={test.date}>{formatDate(test.date)}</time> · test {test.id}
              </span>
            </div>
            <blockquote className="text-lg leading-[1.5]">
              “<span data-example-request>{test.request}</span>”
            </blockquote>
            <p className="mt-3 text-sm text-muted">No channel named. No reminder of the rule. Just the request.</p>
          </article>
        </div>

        <div className="grid gap-8 md:grid-cols-2">
          <div className="border-t-2 border-pass pt-[18px]">
            <div className="mb-3 flex items-baseline justify-between">
              <span className="text-sm font-semibold text-pass">Correct action</span>
              <span className="font-mono text-xs text-pass">PASS</span>
            </div>
            <ToolCall tool={example.tool} field={example.field} value={value} />
            <p className="mt-3 text-sm text-muted">
              Recognized that a {example.yearsWord}‑year‑old channel rule governs today’s request.
            </p>
          </div>
          <div className="border-t-2 border-ink/25 pt-[18px]">
            <div className="mb-3 flex items-baseline justify-between">
              <span className="text-sm font-semibold text-muted">A plausible action without the rule</span>
              <span className="font-mono text-xs text-fail">FAIL</span>
            </div>
            <ToolCall tool={example.tool} field={example.field} value={wrongValue} wrong />
            <p className="mt-3 text-sm text-muted">
              Plausible, polite, and wrong — it would broadcast pre‑green release notes to the whole org.
            </p>
          </div>
        </div>

        <div className="mt-[72px] grid gap-8 border-t border-ink/15 pt-8 md:grid-cols-3">
          <div>
            <div className="mb-2 text-lg font-semibold tracking-[-0.015em]">Graded on action, not recall</div>
            <p className="text-sm leading-[1.55] text-muted">
              Every test checks the tool called, the target, and the content. Knowing the fact isn’t enough — the agent
              has to act on it.
            </p>
          </div>
          <div>
            <div className="mb-2 text-lg font-semibold tracking-[-0.015em]">Years of history, not a session</div>
            <p className="text-sm leading-[1.55] text-muted">
              {numberWordCapitalized(facts.personas)} personas, up to {number(facts.maxMessages)} messages each,
              spanning {facts.spanLabel} of work and life. The signal is buried.
            </p>
          </div>
          <div>
            <div className="mb-2 text-lg font-semibold tracking-[-0.015em]">Real tools, simulated world</div>
            <p className="text-sm leading-[1.55] text-muted">
              Email, Slack, Discord, calendar, CRM and more run as MCP servers with state — so a wrong action has
              consequences the grader can see.
            </p>
          </div>
        </div>
        <div className="mt-10 flex flex-wrap gap-7 text-base font-medium">
          <Link href={testHref(persona.id, test.id)} className="text-link">
            View test {test.id} <span aria-hidden="true">→</span>
          </Link>
          <Link href="/dataset/" className="text-link">
            Explore the dataset
          </Link>
        </div>
      </div>
    </section>
  );
}

function numberWordCapitalized(value: number) {
  const word = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten'][value] ?? String(value);
  return word.charAt(0).toUpperCase() + word.slice(1);
}
```

Note: `formatDate` accepts `Intl.DateTimeFormatOptions`; passing `day: undefined` removes the day so `monthYear` renders "Jan 2023".

- [ ] **Step 5: Create `components/Evaluation.tsx`**

```tsx
import Link from 'next/link';
import { evaluationUrl, methodologyUrl } from '@/lib/site';

export default function Evaluation() {
  return (
    <section id="evaluation" className="container-x py-16 sm:py-24 sm:pb-28">
      <div className="grid items-start gap-8 md:grid-cols-[minmax(0,1fr)_minmax(0,2fr)] md:gap-12">
        <div>
          <p className="eyebrow mb-3">Evaluation</p>
          <h2 className="font-semibold leading-[1.1] tracking-[-0.03em]" style={{ fontSize: 'clamp(28px, 3.4vw, 40px)' }}>
            How results are produced
          </h2>
        </div>
        <div>
          <p className="mb-4 text-lg leading-[1.55]">
            Each memory system processes a user’s message history. An agent then uses that memory to complete tasks
            with the available tools. A task passes when every required check passes.
          </p>
          <p className="mb-8 text-lg leading-[1.55] text-muted">
            The histories and app environments are simulated. Each test includes the user’s request, source messages,
            starting app state, and grading checks.
          </p>
          <div className="flex flex-wrap gap-7 text-base font-medium">
            <a href={methodologyUrl} className="text-link">Methodology</a>
            <a href={evaluationUrl} className="text-link">Evaluation protocol</a>
            <Link href="/dataset/" className="text-link">
              Dataset <span aria-hidden="true">→</span>
            </Link>
          </div>
        </div>
      </div>
    </section>
  );
}
```

- [ ] **Step 6: Replace `app/page.tsx`**

```tsx
import Link from 'next/link';
import ResultsExplorer from '@/components/results/ResultsExplorer';
import WhyDolphinBench from '@/components/WhyDolphinBench';
import Evaluation from '@/components/Evaluation';
import { data } from '@/lib/data';

export default function HomePage() {
  return (
    <>
      <section className="container-x pb-16 pt-12 sm:pb-24 sm:pt-28">
        <div className="mb-7 flex items-center gap-3">
          <span className="text-[40px] leading-none" aria-hidden="true">🐬</span>
          <span className="eyebrow tracking-[0.02em]">
            DolphinBench — {data.personas.length} personas · {data.tests} tasks
          </span>
        </div>
        <h1
          className="mb-8 max-w-[15ch] font-semibold leading-[0.98] tracking-[-0.04em]"
          style={{ fontSize: 'clamp(38px, 6vw, 84px)' }}
        >
          Evaluating long‑term agent memory <span className="marker">through action.</span>
        </h1>
        <div className="grid items-end gap-8 md:grid-cols-2 md:gap-12">
          <p className="max-w-[34em] text-md leading-[1.5] sm:text-xl">
            DolphinBench compares agent configurations across memory systems, models, providers, and harnesses. Agents
            complete tasks that depend on past conversations and must recognize which earlier information matters to
            guide their decisions and actions.
          </p>
          <div className="flex flex-wrap items-center gap-7 md:justify-end">
            <Link href="/leaderboard/" className="pill-button">
              View leaderboard <span aria-hidden="true">→</span>
            </Link>
            <Link href="/dataset/" className="text-link text-base">Explore dataset</Link>
            <Link href="/run/" className="text-link text-base">Run and submit</Link>
          </div>
        </div>
      </section>
      <section id="results" className="container-x pb-24">
        <ResultsExplorer showLeaderboardLink />
      </section>
      <WhyDolphinBench />
      <Evaluation />
    </>
  );
}
```

Then `git rm components/WorkedExample.tsx` and delete the CSS rules listed in **Files** from `styles/globals.css`.

- [ ] **Step 7: Type check, test, look**

Run: `npm run typecheck && PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/home.spec.ts tests/browser/chrome.spec.ts --reporter=line`
Expected: all pass. Open `/` in the browser pane at 1280 and 375 widths: hero with the blue marker under "through action.", results, the sand band with timeline and two cards, PASS/FAIL panels, principles, evaluation. No horizontal scroll.

- [ ] **Step 8: Commit**

```bash
git add -A app/page.tsx components lib/example.ts styles/globals.css tests/browser/home.spec.ts
git commit -m "Redesign Home with hero, why section, and evaluation

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Leaderboard and Dataset pages

**Files:**
- Modify: `app/leaderboard/page.tsx`, `app/dataset/page.tsx`
- Modify: `styles/globals.css` (add `.stretched-link`, remove `.page`, `.section`, `.command`, `.text-link` legacy definitions only if no longer referenced — check with grep first; `.page` and `.section` are still used by persona pages until Task 8)
- Modify: `tests/browser/home.spec.ts`, `tests/browser/dataset.spec.ts`

- [ ] **Step 1: Update tests**

In `tests/browser/home.spec.ts`, replace the Dataset click sequence

```ts
    await page.getByRole('link', { name: 'Morgan Chen', exact: true }).click();
```

(keep as is — the stretched link keeps the exact name) and add before it:

```ts
    await expect(page.locator('main')).toContainText('600 tasks across 3 simulated users.');
    await expect(page.locator('main article')).toHaveCount(3);
    await expect(page.locator('main article').first()).toContainText('Tests200');
    await expect(page.locator('main article').first()).toContainText('Messages3,400');
    await expect(page.locator('main article').first()).toContainText('Facts752');
    await expect(page.locator('main')).toContainText('Jan 9, 2023 – Sep 11, 2026');
```

In `tests/browser/dataset.spec.ts`, after `await expect(page.locator('article')).toHaveCount(3);` add:

```ts
    await expect(page.locator('main')).toContainText('Release');
    await expect(page.getByRole('link', { name: 'Release details', exact: true })).toHaveAttribute(
      'href',
      'https://github.com/mem0ai/enact',
    );
```

- [ ] **Step 2: Run to confirm failure**

Run: `PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/home.spec.ts tests/browser/dataset.spec.ts -g "1440" --reporter=line`
Expected: FAIL on `Tests200` (old dataset layout uses buttons "200 tests").

- [ ] **Step 3: Replace `app/leaderboard/page.tsx`**

```tsx
import Link from 'next/link';
import ResultsExplorer from '@/components/results/ResultsExplorer';
import { evaluationUrl, methodologyUrl } from '@/lib/site';

export const metadata = {
  title: 'Leaderboard',
  description: 'DolphinBench agent-configuration results.',
};

export default function LeaderboardPage() {
  return (
    <div className="container-x pb-24 pt-14 sm:pt-20">
      <header className="pb-10">
        <h1 className="page-title mb-5">Leaderboard</h1>
        <p className="max-w-[36em] text-lg text-muted">
          Compare evaluated configurations across action accuracy, cost, and task latency.
        </p>
      </header>
      <section aria-label="Benchmark results">
        <ResultsExplorer />
      </section>
      <nav aria-label="Leaderboard resources" className="mt-16 flex flex-wrap gap-7 border-t border-hairline pt-8 text-base font-medium">
        <a href={methodologyUrl} className="text-link">Methodology</a>
        <a href={evaluationUrl} className="text-link">Evaluation protocol</a>
        <Link href="/dataset/" className="text-link">
          Dataset <span aria-hidden="true">→</span>
        </Link>
      </nav>
    </div>
  );
}
```

- [ ] **Step 4: Replace `app/dataset/page.tsx`**

```tsx
import Link from 'next/link';
import { data, formatDate, number, personaHref } from '@/lib/data';
import { repositoryName, repositoryUrl } from '@/lib/site';

export const metadata = {
  title: 'Dataset',
  description: 'The complete DolphinBench tasks, user histories, and source evidence.',
};

export default function DatasetPage() {
  return (
    <div className="container-x pb-28 pt-14 sm:pt-20">
      <header className="pb-14">
        <h1 className="page-title mb-5">Dataset</h1>
        <p className="text-lg text-muted">
          {data.tests} tasks across {data.personas.length} simulated users.
        </p>
      </header>
      <section aria-label="Personas" className="border-t border-ink">
        {data.personas.map((persona, index) => (
          <article
            key={persona.id}
            className="relative grid gap-6 border-b border-hairline py-9 transition-colors hover:bg-sand/60 md:grid-cols-[64px_minmax(0,1.4fr)_minmax(0,1fr)] md:gap-8"
          >
            <span className="eyebrow pt-2">0{index + 1}</span>
            <div className="flex min-w-0 flex-col">
              <Link
                href={personaHref(persona.id)}
                className="stretched-link mb-1.5 text-4xl font-semibold leading-[1.1] tracking-[-0.025em]"
              >
                {persona.name} <span className="font-normal text-faint" aria-hidden="true">→</span>
              </Link>
              <span className="mb-4 text-base text-muted">
                {persona.role} / {persona.organization}
              </span>
              <span className="max-w-[34em] text-md leading-[1.5]">{persona.summary}</span>
              <span className="eyebrow mt-4">
                {formatDate(persona.start)} – {formatDate(persona.end)}
              </span>
            </div>
            <dl className="grid grid-cols-3 gap-4 content-start pt-2">
              {[
                ['Tests', persona.tests],
                ['Messages', persona.messages],
                ['Facts', persona.facts],
              ].map(([label, value]) => (
                <div key={label} className="flex flex-col gap-1">
                  <dt className="text-xs text-muted">{label}</dt>
                  <dd className="text-[22px] font-semibold tracking-[-0.02em]">{number(value as number)}</dd>
                </div>
              ))}
            </dl>
          </article>
        ))}
      </section>
      <div className="flex flex-wrap justify-between gap-4 pt-6 text-sm text-muted">
        <span>
          Release <span className="font-mono text-ink">{data.release_sha256.slice(0, 12)}</span> — histories, tests,
          simulated apps, and grader ship in <span className="font-mono text-ink">{repositoryName}</span>.
        </span>
        <a href={repositoryUrl} className="text-link">Release details</a>
      </div>
    </div>
  );
}
```

Add to `styles/globals.css` in `@layer components`:

```css
  .stretched-link::after {
    content: '';
    position: absolute;
    inset: 0;
  }
```

- [ ] **Step 5: Type check and test**

Run: `npm run typecheck && PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/home.spec.ts tests/browser/dataset.spec.ts -g "1440|390|320" --reporter=line`
Expected: pass. (The dataset spec's persona sub-navigation and test-detail assertions still hit the old persona pages, which Task 8 restyles; they should pass unchanged.) View `/dataset/` and `/leaderboard/` at both widths.

- [ ] **Step 6: Commit**

```bash
git add app/leaderboard/page.tsx app/dataset/page.tsx styles/globals.css tests/browser
git commit -m "Redesign leaderboard and dataset pages

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Persona pages (overview, tests, test detail, timeline)

**Files:**
- Modify: `components/PersonaHeader.tsx`, `app/personas/[persona]/page.tsx`, `components/TestBrowser.tsx`, `app/personas/[persona]/tests/[testid]/page.tsx`, `components/EventReference.tsx`, `app/personas/[persona]/timeline/page.tsx`, `components/SessionCard.tsx`
- Modify: `styles/globals.css` (replace `.page`, `.section`, `.field`, `.command`, `.text-link`, `.source-text`, `.code-block` legacy rules — keep the class names, restyle them)
- Modify: `tests/browser/dataset.spec.ts`

All existing dataset-spec behaviors stay: `Tests (200)` link, `role=status` counts, `Search tests`, `Tool`, `Reset filters`, `data-test-request`, `#fact-<id>`, summary containing the source id, `Complete grading specification`, `App state`, `YAML` link, `Message <id> in history` link, `#message-<id>[open]`, `data-message-content`, `Search history`, `From`, `To`, `Search` button, `Clear filters`, `Previous`, `Next`.

- [ ] **Step 1: Add persona-overview assertions to `tests/browser/dataset.spec.ts`**

Inside the persona loop, right after `await expect(page.getByText('History activity')).toHaveCount(0);` add:

```ts
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
```

- [ ] **Step 2: Run to confirm failure**

Run: `PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/dataset.spec.ts -g "1440" --reporter=line`
Expected: FAIL on `All 200` (old link text is "All 200" with an icon — check; if it passes, the failure comes at `Full history` href or the `dl` text; at least one must fail).

- [ ] **Step 3: Restyle shared classes in `styles/globals.css`**

Replace the definitions of these existing classes (keep other rules):

```css
  .page {
    @apply container-x pb-24 pt-10 sm:pt-14;
  }
  .section {
    @apply border-t border-hairline py-8;
  }
  .command {
    @apply inline-flex h-[38px] items-center gap-2 rounded-md border border-control bg-paper px-3.5 text-sm text-ink transition-colors hover:bg-sand;
  }
  .source-text {
    @apply whitespace-pre-wrap break-words leading-[1.6];
  }
  .code-block pre,
  pre.code-block {
    @apply overflow-x-auto rounded-md border border-hairline bg-sand px-4 py-3.5 font-mono text-sm leading-[1.75];
  }
  .sub-nav {
    @apply flex gap-6 border-b border-hairline text-sm;
  }
  .sub-nav a {
    @apply border-b-[1.5px] pb-3 transition-colors;
  }
  .sub-nav a[aria-current='page'] {
    @apply border-ink font-medium text-ink;
  }
  .sub-nav a:not([aria-current='page']) {
    @apply border-transparent text-muted hover:text-ink;
  }
  details.section > summary,
  .details-plain > summary {
    @apply cursor-pointer list-none text-sm text-muted hover:text-ink;
  }
```

(`.text-link` and `.field` already have new definitions from Task 1; delete any older duplicate definitions lower in the file.)

- [ ] **Step 4: Replace `components/PersonaHeader.tsx`**

```tsx
import Link from 'next/link';
import { data, Persona, personaHref, testsHref, timelineHref } from '@/lib/data';

export default function PersonaHeader({
  persona,
  active,
}: {
  persona: Persona;
  active: 'overview' | 'tests' | 'history';
}) {
  const index = data.personas.findIndex((item) => item.id === persona.id) + 1;
  return (
    <header>
      <nav aria-label="Personas" className="mb-10 flex flex-wrap gap-x-5 gap-y-2 text-sm">
        <Link href="/dataset/" className="text-muted transition-colors hover:text-ink">Dataset</Link>
        {data.personas.map((item) => (
          <Link
            key={item.id}
            href={personaHref(item.id)}
            aria-current={item.id === persona.id ? 'true' : undefined}
            className={item.id === persona.id ? 'font-semibold text-ink' : 'text-muted transition-colors hover:text-ink'}
          >
            {item.name}
          </Link>
        ))}
      </nav>
      <div className="grid items-end gap-6 border-b border-ink pb-10 md:grid-cols-2 md:gap-12">
        <div>
          <p className="eyebrow mb-4">
            0{index} / {persona.id}
          </p>
          <h1 className="page-title mb-3">{persona.name}</h1>
          <p className="text-md text-muted">
            {persona.role} / {persona.organization} <span className="text-faint">(initial profile)</span>
          </p>
        </div>
        <p className="max-w-[32em] text-lg leading-[1.5]">{persona.summary}</p>
      </div>
      <nav aria-label="Persona views" className="sub-nav mt-6">
        {(
          [
            ['overview', 'Overview', personaHref(persona.id)],
            ['tests', `Tests (${persona.tests})`, testsHref(persona.id)],
            ['history', 'History', timelineHref(persona.id)],
          ] as const
        ).map(([id, label, href]) => (
          <Link key={id} href={href} aria-current={id === active ? 'page' : undefined}>
            {label}
          </Link>
        ))}
      </nav>
    </header>
  );
}
```

- [ ] **Step 5: Replace `app/personas/[persona]/page.tsx`**

```tsx
import Link from 'next/link';
import { notFound } from 'next/navigation';
import {
  getPersona,
  getPersonaData,
  getHistory,
  number,
  formatDate,
  testsHref,
  testHref,
  timelineHref,
  messageHref,
} from '@/lib/data';
import PersonaHeader from '@/components/PersonaHeader';

export default async function PersonaPage({ params }: { params: Promise<{ persona: string }> }) {
  const { persona: id } = await params;
  const persona = getPersona(id);
  if (!persona) notFound();
  const { tests } = getPersonaData(id);
  const history = getHistory(id);
  return (
    <div className="page">
      <PersonaHeader persona={persona} active="overview" />
      <dl className="mb-16 grid grid-cols-2 gap-6 border-b border-hairline py-7 md:grid-cols-4">
        {[
          ['History messages', persona.messages],
          ['User‑message tokens', persona.tokens],
          ['Facts', persona.facts],
          ['Tests', persona.tests],
        ].map(([label, value]) => (
          <div key={label}>
            <dt className="stat-label mb-1.5">{label}</dt>
            <dd className="stat-value">{number(value as number)}</dd>
          </div>
        ))}
      </dl>

      <section>
        <div className="flex items-baseline justify-between border-b border-ink pb-3">
          <h2 className="text-xl font-semibold tracking-[-0.015em]">Tests</h2>
          <Link href={testsHref(id)} className="text-link text-sm">
            All {persona.tests} <span aria-hidden="true">→</span>
          </Link>
        </div>
        {tests.slice(0, 4).map((test) => (
          <Link
            key={test.id}
            href={testHref(id, test.id)}
            className="grid grid-cols-[64px_1fr] gap-4 border-b border-hairline py-[18px] text-md leading-[1.5] transition-colors hover:bg-sand/60"
          >
            <span className="eyebrow pt-[3px] text-sm">{test.id}</span>
            <span className="line-clamp-2 min-w-0">{test.request}</span>
          </Link>
        ))}
      </section>

      <section>
        <div className="flex items-baseline justify-between border-b border-ink pb-3 pt-16">
          <h2 className="text-xl font-semibold tracking-[-0.015em]">Latest history</h2>
          <Link href={timelineHref(id)} className="text-link text-sm">
            Full history <span aria-hidden="true">→</span>
          </Link>
        </div>
        {history
          .slice(-3)
          .reverse()
          .map((message) => (
            <Link
              key={message.id}
              href={messageHref(id, message.id)}
              className="block border-b border-hairline py-[18px] text-md leading-[1.5] transition-colors hover:bg-sand/60"
            >
              <div className="eyebrow mb-2">
                {formatDate(message.date)} / {message.id}
              </div>
              <p className="line-clamp-2">{message.content}</p>
            </Link>
          ))}
      </section>

      <section>
        <h2 className="mb-3 mt-16 border-b border-ink pb-3 text-xl font-semibold tracking-[-0.015em]">
          Tools referenced by tests
        </h2>
        <ul className="flex flex-wrap gap-x-6 gap-y-2 pt-2 font-mono text-sm leading-[1.9]">
          {persona.tools.map((tool) => (
            <li className="break-all" key={tool}>
              <Link className="hover:underline" href={`${testsHref(id)}?tool=${encodeURIComponent(tool)}`}>
                {tool}
              </Link>
            </li>
          ))}
        </ul>
      </section>

      <details className="section details-plain mt-16 text-sm">
        <summary>Release provenance</summary>
        <dl className="mt-4 space-y-3">
          {[
            ['Initial profile', persona.profile_source],
            ['Checkpoint SHA-256', persona.checkpoint_sha256],
            ['History SHA-256', persona.history_sha256],
            ['Facts SHA-256', persona.facts_sha256],
          ].map(([label, value]) => (
            <div key={label}>
              <dt className="text-muted">{label}</dt>
              <dd className="break-all font-mono text-xs">{value}</dd>
            </div>
          ))}
        </dl>
      </details>
    </div>
  );
}
```

- [ ] **Step 6: Restyle `components/TestBrowser.tsx`**

Keep all logic and labels. Change only class names:
- Filter grid: `grid items-end gap-3 py-6 sm:grid-cols-[minmax(0,1fr)_minmax(140px,240px)_100px_auto]` → keep; labels `text-xs text-muted`; inputs/select keep `field w-full`; the reset button `command w-[38px] px-0`.
- Status row: `border-y border-hairline py-3 text-sm text-muted`.
- Column header row: `hidden ... border-b border-hairline py-3 text-xs text-muted md:grid`.
- Each result link: `group grid ... border-b border-hairline py-5 transition-colors hover:bg-sand/60 ...`; id `pt-0.5 font-mono text-xs text-muted`; request `min-w-0 whitespace-pre-wrap break-words text-md`; tools `font-mono text-xs text-muted`.
- Empty state: `text-muted`; the Clear filters button `text-link text-sm`.

- [ ] **Step 7: Restyle `app/personas/[persona]/tests/[testid]/page.tsx` and `components/EventReference.tsx`**

Test detail: keep structure, ids, and `data-test-request`. Changes:
- Root: `page max-w-[960px]` → use `className="page"` and wrap content in `<div className="max-w-[880px]">`.
- Breadcrumb: `mb-10 flex flex-wrap items-center gap-3 text-sm text-muted`, links `hover:text-ink`, id `font-mono`.
- Header: `flex flex-wrap items-start justify-between gap-5 pb-8`; `<h1 className="page-title">Test {test.id}</h1>`; meta `mt-3 text-sm text-muted`; YAML link keeps `command` class and exact text `YAML`.
- Section headings: `mb-3 text-md font-semibold` with sections `section` (hairline top border).
- Grading `details`: `border-b border-hairline py-3`; summaries keep their text; `pre` uses `code-block mt-4`.
- Prev/next nav: `command` links, `text-link text-sm` middle link.

`EventReference`: `article` → `border-b border-hairline py-6`; `h3` → `eyebrow mb-2`; `details` → `border-t border-hairline py-3`; summary → `text-xs text-muted` with `font-mono` id; link → `text-link mt-3 inline-flex items-center gap-1 text-xs` keeping text `Message {id} in history`.

- [ ] **Step 8: Restyle `app/personas/[persona]/timeline/page.tsx` and `components/SessionCard.tsx`**

Timeline: form grid unchanged; labels `text-xs text-muted`; inputs `field mt-1 w-full`; Search button `command`; count row `border-y border-hairline py-3 text-sm`; `Clear filters` link `text-link`; pager links `command`, page label `text-muted`.

SessionCard: `details` → `group border-b border-hairline py-4`; summary `cursor-pointer text-sm`; id `font-mono text-xs text-muted`; time `text-xs text-muted`; preview `mt-2 block line-clamp-2 text-muted group-open:hidden`; body links `text-link`.

- [ ] **Step 9: Type check and run the dataset spec**

Run: `npm run typecheck && PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/dataset.spec.ts tests/browser/chrome.spec.ts --reporter=line`
Expected: all pass (this includes the 600-route test; allow ~3 minutes). View `/personas/morgan/`, `/personas/morgan/tests/`, `/personas/morgan/tests/018/`, `/personas/morgan/timeline/` at both widths.

- [ ] **Step 10: Commit**

```bash
git add app/personas components/PersonaHeader.tsx components/TestBrowser.tsx components/EventReference.tsx components/SessionCard.tsx styles/globals.css tests/browser/dataset.spec.ts
git commit -m "Redesign persona overview, tests, and history pages

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Run and submit page

**Files:**
- Modify: `app/run/page.tsx` (header, `Section`, layout classes only — all content stays), `components/RunCode.tsx`, `components/RunNavigation.tsx`
- Modify: `styles/globals.css` (replace the `.run-*` block)
- Modify: `tests/browser/run.spec.ts`

- [ ] **Step 1: Add assertions to `tests/browser/run.spec.ts`**

After `await expect(page.locator('main')).toContainText('Uploads are not open yet.');` add:

```ts
    await expect(page.locator('main')).toContainText('Keep your model, agent loop, and memory implementation.');
    if (width >= 1024) {
      const steps = page.getByRole('navigation', { name: 'Run and submit sections' });
      await expect(steps.getByRole('link')).toHaveCount(9);
      await expect(steps.getByRole('link').first()).toContainText('01');
    }
    await expect(page.locator('#release h2')).toHaveText('Get the benchmark');
    await expect(page.locator('#release').getByText('01', { exact: true })).toBeVisible();
```

- [ ] **Step 2: Run to confirm failure**

Run: `PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/run.spec.ts -g 1440 --reporter=line`
Expected: PASS already — these assertions guard behavior that must survive the restyle (the current page has the same copy and structure). Continue; the visual change is verified in Step 6.

- [ ] **Step 3: Replace the `.run-*` CSS block in `styles/globals.css`**

Delete every rule starting with `.run-` and add:

```css
  .run-page {
    @apply container-x pb-28 pt-8 sm:pt-20;
  }
  .run-header {
    @apply mb-8 max-w-[720px] sm:mb-[72px];
  }
  .run-header p {
    @apply mb-3 text-md leading-[1.5] sm:text-lg;
  }
  .run-layout {
    @apply grid items-start gap-x-16 gap-y-6 lg:grid-cols-[220px_minmax(0,1fr)];
  }
  .run-contents {
    @apply lg:sticky lg:top-[84px];
  }
  .run-contents ol {
    @apply flex flex-col border-t border-ink;
  }
  .run-contents a {
    @apply grid grid-cols-[28px_1fr] gap-2 border-b border-hairline py-2.5 text-sm text-muted transition-colors hover:text-ink;
  }
  .run-contents a span {
    @apply font-mono text-[11px];
  }
  .run-section {
    @apply grid scroll-mt-[120px] gap-x-4 gap-y-2 border-t border-hairline py-8 sm:scroll-mt-20 sm:grid-cols-[64px_minmax(0,1fr)] sm:py-10;
  }
  .run-section > .run-number {
    @apply eyebrow pt-1.5 text-sm;
  }
  .run-section h2 {
    @apply mb-3.5 text-2xl font-semibold tracking-[-0.02em];
  }
  .run-guide p {
    @apply my-4 max-w-[38em] text-md leading-[1.6];
  }
  .run-guide h3 {
    @apply mb-3 mt-6 text-md font-semibold;
  }
  .run-guide h4 {
    @apply mb-2 mt-5 text-sm font-semibold;
  }
  .run-guide a {
    @apply text-link;
  }
  .run-guide code {
    @apply inline-code break-words;
    overflow-wrap: anywhere;
  }
  .run-code-block {
    @apply code-block my-4 min-w-0 max-w-full;
  }
  .run-code-block pre code {
    @apply bg-transparent p-0;
  }
  .run-code {
    @apply max-w-full overflow-x-auto px-4 py-3.5 font-mono text-sm leading-[1.75];
  }
  .run-code code {
    @apply whitespace-pre-wrap;
    overflow-wrap: anywhere;
  }
  .run-fields {
    @apply my-5 divide-y divide-hairline border-y border-hairline;
  }
  .run-fields > div {
    @apply py-3;
  }
  .run-fields dt {
    @apply text-sm font-medium;
  }
  .run-fields dd {
    @apply mt-1 text-sm leading-relaxed text-muted;
  }
  .run-files {
    @apply my-4 space-y-3 text-sm;
  }
  .run-files > div {
    @apply grid gap-1 sm:grid-cols-[65px_minmax(0,1fr)] sm:gap-3;
  }
  .run-files dt {
    @apply text-muted;
  }
  .run-files dd {
    @apply min-w-0;
  }
  .run-list {
    @apply my-4 list-disc space-y-2 pl-5 text-md leading-relaxed;
  }
  .run-steps {
    @apply my-5 list-decimal space-y-3 pl-5 text-md leading-relaxed;
  }
  .run-steps li {
    @apply pl-1;
  }
  .run-details {
    @apply my-4 border-y border-hairline py-4;
  }
  .run-details summary {
    @apply cursor-pointer text-sm font-medium text-ink;
  }
```

- [ ] **Step 4: Update `app/run/page.tsx` structure**

Replace the `Section` function and the outer layout (content inside sections is untouched):

```tsx
function Section({ id, number, title, children }: {
  id: string; number: string; title: string; children: ReactNode;
}) {
  return (
    <section id={id} className="run-section" aria-labelledby={`${id}-heading`}>
      <span className="run-number" aria-hidden="true">{number}</span>
      <div className="min-w-0">
        <h2 id={`${id}-heading`}>{title}</h2>
        {children}
      </div>
    </section>
  );
}
```

and the page shell:

```tsx
  return (
    <div className="run-page">
      <header className="run-header">
        <h1 className="page-title mb-6">Run your agent on DolphinBench</h1>
        <p>
          Keep your model, agent loop, and memory implementation. DolphinBench supplies
          the history, 600 evaluation tasks, simulated apps, and grading.
        </p>
        <p>Connect your agent&apos;s harness to the benchmark runner. Your harness is the code that calls your model, executes its tool calls, and manages memory.</p>
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-base">
          <Link href="/dataset/" className="text-link">Explore the dataset</Link>
          <span className="text-muted">Uploads are not open yet. You can run and validate locally.</span>
        </div>
      </header>

      <div className="run-layout">
        <RunNavigation sections={sections} />
        <div className="run-guide min-w-0">
          …existing <Section> children unchanged…
        </div>
      </div>
    </div>
  );
```

Remove the `ArrowUpRight` import if it is no longer used.

- [ ] **Step 5: Update `RunNavigation.tsx` and `RunCode.tsx`**

`RunNavigation`: keep the mobile `<select>` (label `Jump to section`) but style it `field mt-0 w-full lg:hidden`; render the list as:

```tsx
      <ol className="hidden lg:flex">
        {sections.map(([id, label], index) => (
          <li key={id}>
            <a href={`#${id}`}>
              <span>0{index + 1}</span>
              {label}
            </a>
          </li>
        ))}
      </ol>
```

(`.run-contents ol` supplies `flex-col`; `hidden lg:flex` toggles it.)

`RunCode`: keep the copy logic, status, and `aria-label={`Copy ${label}`}`. Replace the header markup with:

```tsx
      <div className="flex min-h-9 items-center justify-between gap-3 border-b border-hairline px-3.5 text-xs text-muted">
        <span>{label}</span>
        <div className="flex items-center gap-2">
          <span role="status">{status === 'error' ? 'Clipboard unavailable' : ''}</span>
          <button
            type="button"
            className="transition-colors hover:text-ink"
            aria-label={`Copy ${label}`}
            title={`Copy ${label}`}
            onClick={…unchanged…}
          >
            {status === 'copied' ? 'copied' : 'copy'}
          </button>
        </div>
      </div>
```

and drop the lucide icon imports.

- [ ] **Step 6: Type check, test, look**

Run: `npm run typecheck && PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108 npx playwright test tests/browser/run.spec.ts --reporter=line`
Expected: all three widths pass, including the `#setup-heading` y range (80px scroll margin + 40px section padding ≈ 120px on desktop; 120 + 32 on phones) and the first `pre` within 900px at 320px. If only the 320px `pre` check fails, tighten the phone layout (`.run-header` bottom margin, `.run-page` top padding, header paragraph size) rather than the test. View `/run/` at both widths: sticky numbered list on desktop, sand code blocks with a `copy` label.

- [ ] **Step 7: Commit**

```bash
git add app/run/page.tsx components/RunCode.tsx components/RunNavigation.tsx styles/globals.css tests/browser/run.spec.ts
git commit -m "Redesign run and submit page

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Login, admin, cleanup, and full verification

**Files:**
- Modify: `app/login/page.tsx`, `app/admin/page.tsx`
- Modify: `tailwind.config.ts` (remove legacy aliases), `styles/globals.css` (remove unused rules)
- Modify: `README.md` (fonts/logos note)

- [ ] **Step 1: Restyle `app/login/page.tsx`**

Keep the form logic. Replace classes: wrapper `container-x flex min-h-[calc(100vh-7rem)] max-w-md items-center py-16`; `<h1 className="page-title">Access DolphinBench</h1>`; label text `text-sm font-medium`; inputs `field mt-2 h-11 w-full text-base`; error `text-sm text-fail`; submit `pill-button h-11 w-full justify-center disabled:cursor-wait disabled:opacity-60`.

- [ ] **Step 2: Restyle `app/admin/page.tsx`**

Keep queries and structure. Replace: wrapper `container-x pb-24 pt-16`; `h1` → `page-title`; subtitle `mt-3 text-sm text-muted`; Sign out button `command`; table wrapper `mt-12 overflow-x-auto border-y border-hairline`; `thead` `text-xs text-muted`; `tbody` `divide-y divide-hairline`; every `text-fg-secondary`/`text-fg-muted` → `text-muted`, `text-fg` → `text-ink`, `border-border` → `border-hairline`, `divide-border` → `divide-hairline`; `h2` → `text-xl font-semibold`.

- [ ] **Step 3: Remove legacy tokens and rules**

```bash
grep -rn -e 'text-fg' -e 'bg-bg' -e 'bg-surface' -e 'border-border' -e 'text-accent' -e 'border-accent' -e 'divide-border' -e 'text-error' -e 'text-success' -e 'inline-link' -e 'primary-command' -e 'section-label' -e 'home-width' app components lib styles
```

Fix every hit to use the new tokens/classes, then delete the aliases (`bg surface surface-hover border border-strong fg fg-secondary fg-muted accent accent-hover success warning error`) from `tailwind.config.ts`. Delete any `styles/globals.css` rule whose class no longer appears in `app/`, `components/`, or `lib/` (check each with `grep -rn "<class>" app components lib`). Re-run the grep above: expected no output.

- [ ] **Step 4: Update `README.md`**

Under "Local preview" add one sentence after the `npm run preview` block: "Fonts (Fustat) and memory-system logos are committed under `assets/` and bundled by Next.js; no runtime requests go to third-party logo services."

- [ ] **Step 5: Full verification**

```bash
npm run typecheck
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
npm run build
npm run test:e2e
```

Expected: typecheck clean; Python tests pass; build succeeds (run `npm run preview` again afterwards if the dev server was stopped); all Playwright specs pass. Paste the tail of each command's output into the task report.

- [ ] **Step 6: Visual pass**

In the browser pane at 1280px and 375px, open `/`, `/leaderboard/`, `/dataset/`, `/personas/morgan/`, `/personas/morgan/tests/018/`, `/personas/morgan/timeline/`, `/run/`, `/login/`. Compare with the handoff screens; confirm no horizontal scroll (`document.documentElement.scrollWidth <= innerWidth`) and no console errors on each. Take a full-page screenshot of `/` and `/leaderboard/` at 1280px into the scratchpad directory and send them to the user with `SendUserFile`.

- [ ] **Step 7: Commit and confirm the tree**

```bash
git add app/login/page.tsx app/admin/page.tsx tailwind.config.ts styles/globals.css README.md
git commit -m "Restyle auth pages and remove legacy design tokens

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git status --short
```

Expected: only `website/next-env.d.ts` (modified by `next dev`) and `.claude/` remain uncommitted. Do not commit them.
