'use client';

import {
  FormEvent,
  KeyboardEvent,
  useCallback,
  useEffect,
  useState,
} from 'react';
import React from 'react';
import { PRODUCT_NAME } from '@opsai/shared';
import { ActionControls, describeAction } from './components/ActionControls';
import { IncidentsPanel } from './components/IncidentsPanel';
import {
  ActionStatus,
  ApiError,
  apiRequest,
  AuthResponse,
  ChatMessage,
  citationIdentity,
  Conversation,
  ConversationDetail,
  deduplicateCitations,
  Document,
  Incident,
  streamChatResponse,
  User,
} from './lib/api';

const DOCUMENT_POLL_MS = 3000;
const SESSION_EXPIRED_MESSAGE = 'Your session has expired. Sign in again.';

function canOperate(user: User | null) {
  return user?.role === 'admin' || user?.role === 'analyst';
}

export default function HomePage() {
  const [user, setUser] = useState<User | null>(null);
  const [registering, setRegistering] = useState(false);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [documents, setDocuments] = useState<Document[]>([]);
  const [documentsLoading, setDocumentsLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadMessage, setUploadMessage] = useState('');
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<
    string | null
  >(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [chatInput, setChatInput] = useState('');
  const [chatSending, setChatSending] = useState(false);
  const [toolActivity, setToolActivity] = useState<string[]>([]);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [incidentsLoading, setIncidentsLoading] = useState(false);
  const [actionStatuses, setActionStatuses] = useState<
    Record<string, ActionStatus>
  >({});

  const resetSession = useCallback(() => {
    setUser(null);
    setDocuments([]);
    setConversations([]);
    setMessages([]);
    setActiveConversationId(null);
    setToolActivity([]);
    setIncidents([]);
    setActionStatuses({});
  }, []);

  const reportError = useCallback(
    (requestError: unknown, fallback: string) => {
      if (requestError instanceof ApiError && requestError.status === 401) {
        resetSession();
        setError(SESSION_EXPIRED_MESSAGE);
        return;
      }
      setError(requestError instanceof Error ? requestError.message : fallback);
    },
    [resetSession],
  );

  const loadDocuments = useCallback(
    async (quiet = false) => {
      if (!quiet) setDocumentsLoading(true);
      try {
        const result = await apiRequest<{ items?: Document[] }>('/documents');
        setDocuments(result?.items ?? []);
      } catch (requestError) {
        if (!quiet) reportError(requestError, 'Unable to load documents.');
      } finally {
        if (!quiet) setDocumentsLoading(false);
      }
    },
    [reportError],
  );

  const openConversation = useCallback(
    async (conversationId: string) => {
      try {
        const result = await apiRequest<ConversationDetail | undefined>(
          `/conversations/${conversationId}`,
        );
        setActiveConversationId(conversationId);
        setMessages(result?.messages ?? []);
      } catch (requestError) {
        reportError(requestError, 'Unable to open conversation.');
      }
    },
    [reportError],
  );

  const loadConversations = useCallback(
    async (selectFirst = false) => {
      try {
        const result = await apiRequest<{ items?: Conversation[] }>(
          '/conversations',
        );
        const items = result?.items ?? [];
        setConversations(items);
        if (selectFirst && items.length > 0) {
          await openConversation(items[0].id);
        }
      } catch (requestError) {
        reportError(requestError, 'Unable to load conversations.');
      }
    },
    [openConversation, reportError],
  );

  const loadIncidents = useCallback(async () => {
    setIncidentsLoading(true);
    try {
      const result = await apiRequest<Incident[]>('/incidents');
      const items = result ?? [];
      setIncidents(items);
      // Server state wins over anything the chat cards remembered.
      setActionStatuses((current) => ({
        ...current,
        ...Object.fromEntries(
          items.flatMap((incident) =>
            incident.actions.map((action) => [action.id, action.status]),
          ),
        ),
      }));
    } catch (requestError) {
      reportError(requestError, 'Unable to load incidents.');
    } finally {
      setIncidentsLoading(false);
    }
  }, [reportError]);

  const handleActionStatus = useCallback(
    (actionId: string, status: ActionStatus) => {
      setActionStatuses((current) => ({ ...current, [actionId]: status }));
      void loadIncidents();
    },
    [loadIncidents],
  );

  async function createConversation() {
    const result = await apiRequest<Conversation>('/conversations', {
      method: 'POST',
      body: JSON.stringify({ title: 'New conversation' }),
    });
    setConversations((current) => [result, ...current]);
    setActiveConversationId(result.id);
    setMessages([]);
    return result;
  }

  useEffect(() => {
    apiRequest<User>('/me')
      .then((nextUser) => {
        setUser(nextUser);
      })
      .catch(() => undefined)
      .finally(() => setLoading(false));
  }, []);

  const userId = user?.id;
  const operator = canOperate(user);
  useEffect(() => {
    if (!userId) return;
    void loadDocuments();
    void loadConversations(true);
    if (operator) void loadIncidents();
  }, [userId, operator, loadDocuments, loadConversations, loadIncidents]);

  // Refresh while the worker is still processing uploads.
  const processing = documents.some(
    (document) =>
      document.status === 'uploaded' || document.status === 'processing',
  );
  useEffect(() => {
    if (!userId || !processing) return;
    const timer = setTimeout(() => void loadDocuments(true), DOCUMENT_POLL_MS);
    return () => clearTimeout(timer);
  }, [userId, processing, documents, loadDocuments]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    setSubmitting(true);
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
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
      formElement.reset();
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
      resetSession();
    } catch (requestError) {
      reportError(requestError, 'Unable to sign out.');
    }
  }

  async function uploadDocument(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    setUploadMessage('');
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const file = form.get('file');
    if (!file || typeof file !== 'object' || !('name' in file) || !file.name) {
      setError('Choose a PDF, TXT, or Markdown file.');
      return;
    }
    const selectedFile = file as File;
    const extension = selectedFile.name.toLowerCase().split('.').pop();
    if (!extension || !['pdf', 'txt', 'md', 'markdown'].includes(extension)) {
      setError('Only PDF, TXT, and Markdown files are supported.');
      return;
    }
    if (selectedFile.size === 0) {
      setError('The selected file is empty.');
      return;
    }
    if (selectedFile.size > 10 * 1024 * 1024) {
      setError('The selected file is too large.');
      return;
    }
    setUploading(true);
    try {
      await apiRequest<Document>('/documents', { method: 'POST', body: form });
      setUploadMessage('Upload accepted. Processing will begin shortly.');
      formElement.reset();
      await loadDocuments();
    } catch (requestError) {
      reportError(requestError, 'Unable to upload document.');
    } finally {
      setUploading(false);
    }
  }

  async function handleChatSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = chatInput.trim();
    if (!trimmed || chatSending) {
      return;
    }

    setChatSending(true);
    setError('');
    setToolActivity([]);
    let conversationId = activeConversationId;
    try {
      if (!conversationId) {
        conversationId = (await createConversation()).id;
      }
    } catch (requestError) {
      setChatSending(false);
      reportError(requestError, 'Unable to start a conversation.');
      return;
    }

    const currentConversationId = conversationId;
    const assistantMessageId = `assistant-${Date.now()}`;
    const updateAssistant = (update: (message: ChatMessage) => ChatMessage) =>
      setMessages((current) =>
        current.map((message) =>
          message.id === assistantMessageId ? update(message) : message,
        ),
      );

    setChatInput('');
    setMessages((current) => [
      ...current,
      {
        id: `user-${Date.now()}`,
        conversation_id: currentConversationId,
        role: 'user',
        content: trimmed,
        created_at: new Date().toISOString(),
      },
      {
        id: assistantMessageId,
        conversation_id: currentConversationId,
        role: 'assistant',
        content: '',
        created_at: new Date().toISOString(),
        citations: [],
      },
    ]);

    let streamError = false;
    try {
      const finalStatus = await streamChatResponse(
        currentConversationId,
        trimmed,
        {
          onToken: (token) =>
            updateAssistant((message) => ({
              ...message,
              content: `${message.content}${token}`,
            })),
          onCitations: (citations) =>
            updateAssistant((message) => ({ ...message, citations })),
          onToolActivity: (activity) =>
            setToolActivity((current) => [...current, activity]),
          onApprovalRequired: (approval) => {
            setActionStatuses((current) => ({
              ...current,
              [approval.action_id]: 'pending',
            }));
            updateAssistant((message) => ({ ...message, approval }));
          },
          onError: (message) => {
            streamError = true;
            setError(message);
            updateAssistant((item) => ({ ...item, content: message }));
          },
        },
      );
      if (finalStatus === null && !streamError) {
        setError('The response ended unexpectedly. Please try again.');
      }
      void loadConversations();
      if (operator) void loadIncidents();
    } catch (requestError) {
      reportError(requestError, 'Unable to send the message.');
    } finally {
      setChatSending(false);
    }
  }

  const handleComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      const form = event.currentTarget.form;
      if (form) {
        form.requestSubmit();
      }
    }
  };

  if (loading) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-slate-950 text-slate-100">
        <p role="status">Loading…</p>
      </main>
    );
  }

  if (user) {
    const activeTitle = activeConversationId
      ? (conversations.find(
          (conversation) => conversation.id === activeConversationId,
        )?.title ?? 'Conversation')
      : 'New conversation';
    return (
      <main className="min-h-screen bg-slate-950 px-4 py-8 text-slate-100 sm:px-6 sm:py-10">
        <div className="mx-auto max-w-6xl">
          <header className="flex flex-wrap items-start justify-between gap-6 border-b border-slate-800 pb-6">
            <div>
              <p className="text-sm font-medium uppercase tracking-[0.3em] text-cyan-400">
                Operations workspace
              </p>
              <h1 className="mt-2 text-3xl font-semibold">{PRODUCT_NAME}</h1>
            </div>
            <div className="flex items-center gap-4">
              <div className="text-right">
                <p className="font-medium">{user.name}</p>
                <p className="text-sm text-slate-400">{user.email}</p>
              </div>
              <span className="rounded-full bg-cyan-950 px-3 py-1 text-sm capitalize text-cyan-300">
                {user.role}
              </span>
              <button
                type="button"
                className="rounded border border-slate-600 px-4 py-2 text-sm hover:border-cyan-400"
                onClick={logout}
              >
                Log out
              </button>
            </div>
          </header>

          {error && (
            <p
              className="mt-4 rounded-lg border border-rose-800 bg-rose-950/40 px-4 py-2 text-sm text-rose-200"
              role="alert"
            >
              {error}
            </p>
          )}

          <section
            aria-labelledby="documents-heading"
            className="mt-6 rounded-xl border border-slate-800 bg-slate-900 p-6"
          >
            <h2 id="documents-heading" className="text-xl font-medium">
              Documents
            </h2>
            <form
              className="mt-4 flex flex-wrap items-end gap-3"
              onSubmit={uploadDocument}
            >
              <label className="text-sm">
                Upload a document
                <input
                  aria-label="Document file"
                  className="mt-1 block text-sm text-slate-300"
                  name="file"
                  type="file"
                  accept=".pdf,.txt,.md,.markdown"
                />
              </label>
              <button
                className="rounded bg-cyan-500 px-4 py-2 font-medium text-slate-950 disabled:opacity-50"
                disabled={uploading}
              >
                {uploading ? 'Uploading…' : 'Upload'}
              </button>
            </form>
            <p className="mt-3 text-sm text-emerald-400" role="status">
              {uploadMessage}
            </p>
            {documentsLoading && documents.length === 0 ? (
              <p className="mt-3 text-sm text-slate-400">Loading documents…</p>
            ) : documents.length === 0 ? (
              <p className="mt-3 text-sm text-slate-400">
                No documents uploaded yet.
              </p>
            ) : (
              <ul className="mt-3 divide-y divide-slate-800">
                {documents.map((document) => (
                  <li
                    className="flex flex-wrap items-center justify-between gap-3 py-3"
                    key={document.id}
                  >
                    <div>
                      <p className="font-medium">{document.filename}</p>
                      <p className="text-xs text-slate-400">
                        {document.content_type} ·{' '}
                        {(document.file_size / 1024).toFixed(1)} KB ·{' '}
                        {new Date(document.created_at).toLocaleDateString()}
                      </p>
                      {document.status === 'failed' &&
                        document.error_message && (
                          <p className="text-xs text-rose-300">
                            {document.error_message}
                          </p>
                        )}
                    </div>
                    <span
                      className={`rounded-full bg-slate-800 px-3 py-1 text-xs capitalize ${
                        document.status === 'failed'
                          ? 'text-rose-300'
                          : 'text-cyan-300'
                      }`}
                    >
                      {document.status}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <div className="mt-6 grid gap-6 lg:grid-cols-[280px_minmax(0,1fr)]">
            <nav
              aria-labelledby="conversations-heading"
              className="rounded-xl border border-slate-800 bg-slate-900 p-4"
            >
              <div className="flex items-center justify-between gap-4">
                <h2 id="conversations-heading" className="text-lg font-medium">
                  Conversations
                </h2>
                <button
                  type="button"
                  className="rounded bg-cyan-500 px-3 py-1.5 text-sm font-medium text-slate-950"
                  onClick={() => {
                    createConversation().catch((requestError) =>
                      reportError(
                        requestError,
                        'Unable to start a conversation.',
                      ),
                    );
                  }}
                >
                  New
                </button>
              </div>
              <div className="mt-4 space-y-2">
                {conversations.length === 0 ? (
                  <p className="text-sm text-slate-400">
                    No conversations yet. Send a message to start one.
                  </p>
                ) : (
                  conversations.map((conversation) => {
                    const active = activeConversationId === conversation.id;
                    return (
                      <button
                        type="button"
                        key={conversation.id}
                        aria-current={active ? 'true' : undefined}
                        className={`block w-full rounded-lg border px-3 py-2 text-left ${
                          active
                            ? 'border-cyan-500 bg-cyan-950/40'
                            : 'border-slate-700 bg-slate-950/40'
                        }`}
                        onClick={() => {
                          void openConversation(conversation.id);
                        }}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <p className="truncate text-sm font-medium">
                            {conversation.title}
                          </p>
                          <span className="text-[10px] uppercase tracking-wide text-slate-400">
                            {new Date(
                              conversation.updated_at,
                            ).toLocaleDateString()}
                          </span>
                        </div>
                      </button>
                    );
                  })
                )}
              </div>
            </nav>

            <section
              aria-labelledby="chat-heading"
              className="rounded-xl border border-slate-800 bg-slate-900 p-4"
            >
              <div className="flex min-h-[420px] flex-col">
                <h2 id="chat-heading" className="mb-4 text-lg font-medium">
                  {activeTitle}
                </h2>

                <div className="flex-1 space-y-4 overflow-y-auto rounded-lg border border-slate-800 bg-slate-950/50 p-4">
                  {toolActivity.length > 0 && (
                    <section
                      className="rounded-lg border border-cyan-900 bg-cyan-950/30 p-3 text-xs text-cyan-200"
                      aria-label="Tool activity"
                    >
                      <p className="mb-1 uppercase tracking-[0.2em]">
                        Tool activity
                      </p>
                      {toolActivity.map((activity, index) => (
                        <p key={`${activity}-${index}`}>{activity}</p>
                      ))}
                    </section>
                  )}
                  <div
                    role="log"
                    aria-label="Conversation messages"
                    aria-live="polite"
                    aria-busy={chatSending}
                    className="space-y-4"
                  >
                    {messages.length === 0 ? (
                      <p className="text-sm text-slate-400">
                        Ask about your uploaded documents, check a metric (“What
                        is the latency?”), or ask to “create an incident and
                        restart the api service”.
                      </p>
                    ) : (
                      messages.map((message) => {
                        const uniqueCitations = deduplicateCitations(
                          message.citations ?? [],
                        );
                        return (
                          <div key={message.id} className="space-y-2">
                            <div
                              className={`max-w-2xl rounded-xl px-3 py-2 ${
                                message.role === 'user'
                                  ? 'ml-auto bg-cyan-600 text-slate-950'
                                  : 'bg-slate-800 text-slate-100'
                              }`}
                            >
                              <p className="whitespace-pre-wrap text-sm">
                                {message.content || '…'}
                              </p>
                            </div>
                            {uniqueCitations.length > 0 && (
                              <div className="max-w-2xl rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-300">
                                <p className="mb-2 text-xs uppercase tracking-[0.2em] text-cyan-300">
                                  Sources
                                </p>
                                <ol className="space-y-1 pl-5">
                                  {uniqueCitations.map((citation, index) => (
                                    <li key={citationIdentity(citation)}>
                                      {index + 1}. {citation.filename}
                                      {citation.page_number
                                        ? ` — Page ${citation.page_number}`
                                        : ''}
                                    </li>
                                  ))}
                                </ol>
                              </div>
                            )}
                            {message.approval && (
                              <div className="max-w-2xl space-y-2 rounded-lg border border-amber-700 bg-amber-950/30 px-3 py-2 text-sm text-amber-100">
                                <p>
                                  Proposed action:{' '}
                                  {describeAction(
                                    message.approval.kind,
                                    message.approval.parameters,
                                  )}
                                </p>
                                <ActionControls
                                  actionId={message.approval.action_id}
                                  status={
                                    actionStatuses[
                                      message.approval.action_id
                                    ] ?? 'pending'
                                  }
                                  role={user.role}
                                  ownProposal
                                  onStatusChange={handleActionStatus}
                                  onError={reportError}
                                />
                              </div>
                            )}
                          </div>
                        );
                      })
                    )}
                  </div>
                </div>

                <form className="mt-4 space-y-3" onSubmit={handleChatSubmit}>
                  <textarea
                    aria-label="Chat message"
                    className="min-h-[90px] w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100"
                    placeholder="Ask about documents, metrics, or incidents…"
                    value={chatInput}
                    onChange={(event) => setChatInput(event.target.value)}
                    onKeyDown={handleComposerKeyDown}
                    disabled={chatSending}
                  />
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-xs text-slate-400">
                      Press Enter to send, Shift+Enter for a new line.
                    </p>
                    <button
                      type="submit"
                      className="rounded bg-cyan-500 px-4 py-2 text-sm font-medium text-slate-950 disabled:opacity-50"
                      disabled={!chatInput.trim() || chatSending}
                    >
                      {chatSending ? 'Sending…' : 'Send'}
                    </button>
                  </div>
                </form>
              </div>
            </section>
          </div>

          {operator && (
            <IncidentsPanel
              incidents={incidents}
              loading={incidentsLoading}
              role={user.role}
              currentUserId={user.id}
              actionStatuses={actionStatuses}
              onRefresh={() => void loadIncidents()}
              onStatusChange={handleActionStatus}
              onError={reportError}
            />
          )}
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
                autoComplete="name"
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
              autoComplete="email"
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
              autoComplete={registering ? 'new-password' : 'current-password'}
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
          type="button"
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
