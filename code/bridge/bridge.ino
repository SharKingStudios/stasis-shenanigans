/*
  Spell Arena laptop bridge.

  ESP-NOW in:
    - field beacons
    - player RSSI reports
    - player status packets
    - player spell casts

  USB serial out to server:
    ANCHOR,id,xMeters,yMeters
    OBS,observerId,sourceId,rssiDbm,distanceMeters,sequence,senderUptimeMs
    STATUS,playerId,yawDeg,flags,seq,uptime
    CAST,playerId,spellType,yawDeg,confidence,seq,uptime

  USB serial in from server:
    STATE,targetId,hp,mana,flags,eventSeq,eventType
    WORLD,seq,id,xcm,ycm,yaw10,hp,mana,flags,...
    RECENTER,targetId
    TUNE,targetId,gestureStart,fbDown,fbBrake,fbPeak,shieldUp,shieldBrake,shieldPeak,unusedPitch,unusedPitchDelta,dominance,lockoutMs
    PROP,seq,fanMs,flashCount,flashOnMs,flashOffMs
    RELAY,name,on,ms
*/

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

#if __has_include(<esp_arduino_version.h>)
#include <esp_arduino_version.h>
#endif

#define BRIDGE_ID 250
#define WIFI_CHANNEL 6

const uint8_t LIGHT_RELAY_PIN = 25;
const uint8_t FAN_RELAY_PIN = 26;
const bool RELAY_ACTIVE_LOW = false;

const uint16_t PACKET_MAGIC = 0x51A7;
const uint8_t PACKET_TYPE_FIELD_BEACON = 1;
const uint8_t PACKET_TYPE_PLAYER_HELLO = 2;
const uint8_t PACKET_TYPE_PLAYER_REPORT = 3;
const uint8_t PACKET_TYPE_PLAYER_STATUS = 4;
const uint8_t PACKET_TYPE_SPELL_CAST = 5;
const uint8_t PACKET_TYPE_GAME_STATE = 6;
const uint8_t PACKET_TYPE_WORLD_STATE = 7;
const uint8_t PACKET_TYPE_RECENTER = 8;
const uint8_t PACKET_TYPE_TUNE = 9;

const uint32_t ANCHOR_REANNOUNCE_MS = 2000;

uint8_t broadcastMac[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

bool lightRelayOn = false;
bool fanRelayOn = false;
uint32_t fanRelayOffAtMs = 0;
uint16_t lightFlashTogglesRemaining = 0;
uint16_t lightFlashOnMs = 120;
uint16_t lightFlashOffMs = 120;
uint32_t lightNextToggleMs = 0;

struct __attribute__((packed)) FieldBeaconPacket {
  uint16_t magic;
  uint8_t packetType;
  uint8_t sourceId;
  uint16_t sequence;
  int16_t xCm;
  int16_t yCm;
  uint32_t uptimeMs;
};

struct __attribute__((packed)) PlayerReportPacket {
  uint16_t magic;
  uint8_t packetType;
  uint8_t reporterId;
  uint8_t observedId;
  uint8_t observedType;
  int8_t rssiDbm;
  uint16_t distanceCm;
  uint16_t reportSequence;
  uint16_t observedSequence;
  int16_t observedXCm;
  int16_t observedYCm;
  uint32_t observedUptimeMs;
};

struct __attribute__((packed)) PlayerStatusPacket {
  uint16_t magic;
  uint8_t packetType;
  uint8_t playerId;
  uint16_t sequence;
  int16_t yawDeg10;
  uint8_t flags;
  uint32_t uptimeMs;
};

struct __attribute__((packed)) SpellCastPacket {
  uint16_t magic;
  uint8_t packetType;
  uint8_t playerId;
  uint8_t spellType;
  uint16_t sequence;
  int16_t yawDeg10;
  uint8_t confidence;
  uint32_t uptimeMs;
};

struct __attribute__((packed)) GameStatePacket {
  uint16_t magic;
  uint8_t packetType;
  uint8_t targetId;
  uint8_t hp;
  uint8_t mana;
  uint8_t flags;
  uint16_t eventSequence;
  uint8_t eventType;
};

struct __attribute__((packed)) WorldPlayerState {
  uint8_t id;
  int16_t xCm;
  int16_t yCm;
  int16_t yawDeg10;
  uint8_t hp;
  uint8_t mana;
  uint8_t flags;
};

struct __attribute__((packed)) WorldStatePacket {
  uint16_t magic;
  uint8_t packetType;
  uint16_t sequence;
  uint8_t count;
  WorldPlayerState players[4];
};

struct __attribute__((packed)) RecenterPacket {
  uint16_t magic;
  uint8_t packetType;
  uint8_t targetId;
  uint16_t sequence;
};

struct __attribute__((packed)) TunePacket {
  uint16_t magic;
  uint8_t packetType;
  uint8_t targetId;
  uint16_t sequence;
  int16_t gestureStart100;
  int16_t fireballDown100;
  int16_t fireballBrake100;
  int16_t fireballPeak100;
  int16_t shieldUp100;
  int16_t shieldBrake100;
  int16_t shieldPeak100;
  int16_t shieldPitch100;
  int16_t shieldPitchDelta100;
  int16_t axisDominance100;
  uint16_t castLockoutMs;
};

struct FieldState {
  bool seen;
  uint8_t id;
  float xMeters;
  float yMeters;
  uint32_t lastAnnounceMs;
};

const int MAX_FIELDS = 12;
FieldState fields[MAX_FIELDS];
uint16_t recenterSequence = 0;
uint16_t tuneSequence = 0;

void writeRelay(uint8_t pin, bool on) {
  digitalWrite(pin, RELAY_ACTIVE_LOW ? !on : on);
}

void setLightRelay(bool on) {
  lightRelayOn = on;
  writeRelay(LIGHT_RELAY_PIN, on);
}

void setFanRelay(bool on) {
  fanRelayOn = on;
  writeRelay(FAN_RELAY_PIN, on);
}

void setupRelays() {
  pinMode(LIGHT_RELAY_PIN, OUTPUT);
  pinMode(FAN_RELAY_PIN, OUTPUT);
  setLightRelay(false);
  setFanRelay(false);
}

void startPropAnimation(uint16_t fanMs, uint8_t flashCount, uint16_t flashOnMs, uint16_t flashOffMs) {
  uint32_t now = millis();
  setFanRelay(fanMs > 0);
  fanRelayOffAtMs = fanMs > 0 ? now + fanMs : 0;

  lightFlashOnMs = flashOnMs < 20 ? 20 : flashOnMs;
  lightFlashOffMs = flashOffMs < 20 ? 20 : flashOffMs;
  lightFlashTogglesRemaining = flashCount * 2;
  setLightRelay(false);
  lightNextToggleMs = now;
}

void updateRelayAnimations() {
  uint32_t now = millis();
  if (fanRelayOn && fanRelayOffAtMs != 0 && (int32_t)(now - fanRelayOffAtMs) >= 0) {
    setFanRelay(false);
    fanRelayOffAtMs = 0;
  }

  if (lightFlashTogglesRemaining > 0 && (int32_t)(now - lightNextToggleMs) >= 0) {
    setLightRelay(!lightRelayOn);
    lightFlashTogglesRemaining--;
    lightNextToggleMs = now + (lightRelayOn ? lightFlashOnMs : lightFlashOffMs);
    if (lightFlashTogglesRemaining == 0 && lightRelayOn) {
      setLightRelay(false);
    }
  }
}

FieldState *fieldFor(uint8_t id) {
  for (int i = 0; i < MAX_FIELDS; i++) {
    if (fields[i].seen && fields[i].id == id) {
      return &fields[i];
    }
  }

  for (int i = 0; i < MAX_FIELDS; i++) {
    if (!fields[i].seen) {
      fields[i].seen = true;
      fields[i].id = id;
      fields[i].lastAnnounceMs = 0;
      return &fields[i];
    }
  }

  return nullptr;
}

void printAnchor(uint8_t id, float xMeters, float yMeters, bool throttle) {
  FieldState *field = fieldFor(id);
  uint32_t now = millis();

  if (field != nullptr) {
    field->xMeters = xMeters;
    field->yMeters = yMeters;
    if (throttle && now - field->lastAnnounceMs < ANCHOR_REANNOUNCE_MS) {
      return;
    }
    field->lastAnnounceMs = now;
  }

  Serial.print("ANCHOR,");
  Serial.print(id);
  Serial.print(",");
  Serial.print(xMeters, 3);
  Serial.print(",");
  Serial.println(yMeters, 3);
}

void printObservation(uint8_t observerId, uint8_t sourceId, int rssiDbm,
                      float distanceMeters, uint16_t sequence,
                      uint32_t senderUptimeMs) {
  Serial.print("OBS,");
  Serial.print(observerId);
  Serial.print(",");
  Serial.print(sourceId);
  Serial.print(",");
  Serial.print(rssiDbm);
  Serial.print(",");
  Serial.print(distanceMeters, 3);
  Serial.print(",");
  Serial.print(sequence);
  Serial.print(",");
  Serial.println(senderUptimeMs);
}

void printStatus(const PlayerStatusPacket &packet) {
  Serial.print("STATUS,");
  Serial.print(packet.playerId);
  Serial.print(",");
  Serial.print((float)packet.yawDeg10 / 10.0f, 1);
  Serial.print(",");
  Serial.print(packet.flags);
  Serial.print(",");
  Serial.print(packet.sequence);
  Serial.print(",");
  Serial.println(packet.uptimeMs);
}

void printCast(const SpellCastPacket &packet) {
  Serial.print("CAST,");
  Serial.print(packet.playerId);
  Serial.print(",");
  Serial.print(packet.spellType);
  Serial.print(",");
  Serial.print((float)packet.yawDeg10 / 10.0f, 1);
  Serial.print(",");
  Serial.print(packet.confidence);
  Serial.print(",");
  Serial.print(packet.sequence);
  Serial.print(",");
  Serial.println(packet.uptimeMs);
}

void handleFieldBeacon(const uint8_t *data, int len) {
  if (len != sizeof(FieldBeaconPacket)) {
    return;
  }

  FieldBeaconPacket packet;
  memcpy(&packet, data, sizeof(packet));

  if (packet.magic != PACKET_MAGIC || packet.packetType != PACKET_TYPE_FIELD_BEACON) {
    return;
  }

  printAnchor(packet.sourceId, (float)packet.xCm / 100.0f,
              (float)packet.yCm / 100.0f, true);
}

void handlePlayerReport(const uint8_t *data, int len) {
  if (len != sizeof(PlayerReportPacket)) {
    return;
  }

  PlayerReportPacket packet;
  memcpy(&packet, data, sizeof(packet));

  if (packet.magic != PACKET_MAGIC || packet.packetType != PACKET_TYPE_PLAYER_REPORT) {
    return;
  }

  if (packet.observedType == PACKET_TYPE_FIELD_BEACON) {
    printAnchor(packet.observedId, (float)packet.observedXCm / 100.0f,
                (float)packet.observedYCm / 100.0f, true);
  }

  printObservation(packet.reporterId,
                   packet.observedId,
                   packet.rssiDbm,
                   (float)packet.distanceCm / 100.0f,
                   packet.observedSequence,
                   packet.observedUptimeMs);
}

void handlePlayerStatus(const uint8_t *data, int len) {
  if (len != sizeof(PlayerStatusPacket)) {
    return;
  }

  PlayerStatusPacket packet;
  memcpy(&packet, data, sizeof(packet));
  if (packet.magic == PACKET_MAGIC && packet.packetType == PACKET_TYPE_PLAYER_STATUS) {
    printStatus(packet);
  }
}

void handleSpellCast(const uint8_t *data, int len) {
  if (len != sizeof(SpellCastPacket)) {
    return;
  }

  SpellCastPacket packet;
  memcpy(&packet, data, sizeof(packet));
  if (packet.magic == PACKET_MAGIC && packet.packetType == PACKET_TYPE_SPELL_CAST) {
    printCast(packet);
  }
}

int splitCsv(String line, String parts[], int maxParts) {
  int count = 0;
  int start = 0;
  while (count < maxParts && start <= line.length()) {
    int comma = line.indexOf(',', start);
    if (comma < 0) {
      parts[count++] = line.substring(start);
      break;
    }
    parts[count++] = line.substring(start, comma);
    start = comma + 1;
  }
  return count;
}

void sendGameState(String parts[], int count) {
  if (count < 7) {
    Serial.println("ERR,BAD_STATE");
    return;
  }

  GameStatePacket packet;
  packet.magic = PACKET_MAGIC;
  packet.packetType = PACKET_TYPE_GAME_STATE;
  packet.targetId = (uint8_t)parts[1].toInt();
  packet.hp = (uint8_t)constrain(parts[2].toInt(), 0, 255);
  packet.mana = (uint8_t)constrain(parts[3].toInt(), 0, 255);
  packet.flags = (uint8_t)parts[4].toInt();
  packet.eventSequence = (uint16_t)parts[5].toInt();
  packet.eventType = (uint8_t)parts[6].toInt();

  esp_now_send(broadcastMac, (uint8_t *)&packet, sizeof(packet));
}

void sendWorldState(String parts[], int count) {
  if (count < 3) {
    Serial.println("ERR,BAD_WORLD");
    return;
  }

  WorldStatePacket packet = {};
  packet.magic = PACKET_MAGIC;
  packet.packetType = PACKET_TYPE_WORLD_STATE;
  packet.sequence = (uint16_t)parts[1].toInt();

  int idx = 2;
  while (idx + 6 < count && packet.count < 4) {
    WorldPlayerState &p = packet.players[packet.count];
    p.id = (uint8_t)parts[idx++].toInt();
    p.xCm = (int16_t)parts[idx++].toInt();
    p.yCm = (int16_t)parts[idx++].toInt();
    p.yawDeg10 = (int16_t)parts[idx++].toInt();
    p.hp = (uint8_t)parts[idx++].toInt();
    p.mana = (uint8_t)parts[idx++].toInt();
    p.flags = (uint8_t)parts[idx++].toInt();
    packet.count++;
  }

  esp_now_send(broadcastMac, (uint8_t *)&packet, sizeof(packet));
}

void sendRecenter(String parts[], int count) {
  RecenterPacket packet;
  packet.magic = PACKET_MAGIC;
  packet.packetType = PACKET_TYPE_RECENTER;
  packet.targetId = count >= 2 ? (uint8_t)parts[1].toInt() : 255;
  packet.sequence = recenterSequence++;
  esp_now_send(broadcastMac, (uint8_t *)&packet, sizeof(packet));
}

int16_t scaled100(String value) {
  return (int16_t)roundf(value.toFloat() * 100.0f);
}

void sendTune(String parts[], int count) {
  if (count < 13) {
    Serial.println("ERR,BAD_TUNE");
    return;
  }

  TunePacket packet;
  packet.magic = PACKET_MAGIC;
  packet.packetType = PACKET_TYPE_TUNE;
  packet.targetId = (uint8_t)parts[1].toInt();
  packet.sequence = tuneSequence++;
  packet.gestureStart100 = scaled100(parts[2]);
  packet.fireballDown100 = scaled100(parts[3]);
  packet.fireballBrake100 = scaled100(parts[4]);
  packet.fireballPeak100 = scaled100(parts[5]);
  packet.shieldUp100 = scaled100(parts[6]);
  packet.shieldBrake100 = scaled100(parts[7]);
  packet.shieldPeak100 = scaled100(parts[8]);
  packet.shieldPitch100 = scaled100(parts[9]);
  packet.shieldPitchDelta100 = scaled100(parts[10]);
  packet.axisDominance100 = scaled100(parts[11]);
  packet.castLockoutMs = (uint16_t)constrain(parts[12].toInt(), 200, 5000);

  esp_now_send(broadcastMac, (uint8_t *)&packet, sizeof(packet));
}

void handlePropCommand(String parts[], int count) {
  uint16_t fanMs = count >= 3 ? (uint16_t)constrain(parts[2].toInt(), 0, 10000) : 2500;
  uint8_t flashCount = count >= 4 ? (uint8_t)constrain(parts[3].toInt(), 0, 20) : 4;
  uint16_t flashOnMs = count >= 5 ? (uint16_t)constrain(parts[4].toInt(), 20, 2000) : 120;
  uint16_t flashOffMs = count >= 6 ? (uint16_t)constrain(parts[5].toInt(), 20, 2000) : 120;
  startPropAnimation(fanMs, flashCount, flashOnMs, flashOffMs);
}

void handleRelayCommand(String parts[], int count) {
  if (count < 3) {
    Serial.println("ERR,BAD_RELAY");
    return;
  }

  String name = parts[1];
  name.toLowerCase();
  String action = parts[2];
  action.toLowerCase();
  bool on = action == "on" || action == "1" || action == "true";
  uint16_t durationMs = count >= 4 ? (uint16_t)constrain(parts[3].toInt(), 0, 10000) : 0;
  uint32_t now = millis();

  if (name == "light" || name == "lights") {
    lightFlashTogglesRemaining = 0;
    setLightRelay(on);
    if (on && durationMs > 0) {
      lightFlashOnMs = durationMs;
      lightFlashOffMs = 20;
      lightFlashTogglesRemaining = 1;
      lightNextToggleMs = now + durationMs;
    }
  } else if (name == "fan") {
    setFanRelay(on);
    fanRelayOffAtMs = (on && durationMs > 0) ? now + durationMs : 0;
  } else {
    Serial.println("ERR,BAD_RELAY_NAME");
  }
}

void handleSerialLine(String line) {
  line.trim();
  if (line.length() == 0) {
    return;
  }

  String parts[40];
  int count = splitCsv(line, parts, 40);
  if (count <= 0) {
    return;
  }

  if (parts[0] == "STATE") {
    sendGameState(parts, count);
  } else if (parts[0] == "WORLD") {
    sendWorldState(parts, count);
  } else if (parts[0] == "RECENTER") {
    sendRecenter(parts, count);
  } else if (parts[0] == "TUNE") {
    sendTune(parts, count);
  } else if (parts[0] == "PROP") {
    handlePropCommand(parts, count);
  } else if (parts[0] == "RELAY") {
    handleRelayCommand(parts, count);
  }
}

void readSerialCommands() {
  static String line = "";
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      handleSerialLine(line);
      line = "";
    } else if (c != '\r' && line.length() < 260) {
      line += c;
    }
  }
}

#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
void onDataRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  (void)info;

  if (len < 3 ||
      data[0] != (uint8_t)(PACKET_MAGIC & 0xFF) ||
      data[1] != (uint8_t)(PACKET_MAGIC >> 8)) {
    return;
  }

  if (data[2] == PACKET_TYPE_FIELD_BEACON) {
    handleFieldBeacon(data, len);
  } else if (data[2] == PACKET_TYPE_PLAYER_REPORT) {
    handlePlayerReport(data, len);
  } else if (data[2] == PACKET_TYPE_PLAYER_STATUS) {
    handlePlayerStatus(data, len);
  } else if (data[2] == PACKET_TYPE_SPELL_CAST) {
    handleSpellCast(data, len);
  }
}
#else
void onDataRecv(const uint8_t *mac, const uint8_t *data, int len) {
  (void)mac;

  if (len < 3 ||
      data[0] != (uint8_t)(PACKET_MAGIC & 0xFF) ||
      data[1] != (uint8_t)(PACKET_MAGIC >> 8)) {
    return;
  }

  if (data[2] == PACKET_TYPE_FIELD_BEACON) {
    handleFieldBeacon(data, len);
  } else if (data[2] == PACKET_TYPE_PLAYER_REPORT) {
    handlePlayerReport(data, len);
  } else if (data[2] == PACKET_TYPE_PLAYER_STATUS) {
    handlePlayerStatus(data, len);
  } else if (data[2] == PACKET_TYPE_SPELL_CAST) {
    handleSpellCast(data, len);
  }
}
#endif

void setupEspNow() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);

  if (esp_now_init() != ESP_OK) {
    Serial.println("ERR,ESP_NOW_INIT_FAILED");
    delay(1000);
    ESP.restart();
  }

  esp_now_register_recv_cb(onDataRecv);

  esp_now_peer_info_t peerInfo = {};
  memcpy(peerInfo.peer_addr, broadcastMac, 6);
  peerInfo.channel = WIFI_CHANNEL;
  peerInfo.encrypt = false;

  if (esp_now_add_peer(&peerInfo) != ESP_OK) {
    Serial.println("ERR,ADD_BROADCAST_PEER_FAILED");
    delay(1000);
    ESP.restart();
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);
  setupRelays();

  Serial.println();
  Serial.println("ESP-NOW Spell Arena laptop bridge");
  Serial.print("BRIDGE_ID=");
  Serial.println(BRIDGE_ID);
  Serial.print("WIFI_CHANNEL=");
  Serial.println(WIFI_CHANNEL);

  setupEspNow();

  Serial.print("WiFi MAC=");
  Serial.println(WiFi.macAddress());
  Serial.println("Ready. Relaying radio packets and server commands.");
}

void loop() {
  readSerialCommands();
  updateRelayAnimations();
  delay(5);
}
