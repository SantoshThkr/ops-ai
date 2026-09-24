import { vi } from 'vitest';

export type Reply = { status?: number; body?: unknown; stream?: string };

export const emptyPage = { items: [], page: 1, page_size: 20, total: 0 };

export function userFixture(role: 'admin' | 'analyst' | 'viewer') {
  return {
    id: `${role}-id`,
    email: `${role}@example.com`,
    name: role[0].toUpperCase() + role.slice(1),
    role,
    active: true,
  };
}

export function conversationFixture(title = 'Project summary') {
  return {
    id: 'conversation-id',
    user_id: 'user-id',
    title,
    created_at: '2026-08-28T00:00:00Z',
    updated_at: '2026-08-28T00:00:00Z',
  };
}

/** Encode server-sent events the way the API streams them. */
export function sse(...events: Array<[string, unknown]>) {
  return events
    .map(([name, data]) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`)
    .join('');
}

function jsonResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

function streamResponse(text: string) {
  return {
    ok: true,
    status: 200,
    body: new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(text));
        controller.close();
      },
    }),
  } as Response;
}

/**
 * Route fetch calls by "METHOD /path". A list of replies is consumed in order and
 * its last entry repeats. Unrouted calls get a 404 so gaps show up in assertions.
 */
export function mockApi(routes: Record<string, Reply | Reply[]>) {
  const queues = new Map(
    Object.entries(routes).map(([key, reply]) => [
      key,
      Array.isArray(reply) ? [...reply] : [reply],
    ]),
  );
  const calls: string[] = [];
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const key = `${(init?.method ?? 'GET').toUpperCase()} ${new URL(String(input)).pathname}`;
    calls.push(key);
    const queue = queues.get(key);
    if (!queue) return jsonResponse(404, { detail: `Unmocked ${key}` });
    const reply = queue.length > 1 ? queue.shift()! : queue[0];
    return reply.stream !== undefined
      ? streamResponse(reply.stream)
      : jsonResponse(reply.status ?? 200, reply.body ?? {});
  });
  return calls;
}
