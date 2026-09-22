/** @type {import('next').NextConfig} */
const nextConfig = {
  // NOTE: no `output: 'export'` — authenticated access and activity capture require the app to
  // run on Vercel's runtime. Pages are still pre-rendered (SSG); Vercel serves
  // them through the edge so the proxy can gate every request.
  trailingSlash: true,
  images: { unoptimized: true },
  reactStrictMode: true,
  devIndicators: false,
  outputFileTracingIncludes: { '/*': ['./public/data/*.json'] },
};

module.exports = nextConfig;
