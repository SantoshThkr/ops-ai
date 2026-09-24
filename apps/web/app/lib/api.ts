export type Role = 'admin' | 'analyst' | 'viewer';

export type User = {
  id: string;
  email: string;
  name: string;
  role: Role;
  active: boolean;
};

export type AuthResponse = {
  user: User;
};

export type DocumentStatus = 'uploaded' | 'processing' | 'completed' | 'failed';

export type Document = {
  id: string;
  filename: string;
  content_type: string;
  file_size: number;
  status: DocumentStatus;
  error_message: string | null;
  created_at: string;
};

export type Citation = {
  document_id: string;
  filename: string;
  chunk_id?: string;
  page_number?: number | null;
};

export type ActionStatus =
  'pending' | 'approved' | 'rejected' | 'executed' | 'expired';

export type ApprovalRequest = {
  action_id: string;
  incident_id: string;
  expires_at: string;
  kind?: string;
  parameters?: Record<string, unknown>;
};

export type ChatMessage = {
  id: string;
  conversation_id: string;
  role: 'user' | 'assistant';
  content: string;
  created_at: string;
  citations?: Citation[];
  approval?: ApprovalRequest;
};

export type Conversation = {
  id: string;
  user_id: string;
  title: string;
  created_at: string;
  updated_at: string;
};

export type ConversationDetail = Conversation & {
  messages: ChatMessage[];
};

export type IncidentAction = {
  id: string;
  incident_id: string;
  kind: string;
  parameters: Record<string, unknown>;
  status: ActionStatus;
  expires_at: string;
  executed_at: string | null;
};

export type Incident = {
  id: string;
  owner_id: string;
  title: string;
  summary: string;
  severity: string;
  status: 'proposed' | 'accepted' | 'resolved' | 'cancelled';
  created_at: string;
  actions: IncidentAction[];
};

export const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export async function apiRequest<T>(
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
    throw new ApiError(
      detail || 'Something went wrong. Please try again.',
      response.status,
    );
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function citationIdentity(citation: Citation) {
  const documentId = citation.document_id;
  if (citation.page_number !== undefined && citation.page_number !== null) {
    return `${documentId}:page:${citation.page_number}`;
  }
  if (citation.chunk_id) {
    return `${documentId}:chunk:${citation.chunk_id}`;
  }
  return documentId;
}

export function deduplicateCitations(citations: Citation[]) {
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

export type SseEvent = { event: string; data: unknown };

/** Parse one server-sent event block ("event: x\ndata: {...}"). */
export function parseSseBlock(block: string): SseEvent | null {
  let event = '';
  const dataLines: string[] = [];
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) {
      event = line.slice('event:'.length).trim();
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice('data:'.length).replace(/^ /, ''));
    }
  }
  if (!event || dataLines.length === 0) return null;
  try {
    return { event, data: JSON.parse(dataLines.join('\n')) };
  } catch {
    return null;
  }
}

export type StreamHandlers = {
  onToken: (text: string) => void;
  onCitations: (citations: Citation[]) => void;
  onToolActivity: (activity: string) => void;
  onApprovalRequired: (approval: ApprovalRequest) => void;
  onError: (message: string) => void;
};

type StreamPayload = {
  name?: string;
  text?: string;
  message?: string;
  status?: string;
  citations?: Citation[];
};

/**
 * Stream one chat reply. Resolves with the final status from the `done` event,
 * or `null` when the connection closed without one (a truncated stream).
 */
export async function streamChatResponse(
  conversationId: string,
  content: string,
  handlers: StreamHandlers,
): Promise<string | null> {
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
    throw new ApiError(
      typeof body.detail === 'string' ? body.detail : 'Unable to send message.',
      response.status,
    );
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error('Streaming response is unavailable.');
  }

  const decoder = new TextDecoder();
  let buffer = '';
  let finalStatus: string | null = null;

  const dispatch = (block: string) => {
    const parsed = parseSseBlock(block);
    if (!parsed) return;
    const payload = (parsed.data ?? {}) as StreamPayload;
    switch (parsed.event) {
      case 'tool_call':
        handlers.onToolActivity(`Calling ${payload.name ?? 'tool'}…`);
        break;
      case 'tool_result':
        handlers.onToolActivity(`${payload.name ?? 'Tool'} completed`);
        break;
      case 'approval_required':
        handlers.onApprovalRequired(parsed.data as ApprovalRequest);
        break;
      case 'token':
        handlers.onToken(payload.text ?? '');
        break;
      case 'citation':
        handlers.onCitations(deduplicateCitations(payload.citations ?? []));
        break;
      case 'done':
        finalStatus = payload.status ?? 'completed';
        if (payload.citations) {
          handlers.onCitations(deduplicateCitations(payload.citations));
        }
        break;
      case 'error':
        handlers.onError(payload.message ?? 'Unable to stream the response.');
        break;
      default:
        break;
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split('\n\n');
    buffer = blocks.pop() ?? '';
    blocks.forEach(dispatch);
  }
  buffer += decoder.decode();
  if (buffer.trim()) dispatch(buffer);
  return finalStatus;
}
