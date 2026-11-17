import assert from 'node:assert/strict';
import test from 'node:test';

globalThis.document = { addEventListener() {} };
const {
  MeetingSessionRecorder,
  createRecordingPlayer,
  releaseRecordingPreview,
} = await import('../static/js/meeting/recorder.js');
const { downloadBlobUrl } = await import('../static/js/core/ui.js');
const { PeerConnectionManager } = await import('../static/js/meeting/webrtc.js');

test('recording previews use audio or video players according to the saved MIME type', () => {
  const originalCreateElement = globalThis.document.createElement;
  globalThis.document.createElement = (tagName) => ({ tagName });
  try {
    const audio = createRecordingPlayer('audio/webm', 'recording-preview');
    const video = createRecordingPlayer('video/webm', 'recording-preview');
    assert.deepEqual(audio, { tagName: 'audio', className: 'recording-preview', controls: true });
    assert.deepEqual(video, { tagName: 'video', className: 'recording-preview', controls: true });
  } finally {
    if (originalCreateElement) globalThis.document.createElement = originalCreateElement;
    else delete globalThis.document.createElement;
  }
});

test('recording preview detaches media before revoking its Blob URL', () => {
  const operations = [];
  const originalRevoke = URL.revokeObjectURL;
  URL.revokeObjectURL = (url) => operations.push(`revoke:${url}`);
  const player = {
    pause: () => operations.push('pause'),
    removeAttribute: (name) => operations.push(`remove:${name}`),
    load: () => operations.push('load'),
    remove: () => operations.push('detach'),
  };

  try {
    releaseRecordingPreview(player, 'blob:recording');
    assert.deepEqual(operations, ['pause', 'remove:src', 'load', 'detach', 'revoke:blob:recording']);
  } finally {
    URL.revokeObjectURL = originalRevoke;
  }
});

test('Blob downloads remain available until the delayed cleanup runs', () => {
  const scheduled = [];
  const operations = [];
  const originalCreateElement = globalThis.document.createElement;
  const originalSetTimeout = globalThis.setTimeout;
  const originalRevoke = URL.revokeObjectURL;
  const originalBody = globalThis.document.body;
  globalThis.document.body = {
    appendChild(link) {
      operations.push('append');
      link.remove = () => operations.push('remove');
    },
    removeChild() {},
  };
  globalThis.document.createElement = () => ({
    click: () => operations.push('click'),
  });
  globalThis.setTimeout = (callback, delay) => {
    scheduled.push({ callback, delay });
    return 1;
  };
  URL.revokeObjectURL = (url) => operations.push(`revoke:${url}`);

  try {
    downloadBlobUrl('blob:download', 'recording.webm');
    assert.deepEqual(operations, ['append', 'click', 'remove']);
    assert.equal(scheduled.length, 1);
    assert.equal(scheduled[0].delay, 1000);
    scheduled[0].callback();
    assert.deepEqual(operations, ['append', 'click', 'remove', 'revoke:blob:download']);
  } finally {
    if (originalCreateElement) globalThis.document.createElement = originalCreateElement;
    else delete globalThis.document.createElement;
    if (originalBody) globalThis.document.body = originalBody;
    else delete globalThis.document.body;
    globalThis.setTimeout = originalSetTimeout;
    URL.revokeObjectURL = originalRevoke;
  }
});

test('room peer synchronization removes departed peers and admits current peers', async () => {
  const removed = [];
  const added = [];
  const manager = Object.create(PeerConnectionManager.prototype);
  Object.assign(manager, {
    peers: new Map([['stale', {
      pc: { close() { removed.push('closed'); } },
      tileEl: { remove() {} },
      participant: { sid: 'stale' },
    }]]),
    localSid: 'self',
    localUser: { id: 1, username: 'self' },
    micEnabled: true,
    camEnabled: true,
    isTeacher: false,
    isScreenSharing: false,
    handRaised: false,
    onPeerRemoved: (entry) => removed.push(entry.participant.sid),
    onParticipantsChanged() {},
    ensurePeer: async (peerInfo) => added.push(peerInfo.sid),
  });

  await manager.syncRoomPeers([{ sid: 'current' }, { sid: 'self' }]);

  assert.deepEqual(removed, ['stale', 'closed']);
  assert.deepEqual(added, ['current']);
  assert.equal(manager.peers.has('stale'), false);
});

test('a failed peer is replaced after cleanup of its prior audio sources', async () => {
  const oldMediaStream = globalThis.MediaStream;
  const oldPeerConnection = globalThis.RTCPeerConnection;
  const removed = [];
  globalThis.MediaStream = class {
    getTracks() {
      return [];
    }
  };
  globalThis.RTCPeerConnection = class {
    connectionState = 'new';
    close() {}
  };

  const oldEntry = {
    pc: { connectionState: 'failed', close() {} },
    participant: { sid: 'peer' },
    tileEl: { remove() {} },
    stream: { getAudioTracks: () => [] },
    audioTrackIds: new Set(['old-audio']),
  };
  const manager = Object.create(PeerConnectionManager.prototype);
  Object.assign(manager, {
    peers: new Map([['peer', oldEntry]]),
    localSid: 'self',
    localStream: null,
    cameraTrack: null,
    isScreenSharing: false,
    iceServers: [],
    onPeerRemoved: (entry) => removed.push(...entry.audioTrackIds),
    notifyParticipants() {},
    createRemoteTile() {},
    updateRemoteTileUi() {},
    socketClient: { emit() {} },
  });

  try {
    const replacement = await manager.ensurePeer({ sid: 'peer' });
    assert.deepEqual(removed, ['old-audio']);
    assert.notEqual(replacement, oldEntry);
    assert.equal(manager.peers.get('peer'), replacement);
  } finally {
    if (oldMediaStream === undefined) delete globalThis.MediaStream;
    else globalThis.MediaStream = oldMediaStream;
    if (oldPeerConnection === undefined) delete globalThis.RTCPeerConnection;
    else globalThis.RTCPeerConnection = oldPeerConnection;
  }
});

test('resetting room peers invokes audio cleanup before breakout subchannel replacement', () => {
  const closed = [];
  const manager = Object.create(PeerConnectionManager.prototype);
  const entry = {
    pc: { close() { closed.push(true); } },
    participant: { sid: 'peer' },
    tileEl: { remove() {} },
    audioTrackIds: new Set(['remote-audio']),
  };
  Object.assign(manager, {
    peers: new Map([['peer', entry]]),
    onPeerRemoved: (peerEntry) => closed.push(...peerEntry.audioTrackIds),
    notifyParticipants() {},
  });

  manager.resetRemotePeers();

  assert.deepEqual(closed, ['remote-audio', true]);
  assert.equal(manager.peers.size, 0);
});

test('remote chat mute updates the participant record used by the UI', () => {
  const manager = Object.create(PeerConnectionManager.prototype);
  const entry = { participant: { sid: 'peer' } };
  Object.assign(manager, {
    peers: new Map([['peer', entry]]),
    updateRemoteTileUi() {},
    notifyParticipants() {},
  });

  manager.handleRemoteMediaState({ sid: 'peer', chat_muted: true });

  assert.equal(entry.participant.chat_muted, true);
});

test('recording audio sources are removed when tracks end, peers leave, or tracks disappear', () => {
  const oldMediaStream = globalThis.MediaStream;
  const disconnected = [];
  globalThis.MediaStream = class {
    constructor(tracks) {
      this.tracks = tracks;
    }

    getTracks() {
      return this.tracks;
    }

    getAudioTracks() {
      return this.tracks.filter((track) => track.kind === 'audio');
    }
  };

  const makeTrack = (id) => ({
    id,
    kind: 'audio',
    readyState: 'live',
    addEventListener(_name, listener) {
      this.onEnded = listener;
    },
    removeEventListener() {},
  });
  const first = makeTrack('first');
  const second = makeTrack('second');
  const tracks = [first, second];
  const recorder = new MeetingSessionRecorder();
  Object.assign(recorder, {
    isRecording: true,
    mediaRecorder: { state: 'recording', stop() {} },
    audioContext: {
      createMediaStreamSource() {
        return {
          connect() {},
          disconnect() {
            disconnected.push(true);
          },
        };
      },
    },
    mixedDestination: {},
  });
  const peers = { getAudioTracksForRecording: () => tracks };

  try {
    recorder.refreshAudioSources(peers);
    assert.equal(recorder.audioSourceNodes.length, 2);

    first.readyState = 'ended';
    first.onEnded();
    assert.deepEqual(recorder.audioSourceNodes.map((item) => item.trackId), ['second']);

    recorder.removeAudioSourcesForPeer({
      audioTrackIds: new Set(['second']),
      stream: { getAudioTracks: () => [second] },
    });
    assert.equal(recorder.audioSourceNodes.length, 0);

    const replacement = makeTrack('replacement');
    tracks.splice(0, tracks.length, replacement);
    recorder.refreshAudioSources(peers);
    tracks.splice(0, tracks.length);
    recorder.refreshAudioSources(peers);
    assert.equal(recorder.audioSourceNodes.length, 0);
    assert.equal(disconnected.length, 3);
  } finally {
    recorder._clearAudioSourceTimeout();
    if (oldMediaStream === undefined) delete globalThis.MediaStream;
    else globalThis.MediaStream = oldMediaStream;
  }
});

test('recording startup failure stops captured tracks and closes the audio context', async () => {
  const saved = {
    window: Object.getOwnPropertyDescriptor(globalThis, 'window'),
    navigator: Object.getOwnPropertyDescriptor(globalThis, 'navigator'),
    MediaRecorder: Object.getOwnPropertyDescriptor(globalThis, 'MediaRecorder'),
    MediaStream: Object.getOwnPropertyDescriptor(globalThis, 'MediaStream'),
  };
  let audioContextClosed = false;
  const videoTrack = {
    id: 'display-video',
    kind: 'video',
    readyState: 'live',
    stop() {
      this.readyState = 'ended';
    },
    addEventListener() {},
    removeEventListener() {},
  };
  const displayAudioTrack = {
    id: 'display-audio',
    kind: 'audio',
    readyState: 'live',
    stop() {
      this.readyState = 'ended';
    },
    addEventListener() {},
    removeEventListener() {},
  };

  class FakeStream {
    constructor(tracks = []) {
      this.tracks = tracks;
    }

    getTracks() {
      return this.tracks;
    }

    getAudioTracks() {
      return this.tracks.filter((track) => track.kind === 'audio');
    }

    getVideoTracks() {
      return this.tracks.filter((track) => track.kind === 'video');
    }

    addTrack(track) {
      this.tracks.push(track);
    }
  }

  class FakeAudioContext {
    state = 'running';

    addEventListener(_name, listener) {
      this.stateChangeListener = listener;
    }

    removeEventListener() {
      this.stateChangeListener = null;
    }

    createMediaStreamDestination() {
      return { stream: new FakeStream([{ id: 'mixed-audio', kind: 'audio' }]) };
    }

    createMediaStreamSource() {
      return { connect() {}, disconnect() {} };
    }

    close() {
      audioContextClosed = true;
      return Promise.resolve();
    }
  }

  class FakeMediaRecorder {
    static isTypeSupported() {
      return true;
    }

    constructor() {
      this.mimeType = 'video/webm';
    }

    start() {
      throw new Error('recorder startup failed');
    }
  }

  try {
    globalThis.window = { MediaRecorder: FakeMediaRecorder, AudioContext: FakeAudioContext };
    globalThis.MediaRecorder = FakeMediaRecorder;
    globalThis.MediaStream = FakeStream;
    Object.defineProperty(globalThis, 'navigator', {
      configurable: true,
      value: { mediaDevices: { getDisplayMedia: async () => new FakeStream([videoTrack, displayAudioTrack]) } },
    });

    const recorder = new MeetingSessionRecorder();
    await assert.rejects(
      recorder.start({ getAudioTracksForRecording: () => [] }),
      /recorder startup failed/,
    );
    assert.equal(videoTrack.readyState, 'ended');
    assert.equal(audioContextClosed, true);
    assert.equal(recorder.isRecording, false);
    assert.equal(recorder.displayStream, null);
  } finally {
    for (const [key, descriptor] of Object.entries(saved)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

test('audio-only recording refuses to start without a live audio source', async () => {
  const savedWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  const savedRecorder = Object.getOwnPropertyDescriptor(globalThis, 'MediaRecorder');
  class FakeMediaRecorder {
    static isTypeSupported() {
      return true;
    }
  }

  try {
    globalThis.window = { MediaRecorder: FakeMediaRecorder };
    globalThis.MediaRecorder = FakeMediaRecorder;
    await assert.rejects(
      new MeetingSessionRecorder().start(
        { getAudioTracksForRecording: () => [] },
        { audioOnly: true },
      ),
      /No live microphone or participant audio source/,
    );
  } finally {
    if (savedWindow) Object.defineProperty(globalThis, 'window', savedWindow);
    else delete globalThis.window;
    if (savedRecorder) Object.defineProperty(globalThis, 'MediaRecorder', savedRecorder);
    else delete globalThis.MediaRecorder;
  }
});

test('recording stops and reports an error if a suspended audio context cannot resume', async () => {
  const saved = {
    window: Object.getOwnPropertyDescriptor(globalThis, 'window'),
    navigator: Object.getOwnPropertyDescriptor(globalThis, 'navigator'),
    MediaRecorder: Object.getOwnPropertyDescriptor(globalThis, 'MediaRecorder'),
    MediaStream: Object.getOwnPropertyDescriptor(globalThis, 'MediaStream'),
  };
  const audioTrack = {
    id: 'participant-audio',
    kind: 'audio',
    readyState: 'live',
    addEventListener() {},
    removeEventListener() {},
  };
  const mixedTrack = { id: 'mixed-audio', kind: 'audio', readyState: 'live' };
  let audioContext;

  class FakeStream {
    constructor(tracks = []) {
      this.tracks = tracks;
    }

    getTracks() {
      return this.tracks;
    }

    getAudioTracks() {
      return this.tracks.filter((track) => track.kind === 'audio');
    }

    getVideoTracks() {
      return this.tracks.filter((track) => track.kind === 'video');
    }

    addTrack(track) {
      this.tracks.push(track);
    }
  }

  class FakeAudioContext {
    state = 'running';

    constructor() {
      audioContext = this;
    }

    addEventListener(_name, listener) {
      this.stateChangeListener = listener;
    }

    removeEventListener() {
      this.stateChangeListener = null;
    }

    createMediaStreamDestination() {
      return { stream: new FakeStream([mixedTrack]) };
    }

    createMediaStreamSource() {
      return { connect() {}, disconnect() {} };
    }

    resume() {
      return Promise.reject(new Error('Audio context resume denied.'));
    }

    close() {
      return Promise.resolve();
    }

    suspend() {
      this.state = 'suspended';
      this.stateChangeListener?.();
    }
  }

  class FakeMediaRecorder {
    static isTypeSupported() {
      return true;
    }

    constructor() {
      this.mimeType = 'audio/webm';
      this.state = 'inactive';
    }

    start() {
      this.state = 'recording';
    }

    stop() {
      this.state = 'inactive';
      this.onstop?.();
    }
  }

  try {
    globalThis.window = {
      MediaRecorder: FakeMediaRecorder,
      AudioContext: FakeAudioContext,
    };
    globalThis.MediaRecorder = FakeMediaRecorder;
    globalThis.MediaStream = FakeStream;
    const errors = [];
    const recorder = new MeetingSessionRecorder({ onError: (err) => errors.push(err) });
    await recorder.start({ getAudioTracksForRecording: () => [audioTrack] }, { audioOnly: true });

    audioContext.suspend();
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(recorder.isRecording, false);
    assert.match(errors[0].message, /suspended and could not resume/);
  } finally {
    for (const [key, descriptor] of Object.entries(saved)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});
