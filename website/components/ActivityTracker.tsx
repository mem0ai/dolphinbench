'use client';

import { useEffect, useRef } from 'react';
import { usePathname } from 'next/navigation';

type EventPayload = {
  name: 'page_view' | 'page_time' | 'click' | 'select' | 'search';
  path: string;
  target?: string;
  metadata?: Record<string, string | number | boolean | null>;
  clientAt: string;
};

function send(events: EventPayload[]) {
  const body = JSON.stringify({ events });
  if (navigator.sendBeacon) {
    navigator.sendBeacon('/api/events/', new Blob([body], { type: 'application/json' }));
    return;
  }
  void fetch('/api/events/', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body,
    keepalive: true,
  });
}

function event(name: EventPayload['name'], path: string, target?: string, metadata?: EventPayload['metadata']) {
  send([{ name, path, target, metadata, clientAt: new Date().toISOString() }]);
}

export default function ActivityTracker() {
  const pathname = usePathname();
  const startedAt = useRef(Date.now());

  useEffect(() => {
    if (pathname === '/login/' || pathname.startsWith('/admin') || pathname.startsWith('/run/')) return;
    startedAt.current = Date.now();
    let finished = false;
    event('page_view', pathname);

    function finish() {
      if (finished) return;
      finished = true;
      event('page_time', pathname, undefined, {
        seconds: Math.max(0, Math.round((Date.now() - startedAt.current) / 1000)),
      });
    }

    window.addEventListener('pagehide', finish);
    return () => {
      window.removeEventListener('pagehide', finish);
      finish();
    };
  }, [pathname]);

  useEffect(() => {
    if (pathname === '/login/' || pathname.startsWith('/admin') || pathname.startsWith('/run/')) return;

    function onClick(rawEvent: MouseEvent) {
      const element = (rawEvent.target as HTMLElement | null)?.closest<HTMLElement>('a, button, [role="button"]');
      if (!element) return;
      const target = element.dataset.track || element.textContent?.trim().replace(/\s+/g, ' ').slice(0, 200) || element.tagName.toLowerCase();
      const href = element instanceof HTMLAnchorElement ? element.getAttribute('href') : null;
      event('click', pathname, target, { href });
    }

    function onChange(rawEvent: Event) {
      const element = rawEvent.target;
      if (element instanceof HTMLSelectElement) {
        event('select', pathname, element.name || element.getAttribute('aria-label') || 'select', {
          value: element.value,
          label: element.selectedOptions[0]?.textContent?.trim() || '',
        });
      } else if (element instanceof HTMLInputElement && element.dataset.trackInput) {
        event('search', pathname, element.dataset.trackInput, { value: element.value.slice(0, 500) });
      }
    }

    document.addEventListener('click', onClick, true);
    document.addEventListener('change', onChange, true);
    return () => {
      document.removeEventListener('click', onClick, true);
      document.removeEventListener('change', onChange, true);
    };
  }, [pathname]);

  return null;
}
