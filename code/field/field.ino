/*
  Field node firmware.

  These ESP32s are the fixed "GPS sender" boards for the arena. They do not
  calculate position, listen for players, or talk to the laptop. They only
  broadcast their ID and known fixed position over ESP-NOW.

  Upload this same sketch to each field ESP32. Before each upload, change:
    FIELD_ID
    FIELD_X_METERS
    FIELD_Y_METERS
*/

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

// ---------- Change these per field board ----------
#define FIELD_ID 4
const float FIELD_X_METERS = -2.0f;
const float FIELD_Y_METERS = 3.0f;

// ---------- Shared radio settings ----------
#define WIFI_CHANNEL 6

const uint16_t PACKET_MAGIC = 0x51A7;
const uint8_t PACKET_TYPE_FIELD_BEACON = 1;
const uint32_t BEACON_INTERVAL_MS = 150;

uint8_t broadcastMac[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
uint16_t sequenceNumber = 0;
uint32_t lastBeaconMs = 0;
uint32_t lastSerialMs = 0;

struct __attribute__((packed)) FieldBeaconPacket {
  uint16_t magic;
  uint8_t packetType;
  uint8_t sourceId;
  uint16_t sequence;
  int16_t xCm;
  int16_t yCm;
  uint32_t uptimeMs;
};

void setupEspNow() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);

  if (esp_now_init() != ESP_OK) {
    Serial.println("ESP-NOW init failed. Restarting...");
    delay(1000);
    ESP.restart();
  }

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

void sendBeacon() {
  FieldBeaconPacket packet;
  packet.magic = PACKET_MAGIC;
  packet.packetType = PACKET_TYPE_FIELD_BEACON;
  packet.sourceId = FIELD_ID;
  packet.sequence = sequenceNumber++;
  packet.xCm = (int16_t)roundf(FIELD_X_METERS * 100.0f);
  packet.yCm = (int16_t)roundf(FIELD_Y_METERS * 100.0f);
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
  Serial.println("ESP-NOW field beacon");
  Serial.print("FIELD_ID=");
  Serial.println(FIELD_ID);
  Serial.print("FIELD_X_METERS=");
  Serial.println(FIELD_X_METERS, 2);
  Serial.print("FIELD_Y_METERS=");
  Serial.println(FIELD_Y_METERS, 2);
  Serial.print("WIFI_CHANNEL=");
  Serial.println(WIFI_CHANNEL);

  setupEspNow();

  Serial.print("WiFi MAC=");
  Serial.println(WiFi.macAddress());
  Serial.println("Broadcasting fixed field position.");
}

void loop() {
  uint32_t now = millis();

  if (now - lastBeaconMs >= BEACON_INTERVAL_MS) {
    lastBeaconMs = now;
    sendBeacon();
  }

  if (now - lastSerialMs >= 2000) {
    lastSerialMs = now;
    Serial.print("FIELD_READY id=");
    Serial.print(FIELD_ID);
    Serial.print(" x=");
    Serial.print(FIELD_X_METERS, 2);
    Serial.print(" y=");
    Serial.println(FIELD_Y_METERS, 2);
  }
}
