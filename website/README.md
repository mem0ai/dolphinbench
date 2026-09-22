# DolphinBench website

The website lives in this repository and builds separately from the
benchmark runtime. Its dataset browser reads the accepted release in the parent directory.
Generated browser data is ignored by Git; it is not a second accepted dataset.

## Local preview

From `website/`, install Node.js dependencies and the Python data-export dependency:

```bash
npm ci
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PATH="$PWD/.venv/bin:$PATH" npm run prebuild
npm run preview
```

Open <http://127.0.0.1:3108>. The preview bypasses account authentication only in
development, on a loopback hostname, for Home, Leaderboard, Dataset, Run and submit,
and their data files.
Admin routes remain protected. Submission APIs authorize uploads through bot
verification and private receipts, not partner accounts. Production ignores the
preview environment flag.

To display the live site's accepted submissions in the local leaderboard:

```bash
DOLPHINBENCH_PUBLIC_SUBMISSIONS_ORIGIN=https://dolphinbench-review.vercel.app npm run preview
```

This development-only option reads the site's public submissions API without
database credentials or cookies. It disables local uploads and submission changes;
private receipts and administrative records are not copied from the live site.
The local website code remains independent of the deployed website.

## Official results

The home page, leaderboard, and JSON download use
`content/official-results.json`. Each configuration identifies its harness,
model (including provider), and memory system. It must include approved
summaries for Alex, Morgan, and Riley, with exactly 200 tests each. The overall
score is the sum of their passes divided by 600. Partial results do not appear
as separate entries. An empty configuration list displays no official results.

The official report covers Hermes Luna with built-in memory, Mem0, Honcho,
Hindsight, and Supermemory; Hermes MiniMax M3 with built-in memory, Mem0,
Honcho, Hindsight, and Supermemory; and Claude Code with built-in memory, Mem0, and Honcho. Its `release_sha256`
identifies the accompanying dataset manifest.
Supermemory memory-processing costs are estimated from Riley's recorded token
usage, scaled by each persona's stored document tokens.
The comparison includes self-submissions only when their validated release hash
matches; older receipts remain available without mixing different test versions.
`content/official-evidence.json` lists the approved, hash-verified files in the
existing private Vercel Blob store. The evidence download route streams only
those files and retains the website's access controls.

To add approved results, prepare a JSON file using the fields in
`lib/results.ts`. Each persona's `source` identifies its approved summary; the
configuration's `evidence` identifies its configuration file, exact source bundle,
recordings, and grades. Each reference includes an HTTPS `url` and the file's
`sha256`. Confirm that those files are accessible, match their hashes, and belong
to the reported run before approving the update. The exporter validates reference
format, not remote file contents or the authenticity of a run.

Use full-600-test measurements for cost, median latency, and p95 latency; use
`null` when unavailable. Never average the three persona medians or substitute
zero for missing measurements. A reported cost must state its scope in
`agent_inference_cost_scope`.

`build_results.summarize_tests` checks the three saved `combined_results.json`
records and aggregates their 600 per-test measurements without regrading.
It uses the ordinary median and a linearly interpolated 95th percentile at
position `(600 - 1) * 0.95` in the sorted latencies. Verify the saved file
hashes before calling it. Confirm that the published dataset, grading checks,
and simulated apps match those recorded in the selected run configurations.

Export the approved summaries and compute their overall scores:

```bash
python3 build_results.py --source /path/to/approved-results.json
python3 build_results.py --check
```

The build runs the same check and rejects incomplete entries, duplicate
configurations, invalid measurements, and totals that disagree with the persona
summaries. Review and merge the resulting JSON with its evidence links. This
does not run tests, regrade answers, or publish a deployment.

For local review before evidence hosting, set `DOLPHINBENCH_RESULTS_PREVIEW_DIR`
to a prepared directory containing `preview-results.json` and
`preview-files.json`, and start the development server with
`DOLPHINBENCH_LOCAL_PREVIEW=1`. The results file uses the same schema with
`preview: true`; evidence URLs use `/leaderboard/evidence/<filename>/`.
The file index maps each allowed filename to its local `path` and `sha256`.
Review the indexed files for credentials before enabling downloads. This
preview works only in development on a loopback hostname. Production ignores
the directory, and the publisher rejects preview reports and local evidence
URLs. The checked-in official results stay unchanged until publication review.

`tests/fixtures/complete-results.json` contains synthetic browser-test data only;
it is not imported by the website or included in the public results download.

## Data and verification

`build_data.py` verifies every file hash in the root `manifest.json` before
exporting all three histories, facts, and 600 tests. It preserves full message
text, numeric fact references, source-message IDs, grading specifications, app
state, and self-contained test YAML downloads. Related history remains distinct from source
evidence. Source files are never modified.

The published fact registries and their original message-level source records
are retained under `authoring/release_manifests/<persona>/`. Each public fact
links to the specific source messages, not every message in the original session.
`mock_mcp/runtime/<persona>.json` preserves each test's tool configuration and
app directory. Morgan and Alex tests reference one shared `state.json` per
persona and store only differing top-level app collections. The loader checks
the shared file's hash and replaces those collections without merging records.
App-state JSON loads on demand on test pages. YAML downloads contain the full
reconstructed state; their formatting can differ from the evaluated files.
The evidence archives retain the original evaluated bytes. Release manifests
keep the original candidate hashes, compact-file hashes, and fingerprints of
the complete evaluated inputs separately.

`scripts/build_run_docs.py` copies only the allowlisted runner instructions and
examples into `content/run-repo-files.json`. This lets hosted
deployments serve those files without access to the parent checkout. Regenerate
the bundle whenever an allowlisted source changes. `npm run build` regenerates
both this bundle and the dataset exports before compiling the website.

The generated files live in `public/data/`. Regenerate them whenever accepted
release inputs change. To verify the projection and application:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
npm run typecheck
PATH="$PWD/.venv/bin:$PATH" npm run build
npx playwright install chromium
npm run test:e2e
```

Browser tests start their own loopback-only preview on port 3110.
To test an already running local preview, set
`PLAYWRIGHT_BASE_URL=http://127.0.0.1:3108` when running `npm run test:e2e`.

## Deployment

Use `website/` as the Next.js project root, with repository files outside that
directory available during the build. `vercel.json` installs Python dependencies
and builds Next.js. The Python install hook in `pyproject.toml` separately bundles
the hash-verified release, graders, and pricing for `api/submission_validator.py`.
Vercel deploys that Python function alongside the website in the same project.
Do not commit generated data,
environment files, or local Vercel configuration.

Deployment requires explicit approval. The application requires a Next.js
runtime; it is not a static export. Administrator authentication and revocable
sessions use Postgres. Configure `DATABASE_URL` in the deployment environment.
Preserve the existing database and accounts. Submissions require the additional table and
indexes in `submissions.sql`; `npm run db:setup` creates them without replacing
existing accounts or sessions.

The existing `db:setup` and `accounts` commands are administrative operations,
not build or preview steps. Do not run them against a connected service without
approval.

## Enable ZIP submissions

After approval to update the deployment and database:

1. Set `DATABASE_URL` to the existing account database and run `npm run db:setup`
   from `website/` against that database.
2. Connect a **private** Vercel Blob store to the project. Vercel supplies
   `BLOB_READ_WRITE_TOKEN`; never expose it as a `NEXT_PUBLIC_` variable.
3. Generate a random server secret with `openssl rand -hex 32` and set it as
   `CRON_SECRET`. The website and Vercel cron use it to authenticate requests to
   `/api/submission_validator/`. It also signs browser identifiers and private
   receipt tokens with separate purpose labels. Keep this secret stable;
   rotating it invalidates existing receipt links and browser cookies.
4. Create a managed Cloudflare Turnstile widget for the website's hostname.
   Set `NEXT_PUBLIC_TURNSTILE_SITE_KEY` and `TURNSTILE_SECRET_KEY` in Vercel.
   The server verifies each token's success, hostname, and `submit-run` action
   before reserving an upload. Production deployments reject Turnstile test keys.
5. Set `DOLPHINBENCH_SUBMISSIONS_ENABLED=1` and deploy. If deployment protection
   blocks service requests, configure Vercel's automation bypass secret as
   `VERCEL_AUTOMATION_BYPASS_SECRET`.
6. Upload a packaged run on `/run/#upload` without a submission account. Confirm that validation
   finishes, the result appears under self-submitted runs on `/leaderboard/`, and
   an administrator can remove it with a reason on `/admin/`.

Benchmark pages and downloads are public without sign-in. Admin pages and
moderation still require an administrator session. Public pages do not send
partner activity events. Vercel's separate deployment-protection settings,
if enabled, can still restrict access before a request reaches the application.

Keep submissions disabled in preview deployments unless you provide a separate
test database and private Blob store. Do not point preview validation at the
production submission queue.

The browser uploads directly to private Blob storage. The Python function reads
only `ingestion.json` and `tests.json` from the ZIP without extracting files to
disk. It uses the repository validator and recorded judge responses, not model
calls. An accepted submission publishes its name, harness, model, memory system,
scores, and evidence hashes as **Self-submitted · Unverified**. Raw ZIPs remain
private. Validation checks the submitted evidence; it does not establish that the
submitter ran the claimed model or independently authenticate their recordings.

Submitters receive a private receipt link for viewing status and withdrawing
their run from any browser. The token stays in the URL fragment and is sent to
the API through an Authorization header, never a query parameter. Run and
receipt pages do not record activity events. Anyone with the link can manage the
submission; it is not proof of the submitter's identity. The original browser
also lists its submissions using an HttpOnly signed cookie. Optional contact
emails are visible only to administrators.

Hosted uploads are limited to 256 MiB compressed, 512 MiB per JSON file, and
1 GiB uncompressed total. Each browser can reserve five uploads per day and have
two pending submissions. Each IP address can reserve 20 uploads per day. The
service uses Vercel's client-address header and stores a keyed hash, not the raw
address. Clearing cookies does not reset the IP limit; a shared network shares
that limit. These controls reduce abuse but do not establish unique human identity.
The service allows 50 uploads per day and reserves at
most 50 GiB of retained storage. Upload reservations expire after one hour.
Accepted ZIPs remain private for review; rejected and removed ZIPs are deleted
after seven days. Incomplete uploads become eligible for deletion after expiry.

The worker processes one submission at a time. Validation runs in a child process
without service credentials, with a 2 GiB address-space limit, 180 CPU seconds,
and a 240-second wall limit. Invalid evidence is rejected with a reason. Technical
failures stay pending, with up to three automatic attempts; administrators can
retry them. A once-per-minute Vercel cron recovers queued or interrupted work.

For local worker and database tests, use a disposable Postgres database named
`submissions` on `127.0.0.1:55439`. Set `DOLPHINBENCH_TEST_DATABASE_URL` to its
connection string, apply `npm run db:setup` with `DATABASE_URL` pointing to the
same database, and run `npm run test:submissions` plus the Python tests above.
These tests delete submission rows and must never use the production database.
The anonymous API browser test additionally requires a local development server
with submissions enabled and Cloudflare's official passing test keys:
`NEXT_PUBLIC_TURNSTILE_SITE_KEY=1x00000000000000000000AA` and
`TURNSTILE_SECRET_KEY=1x0000000000000000000000000000000AA`.
It calls Cloudflare's test verification endpoint; it does not upload to Blob or
call models. Set `PLAYWRIGHT_BASE_URL` to that server when running the browser tests.

## Analytics

Every page carries the Google tag (`gtag.js`) for Google Analytics 4 property
`G-DS880BMQ34`, mounted from the root layout through `components/Analytics.tsx`.
The measurement ID is a public identifier and lives in `lib/site.ts`; it is not
an environment variable, so no deployment step can silently drop the tag.

`/run/receipt/` is the one excluded route. It is authorized by its fragment,
`gtag` reports the full URL, and the page already suppresses its referrer to keep
that fragment off the wire. Receipt links are plain anchors, so every visit is a
full navigation and the tag is never left running from a previous route.

The tag is not gated by environment, so `next dev` and browser-test runs would
otherwise report from `127.0.0.1`. Exclude them in the property itself, under
Admin, Data streams, Configure tag settings, Define internal traffic. The browser
tests block requests to `googletagmanager.com`, so `npm run test:e2e` never
reports.

## Routes

| Route | Content |
| --- | --- |
| `/` | Benchmark introduction, complete official results, accepted task example, and evaluation summary |
| `/leaderboard/` | Complete official results, persona breakdowns, evidence links, and separate self-submitted results |
| `/leaderboard/results.json` | Downloadable official result report |
| `/dataset/` | Personas and dataset entry points |
| `/run/guide/` | Formatted harness integration guide with full-source copying |
| `/run/template/` | Highlighted Python template with full-source copying |
| `/run/` | Setup, execution, required ZIP format, and account-free submission |
| `/run/receipt/` | Private submission status and withdrawal, authorized by the receipt fragment |
| `/admin/` | Account administration and submission removal or retry |
| `/about/` | Redirect to the evaluation section on Home |
| `/personas/<id>/` | Initial persona profile and accepted-release statistics |
| `/personas/<id>/timeline/` | Searchable, dated history, 40 messages per page |
| `/personas/<id>/timeline/?session=<id>#message-<id>` | Exact source message |
| `/personas/<id>/tests/` | 200 tests, searchable by request, tool, and fact ID |
| `/personas/<id>/tests/<test_id>/` | Request, facts, evidence, grading, state, and YAML |
