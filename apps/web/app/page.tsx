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

type Document = {
  id: string;
  filename: string;
  content_type: string;
  file_size: number;
  status: 'uploaded' | 'processing' | 'completed' | 'failed';
  error_message: string | null;
  created_at: string;
};

type Citation = {
  document_id: string;
  filename: string;
  chunk_id?: string;
  page_number?: number | null;
};

type ChatMessage = {
  id: string;
  conversation_id: string;
  role: 'user' | 'assistant';
  content: string;
  created_at: string;
  citations?: Citation[];
  toolActivity?: string[];
  approval?: ApprovalRequest;
};

type ApprovalRequest = {
  action_id: string;
  incident_id: string;
  expires_at: string;
  status?: string;
};

function citationIdentity(citation: Citation) {
  const documentId = citation.document_id;
  if (citation.page_number !== undefined && citation.page_number !== null) {
    return `${documentId}:page:${citation.page_number}`;
  }
  if (citation.chunk_id) {
    return `${documentId}:chunk:${citation.chunk_id}`;
  }
  return documentId;
}

function deduplicateCitations(citations: Citation[]) {
  const seen = new Set<string>();
  return citations.filter((citation) => {
    const identity = citationIdentity(citation);
    if (seen.has(identity)) {
      return false;
    }
    seen.add(identity);
    return true;
  });
}

type Conversation = {
  id: string;
  user_id: string;
  title: string;
  created_at: string;
  updated_at: string;
};

type ConversationDetail = Conversation & {
  messages: ChatMessage[];
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
      ...(options.body && !(options.body instanceof FormData)
        ? { 'Content-Type': 'application/json' }
        : {}),
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
        : body.detail
            ?.map((item) => item.msg)
            .filter(Boolean)
            .join(', ');
    throw new Error(detail || 'Something went wrong. Please try again.');
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function streamChatResponse(
  conversationId: string,
  content: string,
  onToken: (text: string) => void,
  onCitation: (citations: Citation[]) => void,
  onDone: (citations: Citation[]) => void,
  onError: (message: string) => void,
  onToolActivity: (activity: string) => void,
  onApprovalRequired: (approval: ApprovalRequest) => void,
) {
  const response = await fetch(
    `${API_URL}/conversations/${conversationId}/messages`,
    {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    },
  );

  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as {
      detail?: string;
    };
    throw new Error(body.detail ?? 'Unable to send message.');
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error('Streaming response is unavailable.');
  }

  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const chunks = buffer.split('\n\n');
    buffer = chunks.pop() ?? '';

    for (const chunk of chunks) {
      const lines = chunk.split('\n');
      const eventName = lines
        .find((line) => line.startsWith('event:'))
        ?.replace('event:', '')
        .trim();
      const dataLine = lines.find((line) => line.startsWith('data:'));
      if (!eventName || !dataLine) continue;
      const payload = JSON.parse(dataLine.replace('data: ', ''));

      switch (eventName) {
        case 'tool_call':
          onToolActivity(`Calling ${payload.name ?? 'tool'}…`);
          break;
        case 'tool_result':
          onToolActivity(`${payload.name ?? 'Tool'} completed`);
          break;
        case 'approval_required':
          onApprovalRequired(payload as ApprovalRequest);
          break;
        case 'token':
          onToken(payload.text ?? '');
          break;
        case 'citation':
          onCitation(deduplicateCitations(payload.citations ?? []));
          break;
        case 'done':
          onDone(deduplicateCitations(payload.citations ?? []));
          break;
        case 'error':
          onError(payload.message ?? 'Unable to stream the response.');
          break;
        default:
          break;
      }
    }
  }
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
  const [approvalRequests, setApprovalRequests] = useState<ApprovalRequest[]>(
    [],
  );

  async function loadDocuments(showError = true) {
    setDocumentsLoading(true);
    try {
      const result = await apiRequest<{ items?: Document[] }>('/documents');
      setDocuments(result?.items ?? []);
    } catch (requestError) {
      if (showError) {
        setError(
          requestError instanceof Error
            ? requestError.message
            : 'Unable to load documents.',
        );
      }
    } finally {
      setDocumentsLoading(false);
    }
  }

  const loadConversations = useCallback(async () => {
    try {
      const result = await apiRequest<{ items?: Conversation[] }>(
        '/conversations',
      );
      const items = result?.items ?? [];
      setConversations(items);
      if (items.length > 0) {
        setActiveConversationId((current) => current ?? items[0].id);
      }
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : 'Unable to load conversations.',
      );
    }
  }, []);

  async function openConversation(conversationId: string) {
    try {
      const result = await apiRequest<ConversationDetail | undefined>(
        `/conversations/${conversationId}`,
      );
      setActiveConversationId(conversationId);
      setMessages(result?.messages ?? []);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : 'Unable to open conversation.',
      );
    }
  }

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

  useEffect(() => {
    if (user) {
      void loadDocuments(false);
      void loadConversations();
    }
  }, [user, loadConversations]);

  useEffect(() => {
    if (activeConversationId && user) {
      void openConversation(activeConversationId);
    }
  }, [activeConversationId, user]);

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
      setUser(null);
      setConversations([]);
      setMessages([]);
      setActiveConversationId(null);
      setApprovalRequests([]);
      setToolActivity([]);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : 'Unable to sign out.',
      );
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
      setError(
        requestError instanceof Error
          ? requestError.message
          : 'Unable to upload document.',
      );
    } finally {
      setUploading(false);
    }
  }

  async function handleChatSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = chatInput.trim();
    if (!trimmed || chatSending || !activeConversationId) {
      return;
    }

    const currentConversationId = activeConversationId;
    const userMessage: ChatMessage = {
      id: `user-${Date.now()}`,
      conversation_id: currentConversationId,
      role: 'user',
      content: trimmed,
      created_at: new Date().toISOString(),
    };
    const assistantMessageId = `assistant-${Date.now()}`;

    setChatInput('');
    setChatSending(true);
    setError('');
    setToolActivity([]);
    setMessages((current) => [
      ...current,
      userMessage,
      {
        id: assistantMessageId,
        conversation_id: currentConversationId,
        role: 'assistant',
        content: '',
        created_at: new Date().toISOString(),
        citations: [],
      },
    ]);

    try {
      await streamChatResponse(
        currentConversationId,
        trimmed,
        (token) => {
          setMessages((current) =>
            current.map((message) =>
              message.id === assistantMessageId
                ? { ...message, content: `${message.content}${token}` }
                : message,
            ),
          );
        },
        (citations) => {
          const uniqueCitations = deduplicateCitations(citations);
          setMessages((current) =>
            current.map((message) =>
              message.id === assistantMessageId
                ? { ...message, citations: uniqueCitations }
                : message,
            ),
          );
        },
        (citations) => {
          const uniqueCitations = deduplicateCitations(citations);
          setMessages((current) =>
            current.map((message) =>
              message.id === assistantMessageId
                ? { ...message, citations: uniqueCitations }
                : message,
            ),
          );
          setChatSending(false);
          void loadConversations();
        },
        (message) => {
          setChatSending(false);
          setError(message);
          setMessages((current) =>
            current.map((item) =>
              item.id === assistantMessageId
                ? { ...item, content: message }
                : item,
            ),
          );
        },
        (activity) => setToolActivity((current) => [...current, activity]),
        (approval) => {
          setApprovalRequests((current) => [
            ...current.filter((item) => item.action_id !== approval.action_id),
            approval,
          ]);
          setMessages((current) =>
            current.map((message) =>
              message.id === assistantMessageId
                ? { ...message, approval }
                : message,
            ),
          );
        },
      );
    } catch (requestError) {
      setChatSending(false);
      setError(
        requestError instanceof Error
          ? requestError.message
          : 'Unable to send the message.',
      );
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
        Loading…
      </main>
    );
  }

  if (user) {
    return (
      <main className="min-h-screen bg-slate-950 px-6 py-12 text-slate-100">
        <div className="mx-auto max-w-6xl">
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

          <section className="mt-6 rounded-xl border border-slate-800 bg-slate-900 p-6">
            <h2 className="text-xl font-medium">Documents</h2>
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
            {uploadMessage && (
              <p className="mt-3 text-sm text-emerald-400">{uploadMessage}</p>
            )}
            {documentsLoading ? (
              <p className="mt-6 text-sm text-slate-400">Loading documents…</p>
            ) : documents.length === 0 ? (
              <p className="mt-6 text-sm text-slate-400">
                No documents uploaded yet.
              </p>
            ) : (
              <ul className="mt-6 divide-y divide-slate-800">
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
                    </div>
                    <span className="rounded-full bg-slate-800 px-3 py-1 text-xs capitalize text-cyan-300">
                      {document.status}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <div className="mt-8 grid gap-6 lg:grid-cols-[280px_minmax(0,1fr)]">
            <aside className="rounded-xl border border-slate-800 bg-slate-900 p-4">
              <div className="flex items-center justify-between gap-4">
                <h2 className="text-lg font-medium">Conversations</h2>
                <button
                  className="rounded bg-cyan-500 px-3 py-1.5 text-sm font-medium text-slate-950"
                  onClick={async () => {
                    const conversation = await createConversation();
                    setActiveConversationId(conversation.id);
                    setMessages([]);
                  }}
                >
                  New
                </button>
              </div>
              <div className="mt-4 space-y-2">
                {conversations.length === 0 ? (
                  <p className="text-sm text-slate-400">
                    No conversations yet.
                  </p>
                ) : (
                  conversations.map((conversation) => (
                    <button
                      key={conversation.id}
                      className={`block w-full rounded-lg border px-3 py-2 text-left ${
                        activeConversationId === conversation.id
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
                  ))
                )}
              </div>
            </aside>

            <section className="rounded-xl border border-slate-800 bg-slate-900 p-4">
              <div className="flex min-h-[420px] flex-col">
                <div className="mb-4 flex items-center justify-between">
                  <h2 className="text-lg font-medium">
                    {activeConversationId
                      ? (conversations.find(
                          (conversation) =>
                            conversation.id === activeConversationId,
                        )?.title ?? 'Conversation')
                      : 'Chat'}
                  </h2>
                  {activeConversationId && (
                    <span className="text-xs uppercase tracking-[0.2em] text-slate-400">
                      Live
                    </span>
                  )}
                  {approvalRequests.length > 0 && (
                    <span className="text-xs text-amber-300">
                      {approvalRequests.length} approval pending
                    </span>
                  )}
                </div>

                <div className="flex-1 space-y-4 overflow-y-auto rounded-lg border border-slate-800 bg-slate-950/50 p-4">
                  {toolActivity.length > 0 && (
                    <div
                      className="rounded-lg border border-cyan-900 bg-cyan-950/30 p-3 text-xs text-cyan-200"
                      aria-label="Tool activity"
                    >
                      <p className="mb-1 uppercase tracking-[0.2em]">
                        Tool activity
                      </p>
                      {toolActivity.map((activity, index) => (
                        <p key={`${activity}-${index}`}>{activity}</p>
                      ))}
                    </div>
                  )}
                  {messages.length === 0 ? (
                    <p className="text-sm text-slate-400">
                      Ask a question about your uploaded documents.
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
                            <div className="max-w-2xl rounded-lg border border-amber-700 bg-amber-950/30 px-3 py-2 text-sm text-amber-200">
                              {message.approval.status === 'approved'
                                ? 'Approved. '
                                : 'Approval required before this action can run. '}
                              {message.approval.status !== 'approved' &&
                                message.approval.status !== 'executed' && (
                                  <button
                                    className="rounded bg-amber-400 px-2 py-1 text-xs font-medium text-slate-950"
                                    onClick={async () => {
                                      try {
                                        await apiRequest(
                                          `/actions/${message.approval!.action_id}/approve`,
                                          {
                                            method: 'POST',
                                            headers: {
                                              'Idempotency-Key': `web-${message.approval!.action_id}`,
                                            },
                                            body: JSON.stringify({
                                              decision: 'approved',
                                            }),
                                          },
                                        );
                                        setApprovalRequests((current) =>
                                          current.filter(
                                            (item) =>
                                              item.action_id !==
                                              message.approval!.action_id,
                                          ),
                                        );
                                        setMessages((current) =>
                                          current.map((item) =>
                                            item.id === message.id
                                              ? {
                                                  ...item,
                                                  approval: {
                                                    ...message.approval!,
                                                    status: 'approved',
                                                  },
                                                }
                                              : item,
                                          ),
                                        );
                                      } catch (requestError) {
                                        setError(
                                          requestError instanceof Error
                                            ? requestError.message
                                            : 'Unable to approve action.',
                                        );
                                      }
                                    }}
                                  >
                                    Approve
                                  </button>
                                )}
                              {message.approval.status === 'approved' && (
                                <button
                                  className="ml-2 rounded bg-emerald-400 px-2 py-1 text-xs font-medium text-slate-950"
                                  onClick={async () => {
                                    try {
                                      await apiRequest(
                                        `/actions/${message.approval!.action_id}/execute`,
                                        {
                                          method: 'POST',
                                          headers: {
                                            'Idempotency-Key': `web-execute-${message.approval!.action_id}`,
                                          },
                                        },
                                      );
                                      setMessages((current) =>
                                        current.map((item) =>
                                          item.id === message.id
                                            ? {
                                                ...item,
                                                approval: {
                                                  ...message.approval!,
                                                  status: 'executed',
                                                },
                                              }
                                            : item,
                                        ),
                                      );
                                    } catch (requestError) {
                                      setError(
                                        requestError instanceof Error
                                          ? requestError.message
                                          : 'Unable to execute action.',
                                      );
                                    }
                                  }}
                                >
                                  Execute
                                </button>
                              )}
                            </div>
                          )}
                        </div>
                      );
                    })
                  )}
                </div>

                <form className="mt-4 space-y-3" onSubmit={handleChatSubmit}>
                  <textarea
                    aria-label="Chat message"
                    className="min-h-[90px] w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100"
                    placeholder="Ask a question about your uploaded documents..."
                    value={chatInput}
                    onChange={(event) => setChatInput(event.target.value)}
                    onKeyDown={handleComposerKeyDown}
                    disabled={!activeConversationId || chatSending}
                  />
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-xs text-slate-400">
                      Press Enter to send, Shift+Enter for a new line.
                    </p>
                    <button
                      type="submit"
                      className="rounded bg-cyan-500 px-4 py-2 text-sm font-medium text-slate-950 disabled:opacity-50"
                      disabled={
                        !chatInput.trim() ||
                        chatSending ||
                        !activeConversationId
                      }
                    >
                      {chatSending ? 'Sending…' : 'Send'}
                    </button>
                  </div>
                </form>
              </div>
            </section>
          </div>

          {error && (
            <p className="mt-4 text-sm text-rose-400" role="alert">
              {error}
            </p>
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
