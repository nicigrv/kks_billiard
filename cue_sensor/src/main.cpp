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
 *   3. Der Queue zeigt seinen CASTCODE (6 Zeichen). Diesen im Admin-Panel des Pi unter
 *      "Queue mit Gyroskop" eintragen.
 *   4. Haken setzen "Castcode ist eingetragen" -> "Verbinden": der Queue verbindet sich mit dem WLAN.
 *   Ganz oben auf der Einrichtungsseite laeuft eine DEMO-ANZEIGE: Draufsicht auf den Tisch mit dem
 *   Queue (dreht sich live mit), Neigung von der Seite und der gruene Haken wie auf dem Beamer.
 *   Grossansicht: http://192.168.4.1/demo (im WLAN: http://<queue-ip>/demo).
 *   Neu einrichten: Queue beim Einschalten SENKRECHT halten (Spitze nach oben) oder auf
 *   seiner Statusseite "WLAN neu einrichten".
 *
 * Im WLAN:
 *   GET  /api/lage   Lage als JSON (Kopfzeile X-Castcode oder ?code=... noetig)
 *        {"castcode":"K7P2QM","seq":123,"yaw":37.52,"pitch":-4.1,"still":0,"bat":3.91,"ms":123456}
 *   GET  /api/info   Geraet + Firmware (ohne Castcode)
 *   GET  /demo       Demo-Anzeige (auch im Hotspot), Messwerte dazu: GET /demo/lage (ohne Castcode)
 *   UDP  5005        Suche: {"suche":"K7P2QM"} -> Antwort {"castcode":"K7P2QM","ip":"...","port":80}
 *                    Abo:   {"abo":"K7P2QM","farbe":[0,90,255]}  (farbe null = kein Spiel)
 *                           -> der Queue SENDET danach von selbst ca. 50x pro Sekunde an den Absender:
 *                           {"castcode":"K7P2QM","seq":1,"yaw":37.52,"pitch":-4.1,"still":0,"bat":3.91,
 *                            "rssi":-55,"knopf":0,"ms":123456}
 *                           Der Pi wiederholt das Abo jede Sekunde (Lebenszeichen + aktuelle Farbe).
 *                           Kommt 3 s keins, hoert der Queue auf zu senden.
 *   LED (M5Stack Atom)   rot = keine Verbindung zum Pi, gruen = verbunden, kein Spiel,
 *                        sonst Farbe des Teams, das dran ist. Blinkt rot = Einrichtungs-Hotspot.
 *   Knopf                bei gruener LED: Spiel Team A gegen Team B starten ("knopf" zaehlt hoch)
 *   mDNS             queue-k7p2qm.local
 *
 * yaw: Grad, gegen den Uhrzeigersinn positiv (von oben gesehen). Der absolute Wert ist beliebig -
 * der Pi zieht beim "Queue kalibrieren" den Wert an der Bande ab.
 *
 * Verdrahtung MPU6050:  VCC -> 3V3, GND -> GND, SDA -> SDA_PIN, SCL -> SCL_PIN
 * M5Stack Atom: MPU6050 (GY-521) am Grove-Port, oder der eingebaute IMU (Atom Matrix / AtomS3)
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

#if LED_PIN >= 0
#include <Adafruit_NeoPixel.h>
static Adafruit_NeoPixel led(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);
#endif

static uint8_t MPU = 0x68;               // 0x69, wenn AD0 auf 3V3 liegt
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
static uint32_t lastGoodRead = 0;          // zuletzt Werte vom Sensor bekommen (Demo: "Sensor fehlt")


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

static bool mpuFind(int sda, int scl) {
  Wire.end();
  Wire.begin(sda, scl);
  Wire.setClock(400000);
  for (uint8_t addr : {(uint8_t)0x68, (uint8_t)0x69}) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() != 0) continue;
    MPU = addr;
    Wire.beginTransmission(MPU);
    Wire.write(0x75);   // WHO_AM_I: 0x68 MPU6050, 0x19 MPU6886, 0x70 MPU6500, 0x71 MPU9250
    uint8_t who = 0;
    if (Wire.endTransmission(false) == 0 && Wire.requestFrom((int)MPU, 1) == 1) who = Wire.read();
    Serial.printf("Sensor an SDA %d / SCL %d, Adresse 0x%02X, WHO_AM_I 0x%02X\n", sda, scl, MPU, who);
    // MPU6050 und Nachbauten, MPU6500/9250, MPU6886 - alle mit denselben Registern
    if (who == 0x68 || who == 0x69 || who == 0x70 || who == 0x71 || who == 0x72 || who == 0x73 ||
        who == 0x98 || who == 0x19)
      return true;
    Serial.println("  kein MPU-kompatibler Sensor (z. B. BMI270) - wird nicht unterstuetzt");
  }
  return false;
}

// Erst die Pins aus platformio.ini (Grove-Port), dann der im Atom eingebaute IMU (falls vorhanden)
static bool mpuInit() {
  bool found = mpuFind(SDA_PIN, SCL_PIN);
#ifdef ALT_SDA_PIN
  if (!found) found = mpuFind(ALT_SDA_PIN, ALT_SCL_PIN);
#endif
  if (!found) return false;
  writeReg(0x6B, 0x80);   // Reset (MPU6886 startet sonst nicht sauber)
  delay(100);
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

// Abo des Pi: an diese Adresse wird gesendet, solange Lebenszeichen kommen
static IPAddress aboIp;
static uint16_t aboPort = 0;
static uint32_t aboLast = 0;            // millis() des letzten Abos
static bool aboActive = false;
static bool spiel = false;              // laeuft ein Spiel? (dann Teamfarbe statt gruen)
static uint8_t teamRgb[3] = {0, 0, 0};
static uint32_t knopf = 0;              // Knopfdruecke (bei gruener LED), der Pi startet damit ein Spiel

static bool piConnected() { return aboActive && millis() - aboLast < ABO_TIMEOUT_MS; }

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

// ---- Demo-Anzeige: so sieht der Pi den Queue (laeuft im Hotspot, ohne WLAN und ohne Pi)
// Draufsicht: Tisch mit Weisser, der helle Queue dreht sich mit (Richtung), der dunkle ist das Ziel.
// Liegt der Queue bis +-2 Grad auf dem Ziel, kommt der gruene Haken - wie auf dem Beamer.
// Seitenansicht: Neigung. Die Seite fragt ca. 15x pro Sekunde /demo/lage ab.
static const char DEMO_WIDGET[] = R"HTML(<style>
.demo{background:#fff;border:2px solid #0E3F31;border-radius:14px;padding:10px 12px;margin:10px 0 18px}
.demo h2{font-size:17px;margin:2px 0 8px}.demo svg{display:block;width:100%;height:auto}
.dv{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;text-align:center;margin:8px 0}
.dv small{display:block;font-size:12px;color:#666}.dv b{font:700 22px/1.2 ui-monospace,monospace}
.demo button{margin:6px 0 4px}.demo p{margin:4px 0;font-size:13px;color:#555}
</style><div class=demo><h2>Demo: so sieht der Pi den Queue</h2>
<svg viewBox="-112 -62 224 124" aria-label="Draufsicht auf den Tisch mit dem Queue">
<rect x=-110 y=-60 width=220 height=120 rx=10 fill="#5A3520"/><rect x=-100 y=-50 width=200 height=100 fill="#1C5496"/>
<g fill="#111"><circle cx=-100 cy=-50 r=5.5 /><circle cx=0 cy=-52 r=5 /><circle cx=100 cy=-50 r=5.5 />
<circle cx=-100 cy=50 r=5.5 /><circle cx=0 cy=52 r=5 /><circle cx=100 cy=50 r=5.5 /></g>
<g id=dtg opacity=.75><polygon points="-9,-.9 -98,-2.4 -98,2.4 -9,.9" fill="#3a2414"/></g>
<g id=dcu><polygon points="-9,-.9 -98,-2.4 -98,2.4 -9,.9" fill="#E9D3A8" stroke="#7a5a32" stroke-width=.4 />
<rect x=-12 y=-1 width=3 height=2 fill="#4FA3E0"/></g>
<circle cx=0 cy=0 r=3.6 fill="#F6F4EC" stroke="#999" stroke-width=.3 />
<g id=dok visibility=hidden><circle cx=78 cy=-32 r=14 fill="#0C2014" stroke="#50DC78" stroke-width=2.5 />
<path d="M71 -32 l5 5 l10 -11" fill=none stroke="#50DC78" stroke-width=3.5 stroke-linecap=round stroke-linejoin=round /></g></svg>
<div class=dv><div><small>Richtung</small><b id=dy>-</b></div><div><small>Neigung</small><b id=dp>-</b></div>
<div><small>Sensor</small><b id=ds>-</b></div></div>
<svg viewBox="-62 -16 124 32" aria-label="Seitenansicht: Neigung des Queues"><rect x=-60 y=4 width=120 height=4 fill="#1C5496"/>
<g id=dsi><polygon points="40,-.7 -55,-2 -55,2 40,.7" fill="#E9D3A8" stroke="#7a5a32" stroke-width=.4 /></g>
<circle cx=44 cy=1.5 r=2.5 fill="#F6F4EC" stroke="#999" stroke-width=.3 /></svg>
<button type=button id=dz>Richtung hier auf 0 setzen</button>
<p>Queue flach halten, Spitze nach rechts (wie an der Bande), dann „auf 0 setzen“. Danach auf den dunklen Queue drehen: bis &plusmn;2&deg; kommt der grüne Haken – wie später auf dem Beamer.</p>
</div><script>(function(){var z=null,y0=0,$=function(i){return document.getElementById(i)};
function nm(a){a=((a+180)%360+360)%360-180;return a}
function f(v){return(v>=0?'+':'')+v.toFixed(1).replace('.',',')+'°'}
try{var s=localStorage.getItem('qz');if(s!==null)z=parseFloat(s)}catch(e){}
$('dz').onclick=function(){z=y0;try{localStorage.setItem('qz',z)}catch(e){}};
function poll(){var x=new XMLHttpRequest();x.open('GET','/demo/lage?t='+Date.now());x.timeout=1500;
x.onload=function(){try{var d=JSON.parse(x.responseText);y0=d.yaw;var r=nm(d.yaw-(z===null?0:z));
$('dcu').setAttribute('transform','rotate('+(-r).toFixed(2)+')');
$('dsi').setAttribute('transform','rotate('+(-d.pitch).toFixed(2)+' 40 0)');
$('dy').textContent=z===null?f(nm(d.yaw)):f(r);$('dp').textContent=f(d.pitch);
$('ds').textContent=!d.sensor?'fehlt!':d.still?'ruhig':'bewegt';$('ds').style.color=d.sensor?'#080':'#b00';
$('dtg').setAttribute('visibility',z===null?'hidden':'visible');
$('dok').setAttribute('visibility',z!==null&&Math.abs(r)<=2?'visible':'hidden');}catch(e){}setTimeout(poll,60)};
x.onerror=x.ontimeout=function(){$('ds').textContent='getrennt';$('ds').style.color='#b00';setTimeout(poll,500)};x.send()}
poll()})();</script>)HTML";

static void demoLage() {
  char buf[140];
  bool ok = millis() - lastGoodRead < 500;
  snprintf(buf, sizeof(buf), "{\"yaw\":%.2f,\"pitch\":%.2f,\"still\":%d,\"sensor\":%d,\"bat\":%.2f}",
           curYaw, curPitch, still ? 1 : 0, ok ? 1 : 0, curBat);
  server.sendHeader("Cache-Control", "no-store");
  server.send(200, "application/json", buf);
}

static void demoPage() {
  sendPage(String("<h1>Billard-Queue: Demo</h1>") + DEMO_WIDGET +
           (portal ? "<p><a href='/'>Zur Einrichtung (WLAN)</a></p>" : "<p><a href='/'>Zur Statusseite</a></p>"));
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
  String h = "<h1>Billard-Queue</h1>";
  h += DEMO_WIDGET;
  h += "<h2>Einrichten</h2>";
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
  String h = "<h1>Castcode</h1><p>Diesen Code im <b>Admin-Panel</b> des Billard-Systems unter "
             "<b>„Queue mit Gyroskop“</b> eintragen:</p><div class=code>" + castcode + "</div>"
             "<form method=post action='/verbinden'><label class=chk><input type=checkbox name=ok id=ok "
             "onchange=\"document.getElementById('b').disabled=!this.checked\"> Castcode ist im Admin-Panel eingetragen</label>"
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
           "<p>Im Admin-Panel steht gleich <b>Queue verbunden</b>.</p>"
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
  server.on("/demo", HTTP_GET, demoPage);
  server.on("/demo/lage", HTTP_GET, demoLage);
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
             "<p>Richtung " + String(curYaw, 1) + "&deg;, Neigung " + String(curPitch, 1) + "&deg; &middot; "
             "<a href='/demo'>Demo-Anzeige (live)</a></p>"
             "<form method=post action='/einrichten' onsubmit=\"return confirm('WLAN neu einrichten? Der Queue oeffnet danach wieder den Hotspot.')\">"
             "<button>WLAN neu einrichten</button></form><p><small>Firmware " FW_VERSION "</small></p>";
  sendPage(h);
}

static void startStation() {
  portal = false;
  server.on("/", HTTP_GET, staRoot);
  server.on("/api/lage", HTTP_GET, apiLage);
  server.on("/demo", HTTP_GET, demoPage);
  server.on("/demo/lage", HTTP_GET, demoLage);
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

// Wert hinter "schluessel": aus einem kleinen JSON lesen (ohne Bibliothek)
static int jsonPos(const String& msg, const char* key) {
  int i = msg.indexOf(String("\"") + key + "\"");
  if (i < 0) return -1;
  i = msg.indexOf(':', i);
  if (i < 0) return -1;
  i++;
  while (i < (int)msg.length() && msg[i] == ' ') i++;
  return i;
}

static String jsonStr(const String& msg, const char* key) {
  int i = jsonPos(msg, key);
  if (i < 0 || msg[i] != '"') return "";
  int j = msg.indexOf('"', i + 1);
  return j < 0 ? "" : msg.substring(i + 1, j);
}

// UDP an Port 5005: Suche des Pi beantworten und Abos annehmen
static void handleUdp() {
  int n = udp.parsePacket();
  if (n <= 0) return;
  char buf[200];
  int len = udp.read(buf, sizeof(buf) - 1);
  buf[len > 0 ? len : 0] = 0;
  String msg(buf);

  String code = jsonStr(msg, "suche");
  code.toUpperCase();
  if (code == castcode) {                         // {"suche":"K7P2QM"} -> wer bin ich, wo bin ich
    char out[160];
    int m = snprintf(out, sizeof(out), "{\"castcode\":\"%s\",\"ip\":\"%s\",\"port\":%d,\"fw\":\"%s\"}",
                     castcode.c_str(), WiFi.localIP().toString().c_str(), API_PORT, FW_VERSION);
    udp.beginPacket(udp.remoteIP(), udp.remotePort());
    udp.write((const uint8_t*)out, m);
    udp.endPacket();
    return;
  }

  code = jsonStr(msg, "abo");
  code.toUpperCase();
  if (code != castcode) return;                   // fremder Queue / kein Abo
  bool neu = !piConnected();
  aboIp = udp.remoteIP();
  aboPort = udp.remotePort();
  aboLast = millis();
  aboActive = true;
  int i = jsonPos(msg, "farbe");                  // [r,g,b] = Spiel laeuft, null = kein Spiel
  if (i >= 0 && msg[i] == '[') {
    int r = 0, g = 0, b = 0;
    if (sscanf(msg.c_str() + i, "[%d,%d,%d]", &r, &g, &b) == 3) {
      teamRgb[0] = constrain(r, 0, 255);
      teamRgb[1] = constrain(g, 0, 255);
      teamRgb[2] = constrain(b, 0, 255);
      spiel = true;
    }
  } else {
    spiel = false;
  }
  if (neu) Serial.printf("Pi %s:%u hat abonniert - sende Lage\n", aboIp.toString().c_str(), aboPort);
}

// Lage an den Pi senden (nur solange er abonniert hat)
static void sendLage() {
  if (!piConnected()) {
    if (aboActive) {
      aboActive = false;
      spiel = false;
      Serial.println("Kein Lebenszeichen vom Pi - Senden gestoppt");
    }
    return;
  }
  char out[220];
  int m = snprintf(out, sizeof(out),
                   "{\"castcode\":\"%s\",\"seq\":%lu,\"yaw\":%.2f,\"pitch\":%.2f,\"still\":%d,\"bat\":%.2f,"
                   "\"rssi\":%d,\"knopf\":%lu,\"ms\":%lu}",
                   castcode.c_str(), (unsigned long)seq++, curYaw, curPitch, still ? 1 : 0, curBat,
                   (int)WiFi.RSSI(), (unsigned long)knopf, (unsigned long)millis());
  udp.beginPacket(aboIp, aboPort);
  udp.write((const uint8_t*)out, m);
  udp.endPacket();
}

// ======================================================== LED und Knopf
static void ledColor(uint8_t r, uint8_t g, uint8_t b) {
#if LED_PIN >= 0
  static uint32_t last = 0xFFFFFFFF;
  uint32_t c = ((uint32_t)r << 16) | ((uint32_t)g << 8) | b;
  if (c == last) return;                           // nur bei Aenderung schreiben
  last = c;
  for (int i = 0; i < LED_COUNT; i++) led.setPixelColor(i, led.Color(r, g, b));
  led.show();
#endif
}

enum LedState { LED_ROT, LED_GRUEN, LED_TEAM };

static LedState ledState() {
  if (!piConnected()) return LED_ROT;
  return spiel ? LED_TEAM : LED_GRUEN;
}

static void updateLed() {
  uint32_t ms = millis();
  if (portal) {                                    // Einrichtungs-Hotspot: rot blinken
    bool on = (ms / 500) % 2 == 0;
    ledColor(on ? 255 : 0, 0, 0);
    return;
  }
  switch (ledState()) {
    case LED_ROT: ledColor(255, 0, 0); break;
    case LED_GRUEN: ledColor(0, 255, 0); break;
    case LED_TEAM: ledColor(teamRgb[0], teamRgb[1], teamRgb[2]); break;
  }
}

static void handleButton() {
#if BTN_PIN >= 0
  static bool lastDown = false;
  static uint32_t changed = 0;
  bool down = digitalRead(BTN_PIN) == LOW;
  uint32_t ms = millis();
  if (down == lastDown || ms - changed < 40) return;   // entprellen
  changed = ms;
  lastDown = down;
  if (!down) return;                                   // nur beim Druecken
  if (ledState() == LED_GRUEN) {
    knopf++;
    Serial.printf("Knopf: Spiel starten (%lu)\n", (unsigned long)knopf);
    for (int i = 0; i < 3; i++) sendLage();            // sofort melden (UDP: lieber mehrfach)
  } else {
    Serial.println("Knopf: nur bei gruener LED (verbunden, kein Spiel)");
  }
#endif
}

// ======================================================== Ablauf
void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\nBillard-Queue " FW_VERSION);
#if LED_PIN >= 0
  led.begin();
  led.setBrightness(LED_BRIGHTNESS);
  ledColor(255, 255, 255);                       // weiss: Gyroskop-Nullpunkt, Queue ruhig liegen lassen
#endif
#if BTN_PIN >= 0
  pinMode(BTN_PIN, INPUT_PULLUP);
#endif
  if (!mpuInit()) Serial.println("Gyro-Sensor nicht gefunden - Verdrahtung pruefen!");
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
  static uint32_t lastSend = 0;
  uint32_t now = micros();
  uint32_t ms = millis();

  if (portal) {
    dns.processNextRequest();
    if (ssid.length() && ms - portalSince > PORTAL_RETRY_MS) restartAt = ms;   // WLAN spaeter nochmal versuchen
  } else {
    handleUdp();
    if (ms - lastSend >= SEND_INTERVAL_MS) {
      lastSend = ms;
      sendLage();
    }
    if (ms - lastWifiCheck > 5000) {
      lastWifiCheck = ms;
      if (WiFi.status() != WL_CONNECTED) WiFi.reconnect();
    }
  }
  handleButton();
  updateLed();
  server.handleClient();
  if (restartAt && (int32_t)(ms - restartAt) >= 0) ESP.restart();

  if (now - lastSample < 1000000UL / SAMPLE_HZ) return;
  float dt = (now - lastSample) / 1e6f;
  lastSample = now;

  int16_t a[3], g[3];
  if (!readRaw(a, g)) return;
  lastGoodRead = ms;
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
