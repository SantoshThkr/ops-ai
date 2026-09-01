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
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ items: [], page: 1, page_size: 20, total: 0 }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ items: [], page: 1, page_size: 20, total: 0 }),
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
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(3);
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

  it('streams chat responses and renders citations', async () => {
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
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ items: [], page: 1, page_size: 20, total: 0 }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          items: [{ id: 'conversation-id', title: 'Project summary', created_at: '2026-08-28T00:00:00Z', updated_at: '2026-08-28T00:00:00Z', user_id: 'user-id' }],
          page: 1,
          page_size: 20,
          total: 1,
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          id: 'conversation-id',
          title: 'Project summary',
          created_at: '2026-08-28T00:00:00Z',
          updated_at: '2026-08-28T00:00:00Z',
          user_id: 'user-id',
          messages: [],
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        body: new ReadableStream({
          start(controller) {
            controller.enqueue(
              new TextEncoder().encode(
                'event: token\ndata: {"text":"Hello "}\n\n' +
                  'event: token\ndata: {"text":"world"}\n\n' +
                  'event: citation\ndata: {"citations":[{"document_id":"doc-1","filename":"resume.pdf","page_number":1}]}\n\n' +
                  'event: done\ndata: {"status":"completed","citations":[{"document_id":"doc-1","filename":"resume.pdf","page_number":1}]}\n\n',
              ),
            );
            controller.close();
          },
        }),
      } as Response);

    render(<HomePage />);

    const email = await screen.findByLabelText('Email');
    fireEvent.change(email, { target: { value: 'user@example.com' } });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'correct horse' },
    });
    fireEvent.submit(screen.getByRole('button', { name: 'Sign in' }).closest('form')!);

    await waitFor(() => {
      expect(screen.getByText('Conversations')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText('Chat message'), {
      target: { value: 'Who is this person?' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    await waitFor(() => {
      expect(screen.getByText('Hello world')).toBeInTheDocument();
    });
    expect(screen.getByText('Sources')).toBeInTheDocument();
    expect(screen.getByText(/resume.pdf/)).toBeInTheDocument();
  });

  it('renders unique page-level citations from repeated SSE events', async () => {
    vi.mocked(fetch)
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
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ items: [], page: 1, page_size: 20, total: 0 }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          items: [{ id: 'conversation-id', title: 'Project summary', created_at: '2026-08-28T00:00:00Z', updated_at: '2026-08-28T00:00:00Z', user_id: 'user-id' }],
          page: 1,
          page_size: 20,
          total: 1,
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          id: 'conversation-id',
          title: 'Project summary',
          created_at: '2026-08-28T00:00:00Z',
          updated_at: '2026-08-28T00:00:00Z',
          user_id: 'user-id',
          messages: [],
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        body: new ReadableStream({
          start(controller) {
            controller.enqueue(
              new TextEncoder().encode(
                'event: token\ndata: {"text":"Answer "}\n\n' +
                  'event: citation\ndata: {"citations":[{"document_id":"doc-1","filename":"31Aug2026.pdf","page_number":1},{"document_id":"doc-2","filename":"27Aug2026.docx.pdf","page_number":1},{"document_id":"doc-2","filename":"27Aug2026.docx.pdf","page_number":2},{"document_id":"doc-1","filename":"31Aug2026.pdf","page_number":2},{"document_id":"doc-1","filename":"31Aug2026.pdf","page_number":1}]}\n\n' +
                  'event: done\ndata: {"status":"completed","citations":[{"document_id":"doc-1","filename":"31Aug2026.pdf","page_number":1},{"document_id":"doc-2","filename":"27Aug2026.docx.pdf","page_number":1},{"document_id":"doc-2","filename":"27Aug2026.docx.pdf","page_number":2},{"document_id":"doc-1","filename":"31Aug2026.pdf","page_number":2},{"document_id":"doc-1","filename":"31Aug2026.pdf","page_number":1}]}\n\n',
              ),
            );
            controller.close();
          },
        }),
      } as Response);

    render(<HomePage />);

    await waitFor(() => {
      expect(screen.getAllByText('Project summary').length).toBeGreaterThan(0);
    });

    fireEvent.change(screen.getByLabelText('Chat message'), {
      target: { value: 'What did the documents say?' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    await waitFor(() => {
      expect(screen.getByText('Answer')).toBeInTheDocument();
    });
    expect(screen.getByText('Sources')).toBeInTheDocument();
    expect(
      screen.getAllByRole('listitem').map((item) => item.textContent?.replace(/\s+/g, ' ').trim()),
    ).toEqual([
      '1. 31Aug2026.pdf — Page 1',
      '2. 27Aug2026.docx.pdf — Page 1',
      '3. 27Aug2026.docx.pdf — Page 2',
      '4. 31Aug2026.pdf — Page 2',
    ]);
    expect(screen.getAllByText(/31Aug2026.pdf/)).toHaveLength(2);
    expect(screen.getAllByText(/27Aug2026.docx.pdf/)).toHaveLength(2);
  });

  it('shows the no-context fallback and no sources for weak retrieval', async () => {
    vi.mocked(fetch)
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
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ items: [], page: 1, page_size: 20, total: 0 }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          items: [{ id: 'conversation-id', title: 'Project summary', created_at: '2026-08-28T00:00:00Z', updated_at: '2026-08-28T00:00:00Z', user_id: 'user-id' }],
          page: 1,
          page_size: 20,
          total: 1,
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          id: 'conversation-id',
          title: 'Project summary',
          created_at: '2026-08-28T00:00:00Z',
          updated_at: '2026-08-28T00:00:00Z',
          user_id: 'user-id',
          messages: [],
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        body: new ReadableStream({
          start(controller) {
            controller.enqueue(
              new TextEncoder().encode(
                'event: token\ndata: {"text":"I couldn\'t find enough information in your uploaded documents to answer that."}\n\n' +
                  'event: done\ndata: {"status":"completed"}\n\n',
              ),
            );
            controller.close();
          },
        }),
      } as Response);

    render(<HomePage />);

    await waitFor(() => {
      expect(screen.getAllByText('Project summary').length).toBeGreaterThan(0);
    });

    fireEvent.change(screen.getByLabelText('Chat message'), {
      target: { value: 'how are u' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    await waitFor(() => {
      expect(
        screen.getByText(/I couldn't find enough information in your uploaded documents to answer that\./i),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText('Sources')).not.toBeInTheDocument();
  });
});
