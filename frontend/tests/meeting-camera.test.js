import assert from 'node:assert/strict';
import test from 'node:test';

globalThis.document = { addEventListener() {}, getElementById() { return null; } };
const { PeerConnectionManager } = await import('../static/js/meeting/webrtc.js');

class FakeMediaStream {
  constructor(tracks = []) {
    this.tracks = [...tracks];
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

  removeTrack(track) {
    this.tracks = this.tracks.filter((candidate) => candidate !== track);
  }
}

function makeTrack(id, kind) {
  return {
    id,
    kind,
    readyState: 'live',
    enabled: true,
    stop() {
      this.readyState = 'ended';
      this.onended?.();
    },
    addEventListener(name, listener) {
      if (name === 'ended') this.onended = listener;
    },
  };
}

function installBrowserGlobals({ getUserMedia, localVideo = { srcObject: null }, messages = [] }) {
  const saved = {
    document: Object.getOwnPropertyDescriptor(globalThis, 'document'),
    navigator: Object.getOwnPropertyDescriptor(globalThis, 'navigator'),
    MediaStream: Object.getOwnPropertyDescriptor(globalThis, 'MediaStream'),
    setTimeout: globalThis.setTimeout,
  };
  const toastRegion = {
    appendChild(toast) {
      messages.push(toast.children[0].textContent);
    },
  };
  globalThis.document = {
    addEventListener() {},
    getElementById(id) {
      if (id === 'local-video') return localVideo;
      if (id === 'toast-region') return toastRegion;
      return null;
    },
    createElement() {
      return {
        children: [],
        appendChild(child) {
          this.children.push(child);
        },
        addEventListener() {},
        setAttribute() {},
        remove() {},
      };
    },
  };
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: { mediaDevices: { getUserMedia } },
  });
  Object.defineProperty(globalThis, 'MediaStream', {
    configurable: true,
    value: FakeMediaStream,
  });
  globalThis.setTimeout = () => 1;

  return () => {
    if (saved.document) Object.defineProperty(globalThis, 'document', saved.document);
    else delete globalThis.document;
    if (saved.navigator) Object.defineProperty(globalThis, 'navigator', saved.navigator);
    else delete globalThis.navigator;
    if (saved.MediaStream) Object.defineProperty(globalThis, 'MediaStream', saved.MediaStream);
    else delete globalThis.MediaStream;
    globalThis.setTimeout = saved.setTimeout;
  };
}

function makeManager({ isTeacher = false, allowStudentCamera = true, ...options } = {}) {
  const mediaStates = [];
  const manager = new PeerConnectionManager({
    localUser: { id: 1, username: isTeacher ? 'teacher' : 'student' },
    socketClient: {
      emit(event, payload) {
        if (event === 'media_state_change') mediaStates.push(payload);
        return true;
      },
    },
    ...options,
  });
  manager.setPermissions({ isTeacher, allowStudentCamera });
  manager.updateLocalMediaUi = () => {};
  return { manager, mediaStates };
}

test('students join with camera off while teachers start camera regardless of student permission', async () => {
  const calls = [];
  const audioTrack = makeTrack('student-audio', 'audio');
  const teacherAudio = makeTrack('teacher-audio', 'audio');
  const teacherVideo = makeTrack('teacher-video', 'video');
  const restore = installBrowserGlobals({
    getUserMedia: async (constraints) => {
      calls.push(constraints);
      return constraints.video === false
        ? new FakeMediaStream([audioTrack])
        : new FakeMediaStream([teacherAudio, teacherVideo]);
    },
  });

  try {
    const { manager: student } = makeManager();
    await student.initLocalMedia();
    assert.equal(student.camEnabled, false);
    assert.equal(student.cameraTrack, null);
    assert.equal(student.micEnabled, true);
    assert.equal(calls[0].video, false);

    const { manager: teacher, mediaStates: teacherMediaStates } = makeManager({
      isTeacher: true,
      allowStudentCamera: false,
    });
    await teacher.initLocalMedia();
    assert.equal(teacher.camEnabled, true);
    assert.equal(teacher.cameraTrack, teacherVideo);
    assert.equal(teacher.cameraTrack.enabled, true);
    assert.notEqual(calls[1].video, false);

    await teacher.toggleCamera();
    assert.equal(teacher.camEnabled, false);
    assert.equal(teacherVideo.enabled, false);
    await teacher.toggleCamera();
    assert.equal(teacher.camEnabled, true);
    assert.equal(teacherVideo.enabled, true);
    assert.equal(calls.length, 2);
    assert.deepEqual(teacherMediaStates.map((state) => state.cam_enabled), [false, true]);
  } finally {
    restore();
  }
});

test('students acquire a camera on demand, then reuse it for off/on toggles', async () => {
  const calls = [];
  const audioTrack = makeTrack('student-audio', 'audio');
  const cameraTrack = makeTrack('student-camera', 'video');
  const localVideo = { srcObject: null };
  const restore = installBrowserGlobals({
    localVideo,
    getUserMedia: async (constraints) => {
      calls.push(constraints);
      return constraints.video === false
        ? new FakeMediaStream([audioTrack])
        : new FakeMediaStream([cameraTrack]);
    },
  });

  try {
    let permissionChecks = 0;
    const { manager, mediaStates } = makeManager({
      refreshCameraPermissions: async () => {
        permissionChecks += 1;
        return { isTeacher: false, allowStudentCamera: true, cameraLocked: false };
      },
    });
    await manager.initLocalMedia();

    await manager.toggleCamera();
    assert.equal(manager.camEnabled, true);
    assert.equal(manager.localStream.getVideoTracks()[0], cameraTrack);
    assert.equal(localVideo.srcObject, manager.localStream);

    await manager.toggleCamera();
    assert.equal(manager.camEnabled, false);
    assert.equal(cameraTrack.enabled, false);

    await manager.toggleCamera();
    assert.equal(manager.camEnabled, true);
    assert.equal(cameraTrack.enabled, true);
    assert.equal(calls.length, 2);
    assert.equal(permissionChecks, 2);
    assert.deepEqual(mediaStates.map((state) => state.cam_enabled), [true, false, true]);
  } finally {
    restore();
  }
});

test('students cannot acquire a camera after the server reports permission disabled', async () => {
  let cameraRequests = 0;
  const messages = [];
  const restore = installBrowserGlobals({
    messages,
    getUserMedia: async (constraints) => {
      if (constraints.video !== false) cameraRequests += 1;
      return new FakeMediaStream([makeTrack('audio', 'audio')]);
    },
  });

  try {
    const { manager, mediaStates } = makeManager({
      refreshCameraPermissions: async () => ({
        isTeacher: false,
        allowStudentCamera: false,
        cameraLocked: true,
      }),
    });
    await manager.initLocalMedia();
    await manager.toggleCamera();
    assert.equal(cameraRequests, 0);
    assert.equal(manager.camEnabled, false);
    assert.deepEqual(mediaStates, []);
    assert.ok(messages.some((message) => message.includes('disabled student cameras')));
  } finally {
    restore();
  }
});

test('camera permission denial and missing devices produce actionable messages', async (t) => {
  for (const [name, message] of [
    ['NotAllowedError', 'access was denied'],
    ['NotFoundError', 'No camera was found'],
  ]) {
    await t.test(name, async () => {
      const messages = [];
      const restore = installBrowserGlobals({
        messages,
        getUserMedia: async (constraints) => {
          if (constraints.video !== false) {
            const error = new Error(name);
            error.name = name;
            throw error;
          }
          return new FakeMediaStream([makeTrack('audio', 'audio')]);
        },
      });

      try {
        const { manager } = makeManager({
          refreshCameraPermissions: async () => ({
            isTeacher: false,
            allowStudentCamera: true,
            cameraLocked: false,
          }),
        });
        await manager.initLocalMedia();
        await manager.toggleCamera();
        assert.equal(manager.camEnabled, false);
        assert.ok(messages.some((entry) => entry.includes(message)));
      } finally {
        restore();
      }
    });
  }
});

test('revoking student camera permission disables an active track but leaves teacher camera unrestricted', () => {
  const studentTrack = makeTrack('student-camera', 'video');
  const { manager: student } = makeManager();
  student.localStream = new FakeMediaStream([studentTrack]);
  student.cameraTrack = studentTrack;
  student.camEnabled = true;
  student.setPermissions({ isTeacher: false, allowStudentCamera: false, cameraLocked: true });
  assert.equal(student.camEnabled, false);
  assert.equal(studentTrack.enabled, false);

  const teacherTrack = makeTrack('teacher-camera', 'video');
  const { manager: teacher } = makeManager({ isTeacher: true });
  teacher.localStream = new FakeMediaStream([teacherTrack]);
  teacher.cameraTrack = teacherTrack;
  teacher.camEnabled = true;
  teacher.setPermissions({ isTeacher: true, allowStudentCamera: false });
  assert.equal(teacher.camEnabled, true);
  assert.equal(teacherTrack.enabled, true);
});

test('adding video updates every existing peer without duplicate video senders', async () => {
  const localStream = new FakeMediaStream();
  const firstSender = {
    track: null,
    async replaceTrack(track) {
      this.track = track;
    },
  };
  const peers = new Map([
    ['first', {
      pc: {
        getSenders: () => [firstSender],
        getTransceivers: () => [{ sender: firstSender, receiver: { track: { kind: 'video' } } }],
      },
    }],
    ['second', {
      pc: {
        senders: [],
        getSenders() {
          return this.senders;
        },
        getTransceivers: () => [],
        addTrack(track, stream) {
          this.senders.push({
            track,
            stream,
            async replaceTrack(nextTrack) {
              this.track = nextTrack;
            },
          });
        },
      },
    }],
  ]);
  const offers = [];
  const manager = Object.create(PeerConnectionManager.prototype);
  Object.assign(manager, {
    peers,
    localStream,
    async sendOffer(sid) {
      offers.push(sid);
    },
  });
  const track = makeTrack('camera', 'video');

  await manager.replaceVideoTrackForPeers(track);
  await manager.replaceVideoTrackForPeers(track);

  assert.equal(firstSender.track, track);
  assert.equal(peers.get('second').pc.getSenders().length, 1);
  assert.equal(peers.get('second').pc.getSenders()[0].track, track);
  assert.deepEqual(offers, ['second']);
});
