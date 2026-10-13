import path from 'node:path';
import { fileURLToPath } from 'node:url';

const repositoryRoot = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  '../..',
);
const apiOrigin = new URL(
  process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000',
).origin;
const isDevelopment = process.env.NODE_ENV !== 'production';

// Next.js hydration uses inline scripts, so script-src needs 'unsafe-inline' unless
// every page is rendered per request with a nonce. The policy still blocks framing
// (clickjacking on approval buttons), plugins, base-tag injection, foreign form
// targets, and network calls to anything but this app and the API.
const contentSecurityPolicy = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDevelopment ? " 'unsafe-eval'" : ''}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self'",
  `connect-src 'self' ${apiOrigin}${isDevelopment ? ' ws: wss:' : ''}`,
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join('; ');

const securityHeaders = [
  { key: 'Content-Security-Policy', value: contentSecurityPolicy },
  { key: 'X-Frame-Options', value: 'DENY' },
  { key: 'X-Content-Type-Options', value: 'nosniff' },
  { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
  {
    key: 'Permissions-Policy',
    value: 'camera=(), microphone=(), geolocation=()',
  },
];
if (!isDevelopment) {
  // Browsers ignore this over plain HTTP, so it is safe for local production builds
  // and takes effect once the app is served behind TLS.
  securityHeaders.push({
    key: 'Strict-Transport-Security',
    value: 'max-age=31536000',
  });
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  transpilePackages: ['@opsai/shared'],
  output: 'standalone',
  // Pin the monorepo root so a stray lockfile elsewhere cannot change the output layout.
  outputFileTracingRoot: repositoryRoot,
  // The app serves no images; turning the optimizer off removes the /_next/image endpoint.
  images: { unoptimized: true },
  poweredByHeader: false,
  async headers() {
    return [{ source: '/:path*', headers: securityHeaders }];
  },
};

export default nextConfig;
