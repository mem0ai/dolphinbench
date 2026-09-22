import 'server-only';
import fs from 'node:fs';
import path from 'node:path';
import { cache } from 'react';
import type { HistoryMessage, PersonaData, Release } from './types';

export * from './types';

const directory = path.join(process.cwd(), 'public/data');
export const data: Release = JSON.parse(
  fs.readFileSync(path.join(directory, 'index.json'), 'utf8'),
);
if (data.schema_version !== 2)
  throw new Error('Rebuild website data with npm run data.');
export const PERSONA_IDS = data.personas.map((persona) => persona.id);
export const getPersona = (id: string) =>
  data.personas.find((persona) => persona.id === id);

export const getPersonaData = cache((id: string): PersonaData => {
  if (!getPersona(id)) throw new Error('Unknown persona');
  return JSON.parse(
    fs.readFileSync(path.join(directory, `${id}.json`), 'utf8'),
  );
});

export const getHistory = cache((id: string): HistoryMessage[] => {
  if (!getPersona(id)) throw new Error('Unknown persona');
  return JSON.parse(
    fs.readFileSync(path.join(directory, `${id}-history.json`), 'utf8'),
  );
});

export const getMessages = cache(
  (id: string) =>
    new Map(getHistory(id).map((message) => [message.id, message])),
);
