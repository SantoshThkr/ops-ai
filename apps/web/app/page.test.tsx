import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  conversationFixture,
  emptyPage,
  mockApi,
  sse,
  userFixture,
} from '../test/mockApi';
import HomePage from './page';

const viewer = userFixture('viewer');
const analyst = userFixture('analyst');
const admin = userFixture('admin');

function signedIn(
  user: ReturnType<typeof userFixture>,
  routes: Parameters<typeof mockApi>[0] = {},
) {
  return mockApi({
    'GET /me': { body: user },
    'GET /documents': { body: emptyPage },
    'GET /conversations': {
      body: { ...emptyPage, items: [conversationFixture()], total: 1 },
    },
    'GET /conversations/conversation-id': {
      body: { ...conversationFixture(), messages: [] },
    },
    'GET /incidents': { body: [] },
    ...routes,
  });
}

async function send(text: string) {
  await screen.findByRole('heading', { name: 'Project summary' });
  fireEvent.change(screen.getByLabelText('Chat message'), {
    target: { value: text },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
}

function analystIncident(
  status: 'pending' | 'approved' | 'rejected' | 'executed',
) {
  return {
    id: 'incident-id',
    owner_id: 'analyst-id',
    title: 'API error rate is elevated',
    summary: 'Restart the api',
    severity: 'high',
    status: 'proposed',
    created_at: '2026-08-28T00:00:00Z',
    actions: [
      {
        id: 'action-id',
        incident_id: 'incident-id',
        kind: 'restart_service',
        parameters: { service: 'api' },
        status,
        expires_at: '2026-08-28T01:00:00Z',
        executed_at: null,
      },
    ],
  };
}

const approvalStream = sse(
  ['tool_call', { name: 'create_incident' }],
  [
    'approval_required',
    {
      action_id: 'action-id',
      incident_id: 'incident-id',
      kind: 'restart_service',
      parameters: { service: 'api' },
      expires_at: '2026-08-28T01:00:00Z',
    },
  ],
  ['token', { text: 'I created an incident proposal.' }],
  ['done', { status: 'completed' }],
);

describe('HomePage', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.useRealTimers();
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
    mockApi({
      'GET /me': { status: 401 },
      'POST /auth/login': { body: { user: viewer } },
      'GET /documents': { body: emptyPage },
      'GET /conversations': { body: emptyPage },
    });

    render(<HomePage />);
    const email = await screen.findByLabelText('Email');
    const password = screen.getByLabelText('Password');
    fireEvent.change(email, { target: { value: 'viewer@example.com' } });
    fireEvent.change(password, { target: { value: 'correct horse' } });
    fireEvent.submit(password.closest('form')!);

    await waitFor(() => {
      expect(screen.getByText('viewer@example.com')).toBeInTheDocument();
    });
    expect(email).toHaveValue('');
    expect(password).toHaveValue('');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('requires a document selection before uploading', async () => {
    const calls = signedIn(viewer, {
      'GET /conversations': { body: emptyPage },
    });
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
    expect(calls).not.toContain('POST /documents');
  });

  it('resets the upload form after a successful async upload', async () => {
    const uploaded = {
      id: 'document-id',
      filename: 'notes.txt',
      content_type: 'text/plain',
      file_size: 5,
      status: 'completed',
      error_message: null,
      created_at: '2026-08-28T00:00:00Z',
    };
    signedIn(viewer, {
      'GET /documents': [
        { body: emptyPage },
        { body: { ...emptyPage, items: [uploaded], total: 1 } },
      ],
      'POST /documents': {
        status: 201,
        body: { ...uploaded, status: 'uploaded' },
      },
    });

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

    expect(
      await screen.findByText(
        'Upload accepted. Processing will begin shortly.',
      ),
    ).toHaveAttribute('role', 'status');
    expect(resetSpy).toHaveBeenCalledOnce();
    expect(await screen.findByText('notes.txt')).toBeInTheDocument();
  });

  it('refreshes document status while processing is in progress', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const document = {
      id: 'document-id',
      filename: 'runbook.pdf',
      content_type: 'application/pdf',
      file_size: 2048,
      error_message: null,
      created_at: '2026-08-28T00:00:00Z',
    };
    signedIn(viewer, {
      'GET /documents': [
        {
          body: {
            ...emptyPage,
            items: [{ ...document, status: 'processing' }],
            total: 1,
          },
        },
        {
          body: {
            ...emptyPage,
            items: [{ ...document, status: 'completed' }],
            total: 1,
          },
        },
      ],
    });

    render(<HomePage />);
    expect(await screen.findByText('processing')).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(await screen.findByText('completed')).toBeInTheDocument();
  });

  it('streams chat responses and renders citations', async () => {
    signedIn(viewer, {
      'POST /conversations/conversation-id/messages': {
        stream: sse(
          ['tool_call', { name: 'search_knowledge' }],
          ['tool_result', { name: 'search_knowledge' }],
          ['token', { text: 'Hello ' }],
          ['token', { text: 'world' }],
          [
            'citation',
            {
              citations: [
                {
                  document_id: 'doc-1',
                  filename: 'resume.pdf',
                  page_number: 1,
                },
              ],
            },
          ],
          [
            'done',
            {
              status: 'completed',
              citations: [
                {
                  document_id: 'doc-1',
                  filename: 'resume.pdf',
                  page_number: 1,
                },
              ],
            },
          ],
        ),
      },
    });

    render(<HomePage />);
    await send('Who is this person?');

    expect(await screen.findByText('Hello world')).toBeInTheDocument();
    expect(screen.getByText('Sources')).toBeInTheDocument();
    expect(screen.getByText(/resume.pdf/)).toBeInTheDocument();
    expect(screen.getByLabelText('Tool activity')).toHaveTextContent(
      'Calling search_knowledge',
    );
    expect(
      screen.getByRole('log', { name: 'Conversation messages' }),
    ).toHaveAttribute('aria-busy', 'false');
  });

  it('renders unique page-level citations from repeated SSE events', async () => {
    const citations = [
      { document_id: 'doc-1', filename: '31Aug2026.pdf', page_number: 1 },
      { document_id: 'doc-2', filename: '27Aug2026.docx.pdf', page_number: 1 },
      { document_id: 'doc-2', filename: '27Aug2026.docx.pdf', page_number: 2 },
      { document_id: 'doc-1', filename: '31Aug2026.pdf', page_number: 2 },
      { document_id: 'doc-1', filename: '31Aug2026.pdf', page_number: 1 },
    ];
    signedIn(viewer, {
      'POST /conversations/conversation-id/messages': {
        stream: sse(
          ['token', { text: 'Answer ' }],
          ['citation', { citations }],
          ['done', { status: 'completed', citations }],
        ),
      },
    });

    render(<HomePage />);
    await send('What did the documents say?');

    expect(await screen.findByText('Answer')).toBeInTheDocument();
    const sources = screen.getByText('Sources').parentElement!;
    expect(
      within(sources)
        .getAllByRole('listitem')
        .map((item) => item.textContent?.replace(/\s+/g, ' ').trim()),
    ).toEqual([
      '1. 31Aug2026.pdf — Page 1',
      '2. 27Aug2026.docx.pdf — Page 1',
      '3. 27Aug2026.docx.pdf — Page 2',
      '4. 31Aug2026.pdf — Page 2',
    ]);
  });

  it('renders streamed permission errors in the assistant response', async () => {
    signedIn(viewer, {
      'POST /conversations/conversation-id/messages': {
        stream: sse(
          ['tool_call', { name: 'create_incident' }],
          [
            'tool_result',
            { name: 'create_incident', result: { error: 'permission_denied' } },
          ],
          [
            'error',
            {
              message:
                'You do not have permission to create incident proposals.',
            },
          ],
          ['done', { status: 'denied' }],
        ),
      },
    });

    render(<HomePage />);
    await send('Create an incident for the api service.');

    await waitFor(() => {
      expect(
        screen.getAllByText(
          'You do not have permission to create incident proposals.',
        ),
      ).toHaveLength(2);
    });
    expect(screen.getByLabelText('Tool activity')).toHaveTextContent(
      'create_incident completed',
    );
  });

  it('shows the no-context fallback and no sources for weak retrieval', async () => {
    signedIn(viewer, {
      'POST /conversations/conversation-id/messages': {
        stream: sse(
          [
            'token',
            {
              text: "I couldn't find enough information in your uploaded documents to answer that.",
            },
          ],
          ['done', { status: 'completed' }],
        ),
      },
    });

    render(<HomePage />);
    await send('how are u');

    expect(
      await screen.findByText(/I couldn't find enough information/i),
    ).toBeInTheDocument();
    expect(screen.queryByText('Sources')).not.toBeInTheDocument();
  });

  it('reports a stream that ends without completing and re-enables sending', async () => {
    signedIn(viewer, {
      'POST /conversations/conversation-id/messages': {
        stream: sse(['token', { text: 'Partial' }]),
      },
    });

    render(<HomePage />);
    await send('Summarize the policy');

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The response ended unexpectedly',
    );
    expect(screen.getByLabelText('Chat message')).not.toBeDisabled();
  });

  it('starts a conversation automatically for the first message', async () => {
    const calls = signedIn(viewer, {
      'GET /conversations': { body: emptyPage },
      'POST /conversations': {
        status: 201,
        body: conversationFixture('New conversation'),
      },
      'POST /conversations/conversation-id/messages': {
        stream: sse(
          ['token', { text: 'Hi there' }],
          ['done', { status: 'completed' }],
        ),
      },
    });

    render(<HomePage />);
    await screen.findByText(/No conversations yet/);
    fireEvent.change(screen.getByLabelText('Chat message'), {
      target: { value: 'Hello' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByText('Hi there')).toBeInTheDocument();
    expect(calls.indexOf('POST /conversations')).toBeLessThan(
      calls.indexOf('POST /conversations/conversation-id/messages'),
    );
  });

  it('does not let an administrator approve their own proposal', async () => {
    signedIn(admin, {
      'POST /conversations/conversation-id/messages': {
        stream: approvalStream,
      },
    });

    render(<HomePage />);
    await send('Create an incident and restart the api service');

    expect(
      await screen.findByText('Awaiting approval from another administrator'),
    ).toBeInTheDocument();
    const card = screen.getByText(/Proposed action:/).parentElement!;
    expect(
      within(card).queryByRole('button', { name: 'Approve' }),
    ).not.toBeInTheDocument();
    // Withdrawing your own proposal is still allowed.
    expect(
      within(card).getByRole('button', { name: 'Reject' }),
    ).toBeInTheDocument();
  });

  it("lets an administrator approve and execute another user's proposal", async () => {
    const calls = signedIn(admin, {
      'GET /incidents': [
        { body: [analystIncident('pending')] },
        { body: [analystIncident('approved')] },
        { body: [analystIncident('executed')] },
      ],
      'POST /actions/action-id/approve': { body: { decision: 'approved' } },
      'POST /actions/action-id/execute': { body: { status: 'executed' } },
    });

    render(<HomePage />);
    const panel = await screen.findByRole('region', {
      name: 'Incidents and approvals',
    });
    expect(
      await within(panel).findByText('Awaiting administrator approval'),
    ).toBeInTheDocument();
    fireEvent.click(within(panel).getByRole('button', { name: 'Approve' }));

    expect(
      await within(panel).findByText('Approved — ready to execute'),
    ).toBeInTheDocument();
    fireEvent.click(within(panel).getByRole('button', { name: 'Execute' }));

    expect(await within(panel).findByText('Executed')).toBeInTheDocument();
    expect(
      within(panel).queryByRole('button', { name: 'Approve' }),
    ).not.toBeInTheDocument();
    expect(
      within(panel).queryByRole('button', { name: 'Execute' }),
    ).not.toBeInTheDocument();
    expect(calls).toContain('POST /actions/action-id/approve');
    expect(calls).toContain('POST /actions/action-id/execute');
  });

  it('shows approval failures and resynchronizes the action state', async () => {
    signedIn(admin, {
      // Another administrator rejected it before this click reached the server.
      'GET /incidents': [
        { body: [analystIncident('pending')] },
        { body: [analystIncident('rejected')] },
      ],
      'POST /actions/action-id/approve': {
        status: 409,
        body: { detail: 'Action has already reached a terminal status' },
      },
      'GET /actions/action-id': { body: { status: 'rejected' } },
    });

    render(<HomePage />);
    const panel = await screen.findByRole('region', {
      name: 'Incidents and approvals',
    });
    fireEvent.click(
      await within(panel).findByRole('button', { name: 'Approve' }),
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Action has already reached a terminal status',
    );
    expect(
      await within(panel).findByText('Rejected — this action will not run'),
    ).toBeInTheDocument();
    expect(
      within(panel).queryByRole('button', { name: 'Approve' }),
    ).not.toBeInTheDocument();
  });

  it('does not offer approval controls to analysts', async () => {
    signedIn(analyst, {
      'POST /conversations/conversation-id/messages': {
        stream: approvalStream,
      },
    });

    render(<HomePage />);
    await send('Create an incident and restart the api service');

    expect(
      await screen.findByText('Awaiting administrator approval'),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Approve' }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Execute' }),
    ).not.toBeInTheDocument();
  });

  it('lists proposals from other users in the administrator incidents panel', async () => {
    signedIn(admin, {
      'GET /incidents': {
        body: [
          {
            id: 'incident-id',
            owner_id: 'analyst-id',
            title: 'API error rate is elevated',
            summary: 'Restart the api',
            severity: 'high',
            status: 'proposed',
            created_at: '2026-08-28T00:00:00Z',
            actions: [
              {
                id: 'action-id',
                incident_id: 'incident-id',
                kind: 'restart_service',
                parameters: { service: 'api' },
                status: 'pending',
                expires_at: '2026-08-28T01:00:00Z',
                executed_at: null,
              },
            ],
          },
        ],
      },
    });

    render(<HomePage />);
    const panel = await screen.findByRole('region', {
      name: 'Incidents and approvals',
    });
    expect(
      await within(panel).findByText('API error rate is elevated'),
    ).toBeInTheDocument();
    expect(
      within(panel).getByText('Restart the api service'),
    ).toBeInTheDocument();
    expect(
      within(panel).getByRole('button', { name: 'Approve' }),
    ).toBeInTheDocument();
    expect(
      within(panel).getByRole('button', { name: 'Reject' }),
    ).toBeInTheDocument();
  });

  it('hides the incidents panel from viewers', async () => {
    const calls = signedIn(viewer);
    render(<HomePage />);
    await screen.findByRole('heading', { name: 'Project summary' });
    expect(
      screen.queryByRole('region', { name: 'Incidents and approvals' }),
    ).not.toBeInTheDocument();
    expect(calls).not.toContain('GET /incidents');
  });

  it('returns to sign-in when the session expires', async () => {
    signedIn(viewer, {
      'POST /conversations/conversation-id/messages': {
        status: 401,
        body: { detail: 'Invalid or missing authentication credentials' },
      },
    });

    render(<HomePage />);
    await send('What is the latency?');

    expect(
      await screen.findByRole('button', { name: 'Sign in' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Your session has expired',
    );
  });
});
