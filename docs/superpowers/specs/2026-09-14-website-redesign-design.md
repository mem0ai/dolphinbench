# DolphinBench website redesign

Date: 2026-09-14
Status: approved in chat

## Goal

Rebuild the look of the DolphinBench website (`website/`) to match the Claude Design
handoff `DolphinBench v2.dc.html`, while keeping the site's real data, routes,
authentication, and behavior. The redesigned site stays in the same Next.js app and
the same Vercel project.

## Decisions

- **Restyle in place.** Keep the App Router routes, `lib/data.ts`, `lib/results.ts`,
  `proxy.ts` and session auth, `ActivityTracker`, admin, login, the data build
  scripts, and the results report. Replace the visual layer only.
- **Real data only.** The handoff's leaderboard has 15 configurations; only Mem0,
  Honcho, and Built-in on Hermes + GPT 5.6 Luna are real. The handoff's Alex and
  Riley stats, tests, and history are placeholders. Every number, test, message, and
  label on the site comes from `content/morgan-results.json` or `public/data/`.
  Controls that only make sense with more data are built from the data and hide
  themselves until the data supports them.
- **Run page keeps all content.** Use the handoff layout; keep every current detail
  in styled `<details>` sections.
- **Self-hosted logos.** Mem0 from the handoff; Honcho, Nous Research (Hermes), and
  OpenAI favicons downloaded once into `website/assets/logos/` and imported as static
  assets, so they are served from `/_next/static/` and are not caught by the
  authentication proxy. No runtime requests to Google's favicon service. Built-in
  uses a "B" monogram.
- **No deploy without explicit approval.** Work lands on a branch and a PR.

## Design system

Taken from the handoff source.

| Token | Value | Use |
| --- | --- | --- |
| `ink` | `#0A0A0A` | Text, strong rules, primary button, active segments |
| `paper` | `#FFFFFF` | Page background, cards |
| `sand` | `#F5F3EE` | Footer, "Why DolphinBench" band, code blocks, inline code |
| `muted` | `#6E6A62` | Secondary text, axis labels, IDs |
| `faint` | `#A8A39A` | Placeholders, arrows, tertiary text |
| `highlight` | `#A9DDF5` | Marker highlight behind key words, selection |
| `pass` | `#1E7A62` | Pass states, quadrant label, Pareto line |
| `quadrant` | `#2FA98C` at 8% | "Most attractive quadrant" fill |
| `fail` | `#B3261E` | Fail states |
| `hover-ink` | `#2A2A2A` | Primary button hover |
| Hairline | `rgba(10,10,10,0.1)` | Row dividers |
| Control border | `rgba(10,10,10,0.15)` | Inputs, segmented controls |
| Link underline | `rgba(10,10,10,0.3)` | Text links (solid ink on hover) |

- Fonts: Fustat 400/500/600/700 via `next/font/local` from the handoff TTFs;
  JetBrains Mono 400/500 via `next/font/google`.
- Container: `max-width: 1120px`, horizontal padding 32px (20px on phones).
- Headings: weight 600, negative tracking (`-0.04em` hero, `-0.035em` page titles,
  `-0.02em` section titles).
- Page titles: `clamp(40px, 5vw, 64px)`, line-height 1. Hero: `clamp(38px, 6vw, 84px)`
  (the handoff's 44px floor does not fit a 320px phone with the results block in
  reach), line-height 0.98, `max-width: 15ch`.
- Marker highlight: `linear-gradient(transparent 62%, #A9DDF5 62%, #A9DDF5 92%, transparent 92%)`.
- Segmented controls: 1px control border, 6px radius, 13px text, active segment ink
  with white text.
- Series colors: memory Mem0 `#3AA7E0`, Honcho `#FF8A5B`, Hindsight `#7B61FF`,
  Supermemory `#2FA98C`, Built-in `#A8A39A`; harness Hermes `#3AA7E0`, Claude Code
  `#D97B4A`. Unknown values fall back to a fixed extra palette.
- Light theme only, as in the handoff.
- Responsive: the handoff is desktop-only. Below 768px, multi-column grids stack,
  the nav wraps under the wordmark, the results table scrolls inside its own
  `overflow-x: auto` container, and the chart keeps its aspect ratio.

## Shared chrome

- **Header:** sticky, 60px, white at 85% with 12px backdrop blur, hairline bottom
  border. 🐬 + "DolphinBench" wordmark (15px/600). Nav: Home, Leaderboard, Dataset,
  Run and submit — 14px, muted, active item ink with a 1.5px ink underline.
  Persona routes mark Dataset active. Right: GitHub mark + `mem0ai/enact`.
- **Footer:** sand band, hairline top border. 🐬 DolphinBench · "Evaluating
  long-term agent memory through action"; links Leaderboard, Dataset, GitHub.
- Favicon: 🐬 SVG data URI.

## Home (`/`)

1. **Hero** (112px top, 96px bottom): 🐬 40px + mono label "DolphinBench — 3 personas ·
   600 tasks" (computed; the handoff's "release 2026.09" has no source, and release
   hashes stay off Home). H1 "Evaluating long-term agent memory
   *through action.*" with the marker highlight on "through action." Two columns:
   intro paragraph (19px) and actions — pill button "View leaderboard →", text links
   "Explore dataset", "Run and submit".
2. **Results block** (see below) with a "Full leaderboard →" link.
3. **Why DolphinBench** (sand band, 96px padding). Everything is derived from
   Morgan test 018 and its fact's source message:
   - Eyebrow "Why DolphinBench"; H2 "Memory isn't recalling a fact. It's doing the
     *right thing* years later." The years number is computed from the source and
     request dates. Side paragraph as in the handoff.
   - Timeline strip: first/last history dates, message count, months spanned, rule
     marker at `source index / message count`, request marker at the end, and a
     computed "N unrelated messages in between" caption.
   - Two cards: "What Morgan said once" (source message text, date, ID, with the
     required channel shown as inline code) and "What Morgan asks today" (test
     request, date, test ID, "No channel named. No reminder of the rule. Just the
     request.").
   - Outcomes: "Correct action · PASS" shows `send_discord_message(channel =
     "<required value>", …)` with the highlight on the value. "A plausible action
     without the rule · FAIL" shows the same call with a struck-through `#eng-all`,
     and a caption that it would break the channel rule. `#eng-all` comes from the
     fact statement; the panel is explicitly illustrative.
   - Principles (3 columns): graded on action; years of history (message maximum and
     span computed from persona data); real tools in a simulated world.
   - Links: "View test 018 →" (test page), "Explore the dataset".
4. **Evaluation**: 1fr/2fr grid, eyebrow + "How results are produced", two
   paragraphs, links Methodology and Evaluation protocol (GitHub docs), Dataset →.

## Results block (`ResultsExplorer`)

Used on Home and Leaderboard. Data from `lib/results.ts`.

- **Header row** (1px ink top border): "Results" + muted coverage line
  "Partial benchmark · 1 of 3 personas · 200 of 600 tests · 3 configurations" (all
  computed; "Partial benchmark" only when coverage is incomplete). Links: "Results
  JSON" (download) and, on Home, "Full leaderboard →".
- **Controls:** left, segmented switcher "All configurations" plus one segment per
  harness + model pair with harness and model logos — rendered only when there are
  two or more pairs. Right: search input (160px) and a memory-system select built
  from the data.
- **Scatter:** title "Accuracy vs. cost|latency", caption, Cost/Latency segmented
  control. SVG 800×482 viewBox, y from 30% to 80% (widened when data falls outside),
  x padded 15%, grid lines, top-left quadrant shading with "Most attractive
  quadrant", dashed Pareto frontier, markers = white circle with memory logo (or
  "B"), colored ring, harness badge bottom-right. Labels use the handoff's
  collision-avoiding slot placement ("Memory · 69%" plus "Model · Harness" when there
  are 8 or fewer points). Hover shows the dark tooltip; markers are keyboard
  focusable with the same tooltip and an `aria-label`.
- **Breakdown:** "Breakdown" + segmented Configurations / Avg by memory / Avg by
  model / Avg by harness. Group views with fewer than two groups are hidden. Three
  columns: Accuracy (tasks passed of total, higher better), Cost per task, Median
  latency — each a list of labeled 10px bars.
- **Table:** "All configurations" + "N of M configurations shown". Columns Memory,
  Model / provider, Harness, Accuracy (percent + passed / total), Cost / task, Median
  latency, p95 latency. Headers are sort buttons (↕ ↑ ↓), default accuracy descending.
  Empty state "No configurations match these filters." Footnote "A task passes when
  every required check passes."

## Leaderboard (`/leaderboard/`)

Header (80px top): H1 "Leaderboard", muted subtitle. Then the results block (without
"Full leaderboard"), then a links row: Methodology, Evaluation protocol, Dataset →.

## Dataset (`/dataset/`)

H1 "Dataset", "600 tasks across 3 simulated users." Ink-top-bordered list; each
persona is a link row: grid 64px / 1.4fr / 1fr — mono number, name 32px + faint →,
role / organization, summary, date span; right, Tests / Messages / Facts stats.
Footer line: release SHA-256 (mono) and a "Release details" link to the repository.

## Persona overview (`/personas/<id>/`)

- Breadcrumb row: Dataset, then all persona names (current ink/600).
- Header grid: mono "0N / id", H1 name, "role / organization (initial profile)";
  summary paragraph right. Ink bottom border.
- Stats (4 columns): History messages, User-message tokens, Facts, Tests.
- Tests: first 4, each linking to its test page; "All 200 →".
- Latest history: last 3 messages, each linking to its timeline anchor; "Full
  history →".
- Tools referenced by tests: mono list, each linking to the filtered tests list.
- Release provenance in a styled `<details>`.
- Persona sub-navigation (Overview / Tests / History) is shared with the tests and
  timeline pages, styled like the main nav underline.

## Pages outside the handoff

Tests list, test detail, timeline, login, and admin keep their structure and
behavior and adopt the design system: page title scale, ink/hairline rules, mono
IDs, sand code blocks, segmented controls for filters, text-link style.

## Run and submit (`/run/`)

Header (max-width 720px): H1 "Run your agent on DolphinBench", lead, muted "Uploads
are not open yet. You can run and validate locally." Grid 220px / 1fr: sticky step
list (ink top border, mono numbers, hairline rows); steps as 64px number column +
content, H2 24px, body 16px/1.6. Code blocks: sand, hairline border, 6px radius,
label bar with a working copy button, 13px mono. Every current paragraph, list,
field table, and `<details>` section is kept.

## Testing

- `npm run typecheck`, `npm run build`, and the Python unit tests pass.
- Playwright specs are updated for the new markup and keep all current behavioral
  coverage: filters/sort/grouping, worked example sourcing, deep links, all 600 test
  routes, history search and paging, timezone independence, and preview route gating.
- Visual check of each page against the handoff at desktop (1280px) and phone
  (375px) widths, with no horizontal page scroll and no console errors.
