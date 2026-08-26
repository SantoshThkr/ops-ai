/** @type {import('next').NextConfig} */
const nextConfig = {
  transpilePackages: ['@opsai/shared'],
  output: 'standalone',
};

export default nextConfig;
