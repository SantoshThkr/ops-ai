import { defineConfig } from 'vitest/config';

export default defineConfig({
  // tsconfig keeps "jsx": "preserve" for Next.js; tests need JSX compiled.
  oxc: { jsx: { runtime: 'automatic' } },
  test: {
    environment: 'jsdom',
    setupFiles: ['./test/setup.ts'],
  },
});
