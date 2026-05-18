/*
  Spell Arena player node.

  Hardware:
    - ESP32
    - MPU6050 on I2C SDA=21, SCL=22, address 0x68
    - Optional SSD1306 128x64 OLED on I2C address 0x3C
    - 5 green health LEDs on GPIO 32, 33, 25, 26, 27
    - 4 red effect LEDs on GPIO 16, 17, 18, 19
    - Onboard status LED on GPIO 2
    - Active-low yaw recenter button on GPIO 23 to GND

  The player listens for field beacons and other player hellos, sends RSSI
  observations, tracks yaw from the MPU6050 gyro, detects gestures locally,
  and accepts game-state packets from the bridge.
*/

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Wire.h>

#if __has_include(<esp_arduino_version.h>)
#include <esp_arduino_version.h>
#endif

#if __has_include(<Adafruit_GFX.h>) && __has_include(<Adafruit_SSD1306.h>)
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#define HAS_OLED 1
#else
#define HAS_OLED 0
#endif

// ---------- Change this per player ----------
#define PLAYER_ID 102

// ---------- Hardware defaults ----------
#define I2C_SDA_PIN 21
#define I2C_SCL_PIN 22
#define MPU6050_ADDR 0x68
#define OLED_ADDR 0x3C

// Physical row priority on the common 30-pin ESP32 DevKit:
// Health LEDs are left-side pins 7-11 in a clean row.
// Effect LEDs are right-side pins 27, 28, 30, 31. GPIO5/pin29 is skipped
// because it is a boot strapping pin.
const uint8_t HEALTH_LED_PINS[5] = {32, 33, 25, 26, 27};
const uint8_t EFFECT_LED_PINS[4] = {16, 17, 18, 19};
const uint8_t ONBOARD_LED_PIN = 2;
const uint8_t RECENTER_BUTTON_PIN = 23;

// ---------- Shared radio settings ----------
#define WIFI_CHANNEL 6

// Calibration knobs from your two-board test.
const float RSSI_AT_1_METER = -48.0f;
const float PATH_LOSS_N = 2.2f;

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

const uint8_t SPELL_FIREBALL = 1;
const uint8_t SPELL_SHIELD = 2;

const uint8_t EVENT_NONE = 0;
const uint8_t EVENT_CAST_FIREBALL = 1;
const uint8_t EVENT_CAST_SHIELD = 2;
const uint8_t EVENT_HIT = 3;
const uint8_t EVENT_BLOCK = 4;
const uint8_t EVENT_DEATH = 5;
const uint8_t EVENT_DENIED = 6;

const uint8_t FLAG_ALIVE = 1 << 0;
const uint8_t FLAG_SHIELD = 1 << 1;

const uint32_t HELLO_INTERVAL_MS = 250;
const uint32_t STATUS_INTERVAL_MS = 80;
const uint32_t ANCHOR_REANNOUNCE_MS = 2000;
const uint32_t OLED_INTERVAL_MS = 120;
const uint32_t FIREBALL_COOLDOWN_MS = 650;
const uint32_t SHIELD_COOLDOWN_MS = 1100;
uint32_t CAST_LOCKOUT_MS = 1800;
const uint32_t IMU_DEBUG_INTERVAL_MS = 1000;
const uint32_t RECENTER_BUTTON_DEBOUNCE_MS = 45;
const uint32_t RECENTER_BUTTON_REPEAT_MS = 800;
const uint16_t RECENTER_GYRO_SAMPLES = 70;
const uint8_t RECENTER_GYRO_SAMPLE_DELAY_MS = 3;
const uint8_t OLED_ROTATION = 2;  // 0 normal, 2 upside down.

// MPU6050 yaw is gyro integration only, so these values intentionally favor
// demo stability over perfect motion tracking.
const int IMU_ACCEL_X_SIGN = -1;  // Current spellbook mount is rotated 180 degrees.
const int IMU_ACCEL_Y_SIGN = -1;
const int IMU_ACCEL_Z_SIGN = 1;
const int IMU_GYRO_Z_SIGN = -1;
const float GYRO_YAW_DEADBAND_DPS = 4.5f;
const float GYRO_STILL_DPS = 8.0f;
const float GYRO_BIAS_LEARN_ALPHA = 0.010f;
const float STILL_ACCEL_DELTA_G = 0.085f;
const float YAW_STILL_LINEAR_ACCEL_G = 0.08f;
const float YAW_STILL_GYRO_DPS = 18.0f;
float GESTURE_START_LINEAR_ACCEL_G = 0.30f;
const float GESTURE_END_LINEAR_ACCEL_G = 0.12f;
const uint32_t GESTURE_MIN_MS = 130;
const uint32_t GESTURE_MAX_MS = 850;
const uint32_t GESTURE_END_STILL_MS = 95;
const int FIREBALL_DOWN_SIGN = -1;  // Change to 1 if a down flick reports positive linearAccelZ.
const int SHIELD_UP_SIGN = 1;       // Change to -1 if an up flick reports negative linearAccelZ.
float FIREBALL_DOWN_ACCEL_G = 0.56f;
float FIREBALL_DOWN_BRAKE_G = -0.28f;
float FIREBALL_MIN_PEAK_LINEAR_G = 0.74f;
float SHIELD_UP_ACCEL_G = 0.30f;
float SHIELD_UP_BRAKE_G = 0.08f;
float SHIELD_MIN_PEAK_LINEAR_G = 0.40f;
float SHIELD_MIN_PITCH_DEG = 0.0f;
float SHIELD_MIN_PITCH_DELTA_DEG = 0.0f;
float SPELL_AXIS_DOMINANCE = 1.10f;

uint8_t broadcastMac[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

#if HAS_OLED
Adafruit_SSD1306 display(128, 64, &Wire, -1);
#endif

struct __attribute__((packed)) FieldBeaconPacket {
  uint16_t magic;
  uint8_t packetType;
  uint8_t sourceId;
  uint16_t sequence;
  int16_t xCm;
  int16_t yCm;
  uint32_t uptimeMs;
};

struct __attribute__((packed)) PlayerHelloPacket {
  uint16_t magic;
  uint8_t packetType;
  uint8_t sourceId;
  uint16_t sequence;
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

uint16_t helloSequence = 0;
uint16_t reportSequence = 0;
uint16_t statusSequence = 0;
uint16_t castSequence = 0;
uint32_t lastHelloMs = 0;
uint32_t lastStatusMs = 0;
uint32_t lastOledMs = 0;
uint32_t lastImuUs = 0;
uint32_t lastFireballMs = 0;
uint32_t lastShieldMs = 0;
uint32_t lastCastMs = 0;

bool mpuReady = false;
float gyroXBias = 0.0f;
float gyroYBias = 0.0f;
float gyroZBias = 0.0f;
float yawDeg = 0.0f;
float accelX = 0.0f;
float accelY = 0.0f;
float accelZ = 1.0f;
float gravityX = 0.0f;
float gravityY = 0.0f;
float gravityZ = 1.0f;
float linearAccelX = 0.0f;
float linearAccelY = 0.0f;
float linearAccelZ = 0.0f;
float lastAccelY = 0.0f;
float linearAccelMag = 0.0f;
float gyroXDps = 0.0f;
float gyroYDps = 0.0f;
float gyroZDps = 0.0f;
float tiltFromBootDeg = 0.0f;
bool yawFrozen = false;
bool shieldFaceUpReady = false;
float lastAccelMag = 1.0f;
float shieldAngleAccum = 0.0f;
float shieldRadiusEstimateM = 0.0f;
uint16_t shieldActiveSamples = 0;
uint32_t lastImuDebugMs = 0;

bool gestureActive = false;
uint32_t gestureStartMs = 0;
uint32_t gestureLastMotionMs = 0;
uint32_t gestureLastSampleMs = 0;
uint32_t gestureFireballPeakMs = 0;
uint32_t gestureShieldPeakMs = 0;
uint8_t gestureFirstDirection = 0;  // 1=fireball/down, 2=shield/up
float gestureStartPitchDeg = 0.0f;
float gesturePeakPitchDeg = 0.0f;
float gesturePeakFireball = 0.0f;
float gestureBrakeFireball = 0.0f;
float gesturePeakShield = 0.0f;
float gestureBrakeShield = 0.0f;
float gesturePeakSidePositive = 0.0f;
float gesturePeakSideNegative = 0.0f;
float gesturePeakVerticalPositive = 0.0f;
float gesturePeakVerticalNegative = 0.0f;
float gesturePeakLinear = 0.0f;
float gestureSideEnergy = 0.0f;
float gestureVerticalEnergy = 0.0f;
uint16_t gestureSamples = 0;
bool gestureSawFireballDown = false;
bool gestureSawBrakeAfterFireballDown = false;
bool gestureSawShieldUp = false;
bool gestureSawBrakeAfterShieldUp = false;

uint8_t currentHp = 5;
uint8_t currentMana = 100;
uint8_t currentFlags = FLAG_ALIVE;
uint16_t lastEventSequence = 0;
uint8_t lastEventType = EVENT_NONE;
uint32_t eventFlashUntilMs = 0;
uint32_t onboardBlinkUntilMs = 0;
uint32_t recenterFlashUntilMs = 0;
bool recenterButtonLastRaw = HIGH;
bool recenterButtonPressed = false;
uint32_t recenterButtonChangedMs = 0;
uint32_t lastLocalRecenterMs = 0;
volatile bool radioRecenterRequested = false;
bool hasWorldPosition = false;
float worldX = 0.0f;
float worldY = 0.0f;

bool readMpuRaw(int16_t &ax, int16_t &ay, int16_t &az,
                int16_t &gx, int16_t &gy, int16_t &gz);
void resetGestureClassifier(uint32_t now);

float normalizeDeg(float deg) {
  while (deg >= 180.0f) deg -= 360.0f;
  while (deg < -180.0f) deg += 360.0f;
  return deg;
}

float boardPitchFromFaceUpDeg() {
  float side = sqrtf(accelX * accelX + accelY * accelY);
  return atan2f(side, max(0.001f, fabsf(accelZ))) * 180.0f / PI;
}

float angleFromBootGravityDeg() {
  float currentMag = sqrtf(accelX * accelX + accelY * accelY + accelZ * accelZ);
  float bootMag = sqrtf(gravityX * gravityX + gravityY * gravityY + gravityZ * gravityZ);
  if (currentMag < 0.001f || bootMag < 0.001f) {
    return 0.0f;
  }
  float dot = (accelX * gravityX + accelY * gravityY + accelZ * gravityZ) / (currentMag * bootMag);
  dot = constrain(dot, -1.0f, 1.0f);
  return acosf(dot) * 180.0f / PI;
}

float estimateDistanceMeters(int rssiDbm) {
  if (rssiDbm >= 0) {
    return NAN;
  }
  return powf(10.0f, (RSSI_AT_1_METER - (float)rssiDbm) / (10.0f * PATH_LOSS_N));
}

int16_t yawDeg10() {
  return (int16_t)roundf(normalizeDeg(yawDeg) * 10.0f);
}

uint8_t playerFlags() {
  uint8_t flags = currentFlags;
  if (currentHp > 0) {
    flags |= FLAG_ALIVE;
  } else {
    flags &= ~FLAG_ALIVE;
  }
  return flags;
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

void printAnchor(uint8_t id, float xMeters, float yMeters) {
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

void printStatus(uint16_t sequence) {
  Serial.print("STATUS,");
  Serial.print(PLAYER_ID);
  Serial.print(",");
  Serial.print(normalizeDeg(yawDeg), 1);
  Serial.print(",");
  Serial.print(playerFlags());
  Serial.print(",");
  Serial.print(sequence);
  Serial.print(",");
  Serial.println(millis());
}

void printCast(uint8_t spellType, uint8_t confidence, uint16_t sequence) {
  Serial.print("CAST,");
  Serial.print(PLAYER_ID);
  Serial.print(",");
  Serial.print(spellType);
  Serial.print(",");
  Serial.print(normalizeDeg(yawDeg), 1);
  Serial.print(",");
  Serial.print(confidence);
  Serial.print(",");
  Serial.print(sequence);
  Serial.print(",");
  Serial.println(millis());
}

void updateHealthLeds() {
  for (int i = 0; i < 5; i++) {
    digitalWrite(HEALTH_LED_PINS[i], i < currentHp ? HIGH : LOW);
  }
}

void setEffectLeds(uint8_t mask) {
  for (int i = 0; i < 4; i++) {
    digitalWrite(EFFECT_LED_PINS[i], (mask & (1 << i)) ? HIGH : LOW);
  }
}

void triggerOnboardBlink(uint16_t durationMs) {
  onboardBlinkUntilMs = millis() + durationMs;
}

void showRecenterOled(const char *status) {
#if HAS_OLED
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.println("RECENTER");
  display.setCursor(0, 18);
  display.println("Hold still");
  display.setCursor(0, 38);
  display.println(status);
  display.display();
#endif
}

bool sampleStillGyroBias(const char *source) {
  int32_t gxBiasSum = 0;
  int32_t gyBiasSum = 0;
  int32_t gzBiasSum = 0;
  int16_t minGz = 32767;
  int16_t maxGz = -32768;
  int samples = 0;

  for (uint16_t i = 0; i < RECENTER_GYRO_SAMPLES; i++) {
    int16_t ax, ay, az, gx, gy, gz;
    if (readMpuRaw(ax, ay, az, gx, gy, gz)) {
      gxBiasSum += gx;
      gyBiasSum += gy;
      gzBiasSum += gz;
      minGz = min(minGz, gz);
      maxGz = max(maxGz, gz);
      samples++;
    }
    delay(RECENTER_GYRO_SAMPLE_DELAY_MS);
  }

  if (samples < 20) {
    Serial.print("RECENTER_FAIL,");
    Serial.print(source);
    Serial.print(",samples=");
    Serial.println(samples);
    return false;
  }

  gyroXBias = (float)gxBiasSum / (float)samples;
  gyroYBias = (float)gyBiasSum / (float)samples;
  gyroZBias = (float)gzBiasSum / (float)samples;
  gyroXDps = 0.0f;
  gyroYDps = 0.0f;
  gyroZDps = 0.0f;
  lastImuUs = micros();

  Serial.print("RECENTER_GYRO,");
  Serial.print(source);
  Serial.print(",samples=");
  Serial.print(samples);
  Serial.print(",gzBias=");
  Serial.print(gyroZBias, 2);
  Serial.print(",gzRange=");
  Serial.println(maxGz - minGz);
  return true;
}

void resetYawToNorth(const char *source) {
  showRecenterOled("Sampling gyro...");
  sampleStillGyroBias(source);
  yawDeg = 0.0f;
  resetGestureClassifier(millis());
  recenterFlashUntilMs = millis() + 800;
  triggerOnboardBlink(250);
  Serial.print("LOCAL_RECENTER,");
  Serial.print(source);
  Serial.print(",");
  Serial.println(millis());
}

void updateRecenterButton() {
  uint32_t now = millis();
  bool raw = digitalRead(RECENTER_BUTTON_PIN);

  if (raw != recenterButtonLastRaw) {
    recenterButtonLastRaw = raw;
    recenterButtonChangedMs = now;
  }

  if (now - recenterButtonChangedMs < RECENTER_BUTTON_DEBOUNCE_MS) {
    return;
  }

  bool pressed = raw == LOW;
  if (pressed && !recenterButtonPressed &&
      (lastLocalRecenterMs == 0 || now - lastLocalRecenterMs >= RECENTER_BUTTON_REPEAT_MS)) {
    lastLocalRecenterMs = now;
    resetYawToNorth("BUTTON");
  }
  recenterButtonPressed = pressed;
}

void updateRadioRecenterRequest() {
  if (!radioRecenterRequested) {
    return;
  }
  radioRecenterRequested = false;
  resetYawToNorth("RADIO");
}

void updateOnboardLed() {
  uint32_t now = millis();
  if (now >= onboardBlinkUntilMs) {
    digitalWrite(ONBOARD_LED_PIN, LOW);
    return;
  }
  digitalWrite(ONBOARD_LED_PIN, ((now / 85) % 2) == 0 ? HIGH : LOW);
}

bool eventUsesOnboardBlink(uint8_t eventType) {
  return eventType == EVENT_CAST_FIREBALL ||
         eventType == EVENT_CAST_SHIELD ||
         eventType == EVENT_HIT ||
         eventType == EVENT_BLOCK ||
         eventType == EVENT_DEATH;
}

uint32_t castCooldownRemainingMs() {
  uint32_t now = millis();
  uint32_t readyAt = lastCastMs + CAST_LOCKOUT_MS;
  if (lastCastMs == 0 || now >= readyAt) {
    return 0;
  }
  return readyAt - now;
}

void ledSelfTest() {
  for (int i = 0; i < 5; i++) {
    for (int j = 0; j < 5; j++) {
      digitalWrite(HEALTH_LED_PINS[j], i == j ? HIGH : LOW);
    }
    delay(70);
  }
  for (int i = 0; i < 4; i++) {
    setEffectLeds(1 << i);
    delay(70);
  }
  setEffectLeds(0x0F);
  delay(120);
  setEffectLeds(0);
  updateHealthLeds();
}

void updateEffectLeds() {
  uint32_t now = millis();
  updateOnboardLed();

  if (now < recenterFlashUntilMs) {
    setEffectLeds(((now / 85) % 2) == 0 ? 0x0F : 0x00);
    return;
  }

  if (currentFlags & FLAG_SHIELD) {
    uint8_t phase = (now / 120) % 4;
    setEffectLeds(1 << phase);
    return;
  }

  if (now >= eventFlashUntilMs) {
    setEffectLeds(0);
    return;
  }

  uint32_t phase = (now / 85) % 4;
  if (lastEventType == EVENT_CAST_FIREBALL) {
    setEffectLeds(1 << phase);
  } else if (lastEventType == EVENT_HIT || lastEventType == EVENT_DEATH) {
    setEffectLeds((phase % 2) == 0 ? 0x0F : 0x00);
  } else if (lastEventType == EVENT_BLOCK) {
    setEffectLeds(0x09);
  } else if (lastEventType == EVENT_DENIED) {
    setEffectLeds(0x05);
  } else {
    setEffectLeds(0);
  }
}

void updateOled() {
#if HAS_OLED
  uint32_t cooldownMs = castCooldownRemainingMs();

  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.print("P");
  display.print(PLAYER_ID);
  display.print(" HP ");
  display.print(currentHp);
  display.print("/5");

  display.setCursor(0, 14);
  display.print("Mana ");
  display.print(currentMana);
  display.print("/100");
  display.drawRect(0, 26, 104, 10, SSD1306_WHITE);
  display.fillRect(2, 28, map(currentMana, 0, 100, 0, 100), 6, SSD1306_WHITE);

  if (cooldownMs > 0) {
    int tenths = (cooldownMs + 50) / 100;
    int barWidth = map((int)(CAST_LOCKOUT_MS - cooldownMs), 0, CAST_LOCKOUT_MS, 0, 124);
    display.setTextSize(2);
    display.setCursor(0, 39);
    display.print("CD ");
    display.print(tenths / 10);
    display.print(".");
    display.print(tenths % 10);
    display.print("s");
    display.drawRect(0, 60, 128, 4, SSD1306_WHITE);
    display.fillRect(2, 61, constrain(barWidth, 0, 124), 2, SSD1306_WHITE);
  } else {
    display.setCursor(0, 42);
    if (hasWorldPosition) {
      display.print("XY ");
      display.print(worldX, 1);
      display.print(",");
      display.print(worldY, 1);
    } else {
      display.print("Yaw ");
      display.print(normalizeDeg(yawDeg), 0);
    }

    display.setCursor(0, 54);
    if (currentFlags & FLAG_SHIELD) {
      display.print("SHIELD");
    } else if (millis() < eventFlashUntilMs) {
      if (lastEventType == EVENT_HIT) display.print("HIT");
      else if (lastEventType == EVENT_BLOCK) display.print("BLOCK");
      else if (lastEventType == EVENT_DENIED) display.print("NO MANA");
      else display.print("CAST");
    } else {
      display.print(mpuReady ? "READY" : "NO MPU");
    }
  }
  display.display();
#endif
}

void oledSelfTest() {
#if HAS_OLED
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.println("SPELLBOOK");
  display.setCursor(0, 16);
  display.print("Player ");
  display.println(PLAYER_ID);
  display.setCursor(0, 32);
  display.print("OLED OK");
  display.setCursor(0, 44);
  display.print("MPU ");
  display.print(mpuReady ? "OK" : "MISSING");
  display.setCursor(0, 56);
  display.print("RADIO STARTING");
  display.display();
  delay(700);
#endif
}

void oledBootStatus(const char *radioStatus) {
#if HAS_OLED
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.println("SPELLBOOK");
  display.setCursor(0, 16);
  display.print("Player ");
  display.println(PLAYER_ID);
  display.setCursor(0, 32);
  display.print("MPU ");
  display.print(mpuReady ? "OK" : "MISSING");
  display.setCursor(0, 44);
  display.print("OLED OK");
  display.setCursor(0, 56);
  display.print(radioStatus);
  display.display();
#endif
}

void i2cWrite8(uint8_t addr, uint8_t reg, uint8_t value) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.write(value);
  Wire.endTransmission(true);
}

bool readMpuRaw(int16_t &ax, int16_t &ay, int16_t &az,
                int16_t &gx, int16_t &gy, int16_t &gz) {
  Wire.beginTransmission(MPU6050_ADDR);
  Wire.write(0x3B);
  if (Wire.endTransmission(false) != 0) {
    return false;
  }

  if (Wire.requestFrom(MPU6050_ADDR, 14, true) != 14) {
    return false;
  }

  ax = (Wire.read() << 8) | Wire.read();
  ay = (Wire.read() << 8) | Wire.read();
  az = (Wire.read() << 8) | Wire.read();
  Wire.read();
  Wire.read();
  gx = (Wire.read() << 8) | Wire.read();
  gy = (Wire.read() << 8) | Wire.read();
  gz = (Wire.read() << 8) | Wire.read();
  return true;
}

void setupMpu() {
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setClock(400000);

  i2cWrite8(MPU6050_ADDR, 0x6B, 0x00);
  delay(100);
  i2cWrite8(MPU6050_ADDR, 0x1B, 0x00);
  i2cWrite8(MPU6050_ADDR, 0x1C, 0x00);

  int32_t gxBiasSum = 0;
  int32_t gyBiasSum = 0;
  int32_t gzBiasSum = 0;
  int32_t axSum = 0;
  int32_t aySum = 0;
  int32_t azSum = 0;
  int samples = 0;
  int16_t minGz = 32767;
  int16_t maxGz = -32768;
  for (int i = 0; i < 120; i++) {
    int16_t ax, ay, az, gx, gy, gz;
    if (readMpuRaw(ax, ay, az, gx, gy, gz)) {
      gxBiasSum += gx;
      gyBiasSum += gy;
      gzBiasSum += gz;
      axSum += ax * IMU_ACCEL_X_SIGN;
      aySum += ay * IMU_ACCEL_Y_SIGN;
      azSum += az * IMU_ACCEL_Z_SIGN;
      minGz = min(minGz, gz);
      maxGz = max(maxGz, gz);
      samples++;
    }
    delay(4);
  }

  if (samples > 20) {
    gyroXBias = (float)gxBiasSum / (float)samples;
    gyroYBias = (float)gyBiasSum / (float)samples;
    gyroZBias = (float)gzBiasSum / (float)samples;
    gravityX = ((float)axSum / (float)samples) / 16384.0f;
    gravityY = ((float)aySum / (float)samples) / 16384.0f;
    gravityZ = ((float)azSum / (float)samples) / 16384.0f;
    mpuReady = true;
    lastImuUs = micros();
    Serial.print("IMU_READY samples=");
    Serial.print(samples);
    Serial.print(" gzBias=");
    Serial.print(gyroZBias, 2);
    Serial.print(" gxyBias=");
    Serial.print(gyroXBias, 2);
    Serial.print(",");
    Serial.print(gyroYBias, 2);
    Serial.print(" gzRange=");
    Serial.print(maxGz - minGz);
    Serial.print(" gravity=");
    Serial.print(gravityX, 2);
    Serial.print(",");
    Serial.print(gravityY, 2);
    Serial.print(",");
    Serial.print(gravityZ, 2);
    Serial.println(" raw");
  } else {
    Serial.println("IMU_MISSING check VCC/GND/SDA/SCL/address");
  }
}

void updateImu() {
  int16_t axRaw, ayRaw, azRaw, gxRaw, gyRaw, gzRaw;
  if (!readMpuRaw(axRaw, ayRaw, azRaw, gxRaw, gyRaw, gzRaw)) {
    mpuReady = false;
    return;
  }

  mpuReady = true;
  uint32_t nowUs = micros();
  float dt = (float)(nowUs - lastImuUs) / 1000000.0f;
  lastImuUs = nowUs;
  if (dt <= 0.0f || dt > 0.2f) {
    dt = 0.01f;
  }

  accelX = ((float)axRaw * IMU_ACCEL_X_SIGN) / 16384.0f;
  accelY = ((float)ayRaw * IMU_ACCEL_Y_SIGN) / 16384.0f;
  accelZ = ((float)azRaw * IMU_ACCEL_Z_SIGN) / 16384.0f;
  linearAccelX = accelX - gravityX;
  linearAccelY = accelY - gravityY;
  linearAccelZ = accelZ - gravityZ;
  float accelMag = sqrtf(accelX * accelX + accelY * accelY + accelZ * accelZ);
  linearAccelMag = sqrtf(linearAccelX * linearAccelX +
                         linearAccelY * linearAccelY +
                         linearAccelZ * linearAccelZ);
  gyroXDps = ((float)gxRaw - gyroXBias) / 131.0f;
  gyroYDps = ((float)gyRaw - gyroYBias) / 131.0f;
  gyroZDps = (((float)gzRaw - gyroZBias) * IMU_GYRO_Z_SIGN) / 131.0f;

  bool likelyStill = fabsf(gyroXDps) < GYRO_STILL_DPS &&
                     fabsf(gyroYDps) < GYRO_STILL_DPS &&
                     fabsf(gyroZDps) < GYRO_STILL_DPS &&
                     fabsf(accelMag - 1.0f) < STILL_ACCEL_DELTA_G &&
                     fabsf(accelMag - lastAccelMag) < STILL_ACCEL_DELTA_G;
  if (likelyStill) {
    gyroZBias = gyroZBias * (1.0f - GYRO_BIAS_LEARN_ALPHA) + (float)gzRaw * GYRO_BIAS_LEARN_ALPHA;
    gyroXBias = gyroXBias * (1.0f - GYRO_BIAS_LEARN_ALPHA) + (float)gxRaw * GYRO_BIAS_LEARN_ALPHA;
    gyroYBias = gyroYBias * (1.0f - GYRO_BIAS_LEARN_ALPHA) + (float)gyRaw * GYRO_BIAS_LEARN_ALPHA;
    gyroXDps = 0.0f;
    gyroYDps = 0.0f;
    gyroZDps = 0.0f;
  } else if (fabsf(gyroZDps) < GYRO_YAW_DEADBAND_DPS) {
    gyroZDps = 0.0f;
  }

  bool yawStill = linearAccelMag < YAW_STILL_LINEAR_ACCEL_G &&
                  fabsf(accelMag - 1.0f) < STILL_ACCEL_DELTA_G &&
                  fabsf(accelMag - lastAccelMag) < STILL_ACCEL_DELTA_G &&
                  fabsf(gyroZDps) < YAW_STILL_GYRO_DPS;
  if (yawStill) {
    gyroZBias = gyroZBias * (1.0f - GYRO_BIAS_LEARN_ALPHA) + (float)gzRaw * GYRO_BIAS_LEARN_ALPHA;
    gyroZDps = 0.0f;
  }

  tiltFromBootDeg = angleFromBootGravityDeg();
  yawFrozen = false;
  yawDeg = normalizeDeg(yawDeg + gyroZDps * dt);
  lastAccelMag = accelMag;
}

void sendReport(uint8_t observedId, uint8_t observedType, int rssiDbm,
                float distanceMeters, uint16_t observedSequence,
                int16_t observedXCm, int16_t observedYCm,
                uint32_t observedUptimeMs) {
  if (rssiDbm < -128 || rssiDbm > 127 || isnan(distanceMeters)) {
    return;
  }

  PlayerReportPacket packet;
  packet.magic = PACKET_MAGIC;
  packet.packetType = PACKET_TYPE_PLAYER_REPORT;
  packet.reporterId = PLAYER_ID;
  packet.observedId = observedId;
  packet.observedType = observedType;
  packet.rssiDbm = (int8_t)rssiDbm;
  packet.distanceCm = (uint16_t)constrain((int)roundf(distanceMeters * 100.0f), 1, 65535);
  packet.reportSequence = reportSequence++;
  packet.observedSequence = observedSequence;
  packet.observedXCm = observedXCm;
  packet.observedYCm = observedYCm;
  packet.observedUptimeMs = observedUptimeMs;

  esp_now_send(broadcastMac, (uint8_t *)&packet, sizeof(packet));
}

void sendStatus() {
  PlayerStatusPacket packet;
  packet.magic = PACKET_MAGIC;
  packet.packetType = PACKET_TYPE_PLAYER_STATUS;
  packet.playerId = PLAYER_ID;
  packet.sequence = statusSequence++;
  packet.yawDeg10 = yawDeg10();
  packet.flags = playerFlags();
  packet.uptimeMs = millis();

  esp_now_send(broadcastMac, (uint8_t *)&packet, sizeof(packet));
  printStatus(packet.sequence);
}

void sendCast(uint8_t spellType, uint8_t confidence) {
  SpellCastPacket packet;
  packet.magic = PACKET_MAGIC;
  packet.packetType = PACKET_TYPE_SPELL_CAST;
  packet.playerId = PLAYER_ID;
  packet.spellType = spellType;
  packet.sequence = castSequence++;
  packet.yawDeg10 = yawDeg10();
  packet.confidence = confidence;
  packet.uptimeMs = millis();

  esp_now_send(broadcastMac, (uint8_t *)&packet, sizeof(packet));
  triggerOnboardBlink(450);
  printCast(spellType, confidence, packet.sequence);
}

void resetGestureClassifier(uint32_t now) {
  gestureActive = false;
  gestureStartMs = now;
  gestureLastMotionMs = now;
  gestureLastSampleMs = now;
  gestureFireballPeakMs = 0;
  gestureShieldPeakMs = 0;
  gestureFirstDirection = 0;
  gestureStartPitchDeg = boardPitchFromFaceUpDeg();
  gesturePeakPitchDeg = gestureStartPitchDeg;
  float sideAccel = linearAccelX;
  float fireballAccel = linearAccelZ * FIREBALL_DOWN_SIGN;
  float shieldAccel = linearAccelZ * SHIELD_UP_SIGN;
  gesturePeakFireball = fireballAccel;
  gestureBrakeFireball = fireballAccel;
  gesturePeakShield = shieldAccel;
  gestureBrakeShield = shieldAccel;
  gesturePeakSidePositive = sideAccel;
  gesturePeakSideNegative = sideAccel;
  gesturePeakVerticalPositive = linearAccelZ;
  gesturePeakVerticalNegative = linearAccelZ;
  gesturePeakLinear = linearAccelMag;
  gestureSideEnergy = 0.0f;
  gestureVerticalEnergy = 0.0f;
  gestureSamples = 0;
  gestureSawFireballDown = false;
  gestureSawBrakeAfterFireballDown = false;
  gestureSawShieldUp = false;
  gestureSawBrakeAfterShieldUp = false;
  shieldAngleAccum = 0.0f;
  shieldActiveSamples = 0;
  shieldRadiusEstimateM = 0.0f;
}

void startGestureClassifier(uint32_t now) {
  resetGestureClassifier(now);
  gestureActive = true;
}

void finalizeGestureClassifier(uint32_t now) {
  uint32_t duration = now - gestureStartMs;
  bool cooldownShield = now - lastShieldMs > SHIELD_COOLDOWN_MS;
  bool cooldownFireball = now - lastFireballMs > FIREBALL_COOLDOWN_MS;
  bool sharedCastReady = now - lastCastMs > CAST_LOCKOUT_MS;
  bool enoughTime = duration >= GESTURE_MIN_MS;

  float fireballDominance = gestureVerticalEnergy / max(0.001f, gestureSideEnergy);
  float shieldDominance = gestureVerticalEnergy / max(0.001f, gestureSideEnergy);
  float shieldRequiredDominance = min(SPELL_AXIS_DOMINANCE, 0.75f);
  bool downFlick = gestureSawFireballDown && gestureSawBrakeAfterFireballDown;
  bool upFlick = gestureSawShieldUp && gestureSawBrakeAfterShieldUp;
  bool fireballFirst = gestureFirstDirection == 1 || gestureFireballPeakMs <= gestureShieldPeakMs;
  bool shieldFirst = gestureFirstDirection == 2 || gestureShieldPeakMs < gestureFireballPeakMs;
  bool fireballCandidate = enoughTime &&
                           fireballFirst &&
                           downFlick &&
                           gesturePeakLinear >= FIREBALL_MIN_PEAK_LINEAR_G &&
                           fireballDominance >= SPELL_AXIS_DOMINANCE;
  bool shieldCandidate = enoughTime &&
                         !fireballCandidate &&
                         shieldFirst &&
                         upFlick &&
                         gesturePeakLinear >= SHIELD_MIN_PEAK_LINEAR_G &&
                         shieldDominance >= shieldRequiredDominance;

  if (shieldCandidate && cooldownShield && sharedCastReady) {
    lastShieldMs = now;
    lastCastMs = now;
    uint8_t confidence = (uint8_t)constrain((int)(68 + shieldDominance * 8 + gesturePeakLinear * 8), 72, 96);
    sendCast(SPELL_SHIELD, confidence);
  } else if (fireballCandidate && cooldownFireball && sharedCastReady) {
    uint8_t confidence = (uint8_t)constrain((int)(65 + fireballDominance * 8 + gesturePeakLinear * 8), 70, 95);
    lastFireballMs = now;
    lastCastMs = now;
    sendCast(SPELL_FIREBALL, confidence);
  }

  resetGestureClassifier(now);
}

void cancelGestureClassifier(uint32_t now) {
  resetGestureClassifier(now);
}

void updateGestureClassifier(uint32_t now) {
  float sideAccel = linearAccelX;
  float fireballAccel = linearAccelZ * FIREBALL_DOWN_SIGN;
  float shieldAccel = linearAccelZ * SHIELD_UP_SIGN;
  gestureSamples++;
  gesturePeakLinear = max(gesturePeakLinear, linearAccelMag);
  gesturePeakPitchDeg = max(gesturePeakPitchDeg, boardPitchFromFaceUpDeg());
  gesturePeakFireball = max(gesturePeakFireball, fireballAccel);
  gestureBrakeFireball = min(gestureBrakeFireball, fireballAccel);
  gesturePeakShield = max(gesturePeakShield, shieldAccel);
  gestureBrakeShield = min(gestureBrakeShield, shieldAccel);
  gesturePeakSidePositive = max(gesturePeakSidePositive, sideAccel);
  gesturePeakSideNegative = min(gesturePeakSideNegative, sideAccel);
  gesturePeakVerticalPositive = max(gesturePeakVerticalPositive, linearAccelZ);
  gesturePeakVerticalNegative = min(gesturePeakVerticalNegative, linearAccelZ);
  gestureSideEnergy += fabsf(sideAccel);
  gestureVerticalEnergy += fabsf(linearAccelZ);

  bool fireballStarted = fireballAccel >= FIREBALL_DOWN_ACCEL_G;
  bool shieldStarted = shieldAccel >= SHIELD_UP_ACCEL_G;

  if (gestureFirstDirection == 0 && (fireballStarted || shieldStarted)) {
    float fireballStrength = fireballAccel / max(0.001f, FIREBALL_DOWN_ACCEL_G);
    float shieldStrength = shieldAccel / max(0.001f, SHIELD_UP_ACCEL_G);
    gestureFirstDirection = shieldStrength > fireballStrength ? 2 : 1;
  }
  if (gestureFirstDirection == 2 && fireballStarted &&
      fireballAccel >= FIREBALL_DOWN_ACCEL_G * 1.05f &&
      fireballAccel > shieldAccel * 0.80f) {
    gestureFirstDirection = 1;
  }

  if (fireballStarted) {
    gestureSawFireballDown = true;
    gestureFireballPeakMs = now;
  }
  if (gestureSawFireballDown && now >= gestureFireballPeakMs &&
      fireballAccel <= FIREBALL_DOWN_BRAKE_G) {
    gestureSawBrakeAfterFireballDown = true;
  }
  if (shieldStarted) {
    gestureSawShieldUp = true;
    gestureShieldPeakMs = now;
  }
  if (gestureSawShieldUp && now >= gestureShieldPeakMs &&
      shieldAccel <= SHIELD_UP_BRAKE_G) {
    gestureSawBrakeAfterShieldUp = true;
  }

  shieldAngleAccum = gesturePeakShield;
  shieldActiveSamples = gestureSawBrakeAfterShieldUp ? 1 : 0;
  shieldRadiusEstimateM = gesturePeakPitchDeg;
}

void detectGestures() {
  uint32_t now = millis();

  float forwardJerk = linearAccelY - lastAccelY;
  lastAccelY = linearAccelY;
  (void)forwardJerk;

  bool motionNow = linearAccelMag >= GESTURE_START_LINEAR_ACCEL_G ||
                   fabsf(linearAccelZ) >= FIREBALL_DOWN_ACCEL_G ||
                   fabsf(linearAccelZ) >= SHIELD_UP_ACCEL_G;

  if (!gestureActive) {
    if (motionNow) {
      startGestureClassifier(now);
      updateGestureClassifier(now);
    }
    return;
  }

  bool noDirectionYet = gestureFirstDirection == 0 &&
                        !gestureSawShieldUp &&
                        now - gestureStartMs > 260 &&
                        linearAccelMag < GESTURE_START_LINEAR_ACCEL_G;
  if (noDirectionYet) {
    cancelGestureClassifier(now);
    return;
  }

  if (linearAccelMag >= GESTURE_END_LINEAR_ACCEL_G ||
      fabsf(linearAccelY) >= GESTURE_END_LINEAR_ACCEL_G ||
      fabsf(linearAccelX) >= GESTURE_END_LINEAR_ACCEL_G ||
      fabsf(linearAccelZ) >= GESTURE_END_LINEAR_ACCEL_G) {
    gestureLastMotionMs = now;
  }

  updateGestureClassifier(now);

  bool endedByStillness = now - gestureLastMotionMs >= GESTURE_END_STILL_MS &&
                          now - gestureStartMs >= GESTURE_MIN_MS;
  bool endedByTimeout = now - gestureStartMs >= GESTURE_MAX_MS;
  if (endedByStillness || endedByTimeout) {
    finalizeGestureClassifier(now);
  }
}

void printImuDebug() {
  Serial.print("IMU,");
  Serial.print(mpuReady ? 1 : 0);
  Serial.print(",");
  Serial.print(normalizeDeg(yawDeg), 1);
  Serial.print(",");
  Serial.print(gyroZDps, 2);
  Serial.print(",");
  Serial.print(gyroZBias, 2);
  Serial.print(",");
  Serial.print(gyroXDps, 1);
  Serial.print(",");
  Serial.print(gyroYDps, 1);
  Serial.print(",");
  Serial.print(yawFrozen ? 1 : 0);
  Serial.print(",");
  Serial.print(tiltFromBootDeg, 0);
  Serial.print(",");
  Serial.print(accelX, 2);
  Serial.print(",");
  Serial.print(accelY, 2);
  Serial.print(",");
  Serial.print(accelZ, 2);
  Serial.print(",");
  Serial.print(linearAccelMag, 2);
  Serial.print(",");
  Serial.print(shieldAngleAccum, 0);
  Serial.print(",");
  Serial.print(shieldActiveSamples);
  Serial.print(",");
  Serial.print(shieldRadiusEstimateM, 3);
  Serial.print(",");
  Serial.print(gestureActive ? 1 : 0);
  Serial.print(",");
  Serial.print(gesturePeakFireball, 2);
  Serial.print(",");
  Serial.print(gestureBrakeFireball, 2);
  Serial.print(",");
  Serial.print(gesturePeakShield, 2);
  Serial.print(",");
  Serial.print(gestureBrakeShield, 2);
  Serial.print(",");
  Serial.print(gesturePeakVerticalPositive, 2);
  Serial.print(",");
  Serial.print(gesturePeakVerticalNegative, 2);
  Serial.print(",");
  Serial.print(gestureVerticalEnergy / max(0.001f, gestureSideEnergy), 2);
  Serial.print(",");
  Serial.print(gestureVerticalEnergy / max(0.001f, gestureSideEnergy), 2);
  Serial.print(",");
  Serial.print(gestureSawBrakeAfterFireballDown ? 1 : 0);
  Serial.print(",");
  Serial.print(gestureSawBrakeAfterShieldUp ? 1 : 0);
  Serial.print(",");
  Serial.print(gesturePeakLinear, 2);
  Serial.print(",");
  Serial.print(gestureFirstDirection);
  Serial.print(",");
  Serial.print(gestureStartPitchDeg, 0);
  Serial.print(",");
  Serial.print(gesturePeakPitchDeg, 0);
  Serial.print(",");
  Serial.println(digitalRead(RECENTER_BUTTON_PIN) == LOW ? 1 : 0);
}

void handleFieldBeacon(const uint8_t *data, int len, int rssiDbm) {
  if (len != sizeof(FieldBeaconPacket) || rssiDbm >= 0) {
    return;
  }

  FieldBeaconPacket packet;
  memcpy(&packet, data, sizeof(packet));

  if (packet.magic != PACKET_MAGIC || packet.packetType != PACKET_TYPE_FIELD_BEACON) {
    return;
  }

  FieldState *field = fieldFor(packet.sourceId);
  if (field == nullptr) {
    return;
  }

  field->xMeters = (float)packet.xCm / 100.0f;
  field->yMeters = (float)packet.yCm / 100.0f;

  uint32_t now = millis();
  if (now - field->lastAnnounceMs >= ANCHOR_REANNOUNCE_MS) {
    field->lastAnnounceMs = now;
    printAnchor(field->id, field->xMeters, field->yMeters);
  }

  float distanceMeters = estimateDistanceMeters(rssiDbm);
  printObservation(PLAYER_ID, packet.sourceId, rssiDbm, distanceMeters,
                   packet.sequence, packet.uptimeMs);

  sendReport(packet.sourceId, PACKET_TYPE_FIELD_BEACON, rssiDbm, distanceMeters,
             packet.sequence, packet.xCm, packet.yCm, packet.uptimeMs);
}

void handlePlayerHello(const uint8_t *data, int len, int rssiDbm) {
  if (len != sizeof(PlayerHelloPacket) || rssiDbm >= 0) {
    return;
  }

  PlayerHelloPacket packet;
  memcpy(&packet, data, sizeof(packet));

  if (packet.magic != PACKET_MAGIC ||
      packet.packetType != PACKET_TYPE_PLAYER_HELLO ||
      packet.sourceId == PLAYER_ID) {
    return;
  }

  float distanceMeters = estimateDistanceMeters(rssiDbm);
  printObservation(PLAYER_ID, packet.sourceId, rssiDbm, distanceMeters,
                   packet.sequence, packet.uptimeMs);

  sendReport(packet.sourceId, PACKET_TYPE_PLAYER_HELLO, rssiDbm, distanceMeters,
             packet.sequence, 0, 0, packet.uptimeMs);
}

void applyGameState(const GameStatePacket &packet) {
  if (packet.targetId != PLAYER_ID && packet.targetId != 255) {
    return;
  }

  currentHp = constrain(packet.hp, 0, 5);
  currentMana = constrain(packet.mana, 0, 100);
  currentFlags = packet.flags;
  if (packet.eventSequence != lastEventSequence || packet.eventType != lastEventType) {
    lastEventSequence = packet.eventSequence;
    lastEventType = packet.eventType;
    if (lastEventType == EVENT_CAST_FIREBALL || lastEventType == EVENT_CAST_SHIELD) {
      lastCastMs = millis();
    }
    if (eventUsesOnboardBlink(lastEventType)) {
      triggerOnboardBlink(700);
    }
    eventFlashUntilMs = millis() + 700;
  }
  updateHealthLeds();
  updateOled();
}

void applyRecenter(const RecenterPacket &packet) {
  if (packet.targetId == PLAYER_ID || packet.targetId == 255) {
    radioRecenterRequested = true;
  }
}

void applyTuneValues(uint8_t targetId,
                     float gestureStart,
                     float fireballDown,
                     float fireballBrake,
                     float fireballPeak,
                     float shieldUp,
                     float shieldBrake,
                     float shieldPeak,
                     float shieldPitch,
                     float shieldPitchDelta,
                     float axisDominance,
                     uint32_t castLockoutMs) {
  if (targetId != PLAYER_ID && targetId != 255) {
    return;
  }

  GESTURE_START_LINEAR_ACCEL_G = constrain(gestureStart, 0.05f, 2.50f);
  FIREBALL_DOWN_ACCEL_G = constrain(fireballDown, 0.05f, 3.00f);
  FIREBALL_DOWN_BRAKE_G = constrain(fireballBrake, -3.00f, -0.02f);
  FIREBALL_MIN_PEAK_LINEAR_G = constrain(fireballPeak, 0.05f, 4.00f);
  SHIELD_UP_ACCEL_G = constrain(shieldUp, 0.05f, 3.00f);
  SHIELD_UP_BRAKE_G = constrain(shieldBrake, -3.00f, 0.50f);
  SHIELD_MIN_PEAK_LINEAR_G = constrain(shieldPeak, 0.05f, 4.00f);
  SHIELD_MIN_PITCH_DEG = constrain(shieldPitch, 5.0f, 89.0f);
  SHIELD_MIN_PITCH_DELTA_DEG = constrain(shieldPitchDelta, 0.0f, 89.0f);
  SPELL_AXIS_DOMINANCE = constrain(axisDominance, 0.2f, 8.0f);
  CAST_LOCKOUT_MS = constrain((int)castLockoutMs, 200, 5000);

  Serial.print("TUNED,");
  Serial.print(PLAYER_ID);
  Serial.print(",");
  Serial.print(GESTURE_START_LINEAR_ACCEL_G, 2);
  Serial.print(",");
  Serial.print(FIREBALL_DOWN_ACCEL_G, 2);
  Serial.print(",");
  Serial.print(SHIELD_UP_ACCEL_G, 2);
  Serial.print(",");
  Serial.print(SPELL_AXIS_DOMINANCE, 2);
  Serial.print(",");
  Serial.println(CAST_LOCKOUT_MS);
}

void applyTune(const TunePacket &packet) {
  applyTuneValues(
    packet.targetId,
    (float)packet.gestureStart100 / 100.0f,
    (float)packet.fireballDown100 / 100.0f,
    (float)packet.fireballBrake100 / 100.0f,
    (float)packet.fireballPeak100 / 100.0f,
    (float)packet.shieldUp100 / 100.0f,
    (float)packet.shieldBrake100 / 100.0f,
    (float)packet.shieldPeak100 / 100.0f,
    (float)packet.shieldPitch100 / 100.0f,
    (float)packet.shieldPitchDelta100 / 100.0f,
    (float)packet.axisDominance100 / 100.0f,
    packet.castLockoutMs
  );
}

void applyWorldState(const WorldStatePacket &packet) {
  for (int i = 0; i < packet.count && i < 4; i++) {
    if (packet.players[i].id == PLAYER_ID) {
      worldX = (float)packet.players[i].xCm / 100.0f;
      worldY = (float)packet.players[i].yCm / 100.0f;
      hasWorldPosition = true;
      return;
    }
  }
}

void handleServerLine(String line) {
  line.trim();
  if (line.startsWith("RECENTER")) {
    int comma = line.indexOf(',');
    int target = comma >= 0 ? line.substring(comma + 1).toInt() : PLAYER_ID;
    if (target == PLAYER_ID || target == 255) {
      resetYawToNorth("SERIAL");
    }
  } else if (line.startsWith("STATE")) {
    int values[6] = {PLAYER_ID, currentHp, currentMana, currentFlags, 0, EVENT_NONE};
    int field = 0;
    int start = 6;
    while (field < 6 && start < line.length()) {
      int comma = line.indexOf(',', start);
      String part = comma >= 0 ? line.substring(start, comma) : line.substring(start);
      values[field++] = part.toInt();
      if (comma < 0) break;
      start = comma + 1;
    }
    if (values[0] == PLAYER_ID || values[0] == 255) {
      currentHp = constrain(values[1], 0, 5);
      currentMana = constrain(values[2], 0, 100);
      currentFlags = values[3];
      bool eventChanged = values[4] != lastEventSequence || values[5] != lastEventType;
      lastEventSequence = values[4];
      lastEventType = values[5];
      if (eventChanged && (lastEventType == EVENT_CAST_FIREBALL || lastEventType == EVENT_CAST_SHIELD)) {
        lastCastMs = millis();
      }
      if (eventChanged && eventUsesOnboardBlink(lastEventType)) {
        triggerOnboardBlink(700);
      }
      eventFlashUntilMs = millis() + 700;
      updateHealthLeds();
      updateOled();
    }
  } else if (line.startsWith("TUNE")) {
    float values[11] = {
      255.0f,
      GESTURE_START_LINEAR_ACCEL_G,
      FIREBALL_DOWN_ACCEL_G,
      FIREBALL_DOWN_BRAKE_G,
      FIREBALL_MIN_PEAK_LINEAR_G,
      SHIELD_UP_ACCEL_G,
      SHIELD_UP_BRAKE_G,
      SHIELD_MIN_PEAK_LINEAR_G,
      SHIELD_MIN_PITCH_DEG,
      SHIELD_MIN_PITCH_DELTA_DEG,
      SPELL_AXIS_DOMINANCE,
    };
    int field = 0;
    int start = 5;
    while (field < 11 && start < line.length()) {
      int comma = line.indexOf(',', start);
      String part = comma >= 0 ? line.substring(start, comma) : line.substring(start);
      values[field++] = part.toFloat();
      if (comma < 0) break;
      start = comma + 1;
    }
    uint32_t lockout = CAST_LOCKOUT_MS;
    if (start < line.length()) {
      lockout = (uint32_t)line.substring(start).toInt();
    }
    applyTuneValues(
      (uint8_t)values[0], values[1], values[2], values[3], values[4],
      values[5], values[6], values[7], values[8], values[9], values[10], lockout
    );
  }
}

void readSerialCommands() {
  static String line = "";
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      handleServerLine(line);
      line = "";
    } else if (c != '\r' && line.length() < 260) {
      line += c;
    }
  }
}

#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
void onDataRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  int rssiDbm = 127;
  if (info != nullptr && info->rx_ctrl != nullptr) {
    rssiDbm = info->rx_ctrl->rssi;
  }

  if (len < 3 ||
      data[0] != (uint8_t)(PACKET_MAGIC & 0xFF) ||
      data[1] != (uint8_t)(PACKET_MAGIC >> 8)) {
    return;
  }

  if (data[2] == PACKET_TYPE_FIELD_BEACON) {
    handleFieldBeacon(data, len, rssiDbm);
  } else if (data[2] == PACKET_TYPE_PLAYER_HELLO) {
    handlePlayerHello(data, len, rssiDbm);
  } else if (data[2] == PACKET_TYPE_GAME_STATE && len == sizeof(GameStatePacket)) {
    GameStatePacket packet;
    memcpy(&packet, data, sizeof(packet));
    applyGameState(packet);
  } else if (data[2] == PACKET_TYPE_RECENTER && len == sizeof(RecenterPacket)) {
    RecenterPacket packet;
    memcpy(&packet, data, sizeof(packet));
    applyRecenter(packet);
  } else if (data[2] == PACKET_TYPE_TUNE && len == sizeof(TunePacket)) {
    TunePacket packet;
    memcpy(&packet, data, sizeof(packet));
    applyTune(packet);
  } else if (data[2] == PACKET_TYPE_WORLD_STATE && len == sizeof(WorldStatePacket)) {
    WorldStatePacket packet;
    memcpy(&packet, data, sizeof(packet));
    applyWorldState(packet);
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

  if (data[2] == PACKET_TYPE_GAME_STATE && len == sizeof(GameStatePacket)) {
    GameStatePacket packet;
    memcpy(&packet, data, sizeof(packet));
    applyGameState(packet);
  } else if (data[2] == PACKET_TYPE_RECENTER && len == sizeof(RecenterPacket)) {
    RecenterPacket packet;
    memcpy(&packet, data, sizeof(packet));
    applyRecenter(packet);
  } else if (data[2] == PACKET_TYPE_TUNE && len == sizeof(TunePacket)) {
    TunePacket packet;
    memcpy(&packet, data, sizeof(packet));
    applyTune(packet);
  } else if (data[2] == PACKET_TYPE_WORLD_STATE && len == sizeof(WorldStatePacket)) {
    WorldStatePacket packet;
    memcpy(&packet, data, sizeof(packet));
    applyWorldState(packet);
  } else {
    Serial.println("WARN,RSSI_UNAVAILABLE,update ESP32 board package in Arduino IDE");
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

void sendHello() {
  PlayerHelloPacket packet;
  packet.magic = PACKET_MAGIC;
  packet.packetType = PACKET_TYPE_PLAYER_HELLO;
  packet.sourceId = PLAYER_ID;
  packet.sequence = helloSequence++;
  packet.uptimeMs = millis();

  esp_now_send(broadcastMac, (uint8_t *)&packet, sizeof(packet));
}

void setupDisplayAndLeds() {
  pinMode(ONBOARD_LED_PIN, OUTPUT);
  digitalWrite(ONBOARD_LED_PIN, LOW);
  pinMode(RECENTER_BUTTON_PIN, INPUT_PULLUP);
  Serial.print("RECENTER_BUTTON GPIO");
  Serial.print(RECENTER_BUTTON_PIN);
  Serial.print("=");
  Serial.println(digitalRead(RECENTER_BUTTON_PIN) == LOW ? "PRESSED" : "OPEN");
  for (int i = 0; i < 5; i++) {
    pinMode(HEALTH_LED_PINS[i], OUTPUT);
  }
  for (int i = 0; i < 4; i++) {
    pinMode(EFFECT_LED_PINS[i], OUTPUT);
  }
  updateHealthLeds();
  setEffectLeds(0);

#if HAS_OLED
  if (display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {
    display.setRotation(OLED_ROTATION);
    display.clearDisplay();
    display.display();
  } else {
    Serial.println("OLED_MISSING check VCC/GND/SDA/SCL/address/libs");
  }
#else
  Serial.println("OLED_DISABLED install Adafruit SSD1306 and Adafruit GFX Library");
#endif

  ledSelfTest();
  oledSelfTest();
}

void setup() {
  Serial.begin(115200);
  delay(500);

  Serial.println();
  Serial.println("ESP-NOW Spell Arena player");
  Serial.print("PLAYER_ID=");
  Serial.println(PLAYER_ID);
  Serial.print("WIFI_CHANNEL=");
  Serial.println(WIFI_CHANNEL);

  setupMpu();
  setupDisplayAndLeds();
  setupEspNow();
  oledBootStatus("RADIO OK");
  delay(450);
  triggerOnboardBlink(500);

  Serial.print("WiFi MAC=");
  Serial.println(WiFi.macAddress());
  Serial.println("Listening, reporting, and detecting spells.");
}

void loop() {
  uint32_t now = millis();

  readSerialCommands();
  updateImu();
  updateRecenterButton();
  updateRadioRecenterRequest();
  if (mpuReady) {
    detectGestures();
  }

  if (now - lastHelloMs >= HELLO_INTERVAL_MS) {
    lastHelloMs = now;
    sendHello();
  }

  if (now - lastStatusMs >= STATUS_INTERVAL_MS) {
    lastStatusMs = now;
    sendStatus();
  }

  if (now - lastOledMs >= OLED_INTERVAL_MS) {
    lastOledMs = now;
    updateOled();
  }

  if (now - lastImuDebugMs >= IMU_DEBUG_INTERVAL_MS) {
    lastImuDebugMs = now;
    printImuDebug();
  }

  updateEffectLeds();

  delay(5);
}
