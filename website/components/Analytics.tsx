'use client';

import Script from 'next/script';
import { usePathname } from 'next/navigation';
import { useState } from 'react';
import { googleAnalyticsId } from '@/lib/site';

// gtag reports the full URL, fragment included. The receipt link carries a private capability
// token in its fragment — the page already suppresses its referrer to keep that token off the
// wire, so the tag has to stay off that route too. Receipt links are plain anchors, so reaching
// the page is always a full navigation and the tag is never left running from a previous route.
const privatePath = '/run/receipt/';
const source = `https://www.googletagmanager.com/gtag/js?id=${googleAnalyticsId}`;
const initialize = `
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());
  gtag('config', '${googleAnalyticsId}');
`;

export default function Analytics() {
  const pathname = usePathname();
  const isPrivate = pathname === '/run/receipt' || pathname.startsWith(privatePath);
  const [startedOnReceipt] = useState(isPrivate);
  if (isPrivate) return null;

  // A client navigation away from a receipt needs script execution, which React
  // does not provide for inline script elements inserted after the initial load.
  if (startedOnReceipt) {
    return (
      <>
        <Script src={source} strategy="afterInteractive" />
        <Script id="google-analytics" strategy="afterInteractive">{initialize}</Script>
      </>
    );
  }

  // Search Console must find the complete Google tag in the original HTML head.
  return (
    <>
      <script async src={source} />
      <script id="google-analytics" dangerouslySetInnerHTML={{ __html: initialize }} />
    </>
  );
}
