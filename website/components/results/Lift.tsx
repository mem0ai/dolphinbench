'use client';

import { formatPoints, liftOverBuiltin, personaIds, type LiftItem } from '@/lib/results-view';
import { personaNames, type ResultRow } from '@/lib/results';
import { memoryLogos } from '@/lib/logos';
import Logo from './Logo';

function Bar({ value, scale, color }: { value: number; scale: number; color: string }) {
  const width = `${Math.min(50, (Math.abs(value) / scale) * 50).toFixed(1)}%`;
  return (
    <div className="relative h-2.5 overflow-hidden rounded-sm bg-ink/[0.06]">
      <div className="absolute inset-y-0 left-1/2 w-px bg-ink/30" />
      <div
        className="absolute inset-y-0 rounded-sm"
        style={value >= 0 ? { left: '50%', width, background: color } : { right: '50%', width, background: '#B3261E' }}
      />
    </div>
  );
}

export default function Lift({ rows, visibleIds }: { rows: ResultRow[]; visibleIds: Set<string> }) {
  const items = liftOverBuiltin(rows).filter((item) => visibleIds.has(item.id));
  const scale = Math.max(5, ...items.flatMap((item) => [Math.abs(item.overall), ...personaIds.map((p) => Math.abs(item.personas[p]))]));
  const pairs = Array.from(new Set(items.map((item) => item.pair)));
  const columns: [string, (item: LiftItem) => number][] = [
    ['All 600', (item) => item.overall],
    ...personaIds.map((persona): [string, (item: LiftItem) => number] => [personaNames[persona], (item) => item.personas[persona]]),
  ];
  return (
    <div className="lift mb-16" data-lift>
      <div className="mb-5">
        <h3 className="text-md font-semibold">Lift over built-in memory</h3>
        <p className="mt-0.5 text-xs text-muted">
          Accuracy change, in percentage points, against the same harness and model running with no external memory
          system · Higher is better
        </p>
      </div>
      {items.length === 0 ? (
        <p className="py-4 text-sm text-muted">
          No built-in baseline in the current selection. Lift needs a configuration that ran the same harness and
          model without an external memory system.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <div className="min-w-[640px]">
            <div className="grid grid-cols-[minmax(160px,1.2fr)_repeat(4,minmax(100px,1fr))] gap-x-6 border-b border-ink pb-2.5 text-xs text-muted">
              <span>Memory system</span>
              {columns.map(([label]) => <span key={label}>{label}</span>)}
            </div>
            {pairs.map((pair) => (
              <div key={pair} data-lift-pair>
                <div className="pt-4 text-xs text-muted">vs. Built-in · {pair}</div>
                {items.filter((item) => item.pair === pair).map((item) => (
                  <div
                    key={item.id}
                    data-lift-row
                    className="grid grid-cols-[minmax(160px,1.2fr)_repeat(4,minmax(100px,1fr))] items-center gap-x-6 border-b border-hairline py-3"
                  >
                    <span className="inline-flex min-w-0 items-center gap-1.5 truncate text-sm font-semibold">
                      <Logo src={memoryLogos[item.memory]} />
                      {item.memory}
                    </span>
                    {columns.map(([label, value]) => (
                      <div key={label} className="min-w-0">
                        <div className={`mb-1 font-mono text-sm ${value(item) < 0 ? 'text-fail' : ''}`}>{formatPoints(value(item))}</div>
                        <Bar value={value(item)} scale={scale} color={item.color} />
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
