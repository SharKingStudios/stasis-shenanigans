# Fantasy Field Management System (FFMS)

The Fantasy Field Management System is a hackathon-built physical spell duel. Two handheld spellbooks, Sol and Luna, let players cast spells in the real world while a laptop turns radio signals, motion gestures, lights, audio, and a spectator dashboard into an interactive arena.

***I CAST FIREBALLLL!!!***

## Demo Link

[Check out the project demo on YouTube!](https://youtu.be/vkP3gLbwc5k)

## Pictures

Add pictures here

## What It Does

Players hold ESP32-powered spellbooks named **Sol** and **Luna**. Each spellbook tracks movement with an MPU6050, broadcasts radio observations, detects spell gestures, displays mana/health, and reacts to game events.

The arena also contains **Meghana**, our local trapped soul. Meghana is the physical prop target: when a fireball hits it, the lights flash, the head spins, and they let you know they didnt like that...

Scattered around the play area are several fixed field nodes. They work a little like a homemade GPS system, except instead of satellites we use ESP32 boards and Wi-Fi/ESP-NOW packet strength. The laptop receives a stream of radio observations, filters a lot of noisy RSSI data, and estimates where the spellbooks are in the arena.

## System Overview

- **Sol**: Player spellbook
- **Luna**: The other player spellbook
- **Meghana**: Physical spinning head/light prop target near the arena origin.
- **Field nodes**: Fixed ESP32 beacons placed around the play area.
- **Bridge node**: ESP32 connected to the laptop over USB, relaying ESP-NOW packets and controlling the relays.
- **Laptop server**: Python game engine, localization solver, audio coordinator, relay controller, and spectator dashboard.

## Firmware Map

- [`code/player/player.ino`](code/player/player.ino): Firmware for Sol/Luna spellbooks. It reads the MPU6050, detects fireball/shield spell gestures, tracks yaw, and sends radio reports of all of this back to the server.
- [`code/field/field.ino`](code/field/field.ino): Firmware for fixed field nodes. These boards broadcast their ID and fixed arena position so the spellbooks and laptop can use them as reference points.
- [`code/bridge/bridge.ino`](code/bridge/bridge.ino): Firmware for the USB bridge. It listens to ESP-NOW traffic, prints that data to serial for the server, broadcasts server commands back to players, and controls the light/fan relays for Meghana.

## Server And Assets

- [`code/server/server.py`](code/server/server.py): Main Python server and spectator dashboard. It handles serial reconnects, localization, combat rules, phone/laptop audio, prop hits, and the Tkinter arena view.
- [`code/server/static/audio/`](code/server/static/audio/): Sound effects and voice lines. Files are discovered by name prefix, so new sounds can be dropped in without editing code.
- [`code/server/static/qr/site_qr.png`](code/server/static/qr/site_qr.png): QR code for the phone audio page.
- [`code/server/requirements.txt`](code/server/requirements.txt): Python dependencies.

