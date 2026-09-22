import type { MetadataRoute } from 'next';
import { data, getPersonaData, personaHref, testHref, testsHref, timelineHref } from '@/lib/data';
import { siteUrl } from '@/lib/site';

export default function sitemap(): MetadataRoute.Sitemap {
  const page = (path: string, priority: number, changeFrequency: 'weekly' | 'monthly' | 'yearly'): MetadataRoute.Sitemap[number] =>
    ({ url: `${siteUrl}${path}`, priority, changeFrequency });
  return [
    page('/', 1, 'weekly'),
    page('/leaderboard/', 0.9, 'weekly'),
    page('/dataset/', 0.8, 'monthly'),
    page('/run/', 0.8, 'monthly'),
    page('/run/guide/', 0.6, 'monthly'),
    page('/run/template/', 0.5, 'monthly'),
    ...data.personas.flatMap((persona) => [
      page(personaHref(persona.id), 0.7, 'monthly'),
      page(testsHref(persona.id), 0.6, 'monthly'),
      page(timelineHref(persona.id), 0.5, 'monthly'),
      ...getPersonaData(persona.id).tests.map((test) => page(testHref(persona.id, test.id), 0.3, 'yearly')),
    ]),
  ];
}
