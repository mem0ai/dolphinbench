import type { Metadata, Viewport } from 'next';
import localFont from 'next/font/local';
import { JetBrains_Mono } from 'next/font/google';
import '../styles/globals.css';
import Analytics from '@/components/Analytics';
import DolphinMark from '@/components/DolphinMark';
import Header from '@/components/Header';
import StructuredData, { organizationSchema, websiteSchema } from '@/components/StructuredData';
import {
  contactEmail,
  discordUrl,
  mem0Url,
  paperUrl,
  repositoryUrl,
  siteDescription,
  siteName,
  siteUrl,
  tagline,
} from '@/lib/site';

const fustat = localFont({
  src: [
    { path: '../assets/fonts/Fustat-Regular.ttf', weight: '400' },
    { path: '../assets/fonts/Fustat-Medium.ttf', weight: '500' },
    { path: '../assets/fonts/Fustat-SemiBold.ttf', weight: '600' },
    { path: '../assets/fonts/Fustat-Bold.ttf', weight: '700' },
  ],
  variable: '--font-sans',
  display: 'swap',
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ['latin'],
  weight: ['400', '500'],
  variable: '--font-mono',
  display: 'swap',
});

export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title: { default: `${siteName}: ${tagline}`, template: `%s | ${siteName}` },
  description: siteDescription,
  applicationName: siteName,
  keywords: [
    'agent memory benchmark', 'long-term memory', 'LLM agents', 'memory systems', 'Mem0',
    'agent evaluation', 'Pareto frontier', 'tool use', 'MCP', 'DolphinBench',
  ],
  authors: [{ name: 'Mem0', url: mem0Url }],
  creator: 'Mem0',
  publisher: 'Mem0',
  alternates: { canonical: '/' },
  openGraph: {
    type: 'website',
    siteName,
    locale: 'en_US',
    url: '/',
    title: `${siteName}: ${tagline}`,
    description: siteDescription,
  },
  twitter: { card: 'summary_large_image', title: `${siteName}: ${tagline}`, description: siteDescription },
  robots: { index: true, follow: true, googleBot: { index: true, follow: true, 'max-image-preview': 'large', 'max-snippet': -1 } },
};

export const viewport: Viewport = { themeColor: '#FFFFFF' };

// External links open in a new tab so the benchmark stays open.
const external = { target: '_blank', rel: 'noopener noreferrer' } as const;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${fustat.variable} ${jetbrainsMono.variable}`}>
      <body className="min-h-screen bg-paper font-sans text-ink antialiased">
        <Analytics />
        <StructuredData data={[websiteSchema(), organizationSchema()]} />
        <Header />
        <main id="main-content">{children}</main>
        <footer className="border-t border-hairline bg-sand">
          <div className="container-x flex flex-wrap items-start justify-between gap-x-10 gap-y-6 py-10 text-sm text-muted">
            <div className="max-w-[34em]">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <DolphinMark size={18} tile />
                <span className="font-semibold text-ink">DolphinBench</span>
                <span>· {tagline}</span>
              </div>
              <p className="mb-2">
                A benchmark by <a href={mem0Url} {...external} className="font-medium text-ink transition-colors hover:text-muted">mem0</a>.
              </p>
              <p>
                Questions, results, or submissions:{' '}
                <a href={`mailto:${contactEmail}`} className="font-medium text-ink transition-colors hover:text-muted">{contactEmail}</a>
              </p>
            </div>
            <nav aria-label="Footer" className="flex flex-wrap gap-x-6 gap-y-2">
              <a href="/leaderboard/" className="transition-colors hover:text-ink">Leaderboard</a>
              <a href="/dataset/" className="transition-colors hover:text-ink">Dataset</a>
              <a href={paperUrl} {...external} className="transition-colors hover:text-ink">Paper <span aria-hidden="true">↗</span></a>
              <a href={discordUrl} {...external} className="transition-colors hover:text-ink">Discord <span aria-hidden="true">↗</span></a>
              <a href={repositoryUrl} {...external} className="transition-colors hover:text-ink">GitHub <span aria-hidden="true">↗</span></a>
            </nav>
          </div>
        </footer>
      </body>
    </html>
  );
}
