// The DolphinBench mark from the brand kit, drawn in a 240.18 x 199.9 box. Shared by the
// inline component, the generated favicons, and the Open Graph image so every surface uses
// one shape. The source files are served from public/brand/.
export const MARK_WIDTH = 240.18;
export const MARK_HEIGHT = 199.9;
export const MARK_VIEWBOX = `0 0 ${MARK_WIDTH} ${MARK_HEIGHT}`;
export const MARK_PATHS: readonly string[] = [
  'M182.47,53.17c-2.07,20.36-17.61,37.18-38.17,40.54,6.53-8.96,10.93-19.52,12.52-30.87,8.29-3.9,16.85-7.12,25.65-9.67Z',
  'M100.09,6.97c-7.72,4.05-14.96,8.92-21.62,14.48-7.85-5.2-16.8-8.81-26.4-10.43,7.13-5.28,15.86-8.56,25.39-8.96,8.13-.34,15.85,1.46,22.62,4.91Z',
  'M237.89,39.52c-6.7-.24-13.35-.11-19.91.37-5.87.43-11.67,1.14-17.4,2.12-24.73,4.23-47.99,13.48-68.53,26.77-27.85,18.02-50.72,43.47-65.5,73.97-.84,1.73-1.65,3.47-2.44,5.23,18.34,11.01,30.82,30.2,33.04,51.92-5.94-7.77-13.53-14.29-22.33-18.97-.86-.46-1.74-.9-2.63-1.33-5.76-2.76-11.25-6.05-16.23-10.06-1.1-.88-2.28-1.66-3.54-2.31-2.25.24-4.4.83-6.39,1.71-5.39,2.39-11.06,4.09-16.85,5.24-1.77.35-3.51.77-5.24,1.25-8.77,2.46-16.9,6.64-23.95,12.19,7.67-20.79,24.94-36.45,45.87-42.34-.48-1.74-.92-3.48-1.32-5.24-2.89-12.7-3.65-26.12-1.92-39.8C50.56,37.45,107.9-7.02,170.69.92c4.17.52,8.26,1.27,12.25,2.22.02,0,.03,0,.04,0,.22.05.44.1.67.16.37.09.74.18,1.1.28.03,0,.05.01.07.02,9.4,2.54,17.48,7.93,23.38,15.11.22.28.44.56.67.83,3.58,4.28,8.73,7.27,14.7,8.02.12.01.23.03.35.04h.05c.12.01.25.02.36.04h.08c.12.02.25.03.36.05,4.59.58,8.69,2.48,11.97,5.26,0,0,.01.01.02.02,1.07.9,2.04,1.9,2.92,2.98,0,.01.02.03.03.04,1.17,1.45.06,3.6-1.81,3.54Z',
];

// The app-icon tile from the kit: a rounded square with the mark offset inside it.
export const TILE_SIZE = 327.8;
export const TILE_RADIUS = 59.91;
export const TILE_VIEWBOX = `0 0 ${TILE_SIZE} ${TILE_SIZE}`;
export const TILE_MARK_OFFSET = { x: 43.74, y: 76.11 } as const;
export const BRAND_TILE = '#CBB2FF';
export const BRAND_INK = '#0A0A0A';

/** Standalone SVG markup for the app-icon tile: rounded lavender square with the mark. */
export function dolphinTileSvg(size: number): string {
  const mark = MARK_PATHS.map((d) => `<path d="${d}" fill="${BRAND_INK}"/>`).join('');
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="${TILE_VIEWBOX}">` +
    `<rect width="${TILE_SIZE}" height="${TILE_SIZE}" rx="${TILE_RADIUS}" fill="${BRAND_TILE}"/>` +
    `<g transform="translate(${TILE_MARK_OFFSET.x} ${TILE_MARK_OFFSET.y})">${mark}</g></svg>`;
}
