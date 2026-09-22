import {
  BRAND_INK,
  BRAND_TILE,
  MARK_HEIGHT,
  MARK_PATHS,
  MARK_VIEWBOX,
  MARK_WIDTH,
  TILE_MARK_OFFSET,
  TILE_RADIUS,
  TILE_SIZE,
  TILE_VIEWBOX,
} from '@/lib/brand';

/**
 * The DolphinBench mark. `tile` draws it on the lavender app-icon square (a `size` square);
 * otherwise the bare mark is drawn `size` wide in the current text color.
 */
export default function DolphinMark({ size = 20, tile = false, className }: {
  size?: number; tile?: boolean; className?: string;
}) {
  const fill = tile ? BRAND_INK : 'currentColor';
  const paths = MARK_PATHS.map((d, index) => <path key={index} d={d} fill={fill} />);
  return (
    <svg
      width={size}
      height={tile ? size : Math.round((size * MARK_HEIGHT) / MARK_WIDTH)}
      viewBox={tile ? TILE_VIEWBOX : MARK_VIEWBOX}
      aria-hidden="true"
      className={`shrink-0 ${className ?? ''}`}
    >
      {tile ? (
        <>
          <rect width={TILE_SIZE} height={TILE_SIZE} rx={TILE_RADIUS} fill={BRAND_TILE} />
          <g transform={`translate(${TILE_MARK_OFFSET.x} ${TILE_MARK_OFFSET.y})`}>{paths}</g>
        </>
      ) : paths}
    </svg>
  );
}
