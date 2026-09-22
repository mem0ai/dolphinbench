import postgres from 'postgres';

let client: ReturnType<typeof postgres> | null = null;

export function db() {
  if (client) return client;

  const connectionString = process.env.DATABASE_URL || process.env.POSTGRES_URL;
  if (!connectionString) {
    throw new Error('DATABASE_URL is not configured.');
  }

  client = postgres(connectionString, {
    max: 1,
    idle_timeout: 20,
    connect_timeout: 15,
    prepare: false,
  });
  return client;
}
