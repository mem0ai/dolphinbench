import type { MetadataRoute } from 'next';
import { brandIcons } from '@/lib/brand-assets';
import { siteDescription, siteName, tagline } from '@/lib/site';

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: `${siteName}: ${tagline}`,
    short_name: siteName,
    description: siteDescription,
    start_url: '/',
    display: 'standalone',
    background_color: '#FFFFFF',
    theme_color: '#FFFFFF',
    icons: brandIcons,
  };
}
