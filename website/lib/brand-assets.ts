import { siteUrl } from '@/lib/site';

// Static brand files under public/brand/, served without a session (see lib/preview.ts).
export const brandIconPath = '/brand/dolphinbench-icon-512.png';
export const brandIconUrl = `${siteUrl}${brandIconPath}`;
export const brandIcons = [
  { src: '/brand/dolphinbench-icon-192.png', sizes: '192x192', type: 'image/png' },
  { src: brandIconPath, sizes: '512x512', type: 'image/png' },
  { src: '/brand/dolphinbench-icon.svg', sizes: 'any', type: 'image/svg+xml' },
];
