/*
 * Gyroskop im Billard-Queue
 * --------------------------
 * ESP32 + MPU6050 (Beschleunigung + Drehrate). Ein Madgwick-Filter berechnet die Lage des
 * Queues: Richtung der Queue-Achse in der Waagerechten (yaw) und Neigung (pitch).
 *
 * Einrichtung (Captive Portal, wie bei der Kamera):
 *   1. Ohne gespeichertes WLAN (oder wenn es nicht erreichbar ist) oeffnet der Queue den
 *      Hotspot "Billard-Queue-XXXX". Mit dem Handy verbinden - die Einrichtungsseite oeffnet
 *      sich von selbst (sonst http://192.168.4.1).
 *   2. WLAN waehlen, Passwort eingeben, "Weiter".
 *   3. Der Queue zeigt seinen CASTCODE (6 Zeichen). Diesen im Admin-Portal des Pi unter
 *      "Queue mit Gyroskop" eintragen.
 *   4. Haken setzen "Castcode ist eingetragen" -> "Verbinden": der Queue verbindet sich mit dem WLAN.
 *   Neu einrichten: Queue beim Einschalten SENKRECHT halten (Spitze nach oben) oder auf
 *   seiner Statusseite "WLAN neu einrichten".
 *
 * Im WLAN:
 *   GET  /api/lage   Lage als JSON (Kopfzeile X-Castcode oder ?code=... noetig)
 *        {"castcode":"K7P2QM","seq":123,"yaw":37.52,"pitch":-4.1,"still":0,"bat":3.91,"ms":123456}
 *   GET  /api/info   Geraet + Firmware (ohne Castcode)
 *   UDP  5005        Suche: {"suche":"K7P2QM"} -> Antwort {"castcode":"K7P2QM","ip":"...","port":80}
 *   mDNS             queue-k7p2qm.local
 *
 * yaw: Grad, gegen den Uhrzeigersinn positiv (von oben gesehen). Der absolute Wert ist beliebig -
 * der Pi zieht beim "Queue kalibrieren" den Wert an der Bande ab.
 *
 * Verdrahtung MPU6050:  VCC -> 3V3, GND -> GND, SDA -> SDA_PIN, SCL -> SCL_PIN
 */

#include <Arduino.h>
#include <DNSServer.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <WebServer.h>
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


// ======================================================== Netzwerk
static Preferences prefs;
static String ssid, pass, castcode;
static String pendingSsid, pendingPass;     // Schritt 1 eingegeben, wartet auf den Haken
static bool portal = false;
static WebServer server(API_PORT);
static DNSServer dns;
static WiFiUDP udp;
static String scanOptions;
static uint32_t restartAt = 0, portalSince = 0;
static uint32_t seq = 0;
static float curYaw = 0, curPitch = 0, curBat = 0;

static String makeCastcode() {
  const char* abc = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";   // ohne 0/O und 1/I
  String c;
  for (int i = 0; i < CASTCODE_LEN; i++) c += abc[random(32)];   // ESP32: random() nutzt den Hardware-Zufall
  return c;
}

static void loadSettings() {
  prefs.begin("queue", false);
  ssid = prefs.getString("ssid", "");
  pass = prefs.getString("pass", "");
  castcode = prefs.getString("code", "");
  if (castcode.length() != CASTCODE_LEN) {        // einmalig erzeugen, bleibt fuer immer gleich
    castcode = makeCastcode();
    prefs.putString("code", castcode);
  }
  prefs.end();
}

static void saveWifi(const String& s, const String& p) {
  prefs.begin("queue", false);
  prefs.putString("ssid", s);
  prefs.putString("pass", p);
  prefs.end();
}

static String esc(const String& s) {
  String o;
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    if (c == '<') o += "&lt;";
    else if (c == '>') o += "&gt;";
    else if (c == '&') o += "&amp;";
    else if (c == '"') o += "&quot;";
    else o += c;
  }
  return o;
}

static const char PAGE_HEAD[] = R"HTML(<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Billard-Queue</title>
<style>body{font-family:system-ui,sans-serif;max-width:460px;margin:20px auto;padding:0 16px;color:#222;background:#f6f4ee}
h1{font-size:22px}input,button,select{font-size:17px;margin:4px 0 14px;width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #bbb}
button{background:#0E3F31;color:#fff;border:0;font-weight:700}button:disabled{opacity:.4}
label{font-size:14px;color:#555}.code{font:700 44px/1.1 ui-monospace,monospace;letter-spacing:6px;text-align:center;
background:#fff;border:3px solid #0E3F31;border-radius:12px;padding:14px;margin:12px 0}
.chk{display:flex;gap:10px;align-items:center;font-size:16px;margin:10px 0 16px}.chk input{width:26px;height:26px;margin:0}
.bad{color:#b00}.ok{color:#080}small{color:#666}a{color:#06c}</style></head><body>)HTML";

static void sendPage(const String& body) {
  server.send(200, "text/html; charset=utf-8", String(PAGE_HEAD) + body + "</body></html>");
}

static void scanWifi() {
  int n = WiFi.scanNetworks();
  scanOptions = "";
  for (int i = 0; i < n && i < 25; i++) {
    String s = WiFi.SSID(i);
    if (!s.length() || scanOptions.indexOf("value=\"" + esc(s) + "\"") >= 0) continue;
    scanOptions += "<option value=\"" + esc(s) + "\">" + esc(s) + " (" + String(WiFi.RSSI(i)) + " dBm)</option>";
  }
  WiFi.scanDelete();
}

// ---- Captive Portal: Schritt 1 (WLAN), Schritt 2 (Castcode + Haken)
static void portalRoot() {
  String h = "<h1>Billard-Queue einrichten</h1>";
  if (ssid.length()) h += "<p class=bad>WLAN \"" + esc(ssid) + "\" war nicht erreichbar. Bitte pruefen:</p>";
  h += "<form method=post action='/wlan'><label>WLAN</label><select name=ssid>" + scanOptions +
       "</select><label>oder Name von Hand</label><input name=ssid2 placeholder='(nur wenn nicht in der Liste)'>"
       "<label>WLAN-Passwort</label><input name=pass type=password autocomplete=off>"
       "<button>Weiter</button></form><p><a href='/scan'>WLAN-Liste neu laden</a></p>"
       "<p><small>Firmware " FW_VERSION "</small></p>";
  sendPage(h);
}

static void portalWlan() {
  String s = server.arg("ssid2");
  s.trim();
  if (!s.length()) s = server.arg("ssid");
  String p = server.arg("pass");
  if (!s.length()) { sendPage("<p class=bad>Bitte ein WLAN waehlen.</p><p><a href='/'>Zurueck</a></p>"); return; }
  if (p.length() && p.length() < 8) { sendPage("<p class=bad>WLAN-Passwoerter haben mindestens 8 Zeichen.</p><p><a href='/'>Zurueck</a></p>"); return; }
  pendingSsid = s;
  pendingPass = p;
  String h = "<h1>Castcode</h1><p>Diesen Code im <b>Admin-Portal</b> des Billard-Systems unter "
             "<b>„Queue mit Gyroskop“</b> eintragen:</p><div class=code>" + castcode + "</div>"
             "<form method=post action='/verbinden'><label class=chk><input type=checkbox name=ok id=ok "
             "onchange=\"document.getElementById('b').disabled=!this.checked\"> Castcode ist im Admin-Portal eingetragen</label>"
             "<button id=b disabled>Mit „" + esc(s) + "“ verbinden</button></form>"
             "<p><a href='/'>WLAN aendern</a></p>";
  sendPage(h);
}

static void portalConnect() {
  if (!pendingSsid.length()) { server.sendHeader("Location", "/"); server.send(302); return; }
  if (server.arg("ok") != "on") {
    sendPage("<p class=bad>Bitte erst den Haken setzen.</p><p><a href='/'>Zurueck</a></p>");
    return;
  }
  ssid = pendingSsid;
  pass = pendingPass;
  saveWifi(ssid, pass);
  sendPage("<h1>Verbinde ...</h1><p>Der Queue verbindet sich jetzt mit <b>" + esc(ssid) + "</b>. "
           "Dieser Hotspot verschwindet; das Handy wieder mit dem normalen WLAN verbinden.</p>"
           "<p>Im Admin-Portal steht gleich <b>Queue verbunden</b>.</p>"
           "<p>Klappt die Verbindung nicht, erscheint nach ca. 30 s wieder der Hotspot.</p>");
  restartAt = millis() + 1500;
}

static void redirectToPortal() {
  server.sendHeader("Location", "http://192.168.4.1/", true);
  server.send(302, "text/plain", "");
}

static void startPortal() {
  portal = true;
  portalSince = millis();
  WiFi.disconnect(true);
  WiFi.mode(WIFI_AP_STA);
  scanWifi();
  String apName = String(SETUP_AP_PREFIX) + castcode.substring(0, 4);
  WiFi.softAP(apName.c_str());                 // offen: nur zum Einrichten, ohne Daten
  delay(100);
  dns.start(53, "*", WiFi.softAPIP());         // alle Namen -> Queue (Captive Portal)
  server.on("/", HTTP_GET, portalRoot);
  server.on("/scan", HTTP_GET, []() { scanWifi(); server.sendHeader("Location", "/"); server.send(302); });
  server.on("/wlan", HTTP_POST, portalWlan);
  server.on("/verbinden", HTTP_POST, portalConnect);
  // Erkennung der Handys/Laptops ("Anmeldung im Netzwerk erforderlich") -> Einrichtungsseite
  for (const char* u : {"/generate_204", "/gen_204", "/hotspot-detect.html", "/library/test/success.html",
                        "/connecttest.txt", "/ncsi.txt", "/fwlink", "/redirect", "/canonical.html"})
    server.on(u, redirectToPortal);
  server.onNotFound(redirectToPortal);
  server.begin();
  Serial.printf("Hotspot %s offen, Castcode %s\n", apName.c_str(), castcode.c_str());
}

// ---- im WLAN: Statusseite, API, Suche
static bool authorized() {
  String c = server.hasHeader("X-Castcode") ? server.header("X-Castcode") : server.arg("code");
  c.trim();
  c.toUpperCase();
  return c == castcode;
}

static void apiLage() {
  if (!authorized()) { server.send(403, "application/json", "{\"error\":\"falscher Castcode\"}"); return; }
  char buf[200];
  snprintf(buf, sizeof(buf),
           "{\"castcode\":\"%s\",\"seq\":%lu,\"yaw\":%.2f,\"pitch\":%.2f,\"still\":%d,\"bat\":%.2f,\"ms\":%lu}",
           castcode.c_str(), (unsigned long)seq++, curYaw, curPitch, still ? 1 : 0, curBat, (unsigned long)millis());
  server.sendHeader("Cache-Control", "no-store");
  server.send(200, "application/json", buf);
}

static void staRoot() {
  String h = "<h1>Billard-Queue</h1><p class=ok>Verbunden mit <b>" + esc(ssid) + "</b>, IP <b>" +
             WiFi.localIP().toString() + "</b>, Signal " + String(WiFi.RSSI()) + " dBm</p>"
             "<p>Castcode</p><div class=code>" + castcode + "</div>"
             "<p>Richtung " + String(curYaw, 1) + "&deg;, Neigung " + String(curPitch, 1) + "&deg;</p>"
             "<form method=post action='/einrichten' onsubmit=\"return confirm('WLAN neu einrichten? Der Queue oeffnet danach wieder den Hotspot.')\">"
             "<button>WLAN neu einrichten</button></form><p><small>Firmware " FW_VERSION "</small></p>";
  sendPage(h);
}

static void startStation() {
  portal = false;
  server.on("/", HTTP_GET, staRoot);
  server.on("/api/lage", HTTP_GET, apiLage);
  server.on("/api/info", HTTP_GET, []() {
    server.send(200, "application/json", "{\"geraet\":\"billard-queue\",\"fw\":\"" FW_VERSION "\"}");
  });
  server.on("/einrichten", HTTP_POST, []() {
    saveWifi("", "");
    sendPage("<h1>Neu einrichten</h1><p>Der Queue startet neu und oeffnet den Hotspot.</p>");
    restartAt = millis() + 1500;
  });
  server.onNotFound([]() { server.send(404, "application/json", "{\"error\":\"nicht gefunden\"}"); });
  const char* hdrs[] = {"X-Castcode"};
  server.collectHeaders(hdrs, 1);
  server.begin();
  udp.begin(DISCOVERY_PORT);
  String host = "queue-" + castcode;
  host.toLowerCase();
  if (MDNS.begin(host.c_str())) MDNS.addService("http", "tcp", API_PORT);
  Serial.printf("Im WLAN: http://%s  (%s.local), Castcode %s\n", WiFi.localIP().toString().c_str(),
                host.c_str(), castcode.c_str());
}

static bool connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);                  // geringe Verzoegerung wichtiger als Akku
  WiFi.begin(ssid.c_str(), pass.c_str());
  Serial.printf("WLAN %s ", ssid.c_str());
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < CONNECT_TIMEOUT_MS) {
    delay(250);
    Serial.print(".");
  }
  Serial.println(WiFi.status() == WL_CONNECTED ? " verbunden" : " FEHLER");
  return WiFi.status() == WL_CONNECTED;
}

// Suche des Pi beantworten: {"suche":"K7P2QM"} -> {"castcode":...,"ip":...,"port":80}
static void handleDiscovery() {
  int n = udp.parsePacket();
  if (n <= 0) return;
  char buf[128];
  int len = udp.read(buf, sizeof(buf) - 1);
  buf[len > 0 ? len : 0] = 0;
  String msg(buf);
  msg.toUpperCase();
  if (msg.indexOf("\"SUCHE\"") < 0 || msg.indexOf("\"" + castcode + "\"") < 0) return;
  char out[160];
  int m = snprintf(out, sizeof(out), "{\"castcode\":\"%s\",\"ip\":\"%s\",\"port\":%d,\"fw\":\"%s\"}",
                   castcode.c_str(), WiFi.localIP().toString().c_str(), API_PORT, FW_VERSION);
  udp.beginPacket(udp.remoteIP(), udp.remotePort());
  udp.write((const uint8_t*)out, m);
  udp.endPacket();
}

// ======================================================== Ablauf
void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\nBillard-Queue " FW_VERSION);
  if (!mpuInit()) Serial.println("MPU6050 nicht gefunden - Verdrahtung pruefen!");
  calibrateBias();
  // Startlage aus der Schwerkraft, damit der Filter nicht erst einschwingen muss
  beta = 2.0f;
  int16_t a[3], g[3];
  for (int i = 0; i < 200; i++) {
    if (readRaw(a, g)) madgwick(0, 0, 0, a[0], a[1], a[2], 1.0f / SAMPLE_HZ);
    delay(2);
  }
  beta = 0.08f;
  cueAngles(&curYaw, &curPitch);

  loadSettings();
  bool upright = fabsf(curPitch) > 60.0f;      // senkrecht gehalten = neu einrichten
  if (upright) Serial.println("Queue senkrecht: Einrichtungs-Hotspot");
  if (ssid.length() && !upright && connectWifi()) startStation();
  else startPortal();
}

void loop() {
  static uint32_t lastSample = micros(), lastAngles = 0, lastWifiCheck = 0;
  uint32_t now = micros();
  uint32_t ms = millis();

  if (portal) {
    dns.processNextRequest();
    if (ssid.length() && ms - portalSince > PORTAL_RETRY_MS) restartAt = ms;   // WLAN spaeter nochmal versuchen
  } else {
    handleDiscovery();
    if (ms - lastWifiCheck > 5000) {
      lastWifiCheck = ms;
      if (WiFi.status() != WL_CONNECTED) WiFi.reconnect();
    }
  }
  server.handleClient();
  if (restartAt && (int32_t)(ms - restartAt) >= 0) ESP.restart();

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
  if (!calm) stillSince = ms;
  still = calm && ms - stillSince > 1000;
  if (still) {
    for (int i = 0; i < 3; i++) bias[i] += 0.002f * gd[i];
    for (int i = 0; i < 3; i++) gd[i] = 0;        // liegt still: keine Drift aufintegrieren
  }
  madgwick(gd[0] * DEG2RAD, gd[1] * DEG2RAD, gd[2] * DEG2RAD, a[0], a[1], a[2], dt);

  if (ms - lastAngles >= 20) {                     // 50x pro Sekunde fuer die API bereithalten
    lastAngles = ms;
    cueAngles(&curYaw, &curPitch);
#if BAT_PIN >= 0
    curBat = analogReadMilliVolts(BAT_PIN) / 1000.0f * BAT_DIVIDER;
#endif
  }
}
