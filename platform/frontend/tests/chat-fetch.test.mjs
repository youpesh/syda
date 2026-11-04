import assert from 'node:assert/strict';
import test from 'node:test';
import { DefaultChatTransport } from 'ai';
import { createChatFetch } from '../app/lib/chat-fetch.ts';

const request = {
  trigger: 'submit-message', chatId: 'test', messageId: undefined,
  messages: [{ id: 'user-1', role: 'user', parts: [{ type: 'text', text: 'Create a scenario' }] }],
  abortSignal: undefined,
};

for (const status of [502, 503, 504, 524]) {
  test(`HTTP ${status} displays recovery guidance instead of a proxy HTML page`, async () => {
    const response = new Response('<!DOCTYPE html><html>Proxy diagnostics and private request details</html>', {
      status, headers: { 'Content-Type': 'text/html' },
    });
    const transport = new DefaultChatTransport({ fetch: createChatFetch(async () => response) });
    await assert.rejects(transport.sendMessages(request), error => {
      assert.match(error.message, /draft may still finish in the background/);
      assert.doesNotMatch(error.message, /html|diagnostics|private/);
      return true;
    });
  });
}

test('successful SSE remains streaming and heartbeats do not create chat text', async () => {
  let producer;
  const bytes = new TextEncoder();
  const body = new ReadableStream({ start(controller) { producer = controller; } });
  const response = new Response(body, {
    headers: { 'Content-Type': 'text/event-stream', 'x-vercel-ai-ui-message-stream': 'v1' },
  });
  const transport = new DefaultChatTransport({ fetch: createChatFetch(async () => response) });
  const stream = await transport.sendMessages(request);
  const received = [];
  const reading = (async () => { for await (const chunk of stream) received.push(chunk); })();
  producer.enqueue(bytes.encode(': keep-alive\n\n'));
  producer.enqueue(bytes.encode('data: {"type":"start"}\n\n'));
  producer.enqueue(bytes.encode(': keep-alive\n\n'));
  producer.enqueue(bytes.encode('data: {"type":"text-start","id":"reply"}\n\n'));
  producer.enqueue(bytes.encode('data: {"type":"text-delta","id":"reply","delta":"Draft ready"}\n\n'));
  producer.enqueue(bytes.encode('data: {"type":"text-end","id":"reply"}\n\n'));
  producer.enqueue(bytes.encode('data: {"type":"finish","finishReason":"stop"}\n\n'));
  producer.close();
  await reading;
  assert.deepEqual(received.map(chunk => chunk.type), ['start', 'text-start', 'text-delta', 'text-end', 'finish']);
  assert.equal(received[2].delta, 'Draft ready');
});

test('authentication errors give a sign-in instruction without echoing server details', async () => {
  const chatFetch = createChatFetch(async () => new Response('untrusted diagnostic', { status: 401 }));
  await assert.rejects(chatFetch('/api/agent/chat'), /Log in again/);
});
