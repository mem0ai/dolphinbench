'use client';

import { barList, breakdownItems, type BarKey, type View } from '@/lib/results-view';
import type { ResultRow } from '@/lib/results';
import { memoryLogos } from '@/lib/logos';
import Logo from './Logo';

const viewLabels: Record<View, string> = {
  config: 'Configurations',
  memory: 'Avg by memory',
  model: 'Avg by model',
  harness: 'Avg by harness',
};

export default function Breakdown({
  rows,
  view,
  onView,
  views,
  total,
}: {
  rows: ResultRow[];
  view: View;
  onView: (view: View) => void;
  views: View[];
  total: number;
}) {
  const items = breakdownItems(rows, view);
  const columns: [BarKey, string, string][] = [
    ['accuracy', 'Accuracy', `Tasks passed of ${total} · Higher is better`],
    ['cost', 'Total cost', 'USD · Lower is better'],
    ['median', 'Median latency', 'Seconds per task · Lower is better'],
  ];
  return (
    <div className="breakdown mb-16">
      <div className="mb-5 flex flex-wrap items-center justify-between gap-4">
        <h3 className="text-md font-semibold">Official breakdown</h3>
        {views.length > 1 && (
          <div className="segmented" role="group" aria-label="Breakdown view">
            {views.map((option) => (
              <button key={option} type="button" aria-pressed={view === option} onClick={() => onView(option)}>
                {viewLabels[option]}
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="grid gap-10 md:grid-cols-3">
        {columns.map(([key, title, subtitle]) => (
          <div key={key} data-bar-column={key}>
            <div className="mb-2 border-b border-ink pb-3">
              <div className="text-md font-semibold tracking-[-0.01em]">{title}</div>
              <div className="mt-0.5 text-xs text-muted">{subtitle}</div>
            </div>
            {barList(items, key).map(({ item, width, value }) => (
              <div
                key={`${item.label}-${item.sub}`}
                data-bar
                className="grid grid-cols-[minmax(0,1fr)_90px] items-center gap-3 border-b border-hairline py-[9px]"
              >
                <div className="min-w-0">
                  <div className="mb-[5px] flex justify-between gap-2 text-sm leading-[1.3]">
                    <span className="inline-flex min-w-0 items-center gap-1.5 truncate font-semibold">
                      <Logo src={item.memory ? memoryLogos[item.memory] : undefined} />
                      {item.label}
                    </span>
                    <span className="truncate text-xs text-muted">{item.sub}</span>
                  </div>
                  <div className="h-2.5 overflow-hidden rounded-sm bg-ink/[0.06]">
                    <div className="h-full rounded-sm" style={{ width, background: item.color }} />
                  </div>
                </div>
                <span className="text-right font-mono text-sm">{value}</span>
              </div>
            ))}
            {items.length === 0 && <p className="py-4 text-sm text-muted">No configurations match these filters.</p>}
          </div>
        ))}
      </div>
    </div>
  );
}
