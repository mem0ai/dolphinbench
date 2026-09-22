export type HistoryMessage = {
  id: string;
  date: string;
  content: string;
  source_test_ids: string[];
  context_test_ids: string[];
};

export type Fact = {
  id: number;
  statement: string;
  applies_when: string;
  source_session_ids: string[];
  related_history_session_ids: string[];
};

export type Assertion = Record<string, unknown> & {
  type: string;
  tool?: string;
  path?: string;
  value?: unknown;
  criterion?: string;
};

export type Test = {
  id: string;
  date: string;
  request: string;
  fact_ids: number[];
  tools: string[];
  grade: {
    type: string;
    config: { assertions: Assertion[]; [key: string]: unknown };
  };
  sha256: string;
};
export type TestSummary = Pick<
  Test,
  'id' | 'request' | 'date' | 'fact_ids' | 'tools'
>;

export type Persona = {
  id: string;
  name: string;
  role: string;
  organization: string;
  summary: string;
  accent: string;
  profile_source: string;
  start: string;
  end: string;
  evaluation_date: string;
  messages: number;
  facts: number;
  tests: number;
  tokens: number;
  tools: string[];
  months: { month: string; messages: number }[];
  checkpoint_sha256: string;
  history_sha256: string;
  facts_sha256: string;
};
export type PersonaData = { tests: Test[]; facts: Record<string, Fact> };
export type Release = {
  schema_version: number;
  release_sha256: string;
  personas: Persona[];
  messages: number;
  tests: number;
  tokens: number;
};

export const number = (value: number) => value.toLocaleString('en-US');

// Render the calendar date written in the source, independent of the viewer's timezone.
export function formatDate(
  iso: string,
  options?: Intl.DateTimeFormatOptions,
): string {
  return new Date(`${iso.slice(0, 10)}T12:00:00Z`).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    ...options,
    timeZone: 'UTC',
  });
}
export function formatTime(iso: string): string {
  const offset = iso.match(/(Z|[+-]\d\d:\d\d)$/)?.[1];
  return `${iso.slice(11, 16)}${offset ? ` UTC${offset === 'Z' ? '' : offset}` : ''}`;
}
export const personaHref = (id: string) => `/personas/${id}/`;
export const testsHref = (id: string) => `${personaHref(id)}tests/`;
export const testHref = (id: string, test: string) =>
  `${testsHref(id)}${test}/`;
export const timelineHref = (id: string) => `${personaHref(id)}timeline/`;
export const messageHref = (id: string, message: string) =>
  `${timelineHref(id)}?session=${message}#message-${message}`;
