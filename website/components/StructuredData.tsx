import { brandIconUrl } from '@/lib/brand-assets';
import type { Release } from '@/lib/types';
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

type Schema = Record<string, unknown>;

export function organizationSchema(): Schema {
  return {
    '@context': 'https://schema.org',
    '@type': 'Organization',
    '@id': `${mem0Url}/#organization`,
    name: 'Mem0',
    url: mem0Url,
    sameAs: [repositoryUrl, discordUrl],
    contactPoint: { '@type': 'ContactPoint', email: contactEmail, contactType: 'research' },
  };
}

export function websiteSchema(): Schema {
  return {
    '@context': 'https://schema.org',
    '@type': 'WebSite',
    '@id': `${siteUrl}/#website`,
    name: siteName,
    alternateName: `${siteName}: ${tagline}`,
    url: siteUrl,
    description: siteDescription,
    image: brandIconUrl,
    publisher: { '@id': `${mem0Url}/#organization` },
    inLanguage: 'en',
  };
}

export function datasetSchema(release: Release): Schema {
  const start = release.personas.map((persona) => persona.start).sort()[0];
  const end = release.personas.map((persona) => persona.end).sort().at(-1);
  return {
    '@context': 'https://schema.org',
    '@type': 'Dataset',
    '@id': `${siteUrl}/dataset/#dataset`,
    name: `${siteName} dataset`,
    description:
      `${release.tests} tasks across ${release.personas.length} simulated users with ` +
      `${release.messages.toLocaleString('en-US')} history messages spanning ${start?.slice(0, 4)} to ${end?.slice(0, 4)}. ` +
      'Each task is a request whose correct action depends on earlier conversation, graded on the tool called, its target, and its content.',
    url: `${siteUrl}/dataset/`,
    sameAs: repositoryUrl,
    creator: { '@id': `${mem0Url}/#organization` },
    citation: paperUrl,
    isAccessibleForFree: true,
    keywords: ['agent memory', 'long-term memory', 'LLM agents', 'benchmark', 'tool use'],
    temporalCoverage: `${start?.slice(0, 10)}/${end?.slice(0, 10)}`,
    variableMeasured: [
      { '@type': 'PropertyValue', name: 'Accuracy', description: 'Share of the 600 tasks whose required checks all pass' },
      { '@type': 'PropertyValue', name: 'Total cost', unitText: 'USD' },
      { '@type': 'PropertyValue', name: 'Median latency', unitText: 'seconds' },
    ],
    distribution: [
      { '@type': 'DataDownload', encodingFormat: 'application/json', contentUrl: `${siteUrl}/leaderboard/results.json` },
      { '@type': 'DataDownload', encodingFormat: 'text/html', contentUrl: repositoryUrl },
    ],
    version: release.release_sha256.slice(0, 12),
  };
}

export default function StructuredData({ data }: { data: Schema[] }) {
  return data.map((item, index) => (
    <script
      key={index}
      type="application/ld+json"
      // JSON.stringify output is safe to inline once "<" is escaped.
      dangerouslySetInnerHTML={{ __html: JSON.stringify(item).replace(/</g, '\\u003c') }}
    />
  ));
}
