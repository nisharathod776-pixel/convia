import { apiUrl, getAccessToken, uploadWithProgress } from '../core/api.js';

export class MeetingSessionRecorder {
  constructor({ onStateChange, onTick, onError } = {}) {
    this.onStateChange = onStateChange;
    this.onTick = onTick;
    this.onError = onError;

    this.mediaRecorder = null;
    this.displayStream = null;
    this.audioContext = null;
    this.mixedDestination = null;
    this.audioSourceNodes = [];
    this.chunks = [];
    this.startedAtMs = 0;
    this.timerId = null;
    this.isRecording = false;
    this.mimeType = 'video/webm';
    this.audioOnly = false;
    this._stopPromiseResolve = null;
    this._stopPromiseReject = null;
    this.captureError = null;
    this._audioContextStateHandler = null;
    this._resumePromise = null;
    this.audioSourceTimeoutId = null;
  }

  _pickMimeType(audioOnly = false) {
    const candidates = audioOnly
      ? ['audio/webm;codecs=opus', 'audio/webm']
      : [
          'video/webm;codecs=vp9,opus',
          'video/webm;codecs=vp8,opus',
          'video/webm',
          'audio/webm',
        ];
    if (typeof window.MediaRecorder === 'undefined') return null;
    for (const mime of candidates) {
      if (MediaRecorder.isTypeSupported(mime)) {
        return mime;
      }
    }
    return '';
  }

  refreshAudioSources(peerManager) {
    if (!this.isRecording || !this.audioContext || !this.mixedDestination) return;
    const tracks = peerManager?.getAudioTracksForRecording?.() || [];
    const liveTracks = tracks.filter((track) => track?.readyState === 'live');
    const liveTrackIds = new Set(liveTracks.map((track) => track.id));
    this.audioSourceNodes = this.audioSourceNodes.filter((item) => {
      if ((item.sourceKind === 'display' && item.track?.readyState === 'live') || liveTrackIds.has(item.trackId)) return true;
      this._disconnectAudioSource(item);
      return false;
    });
    const existingIds = new Set(this.audioSourceNodes.map((item) => item.trackId));
    for (const track of liveTracks) {
      if (existingIds.has(track.id)) continue;
      try {
        const src = this.audioContext.createMediaStreamSource(new MediaStream([track]));
        src.connect(this.mixedDestination);
        const onEnded = () => this._removeAudioSource(track.id);
        track.addEventListener('ended', onEnded, { once: true });
        this.audioSourceNodes.push({ trackId: track.id, node: src, track, onEnded, sourceKind: 'participant' });
      } catch (err) {
        const error = new Error('Unable to mix participant audio into the recording.');
        error.cause = err;
        if (!this.mediaRecorder) throw error;
        this._failCapture(error);
        return;
      }
    }
    if (this.audioSourceNodes.length) {
        this._clearAudioSourceTimeout();
    } else if (!this.mediaRecorder) {
        const error = new Error('All meeting audio sources ended; the recording was stopped.');
        throw error;
    } else {
        this._scheduleAudioSourceFailure();
    }
  }

  _clearAudioSourceTimeout() {
    if (this.audioSourceTimeoutId) {
        clearTimeout(this.audioSourceTimeoutId);
        this.audioSourceTimeoutId = null;
    }
  }

  _scheduleAudioSourceFailure() {
    if (this.audioSourceTimeoutId || !this.isRecording) return;
    this.audioSourceTimeoutId = setTimeout(() => {
        this.audioSourceTimeoutId = null;
        if (this.isRecording && !this.audioSourceNodes.length) {
          this._failCapture(new Error('No meeting audio source returned; recording stopped to avoid saving silent audio.'));
        }
    }, 10000);
  }

  _disconnectAudioSource(item) {
    item.track?.removeEventListener('ended', item.onEnded);
    try {
      item.node.disconnect();
    } catch {
      // Ignore already disconnected nodes
    }
  }

  _removeAudioSource(trackId) {
    this.audioSourceNodes = this.audioSourceNodes.filter((item) => {
      if (item.trackId !== trackId) return true;
      this._disconnectAudioSource(item);
      return false;
    });
    if (this.isRecording && !this.audioSourceNodes.length) {
      this._scheduleAudioSourceFailure();
    } else if (this.audioSourceNodes.length) {
      this._clearAudioSourceTimeout();
    }
  }

  removeAudioSourcesForPeer(peerEntry) {
    if (!peerEntry) return;
    const peerTrackIds = new Set(peerEntry.audioTrackIds || []);
    peerEntry.stream?.getAudioTracks().forEach((track) => peerTrackIds.add(track.id));
    this.audioSourceNodes = this.audioSourceNodes.filter((item) => {
      if (!peerTrackIds.has(item.trackId)) {
        return true;
      }
      this._disconnectAudioSource(item);
      return false;
    });
    if (this.isRecording && !this.audioSourceNodes.length) {
      this._scheduleAudioSourceFailure();
    } else if (this.audioSourceNodes.length) {
      this._clearAudioSourceTimeout();
    }
  }

  async start(peerManager, { audioOnly = false, onAutoStop = null } = {}) {
    if (this.isRecording) {
      throw new Error('A recording is already in progress.');
    }
    if (typeof window.MediaRecorder === 'undefined') {
      throw new Error('MediaRecorder is not supported in this browser.');
    }
    this.audioOnly = Boolean(audioOnly);
    const mimeType = this._pickMimeType(this.audioOnly);
    if (mimeType === null) {
      throw new Error('Browser recording codecs are unavailable.');
    }

    let videoTrack = null;
    let displayAudioTrack = null;
    this.captureError = null;

    try {
      const participantAudioTracks = (peerManager?.getAudioTracksForRecording?.() || [])
        .filter((track) => track?.readyState === 'live');
      if (this.audioOnly && !participantAudioTracks.length) {
        throw new Error('No live microphone or participant audio source is available to record.');
      }
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) {
        throw new Error('Web Audio is unavailable; meeting audio cannot be mixed into this recording.');
      }

      if (!this.audioOnly) {
        if (!navigator.mediaDevices || typeof navigator.mediaDevices.getDisplayMedia !== 'function') {
          throw new Error('Screen/tab capture is unavailable; try Audio-Only Recording.');
        }
        this.displayStream = await navigator.mediaDevices.getDisplayMedia({
          video: { cursor: 'always' },
          audio: true,
          preferCurrentTab: true,
        });
        videoTrack = this.displayStream.getVideoTracks()[0] || null;
        displayAudioTrack = this.displayStream.getAudioTracks()[0] || null;
        if (!videoTrack || videoTrack.readyState !== 'live') {
          throw new Error('Screen capture did not provide a live video track.');
        }
        videoTrack.onended = () => {
          if (this.isRecording && typeof onAutoStop === 'function') {
            onAutoStop();
          }
        };
      }

      this.audioContext = new AudioCtx();
      if (this.audioContext.state === 'suspended') await this.audioContext.resume();
      this._audioContextStateHandler = () => {
        if (this.isRecording && this.audioContext?.state === 'suspended' && !this._resumePromise) {
          this._resumePromise = this.audioContext.resume()
            .catch((err) => {
              const error = new Error('Audio capture was suspended and could not resume.');
              error.cause = err;
              this._failCapture(error);
            })
            .finally(() => {
              this._resumePromise = null;
            });
        }
      };
      this.audioContext.addEventListener('statechange', this._audioContextStateHandler);
      this.mixedDestination = this.audioContext.createMediaStreamDestination();
      if (displayAudioTrack?.readyState === 'live') {
        const dispNode = this.audioContext.createMediaStreamSource(new MediaStream([displayAudioTrack]));
        dispNode.connect(this.mixedDestination);
        const onEnded = () => this._removeAudioSource(displayAudioTrack.id);
        displayAudioTrack.addEventListener('ended', onEnded, { once: true });
        this.audioSourceNodes.push({
          trackId: displayAudioTrack.id,
          node: dispNode,
          track: displayAudioTrack,
          onEnded,
          sourceKind: 'display',
        });
      }

      const combinedStream = new MediaStream();
      if (videoTrack) {
        combinedStream.addTrack(videoTrack);
      }

      this.isRecording = true;
      this.refreshAudioSources(peerManager);

      const mixedAudioTracks = this.mixedDestination?.stream?.getAudioTracks() || [];
      if (mixedAudioTracks.length) {
        combinedStream.addTrack(mixedAudioTracks[0]);
      } else {
        throw new Error('No live audio source is available to record.');
      }

      if (!combinedStream.getTracks().length) {
        throw new Error('No audio or video source is available to record.');
      }

      this.chunks = [];
      this.mimeType = mimeType || (this.audioOnly ? 'audio/webm' : 'video/webm');
      const recorderOpts = mimeType ? { mimeType } : {};
      this.mediaRecorder = new MediaRecorder(combinedStream, recorderOpts);

      this.mediaRecorder.ondataavailable = (evt) => {
        if (evt.data && evt.data.size > 0) {
          this.chunks.push(evt.data);
        }
      };

      this.mediaRecorder.onstop = () => {
        const durationSeconds = Math.max(1, Math.round((Date.now() - this.startedAtMs) / 1000));
        const finalMime = (this.mediaRecorder?.mimeType || this.mimeType || 'video/webm').split(';')[0];
        const blob = new Blob(this.chunks, { type: finalMime });
        const captureError = this.captureError;
        this._cleanupStreams();
        this.isRecording = false;
        this.onStateChange?.(false);
        this.captureError = null;
        if (captureError) {
          if (this._stopPromiseReject) {
            this._stopPromiseReject(captureError);
            this._stopPromiseResolve = null;
            this._stopPromiseReject = null;
          } else {
            this.onError?.(captureError);
          }
          return;
        }
        if (this._stopPromiseResolve) {
          this._stopPromiseResolve({
            blob,
            mimeType: finalMime,
            extension: finalMime.includes('mp4') ? '.mp4' : '.webm',
            durationSeconds,
          });
          this._stopPromiseResolve = null;
          this._stopPromiseReject = null;
        }
      };

      this.mediaRecorder.onerror = (evt) => {
        const err = evt?.error || new Error('Recording failed.');
        this._cleanupStreams();
        this.isRecording = false;
        this.onStateChange?.(false);
        this.onError?.(err);
        if (this._stopPromiseReject) {
          this._stopPromiseReject(err);
          this._stopPromiseResolve = null;
          this._stopPromiseReject = null;
        }
      };

      this.startedAtMs = Date.now();
      this.mediaRecorder.start(1000);
      this.onStateChange?.(true);
      this.onTick?.(0);
      this.timerId = setInterval(() => {
        const secs = Math.max(0, Math.floor((Date.now() - this.startedAtMs) / 1000));
        this.onTick?.(secs);
      }, 1000);
    } catch (err) {
      this._cleanupStreams();
      this.isRecording = false;
      this.onStateChange?.(false);
      throw err;
    }
  }

  async stop() {
    if (!this.isRecording || !this.mediaRecorder) {
      return null;
    }
    return new Promise((resolve, reject) => {
      this._stopPromiseResolve = resolve;
      this._stopPromiseReject = reject;
      try {
        this.mediaRecorder.stop();
      } catch (err) {
        this._cleanupStreams();
        this.isRecording = false;
        this.onStateChange?.(false);
        this._stopPromiseResolve = null;
        this._stopPromiseReject = null;
        reject(err);
      }
    });
  }

  _failCapture(err) {
    if (!this.isRecording || this.captureError) return;
    this.captureError = err;
    if (this.mediaRecorder?.state === 'recording') {
      try {
        this.mediaRecorder.stop();
        return;
      } catch (stopError) {
        const error = new Error('Recording could not be stopped after an audio capture failure.');
        error.cause = stopError;
        this.captureError = error;
      }
    }
    const captureError = this.captureError;
    this._cleanupStreams();
    this.isRecording = false;
    this.onStateChange?.(false);
    this.captureError = null;
    if (this._stopPromiseReject) {
      this._stopPromiseReject(captureError);
      this._stopPromiseResolve = null;
      this._stopPromiseReject = null;
    } else {
      this.onError?.(captureError);
    }
  }

  _cleanupStreams() {
    this._clearAudioSourceTimeout();
    if (this.timerId) {
      clearInterval(this.timerId);
      this.timerId = null;
    }
    for (const item of this.audioSourceNodes) {
      this._disconnectAudioSource(item);
    }
    this.audioSourceNodes = [];
    if (this.audioContext) {
      if (this._audioContextStateHandler) {
        this.audioContext.removeEventListener('statechange', this._audioContextStateHandler);
      }
      this.audioContext.close().catch((err) => this.onError?.(err));
      this.audioContext = null;
    }
    this._audioContextStateHandler = null;
    this._resumePromise = null;
    this.mixedDestination = null;
    if (this.displayStream) {
      this.displayStream.getTracks().forEach((t) => t.stop());
      this.displayStream = null;
    }
    this.mediaRecorder = null;
  }
}

export function createRecordingPlayer(mimeType, className) {
  const player = document.createElement(mimeType?.startsWith('audio/') ? 'audio' : 'video');
  player.className = className;
  player.controls = true;
  return player;
}

export function releaseRecordingPreview(player, url) {
  if (player) {
    player.pause();
    player.removeAttribute('src');
    player.load();
    player.remove();
  }
  if (url) URL.revokeObjectURL(url);
}

export async function uploadSessionRecording({
  meetingCode,
  blob,
  extension = '.webm',
  uploadId = globalThis.crypto?.randomUUID?.(),
  title,
  visibility = 'teacher_only',
  durationSeconds = 0,
  onProgress,
}) {
  if (!uploadId) {
    throw new Error('Secure recording upload identifiers are unavailable in this browser.');
  }
  const safeCode = meetingCode.toLowerCase();
  const file = new File([blob], `convia-${safeCode}-${uploadId}${extension}`, {
    type: blob.type || 'video/webm',
  });
  const formData = new FormData();
  formData.append('file', file);
  formData.append('title', title || `Session Recording (${safeCode})`);
  formData.append('visibility', visibility);
  formData.append('duration_seconds', String(Math.max(0, Math.round(durationSeconds))));
  formData.append('upload_id', uploadId);

  return uploadWithProgress(
    `/meetings/${encodeURIComponent(meetingCode)}/recordings/`,
    formData,
    onProgress,
  );
}

export async function fetchAuthorizedRecordingBlobUrl(recordingId, inline = true) {
  const token = getAccessToken();
  const q = inline ? '?inline=1' : '';
  const res = await fetch(apiUrl(`/api/v1/meetings/recordings/${encodeURIComponent(recordingId)}/download/${q}`), {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    throw new Error('Unable to load recording stream.');
  }
  const blob = await res.blob();
  return URL.createObjectURL(blob);
}
