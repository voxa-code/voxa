/**
 * app.js - Loop phone web client (ES module)
 *
 * Mic capture pipeline:
 *   getUserMedia -> MediaStreamSource -> AudioWorkletNode (pcm-worklet.js)
 *   -> postMessage (Int16 ArrayBuffer, 16 kHz) -> WebSocket binary frame
 *
 * Playback pipeline:
 *   WebSocket binary frame (Int16 ArrayBuffer, 24 kHz)
 *   -> Float32 conversion -> AudioBufferSourceNode scheduled on 24 kHz AudioContext
 *   (jitter buffer via nextStartTime cursor)
 *
 * JSON control messages: {"type":"status","status":"..."} and friends update the UI.
 */

const PLAYBACK_SAMPLE_RATE = 24000;
// Minimum ahead-of-time scheduling cushion (seconds). Keeps audio gapless.
const SCHEDULE_AHEAD = 0.06;

// --- State ---
let ws = null;
let micContext = null;   // AudioContext for capture (browser's native rate)
let playCtx = null;      // AudioContext fixed at 24 kHz for playback
let workletNode = null;
let sourceNode = null;
let stream = null;
let muted = false;
let nextStartTime = 0;   // jitter-buffer scheduling cursor (in playCtx.currentTime)

let wantConnection = false;   // user intends to stay connected (auto-reconnect on drops)
let reconnectAttempt = 0;
let reconnectTimer = null;
let lastToken = '';

// Backoff: 0.5, 1, 2, 4, 8s, capped at 10s, plus jitter (mirrors the iOS app).
function backoffDelayMs(attempt) {
  return Math.min(10000, 500 * Math.pow(2, attempt)) + Math.random() * 300;
}

function scheduleReconnect() {
  if (!wantConnection || !lastToken) return;
  clearTimeout(reconnectTimer);
  const delay = backoffDelayMs(reconnectAttempt);
  reconnectAttempt += 1;
  reconnectTimer = setTimeout(() => {
    if (!wantConnection) return;
    setStatus('connecting', 'Reconnecting…');
    openWebSocket(lastToken);
  }, delay);
}

// --- DOM refs (populated in init()) ---
let tokenInput, folderInput, terminalSelect, connectBtn, muteBtn, stopBtn, terminalsBtn, terminalsEl, statusDot, statusText, infoText, transcriptEl;

// Transcript state: coalesce consecutive same-role caption chunks into one line.
let lastRole = null;
let lastSaidEl = null;

// ---- Token helpers ----

function getTokenFromUrl() {
  const params = new URLSearchParams(window.location.search);
  return params.get('token') || '';
}

function buildWsUrl(token) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  let url = `${proto}//${location.host}/ws?token=${encodeURIComponent(token)}`;
  // Through the hosted relay the phone leg is matched to the laptop by the
  // pairing code from the QR URL; the local server just ignores it.
  const code = new URLSearchParams(window.location.search).get('code');
  if (code) url += `&code=${encodeURIComponent(code)}`;
  return url;
}

function wsOpen() {
  return ws && ws.readyState === WebSocket.OPEN;
}

function sendTerminal() {
  if (wsOpen()) {
    ws.send(JSON.stringify({ type: 'set_terminal', app: terminalSelect.value }));
  }
}

function sendFolder() {
  const path = folderInput.value.trim();
  if (path && wsOpen()) {
    ws.send(JSON.stringify({ type: 'set_dir', path }));
  }
}

// ---- UI helpers ----

function setStatus(state, text) {
  // state: 'disconnected' | 'connecting' | 'connected' | 'error'
  statusDot.className = 'dot dot-' + state;
  statusText.textContent = text;
}

function setInfo(text) {
  infoText.textContent = text || '';
}

function updateConnectUI(connected) {
  connectBtn.textContent = connected ? 'Disconnect' : 'Connect';
  muteBtn.disabled = !connected;
  stopBtn.disabled = !connected;
  terminalsBtn.disabled = !connected;
  tokenInput.disabled = connected;
  if (!connected && terminalsEl) terminalsEl.innerHTML = '';
}

function sendStop() {
  if (wsOpen()) ws.send(JSON.stringify({ type: 'stop' }));
}

function requestTerminals() {
  if (wsOpen()) ws.send(JSON.stringify({ type: 'list_terminals' }));
}

function renderTerminals(items) {
  terminalsEl.innerHTML = '';
  if (!items || !items.length) {
    const p = document.createElement('div');
    p.className = 'term-item uncontrollable';
    p.textContent = 'No open Claude terminals found.';
    terminalsEl.appendChild(p);
    return;
  }
  for (const it of items) {
    const b = document.createElement('button');
    b.className = 'term-item' + (it.controllable ? '' : ' uncontrollable');
    b.disabled = !it.controllable;
    // Build with textContent, never innerHTML: app/label/cwd/id come from the
    // laptop's terminal enumeration (window titles, directory names) and could
    // contain HTML, which innerHTML would execute (DOM XSS in the phone client).
    const appSpan = document.createElement('span');
    appSpan.className = 'app';
    appSpan.textContent = it.app || '';
    b.appendChild(appSpan);
    b.appendChild(document.createElement('br'));
    b.appendChild(document.createTextNode(
      (it.label || it.cwd || it.id || '') + (it.controllable ? '' : ' (can’t control)')
    ));
    if (it.controllable) {
      b.addEventListener('click', () => {
        if (wsOpen()) ws.send(JSON.stringify({ type: 'attach_terminal', id: it.id }));
      });
    }
    terminalsEl.appendChild(b);
  }
}

// ---- WebSocket ----

function openWebSocket(token) {
  lastToken = token;
  const url = buildWsUrl(token);
  setStatus('connecting', 'Connecting...');
  ws = new WebSocket(url);
  ws.binaryType = 'arraybuffer';

  ws.addEventListener('open', () => {
    reconnectAttempt = 0;   // clean open resets backoff
    setStatus('connected', 'Connected');
    updateConnectUI(true);
    sendTerminal();        // choose terminal app before any session starts
    sendFolder();          // optional: pre-set the project folder (else say it by voice)
  });

  ws.addEventListener('close', (ev) => {
    if (wantConnection) {
      // Unexpected drop: keep the mic capture alive (frames are gated on wsOpen)
      // and reconnect with backoff instead of tearing down.
      setStatus('connecting', 'Reconnecting…');
      scheduleReconnect();
      return;
    }
    const reason = ev.reason ? `: ${ev.reason}` : '';
    setStatus('disconnected', `Disconnected (${ev.code}${reason})`);
    teardown();
  });

  ws.addEventListener('error', () => {
    if (wantConnection) return;   // the paired close event schedules the reconnect
    setStatus('error', 'Connection error');
    teardown();
  });

  ws.addEventListener('message', (ev) => {
    if (ev.data instanceof ArrayBuffer) {
      schedulePlayback(ev.data);
    } else if (typeof ev.data === 'string') {
      handleControlMessage(ev.data);
    }
  });
}

// ---- Control messages ----

function handleControlMessage(raw) {
  let msg;
  try { msg = JSON.parse(raw); } catch { return; }

  if (msg.type === 'transcript') {
    appendTranscript(msg.role, msg.text);
    return;
  }
  if (msg.type === 'terminals') {
    renderTerminals(msg.items);
    return;
  }
  if (msg.type === 'status') {
    // e.g. {"type":"status","status":"finished"}
    if (msg.status === 'laptop offline') {
      wantConnection = false;
      clearTimeout(reconnectTimer);
      setStatus('error', 'Laptop offline: restart voxa on your laptop, then Connect.');
      teardown();
      return;
    }
    setStatus('connected', msg.status || 'Connected');
  }
  if (msg.working_dir) {
    setInfo(`Dir: ${msg.working_dir}${msg.mode ? '  Mode: ' + msg.mode : ''}`);
  }
  if (msg.mode && !msg.working_dir) {
    setInfo(`Mode: ${msg.mode}`);
  }
}

// ---- Live transcript / captions ----

function appendTranscript(role, text) {
  if (!text) return;
  role = role === 'user' ? 'user' : 'agent';

  if (role !== lastRole || !lastSaidEl) {
    const line = document.createElement('div');
    line.className = 'line ' + role;
    const who = document.createElement('span');
    who.className = 'who';
    who.textContent = role === 'user' ? 'You' : 'Voxa';
    const said = document.createElement('span');
    said.className = 'said';
    line.appendChild(who);
    line.appendChild(said);
    transcriptEl.appendChild(line);
    lastSaidEl = said;
    lastRole = role;
  }
  lastSaidEl.textContent += text;
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function clearTranscript() {
  if (transcriptEl) transcriptEl.innerHTML = '';
  lastRole = null;
  lastSaidEl = null;
}

// ---- Mic capture pipeline ----

async function startCapture() {
  stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });

  micContext = new AudioContext();
  await micContext.audioWorklet.addModule('/static/pcm-worklet.js');

  sourceNode = micContext.createMediaStreamSource(stream);
  workletNode = new AudioWorkletNode(micContext, 'pcm-processor');

  workletNode.port.onmessage = (ev) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      // When muted, send silence (zero-filled, same size) instead of stopping the
      // stream, so the realtime channel stays alive and Voxa keeps talking.
      ws.send(muted ? new ArrayBuffer(ev.data.byteLength) : ev.data);
    }
  };

  sourceNode.connect(workletNode);
  // Do NOT connect workletNode to micContext.destination (no echo)
}

function stopCapture() {
  if (workletNode) { workletNode.disconnect(); workletNode = null; }
  if (sourceNode) { sourceNode.disconnect(); sourceNode = null; }
  if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
  if (micContext) { micContext.close(); micContext = null; }
}

// ---- Playback (24 kHz jitter buffer) ----

function ensurePlayContext() {
  if (!playCtx || playCtx.state === 'closed') {
    playCtx = new AudioContext({ sampleRate: PLAYBACK_SAMPLE_RATE });
    nextStartTime = 0;
  }
}

function schedulePlayback(arrayBuffer) {
  ensurePlayContext();

  // Convert Int16 little-endian -> Float32
  const int16 = new Int16Array(arrayBuffer);
  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) {
    float32[i] = int16[i] / 32768;
  }

  const audioBuffer = playCtx.createBuffer(1, float32.length, PLAYBACK_SAMPLE_RATE);
  audioBuffer.copyToChannel(float32, 0);

  const src = playCtx.createBufferSource();
  src.buffer = audioBuffer;
  src.connect(playCtx.destination);

  // Jitter buffer: schedule at least SCHEDULE_AHEAD seconds ahead of playback head,
  // but never in the past. Chain successive buffers end-to-end.
  const now = playCtx.currentTime;
  const earliest = now + SCHEDULE_AHEAD;
  if (nextStartTime < earliest) nextStartTime = earliest;

  src.start(nextStartTime);
  nextStartTime += audioBuffer.duration;
}

function stopPlayback() {
  if (playCtx) {
    playCtx.close();
    playCtx = null;
    nextStartTime = 0;
  }
}

// ---- Connect / Disconnect ----

async function connect() {
  const token = tokenInput.value.trim();
  if (!token) {
    setStatus('error', 'Enter a token first');
    return;
  }

  connectBtn.disabled = true;
  clearTranscript();

  try {
    // Start mic capture BEFORE opening WS so the two are ready together
    await startCapture();
    ensurePlayContext();
    wantConnection = true;
    openWebSocket(token);
  } catch (err) {
    wantConnection = false;
    setStatus('error', `Failed: ${err.message}`);
    stopCapture();
    stopPlayback();
    updateConnectUI(false);
  } finally {
    connectBtn.disabled = false;
  }
}

function disconnect() {
  wantConnection = false;
  clearTimeout(reconnectTimer);
  reconnectAttempt = 0;
  if (ws) {
    ws.close(1000, 'User disconnected');
    ws = null;
  }
  teardown();
}

function teardown() {
  stopCapture();
  stopPlayback();
  updateConnectUI(false);
  muted = false;
  muteBtn.textContent = 'Mute';
  setInfo('');
}

// ---- Mute toggle ----

function toggleMute() {
  muted = !muted;
  muteBtn.textContent = muted ? 'Unmute' : 'Mute';
  muteBtn.classList.toggle('muted', muted);
}

// ---- Init ----

function init() {
  tokenInput = document.getElementById('token');
  folderInput = document.getElementById('folder');
  terminalSelect = document.getElementById('terminal');
  connectBtn = document.getElementById('connect-btn');
  muteBtn = document.getElementById('mute-btn');
  stopBtn = document.getElementById('stop-btn');
  terminalsBtn = document.getElementById('terminals-btn');
  terminalsEl = document.getElementById('terminals');
  statusDot = document.getElementById('status-dot');
  statusText = document.getElementById('status-text');
  infoText = document.getElementById('info-text');
  transcriptEl = document.getElementById('transcript');

  // Prefill token + folder from URL (?token=... &dir=...)
  const params = new URLSearchParams(window.location.search);
  if (params.get('token')) tokenInput.value = params.get('token');
  if (params.get('dir')) folderInput.value = params.get('dir');

  if (params.get('terminal')) terminalSelect.value = params.get('terminal');

  // Push changes to the server mid-call.
  folderInput.addEventListener('change', sendFolder);
  terminalSelect.addEventListener('change', sendTerminal);

  connectBtn.addEventListener('click', () => {
    // Route on intent, not socket state: the button reflects wantConnection, so a
    // click mid-reconnect (socket CLOSED, button still "Disconnect") cancels the
    // pending retry instead of doubling up on mic capture and the WebSocket.
    if (wantConnection) {
      disconnect();
    } else {
      connect();
    }
  });

  muteBtn.addEventListener('click', toggleMute);
  stopBtn.addEventListener('click', sendStop);
  terminalsBtn.addEventListener('click', requestTerminals);

  setStatus('disconnected', 'Disconnected');
  updateConnectUI(false);
}

document.addEventListener('DOMContentLoaded', init);
