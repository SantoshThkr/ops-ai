import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import HomePage from './page';

describe('HomePage', () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue({ ok: false, status: 401, json: async () => ({}) }),
    );
  });

  it('renders the application name and sign-in form', async () => {
    render(<HomePage />);
    await waitFor(() => {
      expect(
        screen.getByRole('heading', { name: 'OpsAI' }),
      ).toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument();
  });

  it('offers registration with a name field', async () => {
    render(<HomePage />);
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: 'Sign in' }),
      ).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole('button', { name: /need an account/i }));
    expect(await screen.findByLabelText('Name')).toBeInTheDocument();
  });
});
