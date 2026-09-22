import { notFound } from 'next/navigation';
import { connection } from 'next/server';
import { getPersona, getPersonaData } from '@/lib/data';
import PersonaHeader from '@/components/PersonaHeader';
import TestBrowser from '@/components/TestBrowser';
import { personaMetadata } from '@/lib/persona-metadata';

export async function generateMetadata({ params }: { params: Promise<{ persona: string }> }) {
  return personaMetadata((await params).persona, 'tests');
}

export default async function TestsPage({
  params,
}: {
  params: Promise<{ persona: string }>;
}) {
  await connection();
  const { persona: id } = await params;
  const persona = getPersona(id);
  if (!persona) notFound();
  const tests = getPersonaData(id).tests.map(
    ({ id, request, date, fact_ids, tools }) => ({
      id,
      request,
      date,
      fact_ids,
      tools,
    }),
  );
  return (
    <div className="page">
      <PersonaHeader persona={persona} active="tests" />
      <TestBrowser personaId={id} tests={tests} tools={persona.tools} />
    </div>
  );
}
