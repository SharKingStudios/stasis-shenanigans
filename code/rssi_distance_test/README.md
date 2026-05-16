# ESP-NOW RSSI Distance Test

Open `rssi_distance_test.ino` in Arduino IDE.

## First-Time Arduino IDE Setup

1. Install Arduino IDE 2.x.
2. Go to `File > Preferences`.
3. Add this to `Additional boards manager URLs`:

   `https://espressif.github.io/arduino-esp32/package_esp32_index.json`

4. Go to `Tools > Board > Boards Manager`.
5. Search `esp32` and install `esp32 by Espressif Systems`.
6. Select your board from `Tools > Board > esp32`.
   - Generic choice if unsure: `ESP32 Dev Module`.
7. Select the USB port from `Tools > Port`.

No extra Arduino libraries are needed for this test. `WiFi`, `esp_now`, and `esp_wifi` come with the ESP32 board package.

## Upload To Two Boards

1. For board A, set:

   `#define NODE_ID 1`

   Upload.

2. For board B, set:

   `#define NODE_ID 2`

   Upload.

3. Open Serial Monitor at `115200 baud`.

Expected output:

```text
RX from NODE 2 mac=AA:BB:CC:DD:EE:FF seq=42 rssi=-57 dBm est_distance=2.56 m
```

## Calibration

Put the two ESP32s exactly 1 meter apart with similar orientation.

Watch the `rssi=` value for 10-20 seconds. Put the rough average into:

```cpp
const float RSSI_AT_1_METER = -48.0f;
```

Then test at 0.5m, 1m, 2m, and 3m. The distance estimate will be noisy, but the trend should move in the right direction.

If Serial Monitor says `RSSI unavailable`, update the ESP32 board package in Boards Manager.
