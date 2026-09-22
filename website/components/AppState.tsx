'use client';

import { useState } from 'react';

export default function AppState({ persona, testId }: { persona: string; testId: string }) {
  const [value, setValue] = useState<string>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const url = `/data/tests/${persona}/${testId}-state.json`;
  async function load() {
    if (value !== undefined || loading) return;
    setLoading(true);
    setError(false);
    try {
      const response = await fetch(url);
      if (!response.ok) throw new Error('Unable to load app state');
      setValue(JSON.stringify(await response.json(), null, 2));
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  }
  return (
    <details className="section text-sm" onToggle={(event) => { if (event.currentTarget.open) void load(); }}>
      <summary>App state</summary>
      <a className="text-link mt-4 inline-block" href={url} download>Download JSON</a>
      {loading && <p role="status">Loading app state...</p>}
      {error && <p role="alert">App state could not be loaded. <button className="text-link" onClick={() => void load()}>Retry</button></p>}
      {value !== undefined && <pre className="code-block mt-4 max-h-[28rem] overflow-y-auto">{value}</pre>}
    </details>
  );
}
