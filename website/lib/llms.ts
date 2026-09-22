import 'server-only';
import results from '@/content/official-results.json';
import { data, formatDate, number, personaHref, testsHref, timelineHref } from './data';
import { runRepoFileContents } from './run-repo-files';
import { totalCost, formatTotalCost, formatLatency, percent, personaNames, type Persona, type ResultRow } from './results';
import {
  contactEmail,
  discordUrl,
  evaluationUrl,
  methodologyUrl,
  paperUrl,
  repositoryUrl,
  siteDescription,
  siteName,
  siteUrl,
  tagline,
} from './site';

const YEAR = 365.25 * 86_400_000;
const url = (path: string) => `${siteUrl}${path}`;
const configurations = results.configurations as ResultRow[];

function keyFacts() {
  const start = data.personas.map((persona) => persona.start).sort()[0];
  const end = data.personas.map((persona) => persona.end).sort().at(-1)!;
  const years = ((Date.parse(end) - Date.parse(start)) / YEAR).toFixed(1);
  return [
    `- ${data.personas.length} simulated users (personas), ${number(data.tests)} tasks, ${number(data.messages)} history messages spanning ${formatDate(start)} to ${formatDate(end)} (about ${years} years).`,
    '- Every task is a request whose correct action depends on something said earlier. The agent gets the request and simulated apps (email, Slack, Discord, calendar, CRM, and more as MCP servers with state), never a reminder of the rule.',
    '- Grading checks the action: the tool called, its target, and its content. A task passes only when every required check passes.',
    '- Reported metrics per configuration (memory system + model + harness): accuracy (tasks passed of 600), total cost in USD, median and p95 latency per task.',
    `- Current official leaderboard: ${configurations.length} configuration${configurations.length === 1 ? '' : 's'}. Anyone can run the benchmark and submit a packaged run; self-submitted runs are listed separately as unverified.`,
  ].join('\n');
}

function personaLines() {
  return data.personas
    .map((persona) =>
      `- [${persona.name}](${url(personaHref(persona.id))}): ${persona.role} at ${persona.organization}. ${persona.summary} ` +
      `${number(persona.messages)} messages, ${number(persona.facts)} facts, ${persona.tests} tests. ` +
      `[Tests](${url(testsHref(persona.id))}) · [Timeline](${url(timelineHref(persona.id))})`)
    .join('\n');
}

/** The llms.txt index: what the benchmark is and where each resource lives. */
export function llmsIndex(): string {
  return `# ${siteName}

> ${siteDescription}

${siteName}: ${tagline}. Website: ${siteUrl}

${keyFacts()}

## Leaderboard

- [Leaderboard](${url('/leaderboard/')}): official configurations ranked by accuracy, with total cost, median and p95 latency, and an accuracy vs. cost chart with the Pareto frontier.
- [Official results JSON](${url('/leaderboard/results.json')}): machine-readable results with per-persona pass counts and links to the evidence bundles for every configuration.

## Dataset

- [Dataset](${url('/dataset/')}): the personas, their histories, and every test with its source messages, starting app state, and grading checks.
${personaLines()}

## Run and submit

- [Run and submit](${url('/run/')}): set up the runner, connect your harness, run all 600 tasks, package and upload a submission.
- [Harness integration guide](${url('/run/guide/')}): how to connect an agent and memory system to the runner.
- [Python template](${url('/run/template/')}): starter harness implementation.

## Methodology

- [Methodology](${methodologyUrl}): how histories, facts, and tests were constructed.
- [Evaluation protocol](${evaluationUrl}): how the official configurations were run and graded.
- [Paper](${paperUrl})
- [Source code](${repositoryUrl}): dataset release, harness, graders, and this website.

## Contact

- Email: ${contactEmail}
- [Discord](${discordUrl})

## Optional

- [Full text for language models](${url('/llms-full.txt')}): everything above plus the current results table, persona details, submission format, and the harness integration guide.
`;
}

function resultsTable() {
  if (!configurations.length) return 'No complete official results are published yet.';
  const rows = [...configurations].sort((a, b) => b.pass_rate - a.pass_rate);
  const personas = Object.keys(personaNames) as Persona[];
  const header = ['Memory', 'Model', 'Harness', 'Accuracy', 'Passes / 600', ...personas.map((p) => personaNames[p]), 'Total cost', 'Median latency', 'p95 latency'];
  const lines = rows.map((row) => [
    row.memory.name, `${row.model.name} (${row.model.provider})`, row.harness.name, percent(row.pass_rate), String(row.passes),
    ...personas.map((p) => `${row.personas[p].passes} / ${row.personas[p].total}`),
    formatTotalCost(row), formatLatency(row.median_latency_seconds), formatLatency(row.p95_latency_seconds),
  ]);
  const table = [header, header.map(() => '---'), ...lines].map((cells) => `| ${cells.join(' | ')} |`).join('\n');
  const cheapest = rows.filter((row) => totalCost(row) !== null).sort((a, b) => totalCost(a)! - totalCost(b)!)[0];
  const best = rows[0];
  return `${table}

Highest accuracy: ${best.memory.name} with ${best.model.name} on ${best.harness.name} at ${percent(best.pass_rate)}.` +
    (cheapest ? ` Lowest total cost: ${cheapest.memory.name} with ${cheapest.model.name} on ${cheapest.harness.name} at ${formatTotalCost(cheapest)}.` : '') +
    `\nCost includes agent and memory-system processing during ingestion and testing.`;
}

/** The llms-full.txt document: the index plus the content an agent would otherwise have to browse for. */
export function llmsFull(): string {
  const guide = runRepoFileContents['docs/DRIVER_CONTRACT.md'].content.replace(/^# .*\n/, '');
  return `${llmsIndex()}
---

# How ${siteName} works

1. History. Each persona has years of conversation with an assistant, mixing work and life. The messages that matter for a later task are stated once and buried among unrelated ones.
2. Ingest. Each memory system processes a persona's history message by message, in order, starting from empty memory for every persona.
3. Act. Each of the persona's tests runs in a fresh conversation with isolated app state and read-only access to the completed memory. The agent receives the request and the simulated apps and must act.
4. Grade. Every test checks the tool called, the target, and the content of the action. A task passes only when every required check passes. Facts and expected answers are never in the agent's context.

# Current official results

${resultsTable()}

# Personas

${data.personas.map((persona) =>
  `## ${persona.name}\n\n${persona.role} at ${persona.organization}. ${persona.summary}\n` +
  `History: ${number(persona.messages)} messages from ${formatDate(persona.start)} to ${formatDate(persona.end)}; ` +
  `${number(persona.facts)} facts; ${persona.tests} tests evaluated as of ${formatDate(persona.evaluation_date)}.\n` +
  `Tools available to the agent: ${persona.tools.join(', ')}.`).join('\n\n')}

# Submitting a run

Run all 600 tests with your harness, model, and memory system, then package the run with \`python -m harness.runner package\`. The submission ZIP contains exactly two files at its root: \`ingestion.json\` (recorded interactions for the complete history of all three personas) and \`tests.json\` (all 600 test interactions and their grading evidence, including failures). Upload it at ${url('/run/#upload')}. Valid submissions appear on the leaderboard as self-submitted and unverified. Questions: ${contactEmail}.

# Harness integration guide

${guide.trim()}
`;
}
