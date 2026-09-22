'use client';

import { FormEvent, useState } from 'react';
import { useRouter } from 'next/navigation';

export default function LoginPage() {
  const router = useRouter();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setError('');

    const form = new FormData(event.currentTarget);
    const response = await fetch('/api/auth/login/', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        username: form.get('username'),
        password: form.get('password'),
      }),
    });

    if (!response.ok) {
      const result = await response.json().catch(() => null);
      setError(result?.error || 'Unable to sign in.');
      setLoading(false);
      return;
    }

    router.replace('/');
    router.refresh();
  }

  return (
    <div className="container-x flex min-h-[calc(100vh-7rem)] max-w-md items-center py-16">
      <div className="w-full">
        <h1 className="page-title">Access DolphinBench</h1>
        <form onSubmit={submit} className="mt-8 space-y-5">
          <label className="block">
            <span className="text-sm font-medium">Username</span>
            <input
              name="username"
              autoComplete="username"
              required
              autoFocus
              className="field mt-2 h-11 w-full text-base"
            />
          </label>
          <label className="block">
            <span className="text-sm font-medium">Password</span>
            <input
              name="password"
              type="password"
              autoComplete="current-password"
              required
              className="field mt-2 h-11 w-full text-base"
            />
          </label>
          {error ? <p className="text-sm text-fail">{error}</p> : null}
          <button type="submit" disabled={loading} className="pill-button h-11 w-full justify-center disabled:cursor-wait disabled:opacity-60">
            {loading ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  );
}
