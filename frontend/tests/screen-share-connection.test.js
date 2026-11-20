import assert from 'node:assert/strict';
import test from 'node:test';

globalThis.document = { addEventListener() {} };
globalThis.localStorage = { getItem: () => null };

const { MeetingSocketClient } = await import('../static/js/meeting/socket-client.js');
const { PeerConnectionManager } = await import('../static/js/meeting/webrtc.js');

class FakeSocket {
  constructor() {
    this.connected = false;
    this.active = true;
    this.listeners = new Map();
    this.emitted = [];
    this.io = { on() {} };
  }

  on(event, callback) {
    const callbacks = this.listeners.get(event) || [];
    callbacks.push(callback);
    this.listeners.set(event, callbacks);
  }

  emit(event, payload) {
    this.emitted.push({ event, payload });
    return true;
  }

  trigger(event, payload) {
    if (event === 'connect') this.connected = true;
    if (event === 'disconnect') this.connected = false;
    for (const callback of this.listeners.get(event) || []) callback(payload);
  }

  disconnect() {
    this.trigger('disconnect', 'io client disconnect');
  }
}

test('screen-share lock waits for room_joined before emitting request_screen_share', async (t) => {
  const originalWindow = globalThis.window;
  const socket = new FakeSocket();
  globalThis.window = { io: () => socket };
  t.after(() => {
    globalThis.window = originalWindow;
  });

  let peerManager;
  const socketClient = new MeetingSocketClient('abc-def-ghi', {
    onScreenShareGranted: (payload) => peerManager.handleScreenShareGranted(payload),
  });
  peerManager = Object.create(PeerConnectionManager.prototype);
  peerManager.socketClient = socketClient;
  peerManager._pendingShareRequest = null;
  peerManager.handleScreenShareState = () => {};

  socketClient.connect();
  socket.trigger('connect');
  assert.equal(socket.emitted[0].event, 'join_meeting');

  const lockPromise = peerManager.requestServerScreenShareLock();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(socket.emitted.some(({ event }) => event === 'request_screen_share'), false);

  socket.trigger('room_joined', { meeting_code: 'abc-def-ghi' });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(socketClient.hasJoined, true);
  assert.equal(socket.emitted.filter(({ event }) => event === 'request_screen_share').length, 1);

  const granted = { meeting_code: 'abc-def-ghi', active_screen_sharer_sid: 'sid-local' };
  socket.trigger('screen_share_granted', granted);
  assert.deepEqual(await lockPromise, granted);

  socketClient.disconnect();
});
