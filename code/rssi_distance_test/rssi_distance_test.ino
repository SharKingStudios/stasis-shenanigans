/*
  ESP32 ESP-NOW RSSI distance smoke test

  Upload this same sketch to two ESP32 boards.
  Change NODE_ID before each upload:
    Board A: NODE_ID 1
    Board B: NODE_ID 2

  Open Serial Monitor at 115200 baud on either board.

  Notes:
  - ESP-NOW and WiFi are included with the ESP32 Arduino board package.
  - RSSI distance is noisy. Use this for relative testing and calibration first.
  - Current Arduino-ESP32 cores expose RSSI in the ESP-NOW receive callback.
    Older cores may show "RSSI unavailable"; install/update the ESP32 board
    package if that happens.
*/

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

#if __has_include(<esp_arduino_version.h>)
#include <esp_arduino_version.h>
#endif

// ---------- Easy per-board settings ----------
#define NODE_ID 2            // Change to 2 on the second board.
#define WIFI_CHANNEL 6       // Every board in the arena must use the same channel.

// Calibration knobs:
// At 1 meter apart, watch the average RSSI and put that value here.
const float RSSI_AT_1_METER = -48.0f;
// 2.0 is open space. 2.5-3.0 is more realistic around bodies/obstacles.
const float PATH_LOSS_N = 2.2f;

const uint32_t SEND_INTERVAL_MS = 250;
const uint16_t PACKET_MAGIC = 0x51A7;

// Broadcast peer: FF:FF:FF:FF:FF:FF
uint8_t broadcastMac[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

struct __attribute__((packed)) RssiTestPacket {
  uint16_t magic;
  uint8_t sourceId;
  uint16_t sequence;
  uint32_t uptimeMs;
};

uint16_t sequenceNumber = 0;
uint32_t lastSendMs = 0;

float estimateDistanceMeters(int rssiDbm) {
  if (rssiDbm >= 0) {
    return NAN;
  }

  // Log-distance path loss model:
  // d = 10 ^ ((RSSI_at_1m - RSSI) / (10 * n))
  return powf(10.0f, (RSSI_AT_1_METER - (float)rssiDbm) / (10.0f * PATH_LOSS_N));
}

void printMacAddress(const uint8_t *mac) {
  for (int i = 0; i < 6; i++) {
    if (i > 0) {
      Serial.print(":");
    }
    if (mac[i] < 16) {
      Serial.print("0");
    }
    Serial.print(mac[i], HEX);
  }
}

void handlePacket(const uint8_t *mac, const uint8_t *data, int len, int rssiDbm) {
  if (len != sizeof(RssiTestPacket)) {
    return;
  }

  RssiTestPacket packet;
  memcpy(&packet, data, sizeof(packet));

  if (packet.magic != PACKET_MAGIC || packet.sourceId == NODE_ID) {
    return;
  }

  Serial.print("RX from NODE ");
  Serial.print(packet.sourceId);
  Serial.print(" mac=");
  printMacAddress(mac);
  Serial.print(" seq=");
  Serial.print(packet.sequence);

  if (rssiDbm < 0) {
    float meters = estimateDistanceMeters(rssiDbm);
    Serial.print(" rssi=");
    Serial.print(rssiDbm);
    Serial.print(" dBm est_distance=");
    Serial.print(meters, 2);
    Serial.println(" m");
  } else {
    Serial.println(" RSSI unavailable on this Arduino-ESP32 core");
  }
}

#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
void onDataRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  int rssiDbm = 127;

  if (info != nullptr && info->rx_ctrl != nullptr) {
    rssiDbm = info->rx_ctrl->rssi;
  }

  const uint8_t *mac = info != nullptr ? info->src_addr : nullptr;
  if (mac != nullptr) {
    handlePacket(mac, data, len, rssiDbm);
  }
}
#else
void onDataRecv(const uint8_t *mac, const uint8_t *data, int len) {
  handlePacket(mac, data, len, 127);
}
#endif

void setupEspNow() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();

  // Force a fixed WiFi channel without connecting to an access point.
  esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);

  if (esp_now_init() != ESP_OK) {
    Serial.println("ESP-NOW init failed. Restarting...");
    delay(1000);
    ESP.restart();
  }

  esp_now_register_recv_cb(onDataRecv);

  esp_now_peer_info_t peerInfo = {};
  memcpy(peerInfo.peer_addr, broadcastMac, 6);
  peerInfo.channel = WIFI_CHANNEL;
  peerInfo.encrypt = false;

  if (esp_now_add_peer(&peerInfo) != ESP_OK) {
    Serial.println("Failed to add broadcast peer. Restarting...");
    delay(1000);
    ESP.restart();
  }
}

void sendPacket() {
  RssiTestPacket packet;
  packet.magic = PACKET_MAGIC;
  packet.sourceId = NODE_ID;
  packet.sequence = sequenceNumber++;
  packet.uptimeMs = millis();

  esp_err_t result = esp_now_send(broadcastMac, (uint8_t *)&packet, sizeof(packet));
  if (result != ESP_OK) {
    Serial.print("TX failed, esp_err=");
    Serial.println(result);
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);

  Serial.println();
  Serial.println("ESP-NOW RSSI distance smoke test");
  Serial.print("NODE_ID=");
  Serial.println(NODE_ID);
  Serial.print("WIFI_CHANNEL=");
  Serial.println(WIFI_CHANNEL);
  Serial.print("RSSI_AT_1_METER=");
  Serial.print(RSSI_AT_1_METER, 1);
  Serial.print(" PATH_LOSS_N=");
  Serial.println(PATH_LOSS_N, 1);

  setupEspNow();
  Serial.print("WiFi MAC=");
  Serial.println(WiFi.macAddress());
  Serial.println("Ready. Flash a second board with a different NODE_ID.");
}

void loop() {
  uint32_t now = millis();
  if (now - lastSendMs >= SEND_INTERVAL_MS) {
    lastSendMs = now;
    sendPacket();
  }
}
