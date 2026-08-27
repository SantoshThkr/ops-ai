'use client';

import { FormEvent, useEffect, useState } from 'react';
import React from 'react';
import { PRODUCT_NAME } from '@opsai/shared';

type User = {
  id: string;
  email: string;
  name: string;
  role: 'admin' | 'analyst' | 'viewer';
  active: boolean;
};

type AuthResponse = {
  user: User;
};

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

async function apiRequest<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...options,
    credentials: 'include',
    headers: {
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as {
      detail?: string | Array<{ msg?: string }>;
    };
    const detail =
      typeof body.detail === 'string'
        ? body.detail
        : body.detail?.map((item) => item.msg).filter(Boolean).join(', ');
    throw new Error(detail || 'Something went wrong. Please try again.');
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export default function HomePage() {
  const [user, setUser] = useState<User | null>(null);
  const [registering, setRegistering] = useState(false);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    apiRequest<User>('/me')
      .then(setUser)
      .catch(() => undefined)
      .finally(() => setLoading(false));
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    setSubmitting(true);
    const form = new FormData(event.currentTarget);
    const payload = {
      email: String(form.get('email') ?? ''),
      password: String(form.get('password') ?? ''),
      ...(registering ? { name: String(form.get('name') ?? '') } : {}),
    };
    try {
      const result = await apiRequest<AuthResponse>(
        registering ? '/auth/register' : '/auth/login',
        { method: 'POST', body: JSON.stringify(payload) },
      );
      setUser(result.user);
      event.currentTarget.reset();
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : 'Unable to sign in.',
      );
    } finally {
      setSubmitting(false);
    }
  }

  async function logout() {
    setError('');
    try {
      await apiRequest<void>('/auth/logout', { method: 'POST' });
      setUser(null);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : 'Unable to sign out.',
      );
    }
  }

  if (loading) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-slate-950 text-slate-100">
        Loading…
      </main>
    );
  }

  if (user) {
    return (
      <main className="min-h-screen bg-slate-950 px-6 py-12 text-slate-100">
        <div className="mx-auto max-w-5xl">
          <div className="flex items-start justify-between gap-6">
            <div>
              <p className="text-sm font-medium uppercase tracking-[0.3em] text-cyan-400">
                OpsAI workspace
              </p>
              <h1 className="mt-3 text-4xl font-semibold">{PRODUCT_NAME}</h1>
            </div>
            <button
              className="rounded border border-slate-600 px-4 py-2 text-sm hover:border-cyan-400"
              onClick={logout}
            >
              Log out
            </button>
          </div>
          <section className="mt-12 rounded-xl border border-slate-800 bg-slate-900 p-6">
            <p className="text-sm text-slate-400">Signed in as</p>
            <h2 className="mt-2 text-2xl font-medium">{user.name}</h2>
            <p className="mt-1 text-slate-300">{user.email}</p>
            <span className="mt-5 inline-block rounded-full bg-cyan-950 px-3 py-1 text-sm capitalize text-cyan-300">
              {user.role}
            </span>
          </section>
          {error && <p className="mt-4 text-sm text-rose-400">{error}</p>}
        </div>
      </main>
    );
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-950 px-6 py-12 text-slate-100">
      <section className="w-full max-w-md rounded-xl border border-slate-800 bg-slate-900 p-8">
        <p className="text-sm font-medium uppercase tracking-[0.3em] text-cyan-400">
          Welcome to
        </p>
        <h1 className="mt-3 text-3xl font-semibold">{PRODUCT_NAME}</h1>
        <p className="mt-2 text-slate-400">
          {registering ? 'Create your viewer account.' : 'Sign in to continue.'}
        </p>
        <form className="mt-8 space-y-4" onSubmit={submit}>
          {registering && (
            <label className="block text-sm">
              Name
              <input
                name="name"
                required
                minLength={1}
                maxLength={120}
                className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2"
              />
            </label>
          )}
          <label className="block text-sm">
            Email
            <input
              name="email"
              type="email"
              required
              className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2"
            />
          </label>
          <label className="block text-sm">
            Password
            <input
              name="password"
              type="password"
              required
              minLength={registering ? 8 : 1}
              maxLength={128}
              className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2"
            />
          </label>
          {error && (
            <p className="text-sm text-rose-400" role="alert">
              {error}
            </p>
          )}
          <button
            disabled={submitting}
            className="w-full rounded bg-cyan-500 px-4 py-2 font-medium text-slate-950 disabled:opacity-50"
          >
            {submitting
              ? 'Please wait…'
              : registering
                ? 'Create account'
                : 'Sign in'}
          </button>
        </form>
        <button
          className="mt-5 text-sm text-cyan-400 hover:text-cyan-300"
          onClick={() => {
            setRegistering(!registering);
            setError('');
          }}
        >
          {registering
            ? 'Already have an account? Sign in'
            : 'Need an account? Register'}
        </button>
      </section>
    </main>
  );
}
