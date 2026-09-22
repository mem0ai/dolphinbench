'use client';

import { useEffect, useState } from 'react';
import { Check, Copy } from 'lucide-react';

export default function CopyText({ text, label, showLabel = false }: {
  text: string; label: string; showLabel?: boolean;
}) {
  const [status, setStatus] = useState<'idle' | 'copied' | 'error'>('idle');
  useEffect(() => {
    if (status === 'idle') return;
    const timer = setTimeout(() => setStatus('idle'), 2500);
    return () => clearTimeout(timer);
  }, [status]);
  return (
    <span className="relative inline-flex shrink-0 items-center">
      <button type="button" aria-label={label} title={label}
        className={`inline-flex items-center justify-center gap-2 rounded text-sm text-muted hover:bg-sand hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-ink ${showLabel ? 'min-h-10 border border-hairline bg-paper px-3' : 'size-8'}`}
        onClick={async () => {
          try { await navigator.clipboard.writeText(text); setStatus('copied'); }
          catch { setStatus('error'); }
        }}>
        {status === 'copied' ? <Check size={16} aria-hidden="true" /> : <Copy size={16} aria-hidden="true" />}
        {showLabel && 'Copy for agent'}
      </button>
      <span role="status" className={status === 'error'
        ? 'absolute right-0 top-full z-10 mt-1 whitespace-nowrap rounded border border-hairline bg-paper px-2 py-1 text-xs text-ink'
        : 'sr-only'}>{status === 'error' ? 'Clipboard unavailable' : status === 'copied' ? 'Copied' : ''}</span>
    </span>
  );
}
