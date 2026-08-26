import React from 'react';
import { PRODUCT_NAME } from '@opsai/shared';

export default function HomePage() {
  return (
    <main className="min-h-screen bg-slate-950 px-6 py-12 text-slate-100">
      <div className="mx-auto flex min-h-[80vh] max-w-5xl flex-col justify-center">
        <p className="mb-4 text-sm font-medium uppercase tracking-[0.3em] text-cyan-400">
          Foundation ready
        </p>
        <h1 className="text-4xl font-semibold tracking-tight sm:text-6xl">
          {PRODUCT_NAME}
        </h1>
        <p className="mt-6 max-w-2xl text-lg leading-8 text-slate-300">
          A dependable foundation for enterprise AI operations, ready for the
          next layer of capabilities.
        </p>
      </div>
    </main>
  );
}
