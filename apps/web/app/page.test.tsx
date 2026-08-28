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
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

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

  it('resets the form after successful authentication', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce({
        ok: false,
        status: 401,
        json: async () => ({}),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          user: {
            id: 'user-id',
            email: 'user@example.com',
            name: 'User',
            role: 'viewer',
            active: true,
          },
        }),
      } as Response);

    render(<HomePage />);
    const email = await screen.findByLabelText('Email');
    const password = screen.getByLabelText('Password');
    fireEvent.change(email, { target: { value: 'user@example.com' } });
    fireEvent.change(password, { target: { value: 'correct horse' } });
    fireEvent.submit(password.closest('form')!);

    await waitFor(() => {
      expect(screen.getByText('user@example.com')).toBeInTheDocument();
    });
    expect(email).toHaveValue('');
    expect(password).toHaveValue('');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('requires a document selection before uploading', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        id: 'user-id',
        email: 'user@example.com',
        name: 'User',
        role: 'viewer',
        active: true,
      }),
    } as Response);
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ items: [], page: 1, page_size: 20, total: 0 }),
    } as Response);
    render(<HomePage />);
    const fileInput = await screen.findByLabelText('Document file');
    Object.defineProperty(fileInput, 'files', {
      value: [new File(['binary'], 'malware.exe')],
    });
    fireEvent.change(fileInput);
    fireEvent.click(screen.getByRole('button', { name: 'Upload' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Choose a PDF, TXT, or Markdown file.',
    );
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(2);
  });

  it('resets the upload form after a successful async upload', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          id: 'user-id',
          email: 'user@example.com',
          name: 'User',
          role: 'viewer',
          active: true,
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ items: [], page: 1, page_size: 20, total: 0 }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 201,
        json: async () => ({
          id: 'document-id',
          filename: 'notes.txt',
          content_type: 'text/plain',
          file_size: 5,
          status: 'uploaded',
          error_message: null,
          created_at: '2026-08-28T00:00:00Z',
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          items: [
            {
              id: 'document-id',
              filename: 'notes.txt',
              content_type: 'text/plain',
              file_size: 5,
              status: 'completed',
              error_message: null,
              created_at: '2026-08-28T00:00:00Z',
            },
          ],
          page: 1,
          page_size: 20,
          total: 1,
        }),
      } as Response);

    render(<HomePage />);
    const fileInput = await screen.findByLabelText('Document file');
    await screen.findByText('No documents uploaded yet.');
    const file = new File(['notes'], 'notes.txt', { type: 'text/plain' });
    Object.defineProperty(fileInput, 'files', { value: [file] });
    vi.spyOn(FormData.prototype, 'get').mockImplementation((name) =>
      name === 'file' ? file : null,
    );
    const resetSpy = vi.spyOn(HTMLFormElement.prototype, 'reset');
    fireEvent.change(fileInput);
    fireEvent.submit(fileInput.closest('form')!);

    await waitFor(() => {
      expect(
        screen.getByText('Upload accepted. Processing will begin shortly.'),
      ).toBeInTheDocument();
    });
    expect(resetSpy).toHaveBeenCalledOnce();
    expect(screen.getByText('notes.txt')).toBeInTheDocument();
  });
});
