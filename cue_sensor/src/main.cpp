/*
 * Gyroskop im Billard-Queue
 * --------------------------
 * ESP32 + MPU6050 (Beschleunigung + Drehrate). Ein Madgwick-Filter berechnet daraus die
 * Lage des Queues; gesendet wird die Richtung der Queue-Achse in der Waagerechten (yaw)
 * und die Neigung (pitch), ca. 30x pro Sekunde als JSON per UDP:
 *
 *   {"id":"koe1","seq":123,"yaw":37.52,"pitch":-4.10,"still":0,"bat":3.91}
 *
 * yaw: Grad, gegen den Uhrzeigersinn positiv (von oben gesehen). Der absolute Wert ist
 * beliebig - der Pi zieht beim "Queue kalibrieren" den Wert an der Bande ab.
 *
 * Drift: Ohne Magnetometer wandert yaw langsam (typisch < 1 Grad pro Minute, wenn der
 * Nullpunkt stimmt). Deshalb wird der Nullpunkt des Gyroskops beim Start und immer
 * wenn der Queue ruhig liegt automatisch nachgefuehrt. Bei Bedarf neu kalibrieren.
 *
 * Verdrahtung MPU6050:  VCC -> 3V3, GND -> GND, SDA -> SDA_PIN, SCL -> SCL_PIN
 */

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <Wire.h>
#include <math.h>

#include "config.h"

static const uint8_t MPU = 0x68;
static const float GYRO_LSB = 65.5f;      // +-500 Grad/s
static const float ACC_LSB = 16384.0f;    // +-2 g
static const float DEG2RAD = 0.017453292f;

// Madgwick-Filter (6 Achsen), Quaternion Sensor -> Welt
static float q0 = 1, q1 = 0, q2 = 0, q3 = 0;
static float beta = 0.08f;

// Gyroskop-Nullpunkt (Grad/s)
static float bias[3] = {0, 0, 0};
static uint32_t stillSince = 0;
static bool still = false;

WiFiUDP udp;
IPAddress target;
static uint32_t seq = 0;

static void writeReg(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(MPU);
  Wire.write(reg);
  Wire.write(val);
  Wire.endTransmission();
}

static bool readRaw(int16_t* a, int16_t* g) {
  Wire.beginTransmission(MPU);
  Wire.write(0x3B);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)MPU, 14) != 14) return false;
  uint8_t b[14];
  for (int i = 0; i < 14; i++) b[i] = Wire.read();
  for (int i = 0; i < 3; i++) a[i] = (int16_t)((b[2 * i] << 8) | b[2 * i + 1]);
  for (int i = 0; i < 3; i++) g[i] = (int16_t)((b[8 + 2 * i] << 8) | b[9 + 2 * i]);
  return true;
}

static bool mpuInit() {
  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(400000);
  Wire.beginTransmission(MPU);
  if (Wire.endTransmission() != 0) return false;
  writeReg(0x6B, 0x01);   // aufwecken, Takt vom Gyroskop
  delay(50);
  writeReg(0x19, 4);      // 1 kHz / (1+4) = 200 Hz
  writeReg(0x1A, 0x03);   // Tiefpass 44 Hz
  writeReg(0x1B, 0x08);   // Gyroskop +-500 Grad/s
  writeReg(0x1C, 0x00);   // Beschleunigung +-2 g
  return true;
}

// Nullpunkt beim Start: Queue 2 Sekunden ruhig hinlegen
static void calibrateBias() {
  Serial.println("Gyroskop-Nullpunkt: Queue ruhig liegen lassen ...");
  double sum[3] = {0, 0, 0};
  int n = 0;
  int16_t a[3], g[3];
  uint32_t t0 = millis();
  while (millis() - t0 < 2000) {
    if (readRaw(a, g)) {
      for (int i = 0; i < 3; i++) sum[i] += g[i] / GYRO_LSB;
      n++;
    }
    delay(5);
  }
  if (n > 0)
    for (int i = 0; i < 3; i++) bias[i] = sum[i] / n;
  Serial.printf("Nullpunkt: %.3f %.3f %.3f Grad/s\n", bias[0], bias[1], bias[2]);
}

static inline float invSqrt(float x) { return 1.0f / sqrtf(x); }

// Standard-Madgwick-Update (IMU-Variante). g in rad/s, a beliebige Einheit.
static void madgwick(float gx, float gy, float gz, float ax, float ay, float az, float dt) {
  float qDot1 = 0.5f * (-q1 * gx - q2 * gy - q3 * gz);
  float qDot2 = 0.5f * (q0 * gx + q2 * gz - q3 * gy);
  float qDot3 = 0.5f * (q0 * gy - q1 * gz + q3 * gx);
  float qDot4 = 0.5f * (q0 * gz + q1 * gy - q2 * gx);
  if (!(ax == 0.0f && ay == 0.0f && az == 0.0f)) {
    float r = invSqrt(ax * ax + ay * ay + az * az);
    ax *= r; ay *= r; az *= r;
    float _2q0 = 2 * q0, _2q1 = 2 * q1, _2q2 = 2 * q2, _2q3 = 2 * q3;
    float _4q0 = 4 * q0, _4q1 = 4 * q1, _4q2 = 4 * q2, _8q1 = 8 * q1, _8q2 = 8 * q2;
    float q0q0 = q0 * q0, q1q1 = q1 * q1, q2q2 = q2 * q2, q3q3 = q3 * q3;
    float s0 = _4q0 * q2q2 + _2q2 * ax + _4q0 * q1q1 - _2q1 * ay;
    float s1 = _4q1 * q3q3 - _2q3 * ax + 4.0f * q0q0 * q1 - _2q0 * ay - _4q1 + _8q1 * q1q1 + _8q1 * q2q2 + _4q1 * az;
    float s2 = 4.0f * q0q0 * q2 + _2q0 * ax + _4q2 * q3q3 - _2q3 * ay - _4q2 + _8q2 * q1q1 + _8q2 * q2q2 + _4q2 * az;
    float s3 = 4.0f * q1q1 * q3 - _2q1 * ax + 4.0f * q2q2 * q3 - _2q2 * ay;
    r = invSqrt(s0 * s0 + s1 * s1 + s2 * s2 + s3 * s3);
    s0 *= r; s1 *= r; s2 *= r; s3 *= r;
    qDot1 -= beta * s0; qDot2 -= beta * s1; qDot3 -= beta * s2; qDot4 -= beta * s3;
  }
  q0 += qDot1 * dt; q1 += qDot2 * dt; q2 += qDot3 * dt; q3 += qDot4 * dt;
  float r = invSqrt(q0 * q0 + q1 * q1 + q2 * q2 + q3 * q3);
  q0 *= r; q1 *= r; q2 *= r; q3 *= r;
}

// Richtung der Queue-Achse (Sensor-X) in der Welt: Waagerechte (yaw) und Neigung (pitch)
static void cueAngles(float* yaw, float* pitch) {
  float xx = 1 - 2 * (q2 * q2 + q3 * q3);
  float xy = 2 * (q1 * q2 + q0 * q3);
  float xz = 2 * (q1 * q3 - q0 * q2);
  if (AXIS_SIGN_X < 0) { xx = -xx; xy = -xy; xz = -xz; }
  *yaw = atan2f(xy, xx) / DEG2RAD;
  *pitch = asinf(fmaxf(-1.0f, fminf(1.0f, xz))) / DEG2RAD;
}

static void wifiConnect() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);          // geringe Verzoegerung wichtiger als Akku
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("WLAN %s ", WIFI_SSID);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) {
    delay(250);
    Serial.print(".");
  }
  Serial.println(WiFi.status() == WL_CONNECTED ? " verbunden, IP " + WiFi.localIP().toString() : " FEHLER");
  if (strlen(TARGET_IP) > 0) target.fromString(TARGET_IP);
  else target = WiFi.broadcastIP();
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\nBillard-Queue: Gyroskop");
  if (!mpuInit()) {
    Serial.println("MPU6050 nicht gefunden - Verdrahtung pruefen!");
  }
  calibrateBias();
  // Startlage aus der Schwerkraft, damit der Filter nicht erst einschwingen muss
  beta = 2.0f;
  int16_t a[3], g[3];
  for (int i = 0; i < 200; i++) {
    if (readRaw(a, g)) madgwick(0, 0, 0, a[0], a[1], a[2], 1.0f / SAMPLE_HZ);
    delay(2);
  }
  beta = 0.08f;
  wifiConnect();
  udp.begin(UDP_PORT);
}

void loop() {
  static uint32_t lastSample = micros(), lastSend = 0, lastWifiCheck = 0;
  uint32_t now = micros();
  if (now - lastSample < 1000000UL / SAMPLE_HZ) return;
  float dt = (now - lastSample) / 1e6f;
  lastSample = now;

  int16_t a[3], g[3];
  if (!readRaw(a, g)) return;
  float gd[3];
  for (int i = 0; i < 3; i++) gd[i] = g[i] / GYRO_LSB - bias[i];

  // Ruhe-Erkennung: kaum Drehung und Beschleunigung ~1 g -> Nullpunkt langsam nachfuehren
  float acc = sqrtf((float)a[0] * a[0] + (float)a[1] * a[1] + (float)a[2] * a[2]) / ACC_LSB;
  float rate = sqrtf(gd[0] * gd[0] + gd[1] * gd[1] + gd[2] * gd[2]);
  bool calm = rate < 1.5f && fabsf(acc - 1.0f) < 0.05f;
  uint32_t ms = millis();
  if (!calm) stillSince = ms;
  still = calm && ms - stillSince > 1000;
  if (still) {
    for (int i = 0; i < 3; i++) bias[i] += 0.002f * gd[i];
    for (int i = 0; i < 3; i++) gd[i] = 0;        // liegt still: keine Drift aufintegrieren
  }

  madgwick(gd[0] * DEG2RAD, gd[1] * DEG2RAD, gd[2] * DEG2RAD, a[0], a[1], a[2], dt);

  if (ms - lastSend >= 1000 / SEND_HZ) {
    lastSend = ms;
    float yaw, pitch;
    cueAngles(&yaw, &pitch);
    float bat = 0;
#if BAT_PIN >= 0
    bat = analogReadMilliVolts(BAT_PIN) / 1000.0f * BAT_DIVIDER;
#endif
    char buf[160];
    int n = snprintf(buf, sizeof(buf),
                     "{\"id\":\"%s\",\"seq\":%lu,\"yaw\":%.2f,\"pitch\":%.2f,\"still\":%d,\"bat\":%.2f}",
                     DEVICE_ID, (unsigned long)seq++, yaw, pitch, still ? 1 : 0, bat);
    if (WiFi.status() == WL_CONNECTED) {
      udp.beginPacket(target, UDP_PORT);
      udp.write((const uint8_t*)buf, n);
      udp.endPacket();
    }
  }
  if (ms - lastWifiCheck > 5000) {
    lastWifiCheck = ms;
    if (WiFi.status() != WL_CONNECTED) wifiConnect();
  }
}
