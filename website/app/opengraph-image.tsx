import { ImageResponse } from 'next/og';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { dolphinTileSvg } from '@/lib/brand';
import { siteName, tagline } from '@/lib/site';

export const alt = `${siteName}: ${tagline}`;
export const size = { width: 1200, height: 630 };
export const contentType = 'image/png';

const INK = '#0A0A0A';
const MUTED = '#6E6A62';
const LAVENDER = '#CBB2FF';
const FRONTIER = '#A58BFF';

// Illustrative accuracy-vs-cost layout: four frontier points and six dominated ones.
const FRONT = [[0.14, 0.82], [0.31, 0.7], [0.51, 0.58], [0.72, 0.36]];
const REST = [[0.27, 0.48], [0.45, 0.4], [0.49, 0.3], [0.62, 0.22], [0.78, 0.24], [0.85, 0.14]];

const font = (file: string) => readFile(join(process.cwd(), 'assets/fonts', file));

export default async function Image() {
  const [semiBold, regular, mono, logo] = await Promise.all([
    font('Fustat-SemiBold.ttf'), font('Fustat-Regular.ttf'), font('JetBrainsMono-Regular.ttf'),
    readFile(join(process.cwd(), 'assets/logos/mem0.svg')),
  ]);
  const mem0 = `data:image/svg+xml;base64,${logo.toString('base64')}`;
  const mark = `data:image/svg+xml;base64,${Buffer.from(dolphinTileSvg(36)).toString('base64')}`;

  const chart = { x: 636, y: 62, w: 486, h: 500 };
  const px = (fx: number) => chart.x + fx * chart.w;
  const py = (fy: number) => chart.y + (1 - fy) * chart.h;
  const path = FRONT.map(([fx, fy], index) => `${index ? 'L' : 'M'}${px(fx)} ${py(fy)}`).join(' ');

  return new ImageResponse(
    (
      <div style={{ display: 'flex', width: '100%', height: '100%', background: '#FFFFFF', color: INK, fontFamily: 'Fustat' }}>
        <div style={{ display: 'flex', position: 'absolute', left: 66, top: 52, alignItems: 'center', fontSize: 26, fontWeight: 600, letterSpacing: -0.5 }}>
          <img src={mark} width={36} height={36} style={{ marginRight: 12 }} alt="" />
          DolphinBench
        </div>

        <div style={{ display: 'flex', position: 'absolute', left: 66, top: 202, width: 540, height: 250 }}>
          <div style={{ display: 'flex', position: 'absolute', left: 0, top: 128, width: 424, height: 32, background: LAVENDER, opacity: 0.55 }} />
          <div style={{ display: 'flex', flexDirection: 'column', fontSize: 74, fontWeight: 600, lineHeight: 1.02, letterSpacing: -3 }}>
            <span>Mapping the</span>
            <span>Pareto frontier of</span>
            <span>agent memory</span>
          </div>
        </div>

        <div style={{ display: 'flex', position: 'absolute', left: 66, top: 540, alignItems: 'center', fontSize: 19, color: MUTED }}>
          <span>A benchmark by</span>
          <img src={mem0} width={24} height={24} style={{ marginLeft: 12, marginRight: 6 }} alt="" />
          <span style={{ color: INK, fontWeight: 600, fontSize: 23 }}>mem0</span>
        </div>

        <div style={{ display: 'flex', position: 'absolute', left: chart.x, top: 26, fontFamily: 'JetBrains Mono', fontSize: 15, color: MUTED }}>
          Accuracy ↑
        </div>
        <div style={{ display: 'flex', position: 'absolute', right: 1200 - chart.x - chart.w, top: 578, fontFamily: 'JetBrains Mono', fontSize: 15, color: MUTED }}>
          Total cost →
        </div>

        <svg width={size.width} height={size.height} viewBox={`0 0 ${size.width} ${size.height}`} style={{ position: 'absolute', left: 0, top: 0 }}>
          <rect x={chart.x} y={chart.y} width={chart.w / 2} height={chart.h / 2} fill={LAVENDER} opacity={0.25} />
          {[1, 2, 3].map((step) => (
            <g key={step}>
              <line x1={px(step / 4)} x2={px(step / 4)} y1={chart.y} y2={chart.y + chart.h} stroke="rgba(10,10,10,0.09)" strokeWidth={1} />
              <line x1={chart.x} x2={chart.x + chart.w} y1={py(step / 4)} y2={py(step / 4)} stroke="rgba(10,10,10,0.09)" strokeWidth={1} />
            </g>
          ))}
          <line x1={chart.x} x2={chart.x} y1={chart.y} y2={chart.y + chart.h} stroke="rgba(10,10,10,0.35)" strokeWidth={1.5} />
          <line x1={chart.x} x2={chart.x + chart.w} y1={chart.y + chart.h} y2={chart.y + chart.h} stroke="rgba(10,10,10,0.35)" strokeWidth={1.5} />
          <path d={path} fill="none" stroke={FRONTIER} strokeWidth={3} strokeDasharray="9 7" strokeLinecap="round" />
          {REST.map(([fx, fy]) => (
            <circle key={`${fx}-${fy}`} cx={px(fx)} cy={py(fy)} r={13} fill="#FFFFFF" stroke="rgba(10,10,10,0.35)" strokeWidth={1.5} />
          ))}
          {FRONT.map(([fx, fy]) => (
            <circle key={`${fx}-${fy}`} cx={px(fx)} cy={py(fy)} r={14} fill="#FFFFFF" stroke={INK} strokeWidth={2.5} />
          ))}
        </svg>
      </div>
    ),
    {
      ...size,
      fonts: [
        { name: 'Fustat', data: semiBold, weight: 600, style: 'normal' },
        { name: 'Fustat', data: regular, weight: 400, style: 'normal' },
        { name: 'JetBrains Mono', data: mono, weight: 400, style: 'normal' },
      ],
    },
  );
}
