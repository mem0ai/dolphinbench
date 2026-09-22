// Rounds a raw interval up or down to the nearest 1, 2, 5 or 10 times a power of ten, so that
// axis labels land on values a reader can do arithmetic with.
function niceStep(raw: number) {
  const power = 10 ** Math.floor(Math.log10(raw));
  const fraction = raw / power;
  return power * (fraction >= Math.sqrt(50) ? 10 : fraction >= Math.sqrt(10) ? 5 : fraction >= Math.sqrt(2) ? 2 : 1);
}

export function chartAxis(values: number[], logarithmic: boolean) {
  const valid = values.filter(value => Number.isFinite(value) && value >= 0);
  const min = valid.length ? Math.min(...valid) : 0;
  const max = valid.length ? Math.max(...valid) : 0;
  let ticks: number[];
  // A real zero cannot be placed on a log axis; retain it on a linear scale.
  const log = logarithmic && min > 0;
  if (log) {
    const lower = min / 1.1;
    const upper = max * 1.1;
    const candidates: number[] = [];
    for (let exponent = Math.floor(Math.log10(lower)); exponent <= Math.ceil(Math.log10(upper)); exponent++) {
      for (const multiplier of [1, 2, 5]) candidates.push(multiplier * 10 ** exponent);
    }
    const start = candidates.findLastIndex(value => value <= lower);
    const end = candidates.findIndex(value => value >= upper);
    ticks = candidates.slice(Math.max(0, start), end + 1);
    if (ticks.length > 10) {
      const stride = Math.ceil((ticks.length - 1) / 8);
      ticks = ticks.filter((_, index, all) => index % stride === 0 || index === all.length - 1);
    }
  } else {
    const upper = max > 0 ? max * 1.1 : 1;
    const step = niceStep(upper / 6);
    ticks = Array.from({ length: Math.ceil(upper / step) + 1 }, (_, index) => Number((index * step).toPrecision(12)));
  }
  const transform = log ? Math.log10 : (value: number) => value;
  const start = transform(ticks[0]);
  const span = transform(ticks[ticks.length - 1]) - start;
  return { ticks, log, position: (value: number) => (transform(value) - start) / span };
}

export const formatAxisTick = (value: number) => value.toLocaleString('en-US', { maximumSignificantDigits: 6 });

// The accuracy axis fits the results it is given rather than always spanning 0-100%, so the
// spread between configurations stays legible. Bounds round outward to whole tick steps and
// are clamped to the 0-100% a pass rate can occupy.
export function percentAxis(values: number[]) {
  const valid = values.filter(value => Number.isFinite(value));
  const low = valid.length ? Math.max(0, Math.min(...valid)) : 0;
  const high = valid.length ? Math.min(100, Math.max(...valid)) : 100;
  // Results that all land on one value still need a window wide enough to read.
  const step = niceStep((high - low || 20) / 5);
  const round = (value: number) => Number(value.toPrecision(12));
  const min = round(Math.max(0, Math.floor(low / step) * step - (high === low ? step : 0)));
  const max = round(Math.min(100, Math.max(Math.ceil(high / step) * step, min + 2 * step)));
  const count = Math.max(1, Math.round((max - min) / step));
  const ticks = Array.from({ length: count + 1 }, (_, index) => round(min + index * step));
  return { min, max, ticks, position: (value: number) => (value - min) / (max - min) };
}
