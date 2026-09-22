'use client';

import Script from 'next/script';
import { usePathname } from 'next/navigation';
import { googleAnalyticsId } from '@/lib/site';

// gtag reports the full URL, fragment included. The receipt link carries a private capability
// token in its fragment — the page already suppresses its referrer to keep that token off the
// wire, so the tag has to stay off that route too. Receipt links are plain anchors, so reaching
// the page is always a full navigation and the tag is never left running from a previous route.
const privatePath = '/run/receipt/';

export default function Analytics() {
  const pathname = usePathname();
  if (pathname.startsWith(privatePath)) return null;

  return (
    <>
      <Script src={`https://www.googletagmanager.com/gtag/js?id=${googleAnalyticsId}`} strategy="afterInteractive" />
      <Script id="google-analytics" strategy="afterInteractive">{`
        window.dataLayer = window.dataLayer || [];
        function gtag(){dataLayer.push(arguments);}
        gtag('js', new Date());
        gtag('config', '${googleAnalyticsId}');
      `}</Script>
    </>
  );
}
