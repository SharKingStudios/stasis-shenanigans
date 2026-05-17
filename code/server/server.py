import argparse
import json
import math
import os
import queue
import random
import statistics
import struct
import sys
import threading
import time
import tkinter as tk
import wave
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None


BAUD_RATE = 115200
BASE_DIR = Path(__file__).resolve().parent
AUDIO_DIR = BASE_DIR / "static" / "audio"

# Localization tuning.
OBS_TTL_SECONDS = 5.5
LINK_WINDOW_SECONDS = 3.2
MAX_DISTANCE_M = 12.0
HUBER_M = 0.45
DISPLAY_SMOOTHING_ALPHA = 0.08
MAX_PLAYER_SPEED_MPS = 1.35
MESH_RELAX_STEPS = 24
POSITION_DEADBAND_M = 0.10
NEAR_ANCHOR_WEIGHT_POWER = 2.6
RSSI_AT_1_METER = -65.0 #-75.0 #-48.0
PATH_LOSS_N = 2.2

# Game tuning.
MAX_HP = 5
MAX_MANA = 100
MANA_REGEN_PER_SEC = 1000.0
FIREBALL_COST = 25
SHIELD_COST = 20
SHIELD_DURATION_SECONDS = 1.5
FIREBALL_COOLDOWN_SECONDS = 0.75
SHIELD_COOLDOWN_SECONDS = 1.25
SPELL_LOCKOUT_SECONDS = 1.0
FIREBALL_DAMAGE = 1
FIREBALL_LENGTH_M = 100.0
FIREBALL_WIDTH_M = 0.8
SIMPLE_FIREBALL_RANGE_M = 2.4
SHIELD_ARC_DEG = 110.0
WORLD_SEND_INTERVAL = 0.10
STATE_SEND_INTERVAL = 0.25

TUNE_PRESETS = {
    "easy": (255, 0.22, 0.45, -0.20, 0.58, 0.24, 0.14, 0.32, 0.0, 0.0, 1.10, 1500),
    "normal": (255, 0.30, 0.56, -0.28, 0.74, 0.30, 0.10, 0.40, 0.0, 0.0, 1.10, 1800),
    "hard": (255, 0.38, 0.70, -0.38, 0.95, 0.42, 0.02, 0.58, 0.0, 0.0, 1.35, 2200),
}

SPELL_FIREBALL = 1
SPELL_SHIELD = 2

EVENT_NONE = 0
EVENT_CAST_FIREBALL = 1
EVENT_CAST_SHIELD = 2
EVENT_HIT = 3
EVENT_BLOCK = 4
EVENT_DEATH = 5
EVENT_DENIED = 6
EVENT_PROP_HIT = 7

FLAG_ALIVE = 1 << 0
FLAG_SHIELD = 1 << 1

PLAYER_LABELS = {
    101: "Player 1",
    102: "Player A",
}

PROP_NAME = "Fan Face"
PROP_TARGET_RADIUS_M = 0.55
PROP_FAN_MS = 2500
PROP_FLASH_COUNT = 4
PROP_FLASH_ON_MS = 120
PROP_FLASH_OFF_MS = 120

AUDIO_MANIFEST = {
    "fireball_core": ["fireball_core_01.wav"],
    "fireball_voice": [
        "fireball_voice_01.wav",
        "fireball_voice_02.wav",
        "fireball_voice_03.wav",
        "fireball_voice_04.wav",
    ],
    "shield_core": ["shield_core_01.wav"],
    "shield_voice": [
        "shield_voice_01.wav",
        "shield_voice_02.wav",
        "shield_voice_03.wav",
    ],
    "hit": ["hit_bitcrush_01.wav"],
    "block": ["block_01.wav"],
    "prop": ["prop_hit_01.wav"],
    "denied": ["denied_01.wav"],
}

# Must match the field positions uploaded in code/field/field.ino.
DEFAULT_ANCHORS = {
    1: (0.0, 0.0),
    2: (2.0, 0.0),
    3: (0.0, 2.0),
}


@dataclass
class Observation:
    observer_id: int
    source_id: int
    rssi: int
    distance_m: float
    sequence: int
    sender_uptime_ms: int
    received_at: float
    confidence: float = 1.0
    sample_count: int = 1
    spread_m: float = 0.0


@dataclass
class PlayerStatus:
    player_id: int
    yaw_deg: float
    flags: int
    sequence: int
    uptime_ms: int
    received_at: float


@dataclass
class SpellCast:
    player_id: int
    spell_type: int
    yaw_deg: float
    confidence: int
    sequence: int
    uptime_ms: int
    received_at: float


@dataclass
class PlayerState:
    player_id: int
    x: float = 0.0
    y: float = 0.0
    yaw_deg: float = 0.0
    hp: int = MAX_HP
    mana: float = MAX_MANA
    alive: bool = True
    shield_until: float = 0.0
    fireball_ready_at: float = 0.0
    shield_ready_at: float = 0.0
    spell_lockout_until: float = 0.0
    last_seen_at: float = 0.0
    last_state_sent_at: float = 0.0
    last_event_seq: int = 0
    last_event_type: int = EVENT_NONE
    score_hits: int = 0
    last_action: str = "ready"
    dirty: bool = True

    def flags(self, now):
        flags = 0
        if self.alive and self.hp > 0:
            flags |= FLAG_ALIVE
        if self.shield_until > now:
            flags |= FLAG_SHIELD
        return flags


@dataclass
class VisualEvent:
    text: str
    event_type: int
    created_at: float
    expires_at: float
    caster_id: int = 0
    target_id: int = 0
    start: tuple | None = None
    end: tuple | None = None
    blocked: bool = False
    prop_hit: bool = False


def player_label(player_id):
    return PLAYER_LABELS.get(player_id, f"P{player_id}")


def nearest_origin_anchor(anchors):
    if not anchors:
        return (0.0, 0.0)
    return min(anchors.values(), key=lambda point: math.hypot(point[0], point[1]))


def wifi_qr_payload(ssid, password):
    if not ssid:
        return None
    escaped_ssid = str(ssid).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace('"', '\\"')
    escaped_password = str(password or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace('"', '\\"')
    auth = "WPA" if password else "nopass"
    return f"WIFI:T:{auth};S:{escaped_ssid};P:{escaped_password};;"


def qr_matrix(text):
    if not text:
        return None
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(border=1, box_size=1)
    qr.add_data(text)
    qr.make(fit=True)
    return qr.get_matrix()


def audio_pitch(lo, hi):
    return round(random.uniform(lo, hi), 3)


class PhoneAudioHub:
    def __init__(self):
        self.clients = []
        self.lock = threading.Lock()
        self.sequence = 1

    def subscribe(self):
        client_queue = queue.Queue(maxsize=32)
        with self.lock:
            self.clients.append(client_queue)
        return client_queue

    def unsubscribe(self, client_queue):
        with self.lock:
            if client_queue in self.clients:
                self.clients.remove(client_queue)

    def client_count(self):
        with self.lock:
            return len(self.clients)

    def broadcast(self, payload):
        payload = dict(payload)
        payload["eventSeq"] = self.sequence
        self.sequence = (self.sequence + 1) & 0xFFFF
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            try:
                client.put_nowait(payload)
            except queue.Full:
                pass


PHONE_PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Spell Arena Audio</title>
  <style>
    :root { color-scheme: dark; --bg:#101114; --panel:#191b20; --line:#2b3038; --text:#f1efe7; --muted:#a7a194; --hot:#ff6b35; --cool:#4dc3ff; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background: var(--bg); color: var(--text); display: grid; place-items: center; }
    main { width: min(92vw, 430px); border: 1px solid var(--line); background: var(--panel); padding: 24px; }
    h1 { margin: 0 0 8px; font-size: 28px; letter-spacing: 0; }
    p { color: var(--muted); margin: 8px 0; line-height: 1.35; }
    button { width: 100%; margin-top: 18px; padding: 16px; border: 0; background: var(--hot); color: #190904; font-weight: 800; font-size: 18px; }
    .bar { height: 12px; border: 1px solid var(--line); margin: 18px 0 8px; overflow: hidden; }
    .fill { height: 100%; width: 0%; background: var(--cool); transition: width .18s ease; }
    .status { display: grid; grid-template-columns: 1fr auto; gap: 8px; border-top: 1px solid var(--line); padding-top: 14px; margin-top: 18px; font-size: 14px; }
    .pulse { width: 12px; height: 12px; background: #555; align-self: center; }
    .pulse.on { background: var(--cool); box-shadow: 0 0 18px var(--cool); }
    .last { min-height: 44px; margin-top: 18px; padding: 12px; background: #111317; border: 1px solid var(--line); color: var(--text); }
  </style>
</head>
<body>
  <main>
    <h1>Spell Arena</h1>
    <p id="lead">Loading spell audio...</p>
    <div class="bar"><div id="fill" class="fill"></div></div>
    <button id="arm">Tap to arm audio</button>
    <div class="status">
      <span>Audio</span><span id="audio">loading</span>
      <span>Server</span><span id="server">connecting</span>
      <span>Events</span><span id="events">0</span>
      <span>Ready</span><span id="readyLight" class="pulse"></span>
    </div>
    <div id="last" class="last">Waiting for spells.</div>
  </main>
  <script>
    const fill = document.getElementById('fill');
    const lead = document.getElementById('lead');
    const arm = document.getElementById('arm');
    const audioState = document.getElementById('audio');
    const serverState = document.getElementById('server');
    const eventState = document.getElementById('events');
    const readyLight = document.getElementById('readyLight');
    const last = document.getElementById('last');
    let ctx, manifest, buffers = {}, eventCount = 0, armed = false;

    async function loadAudio() {
      manifest = await fetch('/manifest.json').then(r => r.json());
      const files = Object.values(manifest.assets).flat();
      if ('caches' in window) {
        const cache = await caches.open('spell-arena-audio-v1');
        await cache.addAll(files.map(f => '/audio/' + f));
      }
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        const data = await fetch('/audio/' + file).then(r => r.arrayBuffer());
        buffers[file] = await ctx.decodeAudioData(data);
        fill.style.width = Math.round(((i + 1) / files.length) * 100) + '%';
      }
      lead.textContent = 'Audio cached. Tap once before the duel starts.';
      audioState.textContent = 'cached';
    }

    function playFile(file, rate, gainValue = 0.9, delay = 0) {
      const buffer = buffers[file];
      if (!armed || !buffer) return;
      const src = ctx.createBufferSource();
      const gain = ctx.createGain();
      src.buffer = buffer;
      src.playbackRate.value = rate || 1;
      gain.gain.value = gainValue;
      src.connect(gain).connect(ctx.destination);
      src.start(ctx.currentTime + delay);
    }

    function pick(list, index) {
      if (!list || list.length === 0) return null;
      const idx = Number.isFinite(index) ? index % list.length : Math.floor(Math.random() * list.length);
      return list[idx];
    }

    function handleAudio(event) {
      eventCount++;
      eventState.textContent = String(eventCount);
      last.textContent = `${event.casterLabel || 'Arena'} ${event.type.replace('_', ' ')} ${event.targetLabel || ''}`.trim();
      const a = manifest.assets;
      if (event.type === 'fireball_cast') {
        playFile(pick(a.fireball_core, 0), event.corePitch, 0.85);
        playFile(pick(a.fireball_voice, event.voiceIndex), event.voicePitch, 0.75, 0.02);
      } else if (event.type === 'shield_cast') {
        playFile(pick(a.shield_core, 0), event.corePitch, 0.8);
        playFile(pick(a.shield_voice, event.voiceIndex), event.voicePitch, 0.65, 0.03);
      } else if (event.type === 'hit') {
        playFile(pick(a.hit, 0), event.corePitch, 1.0);
      } else if (event.type === 'block') {
        playFile(pick(a.block, 0), event.corePitch, 1.0);
      } else if (event.type === 'prop_hit') {
        playFile(pick(a.prop, 0), event.corePitch, 1.0);
      } else if (event.type === 'denied') {
        playFile(pick(a.denied, 0), event.corePitch, 0.8);
      }
    }

    arm.addEventListener('click', async () => {
      await ctx.resume();
      armed = true;
      audioState.textContent = 'armed';
      readyLight.classList.add('on');
      arm.textContent = 'Audio armed';
      arm.disabled = true;
    });

    function connectEvents() {
      const es = new EventSource('/events');
      es.onopen = () => serverState.textContent = 'connected';
      es.onerror = () => serverState.textContent = 'reconnecting';
      es.addEventListener('audio', e => handleAudio(JSON.parse(e.data)));
    }

    loadAudio().then(connectEvents).catch(err => {
      lead.textContent = 'Audio failed to load. Refresh after joining the hotspot.';
      audioState.textContent = 'error';
      console.error(err);
    });
  </script>
</body>
</html>
"""


def ensure_audio_assets():
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    generators = {
        "fireball_core_01.wav": lambda p: write_sfx(p, "fireball_core", 0.75),
        "shield_core_01.wav": lambda p: write_sfx(p, "shield_core", 0.65),
        "hit_bitcrush_01.wav": lambda p: write_sfx(p, "hit", 0.42),
        "block_01.wav": lambda p: write_sfx(p, "block", 0.48),
        "prop_hit_01.wav": lambda p: write_sfx(p, "prop", 0.95),
        "denied_01.wav": lambda p: write_sfx(p, "denied", 0.26),
    }
    for i, name in enumerate(AUDIO_MANIFEST["fireball_voice"], start=1):
        generators[name] = lambda p, idx=i: write_sfx(p, "fireball_voice", 0.55, idx)
    for i, name in enumerate(AUDIO_MANIFEST["shield_voice"], start=1):
        generators[name] = lambda p, idx=i: write_sfx(p, "shield_voice", 0.52, idx)

    for filename, generator in generators.items():
        path = AUDIO_DIR / filename
        if not path.exists():
            generator(path)


def write_sfx(path, kind, seconds, variant=1):
    rate = 22050
    total = int(rate * seconds)
    samples = []
    seed = sum(ord(c) for c in kind) + variant * 97
    rng = random.Random(seed)
    for i in range(total):
        t = i / rate
        env = max(0.0, 1.0 - i / max(1, total))
        if kind == "fireball_core":
            freq = 80 + 780 * t / seconds
            raw = math.sin(2 * math.pi * freq * t) + 0.45 * math.sin(2 * math.pi * (freq * 0.51) * t)
            raw += rng.uniform(-0.55, 0.55)
        elif kind == "fireball_voice":
            freq = 180 + 32 * variant + 90 * math.sin(2 * math.pi * 4.2 * t)
            raw = math.sin(2 * math.pi * freq * t) + 0.35 * math.sin(2 * math.pi * freq * 2.0 * t)
            raw += rng.uniform(-0.18, 0.18)
        elif kind == "shield_core":
            freq = 440 + 140 * math.sin(2 * math.pi * 7.0 * t)
            raw = math.sin(2 * math.pi * freq * t) + 0.35 * math.sin(2 * math.pi * 880 * t)
        elif kind == "shield_voice":
            freq = 260 + 28 * variant
            raw = math.sin(2 * math.pi * freq * t) * (0.7 + 0.3 * math.sin(2 * math.pi * 11 * t))
        elif kind == "hit":
            raw = rng.uniform(-1.0, 1.0) * (1.0 if i < total * 0.45 else 0.45)
        elif kind == "block":
            raw = math.sin(2 * math.pi * (720 - 320 * t / seconds) * t) + rng.uniform(-0.25, 0.25)
        elif kind == "prop":
            raw = math.sin(2 * math.pi * 95 * t) + 0.55 * math.sin(2 * math.pi * 47 * t) + rng.uniform(-0.25, 0.25)
        else:
            raw = math.sin(2 * math.pi * 250 * t)

        if kind == "denied":
            env = 1.0 if int(t * 24) % 2 == 0 else 0.18
        quantized = round(max(-1.0, min(1.0, raw)) * 7) / 7.0
        samples.append(int(max(-1.0, min(1.0, quantized * env * 0.55)) * 32767))

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))


def make_phone_handler(audio_hub):
    class PhoneHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/":
                self.send_bytes(PHONE_PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/manifest.json":
                body = json.dumps({"assets": AUDIO_MANIFEST}).encode("utf-8")
                self.send_bytes(body, "application/json")
            elif path == "/events":
                self.serve_events()
            elif path.startswith("/audio/"):
                self.serve_audio(path[len("/audio/"):])
            else:
                self.send_error(404)

        def send_bytes(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if content_type.startswith("text/html") else "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)

        def serve_audio(self, filename):
            filename = os.path.basename(unquote(filename))
            path = AUDIO_DIR / filename
            if not path.exists():
                self.send_error(404)
                return
            self.send_bytes(path.read_bytes(), "audio/wav")

        def serve_events(self):
            client = audio_hub.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        payload = client.get(timeout=15.0)
                        line = f"event: audio\ndata: {json.dumps(payload)}\n\n".encode("utf-8")
                    except queue.Empty:
                        line = b": keepalive\n\n"
                    self.wfile.write(line)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                audio_hub.unsubscribe(client)

    return PhoneHandler


class PhoneAudioServer:
    def __init__(self, host, port, audio_hub):
        self.host = host
        self.port = port
        self.audio_hub = audio_hub
        self.httpd = None

    def start(self):
        handler = make_phone_handler(self.audio_hub)
        self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


class LinkFilter:
    def __init__(self):
        self.samples = deque(maxlen=80)
        self.last_filtered_distance = None

    def add(self, obs):
        if not math.isfinite(obs.distance_m):
            return
        if obs.distance_m < 0.05 or obs.distance_m > MAX_DISTANCE_M:
            return
        self.samples.append(obs)
        self._drop_old()

    def _drop_old(self):
        cutoff = time.time() - OBS_TTL_SECONDS
        while self.samples and self.samples[0].received_at < cutoff:
            self.samples.popleft()

    def filtered(self):
        self._drop_old()
        if not self.samples:
            return None

        now = time.time()
        recent = [s for s in self.samples if now - s.received_at <= LINK_WINDOW_SECONDS]
        if not recent:
            recent = list(self.samples)

        distances = [s.distance_m for s in recent]
        median_d = statistics.median(distances)
        deviations = [abs(d - median_d) for d in distances]
        mad = statistics.median(deviations) if deviations else 0.0
        keep_band = max(0.25, mad * 2.2)
        kept = [s for s in recent if abs(s.distance_m - median_d) <= keep_band] or recent

        weighted_logs = []
        weights = []
        for sample in kept:
            age = max(0.0, now - sample.received_at)
            age_weight = math.exp(-age / 1.0)
            distance_weight = 1.0 / max(0.7, sample.distance_m)
            weight = age_weight * distance_weight
            weighted_logs.append(math.log(max(0.05, sample.distance_m)) * weight)
            weights.append(weight)

        distance = math.exp(sum(weighted_logs) / max(1e-9, sum(weights)))
        if self.last_filtered_distance is not None:
            distance = self.last_filtered_distance * 0.82 + distance * 0.18
        self.last_filtered_distance = distance

        newest = kept[-1]
        rssi_values = [s.rssi for s in kept]
        confidence = min(1.0, len(kept) / 10.0)
        if mad > 0.25:
            confidence *= 0.55
        if mad > 0.45:
            confidence *= 0.35

        return Observation(
            observer_id=newest.observer_id,
            source_id=newest.source_id,
            rssi=int(statistics.median(rssi_values)),
            distance_m=distance,
            sequence=newest.sequence,
            sender_uptime_ms=newest.sender_uptime_ms,
            received_at=newest.received_at,
            confidence=max(0.12, confidence),
            sample_count=len(kept),
            spread_m=mad,
        )


class MeshSolver:
    def __init__(self):
        self.anchors = dict(DEFAULT_ANCHORS)
        self.anchor_seen_at = {}
        self.links = {}
        self.positions = {}
        self.display_positions = {}
        self.last_display_at = {}

    def _arena_center(self):
        if not self.anchors:
            return (0.0, 0.0)
        xs = [p[0] for p in self.anchors.values()]
        ys = [p[1] for p in self.anchors.values()]
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    def set_anchor(self, node_id, x, y):
        self.anchors[node_id] = (x, y)
        self.anchor_seen_at[node_id] = time.time()

    def add_observation(self, obs):
        key = tuple(sorted((obs.observer_id, obs.source_id)))
        if key not in self.links:
            self.links[key] = LinkFilter()
        self.links[key].add(obs)

        touches_anchor = obs.observer_id in self.anchors or obs.source_id in self.anchors
        for node_id in (obs.observer_id, obs.source_id):
            if touches_anchor and node_id not in self.anchors and node_id not in self.positions:
                self.positions[node_id] = self._arena_center()
            if node_id in self.anchors:
                self.anchor_seen_at[node_id] = obs.received_at

    def fresh_observations(self):
        observations = []
        stale = []
        for key, link_filter in self.links.items():
            obs = link_filter.filtered()
            if obs is None:
                stale.append(key)
            else:
                observations.append(obs)
        for key in stale:
            del self.links[key]
        return observations

    def link_rows(self):
        rows = list(self.fresh_observations())
        rows.sort(key=lambda obs: (obs.observer_id, obs.source_id))
        return rows

    def field_health(self):
        now = time.time()
        health = {}
        rows = self.link_rows()
        for field_id in self.anchors:
            involved = [obs for obs in rows if field_id in (obs.observer_id, obs.source_id)]
            last_seen = self.anchor_seen_at.get(field_id, 0.0)
            if not involved and now - last_seen > 5.0:
                health[field_id] = ("bad", 0.0)
                continue
            if not involved:
                health[field_id] = ("warn", 0.25)
                continue
            avg_conf = sum(obs.confidence for obs in involved) / len(involved)
            age = min(now - obs.received_at for obs in involved)
            if age > 4.0 or avg_conf < 0.25:
                health[field_id] = ("bad", avg_conf)
            elif age > 2.0 or avg_conf < 0.5:
                health[field_id] = ("warn", avg_conf)
            else:
                health[field_id] = ("ok", avg_conf)
        return health

    def solve(self):
        fresh = self.fresh_observations()
        unknown_ids = sorted(set(self.positions) - set(self.anchors))

        for node_id in unknown_ids:
            anchor_edges = [
                obs for obs in fresh
                if node_id in (obs.observer_id, obs.source_id)
                and self._other_id(obs, node_id) in self.anchors
            ]
            solved = self._solve_node_from_anchors(node_id, anchor_edges)
            if solved is not None:
                self.positions[node_id] = solved

        for _ in range(MESH_RELAX_STEPS):
            for obs in fresh:
                a_anchor = obs.observer_id in self.anchors
                b_anchor = obs.source_id in self.anchors
                if a_anchor != b_anchor:
                    self._relax_edge(obs, step=0.014)

        return self._display_positions(unknown_ids, fresh)

    def _other_id(self, obs, node_id):
        return obs.source_id if obs.observer_id == node_id else obs.observer_id

    def _node_pos(self, node_id, display=False):
        if node_id in self.anchors:
            return self.anchors[node_id]
        if display and node_id in self.display_positions:
            return self.display_positions[node_id]
        return self.positions.get(node_id)

    def _solve_node_from_anchors(self, node_id, observations):
        edges = []
        for obs in observations:
            anchor_id = self._other_id(obs, node_id)
            if anchor_id in self.anchors and 0.05 <= obs.distance_m <= MAX_DISTANCE_M:
                age = max(0.0, time.time() - obs.received_at)
                weight = obs.confidence * math.exp(-age / OBS_TTL_SECONDS)
                weight *= 1.0 / max(0.45, obs.distance_m ** NEAR_ANCHOR_WEIGHT_POWER)
                if obs.spread_m > 0.18:
                    weight *= 0.55
                edges.append((self.anchors[anchor_id], obs.distance_m, weight))
        if len(edges) < 2:
            return None

        x, y = self.positions.get(node_id, self._weighted_anchor_guess(edges))
        for _ in range(12):
            aa = ab = bb = av = bv = 0.0
            for (ax, ay), target, weight in edges:
                dx = x - ax
                dy = y - ay
                predicted = max(0.05, math.hypot(dx, dy))
                residual = predicted - target
                robust = min(1.0, HUBER_M / max(HUBER_M, abs(residual)))
                w = weight * robust
                jx = dx / predicted
                jy = dy / predicted
                aa += w * jx * jx
                ab += w * jx * jy
                bb += w * jy * jy
                av += w * jx * residual
                bv += w * jy * residual

            det = aa * bb - ab * ab
            if abs(det) < 1e-8:
                break
            step_x = -(av * bb - bv * ab) / det
            step_y = -(aa * bv - ab * av) / det
            step_len = math.hypot(step_x, step_y)
            if step_len > 0.65:
                scale = 0.65 / step_len
                step_x *= scale
                step_y *= scale
            x += step_x
            y += step_y
            if step_len < 0.01:
                break
        return self._clamp_to_reasonable_bounds((x, y))

    def _weighted_anchor_guess(self, edges):
        sx = sy = sw = 0.0
        for (x, y), distance, weight in edges:
            w = weight / max(0.25, distance ** (NEAR_ANCHOR_WEIGHT_POWER + 0.7))
            sx += x * w
            sy += y * w
            sw += w
        return self._arena_center() if sw <= 0 else (sx / sw, sy / sw)

    def _relax_edge(self, obs, step):
        a_id = obs.observer_id
        b_id = obs.source_id
        a_fixed = a_id in self.anchors
        b_fixed = b_id in self.anchors
        if a_fixed and b_fixed:
            return

        a = self._node_pos(a_id)
        b = self._node_pos(b_id)
        if a is None or b is None:
            return

        dx = b[0] - a[0]
        dy = b[1] - a[1]
        current = math.hypot(dx, dy)
        if current < 0.001:
            return

        target = max(0.05, min(obs.distance_m, MAX_DISTANCE_M))
        error = current - target
        robust = min(1.0, HUBER_M / max(HUBER_M, abs(error)))
        range_weight = 1.0 / max(1.0, target ** 1.8)
        spread_weight = 0.5 if obs.spread_m > 0.18 else 1.0
        move = step * error * obs.confidence * robust * range_weight * spread_weight
        ux = dx / current
        uy = dy / current

        if not a_fixed and not b_fixed:
            ax, ay = self.positions[a_id]
            bx, by = self.positions[b_id]
            self.positions[a_id] = (ax + ux * move * 0.5, ay + uy * move * 0.5)
            self.positions[b_id] = (bx - ux * move * 0.5, by - uy * move * 0.5)
        elif not a_fixed:
            ax, ay = self.positions[a_id]
            self.positions[a_id] = (ax + ux * move, ay + uy * move)
        elif not b_fixed:
            bx, by = self.positions[b_id]
            self.positions[b_id] = (bx - ux * move, by - uy * move)

        if not a_fixed:
            self.positions[a_id] = self._clamp_to_reasonable_bounds(self.positions[a_id])
        if not b_fixed:
            self.positions[b_id] = self._clamp_to_reasonable_bounds(self.positions[b_id])

    def _display_positions(self, unknown_ids, observations):
        now = time.time()
        result = {}
        link_quality = self._link_quality_by_node(observations)
        for node_id in unknown_ids:
            raw = self.positions[node_id]
            if node_id not in self.display_positions:
                self.display_positions[node_id] = raw
                self.last_display_at[node_id] = now
                result[node_id] = raw
                continue

            previous = self.display_positions[node_id]
            dt = max(0.02, now - self.last_display_at.get(node_id, now))
            self.last_display_at[node_id] = now
            raw_delta = math.hypot(raw[0] - previous[0], raw[1] - previous[1])
            link_confidence, link_spread = link_quality.get(node_id, (0.0, 1.0))
            if link_confidence > 0.45 and link_spread < 0.18 and raw_delta < POSITION_DEADBAND_M:
                result[node_id] = previous
                continue
            target_x = previous[0] * (1.0 - DISPLAY_SMOOTHING_ALPHA) + raw[0] * DISPLAY_SMOOTHING_ALPHA
            target_y = previous[1] * (1.0 - DISPLAY_SMOOTHING_ALPHA) + raw[1] * DISPLAY_SMOOTHING_ALPHA
            dx = target_x - previous[0]
            dy = target_y - previous[1]
            max_step = max(0.015, MAX_PLAYER_SPEED_MPS * dt)
            step_len = math.hypot(dx, dy)
            if step_len > max_step:
                scale = max_step / step_len
                dx *= scale
                dy *= scale
            display = self._clamp_to_reasonable_bounds((previous[0] + dx, previous[1] + dy))
            self.display_positions[node_id] = display
            result[node_id] = display
        return result

    def _link_quality_by_node(self, observations):
        related = {}
        for obs in observations:
            related.setdefault(obs.observer_id, []).append(obs)
            related.setdefault(obs.source_id, []).append(obs)

        qualities = {}
        for node_id, rows in related.items():
            confidence = sum(obs.confidence for obs in rows) / len(rows)
            spread = sum(obs.spread_m for obs in rows) / len(rows)
            qualities[node_id] = (confidence, spread)
        return qualities

    def _clamp_to_reasonable_bounds(self, pos):
        xs = [p[0] for p in self.anchors.values()]
        ys = [p[1] for p in self.anchors.values()]
        if not xs or not ys:
            return pos
        margin = 2.5
        return (
            max(min(xs) - margin, min(max(xs) + margin, pos[0])),
            max(min(ys) - margin, min(max(ys) + margin, pos[1])),
        )


class GameEngine:
    def __init__(self, outgoing, simple_combat=False, audio_hub=None, prop_position_getter=None):
        self.players = {}
        self.events = deque(maxlen=24)
        self.outgoing = outgoing
        self.simple_combat = simple_combat
        self.audio_hub = audio_hub
        self.prop_position_getter = prop_position_getter or (lambda: (0.0, 0.0))
        self.last_world_sent_at = 0.0
        self.world_sequence = 0
        self.event_sequence = 1
        self.seen_casts = set()
        self.prop_sequence = 1

    def ensure_player(self, player_id):
        if player_id not in self.players:
            self.players[player_id] = PlayerState(player_id=player_id, last_seen_at=time.time())
            self.add_event(f"{player_label(player_id)} joined", EVENT_NONE)
        return self.players[player_id]

    def update_positions(self, positions):
        now = time.time()
        for player_id, point in positions.items():
            player = self.ensure_player(player_id)
            player.x, player.y = point
            player.last_seen_at = now

    def update_status(self, status):
        player = self.ensure_player(status.player_id)
        player.yaw_deg = normalize_deg(status.yaw_deg)
        player.last_seen_at = status.received_at

    def handle_cast(self, cast):
        key = (cast.player_id, cast.sequence)
        if key in self.seen_casts:
            return
        self.seen_casts.add(key)
        player = self.ensure_player(cast.player_id)
        player.yaw_deg = normalize_deg(cast.yaw_deg)
        player.last_seen_at = cast.received_at

        if cast.spell_type == SPELL_FIREBALL:
            self.cast_fireball(player)
        elif cast.spell_type == SPELL_SHIELD:
            self.cast_shield(player)

    def tick(self):
        now = time.time()
        for player in self.players.values():
            old_mana = int(player.mana)
            if player.alive:
                player.mana = min(MAX_MANA, player.mana + MANA_REGEN_PER_SEC / 30.0)
            if int(player.mana) != old_mana:
                player.dirty = True
            if player.shield_until <= now:
                pass
            self.maybe_send_state(player, now)

        if now - self.last_world_sent_at >= WORLD_SEND_INTERVAL:
            self.last_world_sent_at = now
            self.send_world_state()

        while self.events and self.events[0].expires_at < now:
            self.events.popleft()

    def cast_shield(self, player):
        now = time.time()
        if not player.alive:
            return
        if now < player.spell_lockout_until or now < player.shield_ready_at or player.mana < SHIELD_COST:
            self.deny(player, "shield denied")
            return
        player.mana -= SHIELD_COST
        player.shield_until = now + SHIELD_DURATION_SECONDS
        player.shield_ready_at = now + SHIELD_COOLDOWN_SECONDS
        player.spell_lockout_until = max(
            now + SPELL_LOCKOUT_SECONDS,
            player.shield_until + 0.15,
        )
        player.last_event_type = EVENT_CAST_SHIELD
        player.last_action = "shield"
        player.dirty = True
        self.add_event(f"{player_label(player.player_id)} shield", EVENT_CAST_SHIELD, caster_id=player.player_id)
        self.broadcast_audio("shield_cast", caster_id=player.player_id)
        self.send_state(player, EVENT_CAST_SHIELD)

    def cast_fireball(self, caster):
        now = time.time()
        if not caster.alive:
            return
        if (
            now < caster.spell_lockout_until
            or now < caster.fireball_ready_at
            or caster.shield_until > now
            or caster.mana < FIREBALL_COST
        ):
            self.deny(caster, "fireball denied")
            return

        caster.mana -= FIREBALL_COST
        caster.fireball_ready_at = now + FIREBALL_COOLDOWN_SECONDS
        caster.spell_lockout_until = now + SPELL_LOCKOUT_SECONDS
        caster.last_action = "fireball"
        caster.dirty = True
        self.add_event(f"{player_label(caster.player_id)} fireball", EVENT_CAST_FIREBALL, caster_id=caster.player_id)
        self.broadcast_audio("fireball_cast", caster_id=caster.player_id)
        self.send_state(caster, EVENT_CAST_FIREBALL)

        start = (caster.x, caster.y)
        direction = yaw_to_vec(caster.yaw_deg)
        end = (caster.x + direction[0] * FIREBALL_LENGTH_M, caster.y + direction[1] * FIREBALL_LENGTH_M)

        targets = []
        for target in self.players.values():
            if target.player_id == caster.player_id or not target.alive:
                continue

            if self.simple_combat:
                distance = math.hypot(target.x - caster.x, target.y - caster.y)
                if distance <= SIMPLE_FIREBALL_RANGE_M:
                    targets.append((distance, 0.0, "player", target))
            else:
                along, lateral = fireball_coordinates(start, direction, (target.x, target.y))
                if 0.0 <= along <= FIREBALL_LENGTH_M and abs(lateral) <= FIREBALL_WIDTH_M / 2.0:
                    targets.append((along, abs(lateral), "player", target))

        prop_pos = self.prop_position()
        prop_along, prop_lateral = fireball_coordinates(start, direction, prop_pos)
        if 0.0 <= prop_along <= FIREBALL_LENGTH_M and abs(prop_lateral) <= PROP_TARGET_RADIUS_M:
            targets.append((prop_along, abs(prop_lateral), "prop", prop_pos))

        targets.sort(key=lambda item: (item[0], item[1]))
        if not targets:
            self.events.append(VisualEvent(
                text=f"{player_label(caster.player_id)} missed",
                event_type=EVENT_CAST_FIREBALL,
                created_at=now,
                expires_at=now + 0.55,
                caster_id=caster.player_id,
                start=start,
                end=end,
            ))
            return

        target_kind = targets[0][2]
        target = targets[0][3]
        if target_kind == "prop":
            self.hit_prop(caster, start, prop_pos)
            return

        if self.blocks_attack(caster, target):
            target.last_event_type = EVENT_BLOCK
            target.last_action = "blocked"
            target.dirty = True
            self.add_event(
                f"{player_label(target.player_id)} blocked {player_label(caster.player_id)}",
                EVENT_BLOCK,
                caster_id=caster.player_id,
                target_id=target.player_id,
                start=start,
                end=(target.x, target.y),
                blocked=True,
            )
            self.broadcast_audio("block", caster_id=caster.player_id, target_id=target.player_id)
            self.send_state(target, EVENT_BLOCK)
            return

        target.hp = max(0, target.hp - FIREBALL_DAMAGE)
        caster.score_hits += 1
        caster.dirty = True
        target.last_action = "hit"
        target.dirty = True
        event_type = EVENT_DEATH if target.hp <= 0 else EVENT_HIT
        if target.hp <= 0:
            target.alive = False
        self.add_event(
            f"{player_label(caster.player_id)} hit {player_label(target.player_id)}",
            event_type,
            caster_id=caster.player_id,
            target_id=target.player_id,
            start=start,
            end=(target.x, target.y),
        )
        self.broadcast_audio("hit", caster_id=caster.player_id, target_id=target.player_id)
        self.send_state(caster, EVENT_NONE)
        self.send_state(target, event_type)

    def prop_position(self):
        try:
            return self.prop_position_getter()
        except Exception:
            return (0.0, 0.0)

    def hit_prop(self, caster, start, prop_pos):
        caster.score_hits += 1
        caster.last_action = "prop hit"
        caster.dirty = True
        self.outgoing.put(
            f"PROP,{self.prop_sequence},{PROP_FAN_MS},{PROP_FLASH_COUNT},{PROP_FLASH_ON_MS},{PROP_FLASH_OFF_MS}"
        )
        self.prop_sequence = (self.prop_sequence + 1) & 0xFFFF
        self.add_event(
            f"{player_label(caster.player_id)} hit {PROP_NAME}",
            EVENT_PROP_HIT,
            caster_id=caster.player_id,
            target_id=0,
            start=start,
            end=prop_pos,
            prop_hit=True,
        )
        self.broadcast_audio("prop_hit", caster_id=caster.player_id, target_label=PROP_NAME)
        self.send_state(caster, EVENT_NONE)

    def blocks_attack(self, caster, target):
        now = time.time()
        if target.shield_until <= now:
            return False
        if self.simple_combat:
            return True
        angle_to_attacker = vec_to_yaw(caster.x - target.x, caster.y - target.y)
        return abs(angle_delta(target.yaw_deg, angle_to_attacker)) <= SHIELD_ARC_DEG / 2.0

    def deny(self, player, reason):
        player.dirty = True
        player.last_action = "denied"
        self.add_event(f"{player_label(player.player_id)} {reason}", EVENT_DENIED, caster_id=player.player_id)
        self.broadcast_audio("denied", caster_id=player.player_id)
        self.send_state(player, EVENT_DENIED)

    def add_event(self, text, event_type, caster_id=0, target_id=0, start=None, end=None, blocked=False, prop_hit=False):
        now = time.time()
        self.events.append(VisualEvent(
            text=text,
            event_type=event_type,
            created_at=now,
            expires_at=now + 2.0,
            caster_id=caster_id,
            target_id=target_id,
            start=start,
            end=end,
            blocked=blocked,
            prop_hit=prop_hit or event_type == EVENT_PROP_HIT,
        ))

    def broadcast_audio(self, event_type, caster_id=0, target_id=0, target_label=None):
        if self.audio_hub is None:
            return
        voice_pool = 4 if event_type == "fireball_cast" else 3
        payload = {
            "type": event_type,
            "casterId": caster_id,
            "targetId": target_id,
            "casterLabel": player_label(caster_id) if caster_id else "",
            "targetLabel": target_label or (player_label(target_id) if target_id else ""),
            "corePitch": audio_pitch(0.86, 1.18),
            "voicePitch": audio_pitch(0.93, 1.08),
            "voiceIndex": random.randrange(voice_pool),
        }
        self.audio_hub.broadcast(payload)

    def trigger_prop_test(self):
        self.outgoing.put(
            f"PROP,{self.prop_sequence},{PROP_FAN_MS},{PROP_FLASH_COUNT},{PROP_FLASH_ON_MS},{PROP_FLASH_OFF_MS}"
        )
        self.prop_sequence = (self.prop_sequence + 1) & 0xFFFF
        self.add_event(f"{PROP_NAME} test", EVENT_PROP_HIT, end=self.prop_position(), prop_hit=True)
        self.broadcast_audio("prop_hit", target_label=PROP_NAME)

    def trigger_audio_test(self, event_type):
        mapped = {
            "fireball": "fireball_cast",
            "shield": "shield_cast",
            "hit": "hit",
            "block": "block",
            "prop": "prop_hit",
            "denied": "denied",
        }.get(event_type)
        if mapped:
            self.broadcast_audio(mapped, caster_id=101, target_id=102 if mapped in ("hit", "block") else 0,
                                 target_label=PROP_NAME if mapped == "prop_hit" else None)

    def maybe_send_state(self, player, now):
        if player.dirty or now - player.last_state_sent_at >= STATE_SEND_INTERVAL:
            self.send_state(player, EVENT_NONE)

    def send_state(self, player, event_type):
        if event_type != EVENT_NONE:
            player.last_event_seq = self.event_sequence
            player.last_event_type = event_type
            self.event_sequence = (self.event_sequence + 1) & 0xFFFF
        player.last_state_sent_at = time.time()
        player.dirty = False
        flags = player.flags(time.time())
        line = (
            f"STATE,{player.player_id},{player.hp},{int(player.mana)},{flags},"
            f"{player.last_event_seq},{player.last_event_type}"
        )
        self.outgoing.put(line)

    def send_world_state(self):
        self.world_sequence = (self.world_sequence + 1) & 0xFFFF
        parts = ["WORLD", str(self.world_sequence)]
        now = time.time()
        for player_id in sorted(self.players)[:4]:
            player = self.players[player_id]
            parts.extend([
                str(player.player_id),
                str(int(round(player.x * 100.0))),
                str(int(round(player.y * 100.0))),
                str(int(round(normalize_deg(player.yaw_deg) * 10.0))),
                str(player.hp),
                str(int(player.mana)),
                str(player.flags(now)),
            ])
        self.outgoing.put(",".join(parts))

    def recenter_all(self):
        self.outgoing.put("RECENTER,255")
        self.add_event("recenter all", EVENT_NONE)


class SerialTransport:
    def __init__(self, port_name, baud, incoming, outgoing):
        self.port_name = normalize_port_name(port_name)
        self.baud = baud
        self.incoming = incoming
        self.outgoing = outgoing

    def run(self):
        try:
            with serial.Serial(self.port_name, self.baud, timeout=0.02, write_timeout=0.02) as ser:
                time.sleep(1.0)
                self.incoming.put(f"CONNECTED,{self.port_name},{self.baud}")
                while True:
                    try:
                        while True:
                            line = self.outgoing.get_nowait()
                            ser.write((line + "\n").encode("utf-8"))
                    except queue.Empty:
                        pass

                    raw = ser.readline()
                    if raw:
                        self.incoming.put(raw.decode("utf-8", errors="replace").strip())
        except Exception as exc:
            message = f"Serial error on {self.port_name}: {exc}"
            if "access is denied" in str(exc).lower() or "permission" in str(exc).lower():
                message += " | Close Arduino Serial Monitor/Plotter or another server using this COM port."
            if "file not found" in str(exc).lower() or "cannot find" in str(exc).lower():
                message += " | Run with --list-ports and use a listed name like COM4."
            print(message)
            self.incoming.put(message)


class FakeTransport:
    def __init__(self, incoming, outgoing):
        self.incoming = incoming
        self.outgoing = outgoing

    def run(self):
        t = 0.0
        anchors = DEFAULT_ANCHORS
        for node_id, (x, y) in anchors.items():
            self.incoming.put(f"ANCHOR,{node_id},{x:.3f},{y:.3f}")

        last_fireball = 0.0
        last_shield = 0.0
        while True:
            t += 0.08
            players = {
                101: (0.25 + math.cos(t) * 0.8, 0.65 + math.sin(t * 0.7) * 0.45, 90.0),
                102: (-0.35 + math.cos(t * 0.6) * 0.65, 0.4 + math.sin(t * 0.8) * 0.5, -90.0),
            }
            for player_id, (px, py, yaw) in players.items():
                self.incoming.put(f"STATUS,{player_id},{yaw:.1f},1,0,{int(t * 1000)}")
                for node_id, (x, y) in anchors.items():
                    noise = random.gauss(0.0, 0.16)
                    distance = max(0.15, math.hypot(px - x, py - y) + noise)
                    rssi = int(RSSI_AT_1_METER - 10.0 * PATH_LOSS_N * math.log10(distance))
                    self.incoming.put(f"OBS,{player_id},{node_id},{rssi},{distance:.3f},0,{int(t * 1000)}")

            if t - last_shield > 5.0:
                last_shield = t
                self.incoming.put(f"CAST,102,{SPELL_SHIELD},-90.0,90,0,{int(t * 1000)}")
            if t - last_fireball > 5.0:
                last_fireball = t
                px, py, _ = players[101]
                yaw = vec_to_yaw(-px, -py) if int(t / 5.0) % 2 == 0 else 90.0
                self.incoming.put(f"CAST,101,{SPELL_FIREBALL},{yaw:.1f},90,0,{int(t * 1000)}")

            try:
                while True:
                    self.outgoing.get_nowait()
            except queue.Empty:
                pass
            time.sleep(0.12)


class CommandConsole:
    def __init__(self, outgoing, game, audio_hub, phone_url, wifi_ssid=None, wifi_password=None):
        self.outgoing = outgoing
        self.game = game
        self.audio_hub = audio_hub
        self.phone_url = phone_url
        self.wifi_ssid = wifi_ssid
        self.wifi_password = wifi_password

    def run(self):
        self.print_help()
        while True:
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if not line:
                return
            self.handle(line.strip())

    def print_help(self):
        print("Tuning commands:")
        print("  tune easy | tune normal | tune hard")
        print("  tune <target> <gestureStart> <fbDown> <fbBrake> <fbPeak> <shieldUp> <shieldBrake> <shieldPeak> <unusedPitch> <unusedPitchDelta> <dominance> <lockoutMs>")
        print("  recenter [target]")
        print("  prop test")
        print("  sound test fireball|shield|hit|block|prop|denied")
        print("  clients")
        print("  qr")

    def handle(self, line):
        if not line:
            return
        parts = line.split()
        command = parts[0].lower()
        if command == "help":
            self.print_help()
            return
        if command == "recenter":
            target = parts[1] if len(parts) > 1 else "255"
            self.outgoing.put(f"RECENTER,{target}")
            print(f"sent RECENTER,{target}")
            return
        if command == "prop" and len(parts) >= 2 and parts[1].lower() == "test":
            self.game.trigger_prop_test()
            print("sent prop relay/audio test")
            return
        if command == "sound" and len(parts) >= 3 and parts[1].lower() == "test":
            self.game.trigger_audio_test(parts[2].lower())
            print(f"sent sound test {parts[2].lower()}")
            return
        if command == "clients":
            print(f"phone audio clients: {self.audio_hub.client_count()}")
            return
        if command == "qr":
            print(f"phone URL: {self.phone_url}")
            if self.wifi_ssid:
                print(f"wifi SSID: {self.wifi_ssid}")
            else:
                print("wifi QR disabled: pass --wifi-ssid and --wifi-password")
            return
        if command != "tune":
            print("unknown command; type help")
            return

        if len(parts) == 2 and parts[1].lower() in TUNE_PRESETS:
            values = TUNE_PRESETS[parts[1].lower()]
            line = self.tune_line(values)
            self.outgoing.put(line)
            print(f"sent {parts[1].lower()} tune: {line}")
            return

        if len(parts) == 13:
            try:
                target = int(parts[1])
                nums = [float(value) for value in parts[2:12]]
                lockout = int(float(parts[12]))
            except ValueError:
                print("bad tune values; type help")
                return
            line = self.tune_line((target, *nums, lockout))
            self.outgoing.put(line)
            print(f"sent custom tune: {line}")
            return

        print("bad tune command; type help")

    def tune_line(self, values):
        target = int(values[0])
        numbers = [f"{float(value):.2f}" for value in values[1:11]]
        lockout = str(int(values[11]))
        return ",".join(["TUNE", str(target), *numbers, lockout])


class MapApp:
    def __init__(self, root, solver, game, incoming, outgoing, port_name,
                 phone_url, audio_hub, wifi_ssid=None, wifi_password=None):
        self.root = root
        self.solver = solver
        self.game = game
        self.incoming = incoming
        self.outgoing = outgoing
        self.port_name = port_name
        self.phone_url = phone_url
        self.audio_hub = audio_hub
        self.wifi_ssid = wifi_ssid
        self.wifi_password = wifi_password
        self.last_status = "Waiting for radio data..."
        self.phone_qr = qr_matrix(phone_url)
        self.wifi_qr = qr_matrix(wifi_qr_payload(wifi_ssid, wifi_password))

        self.root.title("Spell Arena Duel Dashboard")
        self.canvas = tk.Canvas(root, width=1360, height=820, bg="#101114", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.root.bind("r", lambda event: self.game.recenter_all())
        self.root.after(33, self.tick)

    def tick(self):
        self.process_events()
        positions = self.solver.solve()
        self.game.update_positions(positions)
        self.game.tick()
        self.draw()
        self.root.after(33, self.tick)

    def process_events(self):
        while True:
            try:
                line = self.incoming.get_nowait()
            except queue.Empty:
                return

            parsed = parse_line(line)
            if parsed is None:
                if line:
                    self.last_status = line[:140]
                continue

            kind = parsed[0]
            if kind == "ANCHOR":
                _, node_id, x, y = parsed
                self.solver.set_anchor(node_id, x, y)
                self.last_status = f"Field {node_id}: ({x:.2f}, {y:.2f})"
            elif kind == "OBS":
                _, obs = parsed
                self.solver.add_observation(obs)
                self.last_status = f"{self.node_label(obs.observer_id)} -> {self.node_label(obs.source_id)} {obs.distance_m:.2f}m RSSI {obs.rssi}"
            elif kind == "STATUS":
                _, status = parsed
                self.game.update_status(status)
                self.last_status = f"{player_label(status.player_id)} yaw {status.yaw_deg:.1f}"
            elif kind == "CAST":
                _, cast = parsed
                self.game.handle_cast(cast)
                self.last_status = f"{player_label(cast.player_id)} cast {spell_name(cast.spell_type)}"

    def draw(self):
        self.canvas.delete("all")
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        left_w = 276
        right_w = 276
        top_h = 52
        bottom_h = 150
        map_rect = (left_w + 16, top_h + 10, width - right_w - 16, height - bottom_h)

        self.canvas.create_rectangle(0, 0, width, height, fill="#101114", outline="")
        self.draw_header(width)
        self.draw_player_panel(0, top_h, left_w, height - bottom_h - top_h, self.game.players.get(101), 101, "#ff6b35")
        self.draw_player_panel(width - right_w, top_h, right_w, height - bottom_h - top_h, self.game.players.get(102), 102, "#4dc3ff")

        screen, bounds = self.screen_mapper(map_rect)
        self.draw_map_panel(map_rect, screen, bounds)
        self.draw_bottom_bar(0, height - bottom_h, width, bottom_h)

    def draw_header(self, width):
        self.canvas.create_rectangle(0, 0, width, 52, fill="#16191f", outline="#2a3038")
        self.canvas.create_text(18, 12, anchor="nw", text="SPELL ARENA", fill="#f2efe8", font=("Segoe UI", 18, "bold"))
        self.canvas.create_text(
            190, 18,
            anchor="nw",
            text=f"Port {self.port_name}   Audio clients {self.audio_hub.client_count()}   {self.last_status}",
            fill="#a8b0ba",
            font=("Segoe UI", 10),
        )
        self.canvas.create_text(width - 18, 18, anchor="ne", text="R = recenter yaw", fill="#808895", font=("Segoe UI", 10))

    def screen_mapper(self, rect):
        x0, y0, x1, y1 = rect
        points = list(self.solver.anchors.values()) + [(p.x, p.y) for p in self.game.players.values()] + [self.game.prop_position()]
        if not points:
            points = [(0.0, 0.0), (2.0, 2.0)]
        min_x = min(p[0] for p in points) - 0.9
        max_x = max(p[0] for p in points) + 0.9
        min_y = min(p[1] for p in points) - 0.9
        max_y = max(p[1] for p in points) + 0.9
        if abs(max_x - min_x) < 1.0:
            min_x -= 0.5
            max_x += 0.5
        if abs(max_y - min_y) < 1.0:
            min_y -= 0.5
            max_y += 0.5
        scale = min((x1 - x0 - 52) / (max_x - min_x), (y1 - y0 - 52) / (max_y - min_y))

        def screen(point):
            x, y = point
            sx = x0 + 26 + (x - min_x) * scale
            sy = y1 - 26 - (y - min_y) * scale
            return sx, sy

        return screen, (min_x, max_x, min_y, max_y)

    def draw_map_panel(self, rect, screen, bounds):
        x0, y0, x1, y1 = rect
        self.canvas.create_rectangle(x0, y0, x1, y1, fill="#12161b", outline="#2a3038", width=2)
        self.draw_grid(screen, *bounds)
        link_rows = self.solver.link_rows()
        self.draw_links(screen, link_rows)
        self.draw_events(screen)
        self.draw_fields(screen)
        self.draw_prop_target(screen)
        self.draw_players(screen)
        self.canvas.create_text(x0 + 14, y0 + 10, anchor="nw", text="Arena Map", fill="#d9dedf", font=("Segoe UI", 12, "bold"))
        self.draw_radio_debug(x1 - 248, y0 + 12, link_rows)

    def draw_grid(self, screen, min_x, max_x, min_y, max_y):
        for x in range(math.floor(min_x), math.ceil(max_x) + 1):
            sx1, sy1 = screen((x, min_y))
            sx2, sy2 = screen((x, max_y))
            self.canvas.create_line(sx1, sy1, sx2, sy2, fill="#20262d")
        for y in range(math.floor(min_y), math.ceil(max_y) + 1):
            sx1, sy1 = screen((min_x, y))
            sx2, sy2 = screen((max_x, y))
            self.canvas.create_line(sx1, sy1, sx2, sy2, fill="#20262d")

    def draw_links(self, screen, link_rows):
        for obs in link_rows[:24]:
            a = self.solver._node_pos(obs.observer_id, display=True)
            b = self.solver._node_pos(obs.source_id, display=True)
            if a is None or b is None:
                continue
            ax, ay = screen(a)
            bx, by = screen(b)
            color = "#33414b" if obs.confidence >= 0.45 else "#2a2523"
            self.canvas.create_line(ax, ay, bx, by, fill=color, width=1)

    def draw_events(self, screen):
        now = time.time()
        for event in list(self.game.events):
            if event.expires_at <= now:
                continue
            frac = max(0.0, min(1.0, (event.expires_at - now) / max(0.1, event.expires_at - event.created_at)))
            if event.start and event.end:
                sx, sy = screen(event.start)
                ex, ey = screen(event.end)
                color = "#ff6b35" if not event.blocked else "#4dc3ff"
                width = 2 + int(7 * frac)
                self.canvas.create_line(sx, sy, ex, ey, fill=color, width=width, dash=() if not event.blocked else (8, 5))
            if event.prop_hit and event.end:
                x, y = screen(event.end)
                radius = 20 + int((1.0 - frac) * 54)
                self.canvas.create_oval(x - radius, y - radius, x + radius, y + radius, outline="#ffcf5a", width=3)

    def draw_fields(self, screen):
        health = self.solver.field_health()
        colors = {"ok": "#3e8fcb", "warn": "#d3a23a", "bad": "#d95545"}
        for node_id, point in sorted(self.solver.anchors.items()):
            x, y = screen(point)
            status, quality = health.get(node_id, ("warn", 0.0))
            self.canvas.create_rectangle(x - 10, y - 10, x + 10, y + 10, fill=colors[status], outline="#0b0e11")
            self.canvas.create_text(x, y - 24, text=f"F{node_id}", fill="#d7e8f8", font=("Segoe UI", 10, "bold"))
            self.canvas.create_text(x, y + 23, text=f"{quality:.0%}", fill="#8e9aa7", font=("Segoe UI", 8))

    def draw_prop_target(self, screen):
        x, y = screen(self.game.prop_position())
        now = time.time()
        recent = any(event.prop_hit and event.expires_at > now for event in self.game.events)
        pulse = 8 if recent and int(now * 12) % 2 == 0 else 0
        self.canvas.create_oval(x - 20 - pulse, y - 20 - pulse, x + 20 + pulse, y + 20 + pulse, fill="#2b1712", outline="#ffcf5a", width=3)
        self.canvas.create_oval(x - 7, y - 7, x + 7, y + 7, fill="#ffcf5a", outline="")
        self.canvas.create_text(x, y + 34, text=PROP_NAME, fill="#ffd98b", font=("Segoe UI", 10, "bold"))

    def draw_players(self, screen):
        now = time.time()
        colors = {101: "#ff6b35", 102: "#4dc3ff"}
        for player_id, player in sorted(self.game.players.items()):
            x, y = screen((player.x, player.y))
            color = colors.get(player_id, "#d6d6d6") if player.alive else "#777777"
            if player.shield_until > now:
                self.draw_shield_cone(screen, player)
            self.canvas.create_oval(x - 17, y - 17, x + 17, y + 17, fill=color, outline="#0b0e11", width=2)
            self.canvas.create_oval(x - 26, y - 26, x + 26, y + 26, outline=color, width=2)
            dx, dy = yaw_to_vec(player.yaw_deg)
            self.canvas.create_line(x, y, x + dx * 45, y - dy * 45, fill="#f2efe8", width=4, arrow=tk.LAST)
            self.canvas.create_text(x, y - 38, text=player_label(player_id), fill="#f2efe8", font=("Segoe UI", 11, "bold"))

    def draw_shield_cone(self, screen, player):
        points = [screen((player.x, player.y))]
        for i in range(16):
            frac = i / 15.0
            angle = player.yaw_deg - SHIELD_ARC_DEG / 2.0 + frac * SHIELD_ARC_DEG
            dx, dy = yaw_to_vec(angle)
            points.append(screen((player.x + dx * 1.25, player.y + dy * 1.25)))
        flat = [coord for point in points for coord in point]
        self.canvas.create_polygon(flat, fill="#183645", outline="#4dc3ff", stipple="gray25")

    def draw_player_panel(self, x, y, w, h, player, player_id, accent):
        self.canvas.create_rectangle(x, y, x + w, y + h, fill="#16191f", outline="#2a3038")
        label = player_label(player_id)
        self.canvas.create_text(x + 18, y + 18, anchor="nw", text=label, fill=accent, font=("Segoe UI", 20, "bold"))
        if player is None:
            self.canvas.create_text(x + 18, y + 62, anchor="nw", text="Waiting for spellbook", fill="#8e9aa7", font=("Segoe UI", 11))
            return
        now = time.time()
        self.canvas.create_text(x + 18, y + 58, anchor="nw", text=f"Score {player.score_hits}", fill="#f2efe8", font=("Segoe UI", 30, "bold"))
        self.draw_hp_blocks(x + 18, y + 120, w - 36, player.hp)
        self.draw_meter(x + 18, y + 184, w - 36, "Mana", player.mana / MAX_MANA, "#2f82ff")
        cooldown = max(0.0, player.spell_lockout_until - now)
        self.draw_meter(x + 18, y + 246, w - 36, "Cooldown", min(1.0, cooldown / SPELL_LOCKOUT_SECONDS), "#ff6b35")
        shield = max(0.0, player.shield_until - now)
        self.draw_meter(x + 18, y + 308, w - 36, "Shield", min(1.0, shield / SHIELD_DURATION_SECONDS), "#4dc3ff")
        self.canvas.create_text(x + 18, y + 372, anchor="nw", text=f"Yaw {player.yaw_deg:.0f}", fill="#a8b0ba", font=("Segoe UI", 12))
        self.canvas.create_text(x + 18, y + 398, anchor="nw", text=f"Last {player.last_action}", fill="#d9dedf", font=("Segoe UI", 12, "bold"))
        state = "DEAD" if not player.alive else ("SHIELD" if shield > 0 else ("COOLDOWN" if cooldown > 0 else "READY"))
        self.canvas.create_text(x + 18, y + h - 54, anchor="nw", text=state, fill=accent, font=("Segoe UI", 26, "bold"))

    def draw_hp_blocks(self, x, y, w, hp):
        self.canvas.create_text(x, y, anchor="nw", text="HP", fill="#a8b0ba", font=("Segoe UI", 11, "bold"))
        block_w = (w - 16) / MAX_HP
        for i in range(MAX_HP):
            bx = x + i * (block_w + 4)
            fill = "#2ecc71" if i < hp else "#2a3038"
            self.canvas.create_rectangle(bx, y + 26, bx + block_w, y + 52, fill=fill, outline="#39424d")

    def draw_meter(self, x, y, w, label, frac, color):
        frac = max(0.0, min(1.0, frac))
        self.canvas.create_text(x, y, anchor="nw", text=label, fill="#a8b0ba", font=("Segoe UI", 11, "bold"))
        self.canvas.create_rectangle(x, y + 24, x + w, y + 42, fill="#0f1216", outline="#39424d")
        if frac > 0:
            self.canvas.create_rectangle(x + 2, y + 26, x + 2 + (w - 4) * frac, y + 40, fill=color, outline="")

    def draw_bottom_bar(self, x, y, w, h):
        self.canvas.create_rectangle(x, y, x + w, y + h, fill="#16191f", outline="#2a3038")
        self.canvas.create_text(x + 18, y + 14, anchor="nw", text="Phone Audio", fill="#f2efe8", font=("Segoe UI", 13, "bold"))
        self.canvas.create_text(x + 18, y + 42, anchor="nw", text=self.phone_url, fill="#4dc3ff", font=("Consolas", 12, "bold"))
        wifi_text = f"Wi-Fi {self.wifi_ssid}" if self.wifi_ssid else "Wi-Fi QR disabled"
        self.canvas.create_text(x + 18, y + 70, anchor="nw", text=wifi_text, fill="#a8b0ba", font=("Segoe UI", 10))
        self.draw_qr(x + 410, y + 16, self.phone_qr, "Audio QR")
        self.draw_qr(x + 540, y + 16, self.wifi_qr, "Wi-Fi QR")
        self.canvas.create_text(x + 690, y + 14, anchor="nw", text="Events", fill="#f2efe8", font=("Segoe UI", 13, "bold"))
        yy = y + 40
        for event in list(self.game.events)[-5:][::-1]:
            self.canvas.create_text(x + 690, yy, anchor="nw", text=event.text, fill="#d9dedf", font=("Segoe UI", 10))
            yy += 20

    def draw_qr(self, x, y, matrix, label):
        self.canvas.create_text(x, y - 2, anchor="sw", text=label, fill="#a8b0ba", font=("Segoe UI", 9))
        if not matrix:
            self.canvas.create_rectangle(x, y, x + 92, y + 92, outline="#39424d")
            self.canvas.create_text(x + 46, y + 43, text="text only", fill="#6f7882", font=("Segoe UI", 9))
            return
        size = 92
        cells = len(matrix)
        cell = max(1, size // cells)
        self.canvas.create_rectangle(x, y, x + cell * cells, y + cell * cells, fill="#f2efe8", outline="#39424d")
        for row_idx, row in enumerate(matrix):
            for col_idx, value in enumerate(row):
                if value:
                    x0 = x + col_idx * cell
                    y0 = y + row_idx * cell
                    self.canvas.create_rectangle(x0, y0, x0 + cell, y0 + cell, fill="#101114", outline="")

    def draw_radio_debug(self, x, y, link_rows):
        self.canvas.create_text(x, y, anchor="nw", text="Radio", fill="#a8b0ba", font=("Segoe UI", 10, "bold"))
        y += 20
        for obs in link_rows[:7]:
            text = f"{self.node_label(obs.observer_id):>3}->{self.node_label(obs.source_id):<3} {obs.distance_m:3.1f}m {int(obs.confidence * 100):02d}%"
            color = "#8e9aa7" if obs.confidence >= 0.45 else "#d98763"
            self.canvas.create_text(x, y, anchor="nw", text=text, fill=color, font=("Consolas", 8))
            y += 15

    def node_label(self, node_id):
        if node_id in self.solver.anchors:
            return f"F{node_id}"
        return player_label(node_id)


def parse_line(line):
    parts = [part.strip() for part in line.strip().split(",")]
    if not parts:
        return None
    try:
        if parts[0] == "ANCHOR" and len(parts) >= 4:
            return ("ANCHOR", int(parts[1]), float(parts[2]), float(parts[3]))
        if parts[0] == "OBS" and len(parts) >= 7:
            rssi = int(parts[3])
            obs = Observation(
                observer_id=int(parts[1]),
                source_id=int(parts[2]),
                rssi=rssi,
                distance_m=rssi_to_distance(rssi),
                sequence=int(parts[5]),
                sender_uptime_ms=int(parts[6]),
                received_at=time.time(),
            )
            return ("OBS", obs)
        if parts[0] == "STATUS" and len(parts) >= 6:
            return ("STATUS", PlayerStatus(
                player_id=int(parts[1]),
                yaw_deg=float(parts[2]),
                flags=int(parts[3]),
                sequence=int(parts[4]),
                uptime_ms=int(parts[5]),
                received_at=time.time(),
            ))
        if parts[0] == "CAST" and len(parts) >= 7:
            return ("CAST", SpellCast(
                player_id=int(parts[1]),
                spell_type=int(parts[2]),
                yaw_deg=float(parts[3]),
                confidence=int(parts[4]),
                sequence=int(parts[5]),
                uptime_ms=int(parts[6]),
                received_at=time.time(),
            ))
    except ValueError:
        return None
    return None


def rssi_to_distance(rssi):
    if rssi >= 0:
        return MAX_DISTANCE_M
    return max(0.05, min(MAX_DISTANCE_M, 10 ** ((RSSI_AT_1_METER - rssi) / (10.0 * PATH_LOSS_N))))


def normalize_deg(deg):
    while deg >= 180.0:
        deg -= 360.0
    while deg < -180.0:
        deg += 360.0
    return deg


def angle_delta(a, b):
    return normalize_deg(b - a)


def yaw_to_vec(yaw_deg):
    rad = math.radians(yaw_deg)
    return (math.sin(rad), math.cos(rad))


def vec_to_yaw(dx, dy):
    return normalize_deg(math.degrees(math.atan2(dx, dy)))


def fireball_coordinates(origin, direction, point):
    vx = point[0] - origin[0]
    vy = point[1] - origin[1]
    along = vx * direction[0] + vy * direction[1]
    lateral = vx * direction[1] - vy * direction[0]
    return along, lateral


def spell_name(spell_type):
    if spell_type == SPELL_FIREBALL:
        return "fireball"
    if spell_type == SPELL_SHIELD:
        return "shield"
    return f"spell {spell_type}"


def choose_port():
    if list_ports is None:
        return None
    ports = list(list_ports.comports())
    if not ports:
        return None
    preferred_words = ("CP210", "Silicon", "USB to UART", "USB Serial", "CH340", "CH341", "Espressif")
    for port in ports:
        haystack = f"{port.description} {port.manufacturer} {port.hwid}"
        if any(word.lower() in haystack.lower() for word in preferred_words):
            return port.device
    return ports[0].device


def normalize_port_name(port_name):
    if port_name is None:
        return None
    port_name = str(port_name).strip()
    if port_name.isdigit():
        return f"COM{port_name}"
    return port_name.upper() if port_name.lower().startswith("com") else port_name


def print_ports():
    if list_ports is None:
        print("pyserial is not installed, so serial ports cannot be listed.")
        return
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found.")
        return
    for port in ports:
        print(f"{port.device}: {port.description} {port.hwid}")


def main():
    global RSSI_AT_1_METER, PATH_LOSS_N

    parser = argparse.ArgumentParser(description="Run the Spell Arena server/dashboard.")
    parser.add_argument("--port", help="Serial port, for example COM4. Auto-detects if omitted.")
    parser.add_argument("--baud", type=int, default=BAUD_RATE)
    parser.add_argument("--rssi-1m", type=float, default=RSSI_AT_1_METER)
    parser.add_argument("--path-loss", type=float, default=PATH_LOSS_N)
    parser.add_argument("--list-ports", action="store_true")
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--simple-combat", action="store_true", help="Ignore yaw for hits: fireball hits nearest player in range, shield blocks all directions.")
    parser.add_argument("--phone-host", default="192.168.137.1", help="Host/IP phones should open, usually the Windows hotspot IP.")
    parser.add_argument("--phone-bind", default="0.0.0.0", help="Interface for the local phone audio HTTP server.")
    parser.add_argument("--phone-port", type=int, default=8080)
    parser.add_argument("--wifi-ssid")
    parser.add_argument("--wifi-password")
    args = parser.parse_args()

    RSSI_AT_1_METER = args.rssi_1m
    PATH_LOSS_N = max(1.2, args.path_loss)

    if args.list_ports:
        print_ports()
        return
    if serial is None and not args.fake:
        print("Missing dependency: pyserial")
        print("Install it with: python -m pip install pyserial")
        sys.exit(1)

    incoming = queue.Queue()
    outgoing = queue.Queue()
    ensure_audio_assets()
    audio_hub = PhoneAudioHub()
    phone_url = f"http://{args.phone_host}:{args.phone_port}/"
    try:
        PhoneAudioServer(args.phone_bind, args.phone_port, audio_hub).start()
        print(f"Phone audio server: {phone_url}")
    except OSError as exc:
        print(f"Phone audio server failed on {args.phone_bind}:{args.phone_port}: {exc}")

    port_name = "fake"
    if args.fake:
        transport = FakeTransport(incoming, outgoing)
    else:
        port_name = normalize_port_name(args.port) or choose_port()
        if port_name is None:
            print("No serial ports found. Plug in the bridge/player ESP32 and rerun.")
            sys.exit(1)
        transport = SerialTransport(port_name, args.baud, incoming, outgoing)

    threading.Thread(target=transport.run, daemon=True).start()

    root = tk.Tk()
    solver = MeshSolver()
    game = GameEngine(
        outgoing,
        simple_combat=args.simple_combat,
        audio_hub=audio_hub,
        prop_position_getter=lambda: nearest_origin_anchor(solver.anchors),
    )
    threading.Thread(
        target=CommandConsole(outgoing, game, audio_hub, phone_url, args.wifi_ssid, args.wifi_password).run,
        daemon=True,
    ).start()
    MapApp(root, solver, game, incoming, outgoing, port_name, phone_url, audio_hub, args.wifi_ssid, args.wifi_password)
    root.mainloop()


if __name__ == "__main__":
    main()
