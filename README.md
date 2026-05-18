# Fantasy Field Management System (FFMS)

The Fantasy Field Management System is a hackathon-built physical spell duel. Two handheld spellbooks, Sol and Luna, let players cast spells in the real world while a laptop turns radio signals, motion gestures, lights, audio, and a spectator dashboard into an interactive arena.

***I CAST FIREBALLLL!!!***

## Demo Link

[Check out the project demo on YouTube!](https://youtu.be/vkP3gLbwc5k)

## Pictures

<img width="1331" height="1002" alt="image" src="https://github.com/user-attachments/assets/f0ab8275-1182-4811-8b3d-6437fd8668c3" />

<img width="1331" height="1002" alt="image" src="https://github.com/user-attachments/assets/b30cc993-f66c-4704-bf36-5bca3aeffbb9" />
<img width="1331" height="1002" alt="image" src="https://github.com/user-attachments/assets/26770d9e-ce19-467d-82be-fc93d5381443" />
<img width="1331" height="1002" alt="image" src="https://github.com/user-attachments/assets/c2a63c58-0a0e-414a-aa64-fadfe6b66414" />
<img width="1331" height="1002" alt="image" src="https://github.com/user-attachments/assets/aac3388f-0f86-46dc-a2dc-0e253a784409" />
<img width="1331" height="1002" alt="image" src="https://github.com/user-attachments/assets/4f3c06cb-5722-4343-b0e2-e7e227db43d7" />

<img width="1331" height="1002" alt="image" src="https://github.com/user-attachments/assets/f5b9fb55-5402-4142-8f79-550c0da71b3d" />


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

<img width="1331" height="1002" alt="image" src="https://github.com/user-attachments/assets/5dade78c-1c2b-47c8-a340-ec15fd205931" />
