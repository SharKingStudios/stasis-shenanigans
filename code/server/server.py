import argparse
import ctypes
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
from urllib.parse import parse_qs, unquote, urlparse

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None


BAUD_RATE = 115200
BASE_DIR = Path(__file__).resolve().parent
AUDIO_DIR = BASE_DIR / "static" / "audio"
FONT_DIR = BASE_DIR / "static" / "fonts"
QR_DIR = BASE_DIR / "static" / "qr"
LOCAL_AUDIO_MIX_DIR = BASE_DIR / "static" / "audio_mix_cache"

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
MANA_REGEN_PER_SEC = 10.0
FIREBALL_COST = 25
SHIELD_COST = 20
SHIELD_DURATION_SECONDS = 1.5
FIREBALL_COOLDOWN_SECONDS = 0.75
SHIELD_COOLDOWN_SECONDS = 1.25
SPELL_LOCKOUT_SECONDS = 1.0
FIREBALL_DAMAGE = 1
FIREBALL_LENGTH_M = 100.0
FIREBALL_WIDTH_M = 1.6
SIMPLE_FIREBALL_RANGE_M = 3.2
SHIELD_ARC_DEG = 110.0
WORLD_SEND_INTERVAL = 0.10
STATE_SEND_INTERVAL = 0.25
SSE_KEEPALIVE_SECONDS = 2.0

TUNE_PRESETS = {
    "easy": (255, 0.22, 0.45, -0.20, 0.58, 0.22, 0.16, 0.30, 0.0, 0.0, 1.10, 1500),
    "normal": (255, 0.30, 0.56, -0.28, 0.74, 0.30, 0.08, 0.40, 0.0, 0.0, 1.10, 1800),
    "hard": (255, 0.38, 0.70, -0.38, 0.95, 0.44, -0.08, 0.58, 0.0, 0.0, 1.35, 2200),
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
    101: "Sol",
    102: "Luna",
}

PROP_NAME = "Fan Face"
PROP_TARGET_RADIUS_M = 0.90
PROP_FAN_MS = 2500
PROP_FLASH_COUNT = 4
PROP_FLASH_ON_MS = 120
PROP_FLASH_OFF_MS = 120
PROP_LOCAL_AUDIO_DELAY_SECONDS = 0.0

AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg", ".m4a"}
AUDIO_CATEGORY_PREFIXES = {
    "fireball_core": ("fireball_core", "fireball"),
    "fireball_voice": ("fireball_voice", "fireball_scream", "fireball_yell"),
    "shield_core": ("shield_core", "shield"),
    "shield_voice": ("shield_voice",),
    "hit": ("hit_bitcrush", "hit", "hurt", "damage"),
    "block": ("block", "deflect", "parry"),
    "prop": ("prop_hit", "prop", "fan_face", "fanface"),
    "denied": ("denied", "fail", "no_mana"),
}
DEFAULT_AUDIO_FILES = {
    "fireball_core": ["fireball_core_01.wav"],
    "fireball_voice": ["fireball_voice_01.wav", "fireball_voice_02.wav", "fireball_voice_03.wav", "fireball_voice_04.wav"],
    "shield_core": ["shield_core_01.wav"],
    "shield_voice": ["shield_voice_01.wav", "shield_voice_02.wav", "shield_voice_03.wav"],
    "hit": ["hit_bitcrush_01.wav"],
    "block": ["block_01.wav"],
    "prop": ["prop_hit_01.wav", "prop_hit_02.wav", "prop_hit_03.wav", "prop_hit_04.wav", "prop_hit_05.wav"],
    "denied": ["denied_01.wav"],
}
AUDIO_MANIFEST = {category: [] for category in AUDIO_CATEGORY_PREFIXES}

EDG = {
    "ember": "#be4a2f",
    "rust": "#d77643",
    "parchment": "#ead4aa",
    "leather": "#733e39",
    "maroon": "#a22633",
    "red": "#e43b44",
    "orange": "#f77622",
    "gold": "#feae34",
    "yellow": "#fee761",
    "green": "#63c74d",
    "deep_green": "#265c42",
    "teal_black": "#193c3e",
    "blue": "#0099db",
    "cyan": "#2ce8f5",
    "white": "#ffffff",
    "silver": "#c0cbdc",
    "steel": "#8b9bb4",
    "slate": "#5a6988",
    "indigo": "#3a4466",
    "navy": "#262b44",
    "void": "#181425",
    "pink": "#ff0044",
    "purple": "#68386c",
}

# Must match the field positions uploaded in code/field/field.ino.
DEFAULT_ANCHORS = {
    # 1: (0.0, 0.0),
    # 2: (2.0, 0.0),
    # 3: (0.0, 2.0),
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
    hp_loss: int = 0


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


def ensure_site_qr(phone_url):
    QR_DIR.mkdir(parents=True, exist_ok=True)
    path = QR_DIR / "site_qr.png"
    try:
        import qrcode
        qr = qrcode.QRCode(box_size=18, border=4)
        qr.add_data(phone_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img.save(path)
    except Exception:
        return None
    return path


def load_dashboard_fonts():
    if os.name != "nt":
        return
    font_paths = [
        FONT_DIR / "PressStart2P-Regular.ttf",
        FONT_DIR / "PixelifySans-wght.ttf",
    ]
    for path in font_paths:
        if path.exists():
            try:
                ctypes.windll.gdi32.AddFontResourceExW(str(path), 0x10, 0)
            except Exception:
                pass


def audio_pitch(lo, hi):
    return round(random.uniform(lo, hi), 3)


class PhoneAudioHub:
    def __init__(self):
        self.clients = []
        self.lock = threading.Lock()
        self.sequence = 1

    def subscribe(self, player_id=0):
        client_queue = queue.Queue(maxsize=32)
        with self.lock:
            self.clients.append({"queue": client_queue, "player_id": player_id, "last_seen": time.time()})
        return client_queue

    def unsubscribe(self, client_queue):
        with self.lock:
            self.clients = [client for client in self.clients if client["queue"] is not client_queue]

    def client_count(self, player_id=None):
        with self.lock:
            if player_id is None:
                return len(self.clients)
            return sum(1 for client in self.clients if client["player_id"] == player_id)

    def client_summary(self):
        with self.lock:
            counts = {}
            for client in self.clients:
                counts[client["player_id"]] = counts.get(client["player_id"], 0) + 1
            return counts

    def broadcast(self, payload, audience_player_id=0):
        payload = dict(payload)
        payload["eventSeq"] = self.sequence
        self.sequence = (self.sequence + 1) & 0xFFFF
        with self.lock:
            clients = [
                client for client in self.clients
                if audience_player_id == 0 or client["player_id"] == audience_player_id
            ]
        delivered = 0
        for client in clients:
            try:
                client["queue"].put_nowait(payload)
                delivered += 1
            except queue.Full:
                pass
        return delivered


PHONE_PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Spell Arena Audio</title>
  <style>
    @font-face { font-family: 'Pixelify Sans'; src: url('/fonts/pixelify-sans-latin-400-normal.woff2') format('woff2'); font-weight: 400; }
    @font-face { font-family: 'Press Start 2P'; src: url('/fonts/press-start-2p-latin-400-normal.woff2') format('woff2'); font-weight: 400; }
    :root { color-scheme: dark; --bg:#181425; --panel:#262b44; --line:#3a4466; --text:#ffffff; --muted:#c0cbdc; --hot:#f77622; --cool:#2ce8f5; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; font-family: 'Pixelify Sans', system-ui, sans-serif; background: var(--bg); color: var(--text); display: grid; place-items: center; }
    main { width: min(92vw, 430px); border: 1px solid var(--line); background: var(--panel); padding: 24px; }
    h1 { margin: 0 0 8px; font-family: 'Press Start 2P', monospace; font-size: 20px; letter-spacing: 0; }
    p { color: var(--muted); margin: 8px 0; line-height: 1.35; }
    button { width: 100%; margin-top: 18px; padding: 16px; border: 0; background: var(--hot); color: #190904; font-weight: 800; font-size: 18px; }
    .bar { height: 12px; border: 1px solid var(--line); margin: 18px 0 8px; overflow: hidden; }
    .fill { height: 100%; width: 0%; background: var(--cool); transition: width .18s ease; }
    .choice { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 14px; }
    .choice button { margin: 0; padding: 12px; background: #3a4466; color: var(--text); }
    .choice button.selected { background: var(--cool); color: #181425; }
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
    <div class="choice">
      <button id="choose101" type="button">Sol</button>
      <button id="choose102" type="button">Luna</button>
    </div>
    <button id="arm">Tap to arm audio</button>
    <div class="status">
      <span>Audio</span><span id="audio">loading</span>
      <span>Server</span><span id="server">connecting</span>
      <span>Events</span><span id="events">0</span>
      <span>Player</span><span id="playerChoice">none</span>
      <span>Wake</span><span id="wake">not armed</span>
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
    const playerChoiceState = document.getElementById('playerChoice');
    const wakeState = document.getElementById('wake');
    const readyLight = document.getElementById('readyLight');
    const last = document.getElementById('last');
    const choose101 = document.getElementById('choose101');
    const choose102 = document.getElementById('choose102');
    let ctx, manifest, buffers = {}, eventCount = 0, armed = false, wakeLock = null, es = null;
    let selectedPlayer = Number(localStorage.getItem('spellArenaPlayer') || 0);

    function updatePlayerChoice() {
      choose101.classList.toggle('selected', selectedPlayer === 101);
      choose102.classList.toggle('selected', selectedPlayer === 102);
      playerChoiceState.textContent = selectedPlayer === 101 ? 'Sol' : (selectedPlayer === 102 ? 'Luna' : 'choose');
    }

    function choosePlayer(id) {
      selectedPlayer = id;
      localStorage.setItem('spellArenaPlayer', String(id));
      updatePlayerChoice();
      connectEvents();
    }

    choose101.addEventListener('click', () => choosePlayer(101));
    choose102.addEventListener('click', () => choosePlayer(102));
    updatePlayerChoice();

    async function loadAudio() {
      manifest = await fetch('/manifest.json').then(r => r.json());
      const assetVersion = manifest.version || Date.now();
      const assetUrl = file => '/audio/' + encodeURIComponent(file) + '?v=' + encodeURIComponent(assetVersion);
      const files = Object.values(manifest.assets).flat();
      if ('caches' in window) {
        const cache = await caches.open('spell-arena-audio-' + assetVersion);
        await cache.addAll(files.map(assetUrl));
      }
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        const data = await fetch(assetUrl(file), {cache: 'reload'}).then(r => r.arrayBuffer());
        buffers[file] = await ctx.decodeAudioData(data);
        fill.style.width = Math.round(((i + 1) / files.length) * 100) + '%';
      }
      lead.textContent = selectedPlayer ? 'Audio cached. Tap once before the duel starts.' : 'Choose your player, then tap to arm audio.';
      audioState.textContent = 'cached';
    }

    async function requestWakeLock() {
      if (!('wakeLock' in navigator)) {
        wakeState.textContent = 'unsupported';
        return;
      }
      try {
        wakeLock = await navigator.wakeLock.request('screen');
        wakeState.textContent = 'on';
        wakeLock.addEventListener('release', () => {
          wakeState.textContent = document.visibilityState === 'visible' ? 'released' : 'hidden';
        });
      } catch (err) {
        wakeState.textContent = 'blocked';
      }
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
      if (event.type === 'prop_hit') return;
      eventCount++;
      eventState.textContent = String(eventCount);
      last.textContent = `${event.casterLabel || 'Arena'} ${event.type.replace('_', ' ')} ${event.targetLabel || ''}`.trim();
      const a = manifest.assets;
      if (event.type === 'fireball_cast') {
        playFile(pick(a.fireball_core, event.coreIndex), event.corePitch, 0.55);
        playFile(pick(a.fireball_voice, event.voiceIndex), event.voicePitch, 1.0);
      } else if (event.type === 'shield_cast') {
        playFile(pick(a.shield_core, event.coreIndex), event.corePitch, 0.55);
        playFile(pick(a.shield_voice, event.voiceIndex), event.voicePitch, 0.95);
      } else if (event.type === 'hit') {
        playFile(pick(a.hit, event.coreIndex), event.corePitch, 1.0);
      } else if (event.type === 'block') {
        playFile(pick(a.block, event.coreIndex), event.corePitch, 1.0);
      } else if (event.type === 'prop_hit') {
        playFile(pick(a.prop, event.coreIndex), event.corePitch, 1.0);
      } else if (event.type === 'denied') {
        playFile(pick(a.denied, event.coreIndex), event.corePitch, 0.8);
      }
    }

    arm.addEventListener('click', async () => {
      await ctx.resume();
      await requestWakeLock();
      armed = true;
      audioState.textContent = 'armed';
      readyLight.classList.add('on');
      arm.textContent = 'Audio armed';
      arm.disabled = true;
    });

    document.addEventListener('visibilitychange', () => {
      if (armed && document.visibilityState === 'visible') requestWakeLock();
    });

    function connectEvents() {
      if (!manifest) return;
      if (!selectedPlayer) {
        serverState.textContent = 'choose player';
        return;
      }
      if (es) es.close();
      es = new EventSource('/events?player=' + encodeURIComponent(selectedPlayer));
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


def audio_category_for_filename(filename):
    stem = Path(filename).stem.lower()
    matches = []
    for category, prefixes in AUDIO_CATEGORY_PREFIXES.items():
        for prefix in prefixes:
            if stem == prefix or stem.startswith(prefix + "_") or stem.startswith(prefix + "-"):
                matches.append((len(prefix), category))
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]


def scan_audio_manifest():
    manifest = {category: [] for category in AUDIO_CATEGORY_PREFIXES}
    if not AUDIO_DIR.exists():
        return manifest
    for path in sorted(AUDIO_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        category = audio_category_for_filename(path.name)
        if category:
            manifest[category].append(path.name)
    return manifest


def refresh_audio_manifest():
    global AUDIO_MANIFEST
    AUDIO_MANIFEST = scan_audio_manifest()
    return AUDIO_MANIFEST


def audio_manifest_version():
    latest = 0
    total_size = 0
    for files in AUDIO_MANIFEST.values():
        for filename in files:
            path = AUDIO_DIR / filename
            if path.exists():
                stat = path.stat()
                latest = max(latest, stat.st_mtime_ns)
                total_size += stat.st_size
    return f"{latest:x}-{total_size:x}"


def audio_content_type(path):
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        return "audio/mpeg"
    if suffix == ".ogg":
        return "audio/ogg"
    if suffix == ".m4a":
        return "audio/mp4"
    return "audio/wav"


def ensure_audio_assets():
    global AUDIO_MANIFEST
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    existing = scan_audio_manifest()
    generators = {
        "fireball_core_01.wav": lambda p: write_sfx(p, "fireball_core", 0.75),
        "shield_core_01.wav": lambda p: write_sfx(p, "shield_core", 0.65),
        "hit_bitcrush_01.wav": lambda p: write_sfx(p, "hit", 0.42),
        "block_01.wav": lambda p: write_sfx(p, "block", 0.48),
        "denied_01.wav": lambda p: write_sfx(p, "denied", 0.26),
    }
    for i, name in enumerate(DEFAULT_AUDIO_FILES["prop"], start=1):
        generators[name] = lambda p, idx=i: write_sfx(p, "prop", 0.95, idx)
    for i, name in enumerate(DEFAULT_AUDIO_FILES["fireball_voice"], start=1):
        generators[name] = lambda p, idx=i: write_sfx(p, "fireball_voice", 0.55, idx)
    for i, name in enumerate(DEFAULT_AUDIO_FILES["shield_voice"], start=1):
        generators[name] = lambda p, idx=i: write_sfx(p, "shield_voice", 0.52, idx)

    for category, defaults in DEFAULT_AUDIO_FILES.items():
        if existing.get(category):
            continue
        for filename in defaults:
            path = AUDIO_DIR / filename
            generator = generators.get(filename)
            if generator and not path.exists():
                generator(path)

    refresh_audio_manifest()


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
                manifest = refresh_audio_manifest()
                body = json.dumps({"assets": manifest, "version": audio_manifest_version()}).encode("utf-8")
                self.send_bytes(body, "application/json")
            elif path == "/events":
                self.serve_events()
            elif path.startswith("/audio/"):
                self.serve_audio(path[len("/audio/"):])
            elif path.startswith("/fonts/"):
                self.serve_font(path[len("/fonts/"):])
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
            self.send_bytes(path.read_bytes(), audio_content_type(path))

        def serve_font(self, filename):
            filename = os.path.basename(unquote(filename))
            path = FONT_DIR / filename
            if not path.exists():
                self.send_error(404)
                return
            content_type = "font/woff2" if path.suffix.lower() == ".woff2" else "font/ttf"
            self.send_bytes(path.read_bytes(), content_type)

        def serve_events(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            try:
                player_id = int(params.get("player", ["0"])[0])
            except (TypeError, ValueError):
                player_id = 0
            client = audio_hub.subscribe(player_id)
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
                        payload = client.get(timeout=SSE_KEEPALIVE_SECONDS)
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


class LocalAudioPlayer:
    def __init__(self):
        self.enabled = os.name == "nt"
        self._winsound = None
        self._winmm = None
        self._mci_counter = 0
        self._mci_lock = threading.Lock()
        if self.enabled:
            try:
                import winsound
                self._winsound = winsound
                self._winmm = ctypes.windll.winmm
            except ImportError:
                self.enabled = False

    def mci_send(self, command):
        if self._winmm is None:
            return 1
        return self._winmm.mciSendStringW(command, None, 0, None)

    def wav_duration_seconds(self, path):
        try:
            with wave.open(str(path), "rb") as wav:
                return wav.getnframes() / max(1, wav.getframerate())
        except (wave.Error, OSError):
            return 1.0

    def playable_files(self, category):
        files = []
        for name in AUDIO_MANIFEST.get(category, []):
            path = AUDIO_DIR / name
            if path.exists() and path.suffix.lower() == ".wav":
                files.append(path)
        return files

    def read_wav_samples(self, path):
        try:
            with wave.open(str(path), "rb") as wav:
                channels = wav.getnchannels()
                sample_width = wav.getsampwidth()
                rate = wav.getframerate()
                frame_count = wav.getnframes()
                raw = wav.readframes(frame_count)
        except (wave.Error, OSError):
            return None, []

        if sample_width == 2:
            count = len(raw) // 2
            values = struct.unpack("<" + "h" * count, raw)
            scale = 32768.0
        elif sample_width == 1:
            values = [byte - 128 for byte in raw]
            scale = 128.0
        else:
            return None, []

        samples = []
        for i in range(0, len(values), channels):
            frame = values[i:i + channels]
            samples.append(sum(frame) / (scale * max(1, len(frame))))
        return rate, samples

    def resample(self, samples, source_rate, target_rate):
        if not samples or source_rate == target_rate:
            return samples
        ratio = source_rate / target_rate
        out_len = max(1, int(len(samples) / ratio))
        return [samples[min(len(samples) - 1, int(i * ratio))] for i in range(out_len)]

    def write_mixed_wav(self, core_path, voice_path, event_type):
        core_rate, core = self.read_wav_samples(core_path)
        voice_rate, voice = self.read_wav_samples(voice_path)
        if not core_rate or not voice_rate or not core or not voice:
            return None

        target_rate = max(core_rate, voice_rate)
        core = self.resample(core, core_rate, target_rate)
        voice = self.resample(voice, voice_rate, target_rate)
        voice_delay = 0
        total = max(len(core), voice_delay + len(voice))
        mixed = [0.0] * total

        for i, sample in enumerate(core):
            mixed[i] += sample * 0.50
        for i, sample in enumerate(voice):
            mixed[voice_delay + i] += sample * 1.05

        peak = max(0.01, max(abs(sample) for sample in mixed))
        if peak > 0.98:
            mixed = [sample * 0.98 / peak for sample in mixed]

        LOCAL_AUDIO_MIX_DIR.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time() * 1000)
        path = LOCAL_AUDIO_MIX_DIR / f"{event_type}_{stamp}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(target_rate)
            wav.writeframes(b"".join(
                struct.pack("<h", int(max(-1.0, min(1.0, sample)) * 32767))
                for sample in mixed
            ))
        return path

    def play_file(self, path):
        with self._mci_lock:
            self._mci_counter += 1
            alias = f"spellarena_{self._mci_counter}"

        safe_path = str(path).replace('"', '')
        opened = self.mci_send(f'open "{safe_path}" type waveaudio alias {alias}') == 0
        if opened and self.mci_send(f"play {alias}") == 0:
            duration = self.wav_duration_seconds(path)
            threading.Timer(duration + 0.45, self.mci_send, args=(f"close {alias}",)).start()
            return
        if opened:
            self.mci_send(f"close {alias}")

        try:
            self._winsound.PlaySound(str(path), self._winsound.SND_FILENAME | self._winsound.SND_ASYNC)
        except RuntimeError:
            pass

    def play_cast_mix(self, core_category, voice_category, event_type):
        core_files = self.playable_files(core_category)
        voice_files = self.playable_files(voice_category)
        if not core_files and not voice_files:
            return
        if core_files and voice_files:
            mixed = self.write_mixed_wav(random.choice(core_files), random.choice(voice_files), event_type)
            if mixed:
                self.play_file(mixed)
                return
        self.play_file(random.choice(voice_files or core_files))

    def play_category(self, category, delay_seconds=0.0):
        if not self.enabled or self._winsound is None:
            return
        if delay_seconds > 0:
            threading.Timer(delay_seconds, self.play_category, args=(category,), kwargs={"delay_seconds": 0}).start()
            return
        files = self.playable_files(category)
        if not files:
            return
        self.play_file(random.choice(files))

    def play_event(self, event_type, delay_seconds=0.0):
        if not self.enabled or self._winsound is None:
            return
        if delay_seconds > 0:
            threading.Timer(delay_seconds, self.play_event, args=(event_type,), kwargs={"delay_seconds": 0}).start()
            return
        if event_type == "fireball_cast":
            self.play_cast_mix("fireball_core", "fireball_voice", event_type)
            return
        if event_type == "shield_cast":
            self.play_cast_mix("shield_core", "shield_voice", event_type)
            return
        category = {
            "hit": "hit",
            "block": "block",
            "prop_hit": "prop",
            "denied": "denied",
        }.get(event_type)
        if category:
            self.play_category(category, delay_seconds=delay_seconds)

    def play_prop(self, delay_seconds=PROP_LOCAL_AUDIO_DELAY_SECONDS):
        self.play_event("prop_hit", delay_seconds=delay_seconds)


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
    def __init__(self, outgoing, simple_combat=False, audio_hub=None, local_audio=None,
                 local_audio_mode="all", prop_position_getter=None):
        self.players = {}
        self.events = deque(maxlen=24)
        self.outgoing = outgoing
        self.simple_combat = simple_combat
        self.audio_hub = audio_hub
        self.local_audio = local_audio
        self.local_audio_mode = local_audio_mode
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
            hp_loss=FIREBALL_DAMAGE,
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

    def add_event(self, text, event_type, caster_id=0, target_id=0, start=None, end=None,
                  blocked=False, prop_hit=False, hp_loss=0):
        now = time.time()
        duration = 2.6 if event_type in (EVENT_HIT, EVENT_DEATH) else 2.0
        self.events.append(VisualEvent(
            text=text,
            event_type=event_type,
            created_at=now,
            expires_at=now + duration,
            caster_id=caster_id,
            target_id=target_id,
            start=start,
            end=end,
            blocked=blocked,
            prop_hit=prop_hit or event_type == EVENT_PROP_HIT,
            hp_loss=hp_loss,
        ))

    def broadcast_audio(self, event_type, caster_id=0, target_id=0, target_label=None):
        refresh_audio_manifest()
        audience_player_id = self.audio_audience_player(event_type, caster_id, target_id)
        voice_category = "fireball_voice" if event_type == "fireball_cast" else "shield_voice"
        voice_pool = max(1, len(AUDIO_MANIFEST.get(voice_category, [])))
        core_pool = max(1, len(AUDIO_MANIFEST.get(self.audio_core_category(event_type), [])))
        payload = {
            "type": event_type,
            "casterId": caster_id,
            "targetId": target_id,
            "casterLabel": player_label(caster_id) if caster_id else "",
            "targetLabel": target_label or (player_label(target_id) if target_id else ""),
            "corePitch": audio_pitch(0.86, 1.18),
            "voicePitch": audio_pitch(0.93, 1.08),
            "coreIndex": random.randrange(core_pool),
            "voiceIndex": random.randrange(voice_pool),
        }
        delivered = 0
        if self.audio_hub is not None and event_type != "prop_hit":
            delivered = self.audio_hub.broadcast(payload, audience_player_id=audience_player_id)
        if self.should_play_local_audio(event_type, audience_player_id, delivered):
            self.local_audio.play_event(event_type, delay_seconds=self.local_audio_delay(event_type))

    def should_play_local_audio(self, event_type, audience_player_id, delivered):
        if not self.local_audio or self.local_audio_mode == "off":
            return False
        if self.local_audio_mode == "all":
            return True
        return event_type == "prop_hit" or (audience_player_id and delivered == 0)

    def local_audio_delay(self, event_type):
        if event_type in ("hit", "block"):
            return 0.20
        if event_type == "prop_hit":
            return PROP_LOCAL_AUDIO_DELAY_SECONDS
        return 0.0

    def audio_audience_player(self, event_type, caster_id, target_id):
        if event_type in ("fireball_cast", "shield_cast", "denied"):
            return caster_id
        if event_type in ("hit", "block"):
            return target_id
        return 0

    def audio_core_category(self, event_type):
        return {
            "fireball_cast": "fireball_core",
            "shield_cast": "shield_core",
            "hit": "hit",
            "block": "block",
            "prop_hit": "prop",
            "denied": "denied",
        }.get(event_type, "hit")

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
        self.requested_port_name = normalize_port_name(port_name)
        self.port_name = self.requested_port_name
        self.baud = baud
        self.incoming = incoming
        self.outgoing = outgoing
        self.control_backlog = deque(maxlen=120)
        self.pending_states = {}
        self.pending_world = None

    def resolve_port(self):
        if self.requested_port_name:
            return self.requested_port_name
        return choose_port()

    def remember_line(self, line, front=False):
        if not line:
            return
        if line.startswith("WORLD,"):
            self.pending_world = line
            return
        if line.startswith("STATE,"):
            parts = line.split(",", 3)
            if len(parts) >= 3:
                self.pending_states[parts[1]] = line
                return
        if front:
            self.control_backlog.appendleft(line)
        else:
            self.control_backlog.append(line)

    def absorb_outgoing(self):
        while True:
            try:
                self.remember_line(self.outgoing.get_nowait())
            except queue.Empty:
                return

    def next_pending_line(self):
        if self.control_backlog:
            return self.control_backlog.popleft()
        if self.pending_states:
            key = sorted(self.pending_states.keys())[0]
            return self.pending_states.pop(key)
        if self.pending_world:
            line = self.pending_world
            self.pending_world = None
            return line
        return None

    def pending_count(self):
        return len(self.control_backlog) + len(self.pending_states) + (1 if self.pending_world else 0)

    def run(self):
        backoff = 0.35
        while True:
            self.absorb_outgoing()
            port_name = self.resolve_port()
            if port_name is None:
                self.incoming.put("SERIAL_WAITING,no_ports")
                time.sleep(min(2.0, backoff))
                backoff = min(2.0, backoff * 1.35)
                continue

            self.port_name = port_name
            try:
                with serial.Serial(port_name, self.baud, timeout=0.04, write_timeout=0.50) as ser:
                    time.sleep(0.85)
                    try:
                        ser.reset_output_buffer()
                    except Exception:
                        pass
                    self.incoming.put(f"CONNECTED,{port_name},{self.baud}")
                    backoff = 0.35
                    last_flush_at = 0.0

                    while True:
                        self.absorb_outgoing()

                        sent = 0
                        while sent < 20:
                            line = self.next_pending_line()
                            if line is None:
                                break
                            try:
                                ser.write((line + "\n").encode("utf-8"))
                                sent += 1
                            except Exception:
                                self.remember_line(line, front=not line.startswith(("STATE,", "WORLD,")))
                                raise

                        now = time.time()
                        if sent and now - last_flush_at > 1.0:
                            last_flush_at = now

                        raw = ser.readline()
                        if raw:
                            self.incoming.put(raw.decode("utf-8", errors="replace").strip())
            except Exception as exc:
                self.absorb_outgoing()
                message = f"Serial reconnecting on {port_name}: {exc}"
                if "access is denied" in str(exc).lower() or "permission" in str(exc).lower():
                    message += " | Close Arduino Serial Monitor/Plotter or another server using this COM port."
                if "file not found" in str(exc).lower() or "cannot find" in str(exc).lower():
                    message += " | Waiting for the bridge COM port to come back."
                message += f" | queued={self.pending_count()}"
                print(message)
                self.incoming.put(f"SERIAL_DOWN,{port_name},{self.pending_count()}")
                time.sleep(backoff)
                backoff = min(2.0, backoff * 1.4)


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
        print("  audio rescan")
        print("  clients")
        print("  qr")
        print("  hotspot")

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
        if command == "audio" and len(parts) >= 2 and parts[1].lower() == "rescan":
            manifest = refresh_audio_manifest()
            print("audio files:")
            for category, files in manifest.items():
                print(f"  {category}: {len(files)}")
            return
        if command == "clients":
            summary = self.audio_hub.client_summary()
            p1 = summary.get(101, 0)
            pa = summary.get(102, 0)
            other = sum(count for player_id, count in summary.items() if player_id not in (101, 102))
            print(f"phone audio clients: total={self.audio_hub.client_count()} Sol={p1} Luna={pa} other={other}")
            return
        if command == "qr":
            print(f"phone URL: {self.phone_url}")
            if self.wifi_ssid:
                print(f"wifi SSID: {self.wifi_ssid}")
            else:
                print("wifi QR disabled: pass --wifi-ssid and --wifi-password")
            return
        if command == "hotspot":
            print("Windows hotspot survival checklist:")
            print("  1. Plug laptop into power and set Power mode to Best performance.")
            print("  2. Device Manager > Wi-Fi adapter > Power Management > uncheck 'Allow the computer to turn off this device'.")
            print("  3. If available, set the hotspot band to 2.4 GHz for range/stability.")
            print("  4. Keep phone screens awake; the server now falls back to laptop audio when a player's phone is gone.")
            print("  5. Run the server as close to the arena as possible; avoid USB3 hubs beside the ESP32/2.4 GHz radios.")
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
                 phone_url, audio_hub, wifi_ssid=None, wifi_password=None, flip_map_y=False):
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
        self.flip_map_y = flip_map_y
        self.last_status = "Waiting for radio data..."
        self.title_font = "Press Start 2P"
        self.body_font = "Pixelify Sans"

        self.root.title("Spell Arena Duel Dashboard")
        self.canvas = tk.Canvas(root, width=1360, height=820, bg=EDG["void"], highlightthickness=0)
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
        map_rect = (0, 0, width, height)

        self.canvas.create_rectangle(0, 0, width, height, fill=EDG["void"], outline="")

        screen, bounds = self.screen_mapper(map_rect)
        self.draw_map_panel(map_rect, screen, bounds)
        hud_w = min(390, max(320, int(width * 0.28)))
        hud_h = 190
        self.draw_player_hud(18, 18, hud_w, hud_h, self.game.players.get(101), 101, EDG["orange"], "left")
        self.draw_player_hud(width - hud_w - 18, 18, hud_w, hud_h, self.game.players.get(102), 102, EDG["cyan"], "right")
        self.draw_hud_damage_bursts(width, hud_w)

    def screen_mapper(self, rect, top_reserved=220):
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
        scale = min((x1 - x0 - 52) / (max_x - min_x), (y1 - y0 - top_reserved - 52) / (max_y - min_y))

        def screen(point):
            x, y = point
            sx = x0 + 26 + (x - min_x) * scale
            if self.flip_map_y:
                sy = y0 + top_reserved + 26 + (y - min_y) * scale
            else:
                sy = y1 - 26 - (y - min_y) * scale
            return sx, sy

        return screen, (min_x, max_x, min_y, max_y)

    def draw_map_panel(self, rect, screen, bounds):
        x0, y0, x1, y1 = rect
        self.canvas.create_rectangle(x0, y0, x1, y1, fill=EDG["void"], outline=EDG["indigo"], width=2)
        self.draw_grid(rect, screen, *bounds)
        link_rows = self.solver.link_rows()
        self.draw_links(screen, link_rows)
        self.draw_events(screen)
        self.draw_fields(screen)
        self.draw_prop_target(screen)
        self.draw_players(screen)
        # self.canvas.create_text(x0 + 22, y1 - 48, anchor="sw", text="SPELL ARENA", fill=EDG["yellow"], font=(self.title_font, 18))

    def draw_grid(self, rect, screen, min_x, max_x, min_y, max_y):
        x0, y0, x1, y1 = rect
        for x in range(math.floor(min_x), math.ceil(max_x) + 1):
            sx, _ = screen((x, min_y))
            self.canvas.create_line(sx, y0, sx, y1, fill=EDG["navy"])
        for y in range(math.floor(min_y), math.ceil(max_y) + 1):
            _, sy = screen((min_x, y))
            self.canvas.create_line(x0, sy, x1, sy, fill=EDG["navy"])

    def draw_links(self, screen, link_rows):
        for obs in link_rows[:24]:
            a = self.solver._node_pos(obs.observer_id, display=True)
            b = self.solver._node_pos(obs.source_id, display=True)
            if a is None or b is None:
                continue
            ax, ay = screen(a)
            bx, by = screen(b)
            color = EDG["indigo"] if obs.confidence >= 0.45 else EDG["navy"]
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
                color = EDG["orange"] if not event.blocked else EDG["cyan"]
                width = 2 + int(7 * frac)
                self.canvas.create_line(sx, sy, ex, ey, fill=color, width=width, dash=() if not event.blocked else (8, 5))
            if event.prop_hit and event.end:
                x, y = screen(event.end)
                radius = 20 + int((1.0 - frac) * 54)
                self.canvas.create_oval(x - radius, y - radius, x + radius, y + radius, outline=EDG["yellow"], width=3)
            if event.hp_loss and event.end:
                x, y = screen(event.end)
                progress = 1.0 - frac
                radius = 34 + int(progress * 70)
                color = EDG["yellow"] if progress < 0.45 else EDG["ember"]
                self.canvas.create_oval(x - radius, y - radius, x + radius, y + radius, outline=color, width=5)
                self.canvas.create_text(x, y - 66 - progress * 24, text="-HP", fill=EDG["red"], font=(self.title_font, 15))

    def draw_fields(self, screen):
        health = self.solver.field_health()
        colors = {"ok": EDG["blue"], "warn": EDG["gold"], "bad": EDG["red"]}
        for node_id, point in sorted(self.solver.anchors.items()):
            x, y = screen(point)
            status, quality = health.get(node_id, ("warn", 0.0))
            self.canvas.create_rectangle(x - 10, y - 10, x + 10, y + 10, fill=colors[status], outline=EDG["void"])
            self.canvas.create_text(x, y - 24, text=f"F{node_id}", fill=EDG["silver"], font=(self.body_font, 11, "bold"))
            self.canvas.create_text(x, y + 23, text=f"{quality:.0%}", fill=EDG["steel"], font=(self.body_font, 9))

    def draw_prop_target(self, screen):
        x, y = screen(self.game.prop_position())
        now = time.time()
        recent = any(event.prop_hit and event.expires_at > now for event in self.game.events)
        pulse = 8 if recent and int(now * 12) % 2 == 0 else 0
        self.canvas.create_oval(x - 20 - pulse, y - 20 - pulse, x + 20 + pulse, y + 20 + pulse, fill=EDG["leather"], outline=EDG["yellow"], width=3)
        self.canvas.create_oval(x - 7, y - 7, x + 7, y + 7, fill=EDG["yellow"], outline="")
        self.canvas.create_text(x, y + 34, text=PROP_NAME.upper(), fill=EDG["yellow"], font=(self.body_font, 11, "bold"))

    def draw_players(self, screen):
        now = time.time()
        colors = {101: EDG["orange"], 102: EDG["cyan"]}
        for player_id, player in sorted(self.game.players.items()):
            x, y = screen((player.x, player.y))
            color = colors.get(player_id, EDG["silver"]) if player.alive else EDG["slate"]
            if player.shield_until > now:
                self.draw_shield_cone(screen, player)
            self.canvas.create_oval(x - 17, y - 17, x + 17, y + 17, fill=color, outline=EDG["void"], width=2)
            self.canvas.create_oval(x - 26, y - 26, x + 26, y + 26, outline=color, width=2)
            dx, dy = yaw_to_vec(player.yaw_deg)
            ax, ay = screen((player.x + dx * 0.45, player.y + dy * 0.45))
            self.canvas.create_line(x, y, ax, ay, fill=EDG["white"], width=4, arrow=tk.LAST)
            self.canvas.create_text(x, y - 38, text=player_label(player_id).upper(), fill=EDG["white"], font=(self.body_font, 12, "bold"))

    def draw_shield_cone(self, screen, player):
        points = [screen((player.x, player.y))]
        for i in range(16):
            frac = i / 15.0
            angle = player.yaw_deg - SHIELD_ARC_DEG / 2.0 + frac * SHIELD_ARC_DEG
            dx, dy = yaw_to_vec(angle)
            points.append(screen((player.x + dx * 1.25, player.y + dy * 1.25)))
        flat = [coord for point in points for coord in point]
        self.canvas.create_polygon(flat, fill=EDG["teal_black"], outline=EDG["cyan"], stipple="gray25")

    def draw_player_hud(self, x, y, w, h, player, player_id, accent, align):
        stipple = "gray50"
        self.canvas.create_rectangle(x + 5, y + 5, x + w + 5, y + h + 5, fill=EDG["navy"], outline="", stipple=stipple)
        self.canvas.create_rectangle(x, y, x + w, y + h, fill=EDG["void"], outline=accent, width=4, stipple=stipple)
        label = player_label(player_id)
        anchor = "nw" if align == "left" else "ne"
        tx = x + 18 if align == "left" else x + w - 18
        state_anchor = "nw" if align == "left" else "ne"
        state_x = x + 202 if align == "left" else x + w - 202
        self.canvas.create_text(tx, y + 18, anchor=anchor, text=label.upper(), fill=accent, font=(self.title_font, 13))
        if player is None:
            self.canvas.create_text(state_x, y + 10, anchor=state_anchor, text="WAITING", fill=EDG["steel"], font=(self.body_font, 20, "bold"))
            return
        now = time.time()
        shield = max(0.0, player.shield_until - now)
        state = "DEAD" if not player.alive else ("SHIELD" if shield > 0 else ("COOLDOWN" if max(0.0, player.spell_lockout_until - now) > 0 else "READY"))
        self.canvas.create_text(state_x, y + 10, anchor=state_anchor, text=state, fill=accent, font=(self.body_font, 20, "bold"))
        self.draw_hp_blocks(x + 18, y + 48, w - 36, player.hp)
        self.draw_meter(x + 18, y + 92, w - 36, "MANA", player.mana / MAX_MANA, EDG["blue"])
        cooldown = max(0.0, player.spell_lockout_until - now)
        self.draw_meter(x + 18, y + 132, w - 36, "COOLDOWN", min(1.0, cooldown / SPELL_LOCKOUT_SECONDS), EDG["orange"])

    def draw_hp_blocks(self, x, y, w, hp):
        self.canvas.create_text(x, y, anchor="nw", text="HP", fill=EDG["steel"], font=(self.body_font, 13, "bold"))
        block_w = (w - 16) / MAX_HP
        for i in range(MAX_HP):
            bx = x + i * (block_w + 4)
            fill = EDG["green"] if i < hp else EDG["indigo"]
            self.canvas.create_rectangle(bx, y + 24, bx + block_w, y + 48, fill=fill, outline=EDG["slate"])

    def draw_meter(self, x, y, w, label, frac, color):
        frac = max(0.0, min(1.0, frac))
        self.canvas.create_text(x, y, anchor="nw", text=label, fill=EDG["steel"], font=(self.body_font, 13, "bold"))
        self.canvas.create_rectangle(x, y + 22, x + w, y + 40, fill=EDG["navy"], outline=EDG["slate"])
        if frac > 0:
            self.canvas.create_rectangle(x + 2, y + 24, x + 2 + (w - 4) * frac, y + 38, fill=color, outline="")

    def hud_damage_block_rect(self, width, hud_w, player_id, hp_after):
        hud_h = 190
        if player_id == 101:
            x = 18
        elif player_id == 102:
            x = width - hud_w - 18
        else:
            return None
        y = 18
        hp_x = x + 18
        hp_y = y + 48
        hp_w = hud_w - 36
        block_w = (hp_w - 16) / MAX_HP
        index = max(0, min(MAX_HP - 1, hp_after))
        bx = hp_x + index * (block_w + 4)
        return bx, hp_y + 24, bx + block_w, hp_y + 48

    def draw_hud_damage_bursts(self, width, hud_w):
        now = time.time()
        for event in list(self.game.events):
            if not event.hp_loss or event.expires_at <= now:
                continue
            player = self.game.players.get(event.target_id)
            if player is None:
                continue
            rect = self.hud_damage_block_rect(width, hud_w, event.target_id, player.hp)
            if rect is None:
                continue
            x0, y0, x1, y1 = rect
            cx = (x0 + x1) / 2.0
            cy = (y0 + y1) / 2.0
            duration = max(0.1, event.expires_at - event.created_at)
            progress = max(0.0, min(1.0, (now - event.created_at) / duration))
            fade_color = EDG["red"] if progress < 0.35 else (EDG["yellow"] if progress < 0.68 else EDG["slate"])
            self.canvas.create_rectangle(x0 - 5, y0 - 5, x1 + 5, y1 + 5, outline=EDG["red"], width=3)
            rng = random.Random(int(event.created_at * 1000) + event.target_id * 31)
            for i in range(18):
                angle = rng.random() * math.tau
                speed = 18 + rng.random() * 72
                px = cx + math.cos(angle) * speed * progress
                py = cy + math.sin(angle) * speed * progress + 18 * progress * progress
                size = max(2, 9 * (1.0 - progress) + rng.random() * 4)
                self.canvas.create_rectangle(px - size, py - size, px + size, py + size, fill=fade_color, outline="")
            ring = 12 + progress * 68
            self.canvas.create_oval(cx - ring, cy - ring, cx + ring, cy + ring, outline=fade_color, width=max(1, int(5 * (1.0 - progress))))

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
    parser.add_argument("--local-audio", choices=("all", "fallback", "off"), default="all",
                        help="Laptop audio behavior: all events, only phone fallback/prop, or off.")
    parser.add_argument("--flip-map-y", action="store_true", help="Flip the arena map vertically in the dashboard.")
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
    local_audio = LocalAudioPlayer()
    phone_url = f"http://{args.phone_host}:{args.phone_port}/"
    site_qr_path = ensure_site_qr(phone_url)
    try:
        PhoneAudioServer(args.phone_bind, args.phone_port, audio_hub).start()
        print(f"Phone audio server: {phone_url}")
        if site_qr_path:
            print(f"Site QR image: {site_qr_path}")
    except OSError as exc:
        print(f"Phone audio server failed on {args.phone_bind}:{args.phone_port}: {exc}")

    port_name = "fake"
    if args.fake:
        transport = FakeTransport(incoming, outgoing)
    else:
        requested_port = normalize_port_name(args.port)
        port_name = requested_port or "auto"
        if requested_port is None and choose_port() is None:
            print("No serial ports found yet. The server will keep waiting for the bridge ESP32.")
        transport = SerialTransport(requested_port, args.baud, incoming, outgoing)

    threading.Thread(target=transport.run, daemon=True).start()

    load_dashboard_fonts()
    root = tk.Tk()
    solver = MeshSolver()
    game = GameEngine(
        outgoing,
        simple_combat=args.simple_combat,
        audio_hub=audio_hub,
        local_audio=local_audio,
        local_audio_mode=args.local_audio,
        prop_position_getter=lambda: nearest_origin_anchor(solver.anchors),
    )
    threading.Thread(
        target=CommandConsole(outgoing, game, audio_hub, phone_url, args.wifi_ssid, args.wifi_password).run,
        daemon=True,
    ).start()
    MapApp(
        root, solver, game, incoming, outgoing, port_name, phone_url, audio_hub,
        args.wifi_ssid, args.wifi_password, flip_map_y=args.flip_map_y
    )
    root.mainloop()


if __name__ == "__main__":
    main()
