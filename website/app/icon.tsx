import { ImageResponse } from 'next/og';
import { dolphinTileSvg } from '@/lib/brand';

export const size = { width: 64, height: 64 };
export const contentType = 'image/png';

export default function Icon() {
  const svg = `data:image/svg+xml;base64,${Buffer.from(dolphinTileSvg(size.width)).toString('base64')}`;
  return new ImageResponse(
    <img src={svg} width={size.width} height={size.height} alt="" style={{ display: 'flex' }} />,
    size,
  );
}
