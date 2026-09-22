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
  if (!fact) throw new Error(`Example test ${testId} has no fact ${test.fact_ids[0]}`);
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
