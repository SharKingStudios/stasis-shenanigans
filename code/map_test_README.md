# Spell Arena Build Notes

## Roles

- `code/field/field.ino`: fixed field beacon. It only broadcasts ID and position.
- `code/player/player.ino`: moving player controller. It reads RSSI, MPU6050 yaw, gestures, OLED mana, and HP LEDs.
- `code/bridge/bridge.ino`: laptop radio bridge. It relays player packets to USB serial and broadcasts server commands back to players.
- `code/server/server.py`: authoritative game server and dashboard.

## Arduino Libraries

ESP-NOW and WiFi come from the ESP32 board package.

For OLED support, install these Arduino libraries:

- `Adafruit SSD1306`
- `Adafruit GFX Library`

The player sketch still compiles without those OLED libraries, but the OLED screen will be disabled.

## Player Hardware Defaults

- MPU6050 I2C: `SDA=21`, `SCL=22`, address `0x68`
- SSD1306 OLED: address `0x3C`
- Green health LEDs: GPIO `32, 33, 25, 26, 27`
- Red effect LEDs: GPIO `16, 17, 18, 19`
- Power on each player while its controller points arena north. Press `r` in the server dashboard to recenter all yaw readings.

LED rows for the common 30-pin ESP32 DevKit:

| LED | GPIO | Board pin |
| --- | --- | --- |
| Green HP 1 | `GPIO32` | left pin `7` |
| Green HP 2 | `GPIO33` | left pin `8` |
| Green HP 3 | `GPIO25` | left pin `9` |
| Green HP 4 | `GPIO26` | left pin `10` |
| Green HP 5 | `GPIO27` | left pin `11` |
| Red FX 1 | `GPIO16` | right pin `27` |
| Red FX 2 | `GPIO17` | right pin `28` |
| Red FX 3 | `GPIO18` | right pin `30` |
| Red FX 4 | `GPIO19` | right pin `31` |

Each LED needs its own resistor:

```text
ESP32 GPIO -> 220-470 ohm resistor -> LED long leg
LED short leg -> GND
```

`GPIO5` on right pin `29` is intentionally skipped. It would make the red LED row perfectly consecutive, but it is a boot strapping pin and is not worth the risk during a hackathon.

## Run

Install Python dependencies:

```powershell
cd "C:\Users\Logan\Documents\GitHub\stasis shenanigans"
python -m pip install -r code/server/requirements.txt
```

List ports:

```powershell
python code/server/server.py --list-ports
```

Run with the bridge or a directly plugged-in player:

```powershell
python code/server/server.py --port COM4 --rssi-1m -52 --path-loss 2.7
```

Emergency mode if MPU yaw is unreliable:

```powershell
python code/server/server.py --port COM4 --rssi-1m -52 --path-loss 2.7 --simple-combat
```

In simple combat, fireball hits the nearest living player within `2.4m` regardless of facing, and shield blocks from every direction. This is the fastest fallback if orientation is not hackathon-ready.

Run without hardware:

```powershell
python code/server/server.py --fake
```

## Game Defaults

- 5 HP per player
- 100 mana
- Mana regen: 10/sec
- Fireball: costs 25 mana, 1 damage, 0.75 sec cooldown
- Shield: costs 20 mana, lasts 1.5 sec, 110 degree protection cone
- Fireball hit area: 0.8m wide rectangle, 100m long

## Player Startup Diagnostics

The player now runs an LED/OLED self-test at boot.

Expected serial clues:

```text
IMU_READY samples=120 gzBias=... gzRange=... raw
OLED_DISABLED install Adafruit SSD1306 and Adafruit GFX Library
OLED_MISSING check VCC/GND/SDA/SCL/address/libs
IMU,1,yaw,gyroZ,bias,gyroX,gyroY,yawFrozen,tiltFromBoot,ax,ay,az,linearAccel,shieldPeak,shieldReady,peakPitch,gestureActive,fireballPeak,fireballBrake,shieldPeak,shieldBrake,peakUp,peakDown,fireballDominance,shieldDominance,fireballReady,shieldReady,peakLinear,firstDir,startPitch,peakPitch
```

If yaw spins while the board is sitting still, watch the `IMU` line:

- `gyroZ` should settle near `0.00`.
- `linearAccel` should be near `0.00` when still.
- If `gyroZ` is large while still, keep the board motionless during boot and press reset.
- If `IMU_READY` never appears, check MPU6050 power, ground, SDA, SCL, and address.

The firmware now applies mount correction before gesture detection. If the
spellbook acts 180 degrees wrong, tune these at the top of `player.ino`:
`IMU_ACCEL_X_SIGN`, `IMU_ACCEL_Y_SIGN`, `IMU_ACCEL_Z_SIGN`,
`IMU_GYRO_Z_SIGN`, and `OLED_ROTATION`.

Fireball waits until the end of a motion before casting. It is now only looking
for a deliberate down flick: acceleration in the fireball-down direction,
followed by braking/deceleration, with vertical-axis energy dominating side
movement. If it casts too easily, raise `FIREBALL_DOWN_ACCEL_G`,
`FIREBALL_DOWN_BRAKE_G`, `FIREBALL_MIN_PEAK_LINEAR_G`, or
`SPELL_AXIS_DOMINANCE`. If a real down flick does not raise `fireballPeak`,
flip `FIREBALL_DOWN_SIGN`.

Shield is the opposite vertical gesture: fling up, then brake. It uses the same
kind of full check as fireball: first direction, acceleration peak, braking
phase, total motion, and axis dominance. If shield casts too easily, raise
`SHIELD_UP_ACCEL_G`, `SHIELD_UP_BRAKE_G`, `SHIELD_MIN_PEAK_LINEAR_G`, or
`SPELL_AXIS_DOMINANCE`. If a real up flick does not raise `shieldPeak`, flip
`SHIELD_UP_SIGN`.

If yaw rotates while the board is sitting still, leave it flat and motionless
for a second. The firmware relearns gyro bias while still. If it keeps drifting,
raise `GYRO_YAW_DEADBAND_DPS` slightly. `yawFrozen` is left in the debug output,
but yaw freezing is disabled; it should stay `0`.

There is also a shared `CAST_LOCKOUT_MS`, so one gesture window can only produce
one spell type and the board will not immediately cast the other spell after it.
The player OLED switches to a large `CD 1.8s` countdown with a progress bar
during this lockout. The server dashboard also shows a red cooldown bar per
player. Shield now locks out casting until after shield expires, so players
cannot shield and immediately fire while still protected.

## Live Gesture Tuning

Run `server.py` from PowerShell and type these into the same terminal while it
is running:

```text
tune easy
tune normal
tune hard
recenter 255
```

The command is broadcast through the bridge to all players. `easy` is useful if
spells are not firing; `hard` is useful if they fire accidentally. You can also
tune one player or all players with raw values:

```text
tune 255 0.30 0.56 -0.28 0.74 0.52 -0.24 0.70 0 0 1.10 1800
```

Fields are:

```text
target gestureStart fireballDown fireballBrake fireballPeak shieldUp shieldBrake shieldPeak unusedPitch unusedPitchDelta axisDominance lockoutMs
```

## Serial Protocol

Server reads:

```text
ANCHOR,id,xMeters,yMeters
OBS,observerId,sourceId,rssiDbm,distanceMeters,sequence,senderUptimeMs
STATUS,playerId,yawDeg,flags,seq,uptime
CAST,playerId,spellType,yawDeg,confidence,seq,uptime
```

Server sends:

```text
STATE,targetId,hp,mana,flags,eventSeq,eventType
WORLD,seq,id,xcm,ycm,yaw10,hp,mana,flags,...
RECENTER,targetId
```

Spell types:

- `1`: fireball
- `2`: shield

Event types:

- `0`: none
- `1`: fireball cast
- `2`: shield cast
- `3`: hit
- `4`: block
- `5`: death
- `6`: denied

## Practical Notes

RSSI is still noisy. The dashboard intentionally uses forgiving spell geometry and robust link filtering so the game can work under hackathon conditions.

The solver now trusts closer, more stable links much more than distant noisy
links. With only three field nodes in an L shape, expect good behavior near one
field and weaker behavior in the open area opposite the L. Adding 4-6 field
nodes around the perimeter, spread out and elevated with line of sight, should
help a lot because each player is more likely to have 2-3 usable nearby anchors
at all times. Bad non-line-of-sight nodes can still hurt, but the server
downweights far/noisy links instead of letting them drag the whole solve around.

If `COM4` says access denied, close Arduino Serial Monitor/Plotter and any other running Python server. Only one process can own a COM port.
