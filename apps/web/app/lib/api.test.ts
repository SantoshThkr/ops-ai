import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, parseSseBlock, streamChatResponse } from './api';

function chunkedStream(...chunks: string[]) {
  return {
    ok: true,
    status: 200,
    body: new ReadableStream({
      start(controller) {
        for (const chunk of chunks) {
          controller.enqueue(new TextEncoder().encode(chunk));
        }
        controller.close();
      },
    }),
  } as Response;
}

const handlers = () => ({
  onToken: vi.fn(),
  onCitations: vi.fn(),
  onToolActivity: vi.fn(),
  onApprovalRequired: vi.fn(),
  onError: vi.fn(),
});

describe('parseSseBlock', () => {
  it('parses events with or without a space after the field name', () => {
    expect(parseSseBlock('event: token\ndata: {"text":"a"}')).toEqual({
      event: 'token',
      data: { text: 'a' },
    });
    expect(parseSseBlock('event:done\ndata:{"status":"completed"}')).toEqual({
      event: 'done',
      data: { status: 'completed' },
    });
  });

  it('ignores incomplete or malformed blocks instead of throwing', () => {
    expect(parseSseBlock('data: {"text":"a"}')).toBeNull();
    expect(parseSseBlock('event: token')).toBeNull();
    expect(parseSseBlock('event: token\ndata: {not json')).toBeNull();
  });
});

describe('streamChatResponse', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reassembles events split across network chunks', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          chunkedStream(
            'event: token\ndata: {"te',
            'xt":"Hello"}\n\nevent: done\n',
            'data: {"status":"completed"}\n\n',
          ),
        ),
    );
    const callbacks = handlers();

    await expect(
      streamChatResponse('conversation-id', 'hi', callbacks),
    ).resolves.toBe('completed');
    expect(callbacks.onToken).toHaveBeenCalledWith('Hello');
  });

  it('returns null when the stream closes without a done event', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          chunkedStream('event: token\ndata: {"text":"Hi"}\n\n'),
        ),
    );

    await expect(
      streamChatResponse('conversation-id', 'hi', handlers()),
    ).resolves.toBeNull();
  });

  it('surfaces HTTP failures with their status code', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 429,
        json: async () => ({ detail: 'Chat rate limit exceeded' }),
      } as Response),
    );

    const failure = streamChatResponse('conversation-id', 'hi', handlers());
    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({
      status: 429,
      message: 'Chat rate limit exceeded',
    });
  });
});
