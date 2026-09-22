import 'server-only';
import type { Metadata } from 'next';
import { getPersona, getPersonaData, number, personaHref, testHref, testsHref, timelineHref } from './data';

type View = 'overview' | 'tests' | 'timeline';

export function personaMetadata(id: string, view: View): Metadata {
  const persona = getPersona(id);
  if (!persona) return {};
  const base = `${persona.name}, ${persona.role} at ${persona.organization}`;
  const stats = `${number(persona.messages)} history messages, ${number(persona.facts)} facts, ${persona.tests} tests`;
  const pages: Record<View, { title: string; description: string; path: string }> = {
    overview: {
      title: persona.name,
      description: `${base}: a simulated DolphinBench user. ${persona.summary} ${stats}.`,
      path: personaHref(id),
    },
    tests: {
      title: `${persona.name} tests`,
      description: `All ${persona.tests} DolphinBench tests for ${base}, each with its request, tools, source facts, and grading checks.`,
      path: testsHref(id),
    },
    timeline: {
      title: `${persona.name} history`,
      description: `The ${number(persona.messages)}-message conversation history of ${base}, the memory every DolphinBench configuration ingests before ${persona.name.split(' ')[0]}'s tests.`,
      path: timelineHref(id),
    },
  };
  const page = pages[view];
  return { title: page.title, description: page.description, alternates: { canonical: page.path } };
}

export function testMetadata(id: string, testid: string): Metadata {
  const persona = getPersona(id);
  if (!persona) return {};
  const test = getPersonaData(id).tests.find((item) => item.id === testid);
  if (!test) return {};
  const request = test.request.length > 140 ? `${test.request.slice(0, 137).trimEnd()}...` : test.request;
  return {
    title: `Test ${test.id} for ${persona.name}`,
    description: `DolphinBench test ${test.id} for ${persona.name}: "${request}" Tools: ${test.tools.join(', ')}. ` +
      `Depends on ${test.fact_ids.length} earlier fact${test.fact_ids.length === 1 ? '' : 's'}.`,
    alternates: { canonical: testHref(id, test.id) },
  };
}
