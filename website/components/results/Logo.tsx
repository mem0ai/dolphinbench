export default function Logo({
  src,
  size = 14,
  className = '',
}: {
  src?: string;
  size?: number;
  className?: string;
}) {
  if (!src) return null;
  return (
    <img
      src={src}
      alt=""
      width={size}
      height={size}
      className={`shrink-0 rounded-[3px] object-contain ${className}`}
    />
  );
}
