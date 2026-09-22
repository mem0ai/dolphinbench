'use client';

import { useId, useLayoutEffect, useRef, useState } from 'react';
import { colorFor, formatTotalCost, formatLatency, percent, type ChartRow } from '@/lib/results';
import { metricValue, paretoFront, type Metric } from '@/lib/results-view';
import { harnessLogos, memoryLogos } from '@/lib/logos';
import { chartAxis, formatAxisTick, percentAxis } from '@/lib/chart-axis';

const X0 = 56;
const X1 = 792;
const Y0 = 16;
const Y1 = 440;
const WIDTH = 800;
const HEIGHT = 482;
const R = 13;

type Anchor = 'start' | 'end' | 'middle';
type Slot = { x: number; y: number; a: Anchor };
type Box = { x0: number; x1: number; y0: number; y1: number };

export default function ScatterChart({
  rows,
  allRows,
  metric,
  onMetric,
  activeId,
  onActive,
  showSubtitles,
}: {
  rows: ChartRow[];
  allRows: ChartRow[];
  metric: Metric;
  onMetric: (metric: Metric) => void;
  activeId: string | null;
  onActive: (id: string | null) => void;
  showSubtitles: boolean;
}) {
  const clipBase = useId().replace(/:/g, '');
  const plotRef = useRef<HTMLDivElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const [pointer, setPointer] = useState<{ id: string; x: number; y: number } | null>(null);
  const [tooltipPosition, setTooltipPosition] = useState({ left: 0, top: 0 });
  const showTooltip = (id: string, clientX: number, clientY: number) => {
    const bounds = plotRef.current!.getBoundingClientRect();
    setPointer({ id, x: clientX - bounds.left, y: clientY - bounds.top });
    onActive(id);
  };
  const hideTooltip = () => { setPointer(null); onActive(null); };
  const isCost = metric === 'cost';
  const selectedCount = rows.length;
  rows = rows.filter(row => metricValue(row, metric) !== null);
  const xValue = (row: ChartRow) => metricValue(row, metric)!;
  const domainRows = rows.length > 1 ? rows : allRows.filter(row => metricValue(row, metric) !== null);
  const xs = domainRows.map(xValue);
  const axis = chartAxis(xs, isCost);
  // Accuracy is scaled to the results on screen rather than a fixed 0-100% window.
  const yAxis = percentAxis(domainRows.map(row => row.pass_rate * 100));
  const sx = (value: number) => X0 + axis.position(value) * (X1 - X0);
  const sy = (value: number) => Y1 - R - 8 - yAxis.position(value) * (Y1 - Y0 - 2 * (R + 8));
  const front = paretoFront(rows, metric);
  const frontierPath = front
    .map((row, index) => `${index ? 'L' : 'M'}${sx(xValue(row)).toFixed(1)} ${sy(row.pass_rate * 100).toFixed(1)}`)
    .join(' ');
  const midX = (X0 + X1) / 2;
  const midY = (Y0 + Y1) / 2;
  const formatX = (value: number) => `${isCost ? '$' : ''}${formatAxisTick(value)}`;

  // Label placement: try slots around each marker, avoiding other labels and markers.
  const points = rows.map((row) => ({ row, cx: sx(xValue(row)), cy: sy(row.pass_rate * 100) }));
  const dense = points.length > 8;
  const placed: Box[] = [{ x0: X0, x1: X0 + 200, y0: 0, y1: Y0 + 28 }];
  const labels = points.map((point) => {
    const { row, cx, cy } = point;
    const right = cx > midX;
    const short = (value: string) => value.length > 28 ? `${value.slice(0, 27)}…` : value;
    const title = dense ? percent(row.pass_rate) : `${short(row.memory.name)} · ${percent(row.pass_rate)}`;
    const sub = dense || !showSubtitles ? '' : short(`${row.model.name} · ${row.harness.name}`);
    const width = Math.max(title.length * 6.6, sub.length * 6.2) + 4;
    const lineHeight = dense ? 15 : 28;
    const base: Slot[] = right
      ? [{ x: cx - 20, y: cy - 3, a: 'end' }, { x: cx + 20, y: cy - 3, a: 'start' }]
      : [{ x: cx + 20, y: cy - 3, a: 'start' }, { x: cx - 20, y: cy - 3, a: 'end' }];
    const slots: Slot[] = [...base];
    const step = dense ? 17 : 30;
    [-1, 1, -2, 2, -3, 3, -4, 4].forEach((k) => base.forEach((b) => slots.push({ x: b.x, y: b.y + k * step, a: b.a })));
    [-1, 1, -2, 2].forEach((k) =>
      base.forEach((b) => slots.push({ x: b.x + (b.a === 'start' ? 40 : -40), y: b.y + k * step, a: b.a })),
    );
    slots.push({ x: cx, y: cy - 22, a: 'middle' }, { x: cx, y: cy + 24, a: 'middle' });
    const box = (slot: Slot): Box => {
      const x0 = slot.a === 'end' ? slot.x - width : slot.a === 'middle' ? slot.x - width / 2 : slot.x;
      return { x0, x1: x0 + width, y0: slot.y - 11, y1: slot.y - 11 + lineHeight };
    };
    const inPlot = (b: Box) => b.x0 >= X0 + 4 && b.x1 <= X1 - 4 && b.y0 >= Y0 - 14 && b.y1 <= Y1 + 8;
    const hits = (b: Box) =>
      placed.some((q) => b.x0 < q.x1 && b.x1 > q.x0 && b.y0 < q.y1 && b.y1 > q.y0) ||
      points.some(
        (o) => o !== point && o.cx > b.x0 - 14 && o.cx < b.x1 + 14 && o.cy > b.y0 - 14 && o.cy < b.y1 + 14,
      );
    const slot = slots.find((s) => inPlot(box(s)) && !hits(box(s))) ?? slots.find((s) => inPlot(box(s))) ?? slots[0];
    const show = !dense || front.includes(row);
    const b = box(slot);
    if (show) placed.push(b);
    const offset = Math.abs(slot.y - (cy - 3)) > 6 || slot.a === 'middle';
    const ax = slot.a === 'end' ? b.x1 + 4 : slot.a === 'start' ? b.x0 - 4 : cx;
    const ay = slot.a === 'middle' ? (slot.y < cy ? b.y1 : b.y0) : (b.y0 + b.y1) / 2;
    return { slot, title, sub, show, offset, ax, ay };
  });

  const active = pointer?.id === activeId ? points.find((point) => point.row.id === activeId) : undefined;
  useLayoutEffect(() => {
    if (!active || !pointer || !plotRef.current || !tooltipRef.current) return;
    const plot = plotRef.current.getBoundingClientRect();
    const tooltip = tooltipRef.current.getBoundingClientRect();
    const above = pointer.y - tooltip.height - 12;
    setTooltipPosition({
      left: Math.max(8, Math.min(pointer.x - tooltip.width / 2, plot.width - tooltip.width - 8)),
      top: Math.max(8, Math.min(above >= 8 ? above : pointer.y + 12, plot.height - tooltip.height - 8)),
    });
  }, [pointer, activeId, metric, Boolean(active)]);

  return (
    <div className="chart-stage">
      <div className="mb-2 flex flex-wrap items-end justify-between gap-4 border-b border-ink pb-5">
        <div>
          <h3 className="mb-1 text-md font-semibold tracking-[-0.01em]">Accuracy vs. {metric}</h3>
          <p className="text-xs text-muted">
            {rows.length} of {allRows.length} configurations shown
            {rows.length < selectedCount && ` (${selectedCount - rows.length} without ${metric} data)`}
          </p>
        </div>
        <div className="segmented" role="group" aria-label="Chart metric">
          <button type="button" aria-pressed={isCost} onClick={() => onMetric('cost')}>
            Cost
          </button>
          <button type="button" aria-pressed={!isCost} onClick={() => onMetric('latency')}>
            Latency
          </button>
        </div>
      </div>
      <div className="mb-4 flex flex-wrap gap-4 text-xs text-muted" aria-label="Result sources">
        <span>● Official</span><span>◆ Self-submitted · Unverified</span><span>Dashed line: official Pareto frontier</span>
      </div>
      <div ref={plotRef} className="chart-plot relative mb-16 min-h-[200px]" onMouseLeave={hideTooltip}>
        {rows.length === 0 ? <p className="flex min-h-[200px] items-center justify-center text-sm text-muted">
          {selectedCount ? `No ${metric} data available` : 'No configurations selected'}
        </p> : <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          className="block h-auto w-full font-mono"
          role="img"
          aria-label={`Accuracy versus ${metric}${axis.log ? ' (log scale)' : ''}. Accuracy axis runs from ${yAxis.min}% to ${yAxis.max}%. Higher accuracy and lower ${metric} are better.`}
        >
          <rect x={X0} y={Y0} width={midX - X0} height={midY - Y0} fill="#2FA98C" opacity={0.08} />
          <text x={X0 + 10} y={Y0 + 10} fill="#1E7A62" fontSize={11} fontWeight={600} dominantBaseline="hanging" className="font-sans">
            Most attractive quadrant
          </text>
          {yAxis.ticks.map((tick) => (
            <g key={tick} className="chart-y-tick">
              <line x1={X0} x2={X1} y1={sy(tick)} y2={sy(tick)} stroke="rgba(10,10,10,0.08)" />
              <text x={X0 - 10} y={sy(tick)} fill="#6E6A62" fontSize={11} textAnchor="end" dominantBaseline="middle">
                {formatAxisTick(tick)}%
              </text>
            </g>
          ))}
          {axis.ticks.map((value, i) => {
            const x = sx(value);
            return (
              <g key={value} className="chart-x-tick">
                <line x1={x} x2={x} y1={Y0} y2={Y1} stroke="rgba(10,10,10,0.08)" />
                <text x={x} y={460} fill="#6E6A62" fontSize={11} textAnchor={i === axis.ticks.length - 1 ? 'end' : i === 0 ? 'start' : 'middle'}>
                  {formatX(value)}
                </text>
              </g>
            );
          })}
          <line x1={X0} x2={X1} y1={Y1} y2={Y1} stroke="rgba(10,10,10,0.3)" />
          <text x={X1} y={478} fill="#6E6A62" fontSize={11} textAnchor="end">
            {isCost ? `Total cost (USD${axis.log ? ', log scale' : ''}) →` : 'Median latency (s) →'}
          </text>
          <text x={X0} y={6} fill="#6E6A62" fontSize={11} dominantBaseline="hanging">
            Accuracy ↑
          </text>
          {front.length > 1 && (
            <path d={frontierPath} className="chart-frontier" fill="none" stroke="#1E7A62" strokeWidth={1.5} strokeDasharray="5 4" />
          )}
          {points.map((point, index) => {
            const { row, cx, cy } = point;
            const label = labels[index];
            const ring = colorFor(row, 'memory');
            const hovered = activeId === row.id;
            const logo = memoryLogos[row.memory.name];
            const harnessLogo = harnessLogos[row.harness.name];
            const clipId = `${clipBase}-${index}`;
            return (
              <g key={row.id}>
                {label.show && label.offset && (
                  <line x1={cx} y1={cy} x2={label.ax} y2={label.ay} stroke="rgba(10,10,10,0.35)" strokeWidth={1} />
                )}
                <g
                  className="chart-marker"
                  data-source={row.submission ? 'self-submitted' : 'official'}
                  tabIndex={0}
                  role="button"
                  aria-label={`${row.submission ? `${row.submission.name}, Self-submitted · Unverified` : 'Official'}, ${row.memory.name}, ${row.model.name}, ${row.harness.name}: ${percent(row.pass_rate)} accuracy, ${formatTotalCost(row)} total cost, ${formatLatency(row.median_latency_seconds)} median latency`}
                  style={{ cursor: 'pointer' }}
                  aria-describedby={active?.row.id === row.id ? `${clipBase}-tooltip` : undefined}
                  onMouseEnter={event => showTooltip(row.id, event.clientX, event.clientY)}
                  onMouseMove={event => showTooltip(row.id, event.clientX, event.clientY)}
                  onMouseLeave={hideTooltip}
                  onMouseDown={event => event.preventDefault()}
                  onClick={event => {
                    if (event.detail) showTooltip(row.id, event.clientX, event.clientY);
                  }}
                  onKeyDown={event => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      const bounds = event.currentTarget.getBoundingClientRect();
                      showTooltip(row.id, bounds.x + bounds.width / 2, bounds.y);
                    }
                    if (event.key === 'Escape') hideTooltip();
                  }}
                  onFocus={event => {
                    const bounds = event.currentTarget.getBoundingClientRect();
                    showTooltip(row.id, bounds.x + bounds.width / 2, bounds.y);
                  }}
                  onBlur={hideTooltip}
                >
                  <defs>
                    <clipPath id={clipId}>
                      <circle cx={cx} cy={cy} r={R - 2.5} />
                    </clipPath>
                  </defs>
                  {hovered && <circle cx={cx} cy={cy} r={R + 5} fill={ring} opacity={0.18} />}
                  {row.submission ? <path d={`M ${cx} ${cy - R - 3} l ${R + 3} ${R + 3} l ${-R - 3} ${R + 3} l ${-R - 3} ${-R - 3} Z`} fill="#FFFFFF" stroke={ring} strokeWidth={hovered ? 2.5 : 2} />
                    : <circle cx={cx} cy={cy} r={R} fill="#FFFFFF" stroke={ring} strokeWidth={hovered ? 2.5 : 2} />}
                  {logo ? (
                    <image
                      href={logo}
                      x={cx - (R - 4)}
                      y={cy - (R - 4)}
                      width={(R - 4) * 2}
                      height={(R - 4) * 2}
                      clipPath={`url(#${clipId})`}
                      preserveAspectRatio="xMidYMid meet"
                    />
                  ) : (
                    <text x={cx} y={cy + 0.5} fill="#6E6A62" fontSize={11} fontWeight={600} textAnchor="middle" dominantBaseline="middle" className="font-sans">
                      {row.memory.name.charAt(0)}
                    </text>
                  )}
                  <circle cx={cx + R - 3} cy={cy + R - 3} r={6.5} fill="#FFFFFF" stroke="rgba(10,10,10,0.25)" strokeWidth={1} />
                  {harnessLogo ? (
                    <image href={harnessLogo} x={cx + R - 3 - 4.5} y={cy + R - 3 - 4.5} width={9} height={9} preserveAspectRatio="xMidYMid meet" />
                  ) : (
                    <text x={cx + R - 3} y={cy + R - 3 + 0.5} fill="#6E6A62" fontSize={7} fontWeight={600} textAnchor="middle" dominantBaseline="middle" className="font-sans">
                      {row.harness.name.charAt(0)}
                    </text>
                  )}
                </g>
                {label.show && (
                  <text x={label.slot.x} y={label.slot.y} fill="#0A0A0A" fontSize={12} fontWeight={600} textAnchor={label.slot.a} className="font-sans">
                    {label.title}
                  </text>
                )}
                {label.show && label.sub && (
                  <text x={label.slot.x} y={label.slot.y + 14} fill="#6E6A62" fontSize={10} textAnchor={label.slot.a}>
                    {label.sub}
                  </text>
                )}
              </g>
            );
          })}
        </svg>}
        {active && (
          <div
            ref={tooltipRef}
            id={`${clipBase}-tooltip`}
            role="tooltip"
            className="chart-tooltip"
            style={tooltipPosition}
          >
            <div className="mb-0.5 text-sm font-semibold">{active.row.submission?.name || active.row.memory.name}</div>
            <div className="mb-1 text-xs text-paper/80">{active.row.submission ? 'Self-submitted · Unverified' : 'Official'}</div>
            {active.row.submission && <div>{active.row.memory.name}</div>}
            <div className="text-paper/80">
              {active.row.harness.name} + {active.row.model.name} ({active.row.model.provider})
            </div>
            <div className="mt-1.5 flex flex-wrap gap-x-3.5 gap-y-1 font-mono">
              <span>{percent(active.row.pass_rate)} acc</span>
              <span>{formatTotalCost(active.row)} total cost</span>
              <span>{formatLatency(active.row.median_latency_seconds)} median</span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
