// Kö-Messgerät: M5Stack Atom Lite + LIS3DH (I2C am Grove-Port)
// WLAN-Access-Point mit Captive Portal: zeigt Beschleunigung und Lage (Neigung).
//
// WICHTIG: Der LIS3DH ist ein reiner Beschleunigungssensor (kein Gyroskop).
// "Lage" = Neigung (Pitch/Roll) aus der Erdbeschleunigung. Sie stimmt nur,
// wenn der Kö ruhig gehalten wird, nicht während eines Stoßes.

#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <math.h>

// ---------------- Einstellungen ----------------
#define SDA_PIN 26          // Grove-Port Atom Lite
#define SCL_PIN 32
#define LED_PIN 27          // RGB-LED des Atom Lite
#define BTN_PIN 39          // Taster des Atom Lite
static const char* AP_SSID = "KOE-Messgeraet";   // offenes WLAN (Captive Portal)
static const IPAddress AP_IP(192, 168, 4, 1);

// Achsen-Zuordnung: Index 0 = Richtung entlang des Kö (Stoßrichtung).
// Falls der Sensor anders eingebaut ist, hier tauschen / Vorzeichen ändern.
static const int   AXIS_MAP[3] = {0, 1, 2};      // Sensor-Achse (X,Y,Z) -> Kö-Achse
static const float AXIS_SGN[3] = {1, 1, 1};

// ---------------- Seite (im Flash) ----------------
static const char PAGE[] PROGMEM = R"rawliteral(
<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kö-Messgerät</title>
<style>
:root{--bg:#f4f4f2;--fg:#222;--card:#fff;--mut:#777}
@media(prefers-color-scheme:dark){:root{--bg:#1b1b1a;--fg:#eee;--card:#2a2a28;--mut:#aaa}}
body{margin:0;padding:12px;background:var(--bg);color:var(--fg);font-family:system-ui,sans-serif}
h1{font-size:20px;margin:0 0 4px}#st{color:var(--mut);font-size:13px;margin-bottom:10px}
.g{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.c{background:var(--card);border-radius:10px;padding:10px}
.c span{display:block;font-size:12px;color:var(--mut)}.c b{font-size:26px}
canvas{width:100%;height:170px;background:var(--card);border-radius:10px;margin-top:8px}
.row{display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap}
button,input{font-size:15px;padding:8px 12px;border-radius:8px;border:1px solid var(--mut);background:var(--card);color:var(--fg)}
input{width:80px}
.w{position:relative;height:60px;background:var(--card);border-radius:10px;margin-top:8px;overflow:hidden}
.w i{position:absolute;left:50%;top:50%;width:80%;height:6px;margin:-3px 0 0 -40%;background:#3b82f6;border-radius:3px}
.w em{position:absolute;left:8px;top:4px;font-size:12px;color:var(--mut);font-style:normal}
</style></head><body>
<h1>Kö-Messgerät</h1><div id="st">Verbinde...</div>
<div class="g">
<div class="c"><span>Neigung Pitch (Kö hoch/runter)</span><b id="p">-</b></div>
<div class="c"><span>Neigung Roll (Kö drehen)</span><b id="r">-</b></div>
<div class="c"><span>Beschleunigung gesamt</span><b id="a">-</b></div>
<div class="c"><span>Spitze letzter Stoß</span><b id="h">-</b></div>
<div class="c"><span>Kraft (F = m·a)</span><b id="f">-</b></div>
<div class="c"><span>ax / ay / az (g)</span><b id="xyz" style="font-size:16px">-</b></div>
</div>
<div class="g"><div class="w"><em>Pitch</em><i id="vp"></i></div><div class="w"><em>Roll</em><i id="vr"></i></div></div>
<canvas id="cv" width="600" height="170"></canvas>
<div class="row"><label>Kö-Masse (kg) <input id="m" type="number" step="0.05" value="0.5"></label>
<button onclick="fetch('/zero')">Nullen (Kö ruhig halten)</button></div>
<script>
const $=i=>document.getElementById(i);let hist=[],hA=0,hT=0;
function draw(){const c=$('cv'),x=c.getContext('2d'),W=c.width,H=c.height;x.clearRect(0,0,W,H);
let mx=Math.max(4,...hist);x.strokeStyle='rgba(128,128,128,.3)';x.fillStyle=getComputedStyle(document.body).color;x.font='12px sans-serif';
for(let i=0;i<=4;i++){let y=H-i*H/4;x.beginPath();x.moveTo(0,y);x.lineTo(W,y);x.stroke();x.fillText((mx*i/4).toFixed(1)+' g',4,y-3)}
x.strokeStyle='#3b82f6';x.lineWidth=2;x.beginPath();hist.forEach((v,i)=>{let X=i*W/200,Y=H-v/mx*H;i?x.lineTo(X,Y):x.moveTo(X,Y)});x.stroke()}
async function poll(){try{const d=await(await fetch('/data',{cache:'no-store'})).json();
$('st').textContent=d.ok?'Verbunden, Sensor OK':'Sensor nicht gefunden! Verkabelung prüfen.';
$('p').textContent=d.pitch.toFixed(1)+'°';$('r').textContent=d.roll.toFixed(1)+'°';
$('a').textContent=d.a.toFixed(2)+' g';
$('xyz').textContent=d.ax.toFixed(2)+' / '+d.ay.toFixed(2)+' / '+d.az.toFixed(2);
const n=Date.now();if(d.peak>2){if(n-hT>400)hA=0;hA=Math.max(hA,d.peak);hT=n}
const m=parseFloat($('m').value)||0.5;
$('h').textContent=hA.toFixed(1)+' g';$('f').textContent=(m*hA*9.81).toFixed(1)+' N';
$('vp').style.transform='rotate('+(-d.pitch)+'deg)';$('vr').style.transform='rotate('+d.roll+'deg)';
hist.push(d.peak);if(hist.length>200)hist.shift();draw()}catch(e){$('st').textContent='Keine Verbindung...'}
setTimeout(poll,40)}
poll();
</script></body></html>
)rawliteral";

// ---------------- Sensor (LIS3DH) ----------------
static uint8_t lisAddr = 0;
static float gPerCount = 0.012f;      // 12 mg/Digit bei +-16 g (wird beim Nullen auf 1 g nachkalibriert)
static float pitch0 = 0, roll0 = 0;
static volatile bool zeroReq = false;
static volatile bool sensorOk = false;
static float cur[3] = {0, 0, 1}, filt[3] = {0, 0, 1}, peakDyn = 0;
static portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;

static bool lisWrite(uint8_t reg, uint8_t v) {
  Wire.beginTransmission(lisAddr);
  Wire.write(reg);
  Wire.write(v);
  return Wire.endTransmission() == 0;
}

static bool lisRead(uint8_t reg, uint8_t* buf, uint8_t n) {
  Wire.beginTransmission(lisAddr);
  Wire.write(reg | 0x80);                       // Auto-Increment
  if (Wire.endTransmission(false) != 0) return false;
  if ((int)Wire.requestFrom((int)lisAddr, (int)n) != (int)n) return false;
  for (uint8_t i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}

static bool initSensor() {
  const uint8_t addrs[2] = {0x18, 0x19};        // je nach SDO-Pin des Moduls
  for (int i = 0; i < 2; i++) {
    lisAddr = addrs[i];
    uint8_t id = 0;
    if (lisRead(0x0F, &id, 1) && id == 0x33) {  // WHO_AM_I
      lisWrite(0x20, 0x77);                     // 400 Hz, X/Y/Z an
      lisWrite(0x23, 0xB8);                     // BDU, +-16 g, High-Resolution
      delay(10);
      return true;
    }
  }
  lisAddr = 0;
  return false;
}

// liefert Beschleunigung in g, bereits auf Kö-Achsen gemappt
static bool readAccel(float* g) {
  uint8_t b[6];
  if (!lisRead(0x28, b, 6)) return false;
  float raw[3];
  for (int i = 0; i < 3; i++) {
    int16_t v = (int16_t)((b[2 * i + 1] << 8) | b[2 * i]);
    raw[i] = (float)(v >> 4);                   // 12 Bit links ausgerichtet
  }
  for (int i = 0; i < 3; i++) g[i] = raw[AXIS_MAP[i]] * gPerCount * AXIS_SGN[i];
  return true;
}

static void doZero() {
  float s[3] = {0, 0, 0};
  int n = 0;
  for (int i = 0; i < 300; i++) {
    float g[3];
    if (readAccel(g)) { for (int k = 0; k < 3; k++) s[k] += g[k]; n++; }
    vTaskDelay(pdMS_TO_TICKS(2));
  }
  if (n < 50) return;
  for (int k = 0; k < 3; k++) s[k] /= n;
  float m = sqrtf(s[0] * s[0] + s[1] * s[1] + s[2] * s[2]);
  if (m < 0.5f || m > 2.0f) return;             // unplausibel: ignorieren
  portENTER_CRITICAL(&mux);
  gPerCount /= m;                               // im Ruhezustand genau 1 g
  for (int k = 0; k < 3; k++) { s[k] /= m; filt[k] = s[k]; }
  pitch0 = atan2f(-s[0], sqrtf(s[1] * s[1] + s[2] * s[2])) * RAD_TO_DEG;
  roll0 = atan2f(s[1], s[2]) * RAD_TO_DEG;
  peakDyn = 0;
  portEXIT_CRITICAL(&mux);
}

static void sampleTask(void*) {
  bool init = false;
  for (;;) {
    if (zeroReq) { doZero(); zeroReq = false; }
    float g[3];
    if (sensorOk && readAccel(g)) {
      float mag = sqrtf(g[0] * g[0] + g[1] * g[1] + g[2] * g[2]);
      float dyn = fabsf(mag - 1.0f);            // grob: Erdbeschleunigung abziehen
      portENTER_CRITICAL(&mux);
      for (int k = 0; k < 3; k++) {
        cur[k] = g[k];
        if (!init) filt[k] = g[k]; else filt[k] += 0.02f * (g[k] - filt[k]);  // Tiefpass ~0,1 s
      }
      if (dyn > peakDyn) peakDyn = dyn;
      portEXIT_CRITICAL(&mux);
      init = true;
    }
    vTaskDelay(pdMS_TO_TICKS(2));
  }
}

// ---------------- Webserver / Captive Portal ----------------
static WebServer server(80);
static DNSServer dns;

static void handleRoot() { server.send_P(200, "text/html; charset=utf-8", PAGE); }

static void redirectToPortal() {
  server.sendHeader("Location", "http://192.168.4.1/", true);
  server.send(302, "text/plain", "");
}

static void handleData() {
  float c[3], f[3], pk, p0, r0;
  portENTER_CRITICAL(&mux);
  for (int k = 0; k < 3; k++) { c[k] = cur[k]; f[k] = filt[k]; }
  pk = peakDyn; peakDyn = 0;                    // Spitzenwert seit letzter Abfrage
  p0 = pitch0; r0 = roll0;
  portEXIT_CRITICAL(&mux);
  float mag = sqrtf(c[0] * c[0] + c[1] * c[1] + c[2] * c[2]);
  float pitch = atan2f(-f[0], sqrtf(f[1] * f[1] + f[2] * f[2])) * RAD_TO_DEG - p0;
  float roll = atan2f(f[1], f[2]) * RAD_TO_DEG - r0;
  if (roll > 180) roll -= 360;
  if (roll < -180) roll += 360;
  char buf[220];
  snprintf(buf, sizeof(buf),
           "{\"ok\":%d,\"ax\":%.3f,\"ay\":%.3f,\"az\":%.3f,\"a\":%.3f,\"peak\":%.3f,\"pitch\":%.1f,\"roll\":%.1f}",
           sensorOk ? 1 : 0, c[0], c[1], c[2], mag, pk, pitch, roll);
  server.sendHeader("Cache-Control", "no-store");
  server.send(200, "application/json", buf);
}

static void handleZero() { zeroReq = true; server.send(200, "text/plain", "ok"); }

static void led(uint8_t r, uint8_t g, uint8_t b) { neopixelWrite(LED_PIN, r, g, b); }

void setup() {
  Serial.begin(115200);
  pinMode(BTN_PIN, INPUT);
  Wire.begin(SDA_PIN, SCL_PIN, 400000);
  sensorOk = initSensor();
  Serial.println(sensorOk ? "LIS3DH gefunden" : "LIS3DH NICHT gefunden (Verkabelung / SDA-SCL pruefen)");

  WiFi.mode(WIFI_AP);
  WiFi.softAPConfig(AP_IP, AP_IP, IPAddress(255, 255, 255, 0));
  WiFi.softAP(AP_SSID);
  dns.start(53, "*", AP_IP);                    // alle Domains -> Gerät (Captive Portal)

  server.on("/", handleRoot);
  server.on("/data", handleData);
  server.on("/zero", handleZero);
  // Erkennungs-URLs von Android / iOS / Windows -> Portal
  const char* probes[] = {"/generate_204", "/gen_204", "/hotspot-detect.html", "/library/test/success.html",
                          "/connecttest.txt", "/ncsi.txt", "/fwlink", "/canonical.html", "/success.txt"};
  for (const char* p : probes) server.on(p, redirectToPortal);
  server.onNotFound(redirectToPortal);
  server.begin();

  xTaskCreatePinnedToCore(sampleTask, "sample", 4096, NULL, 1, NULL, 1);
  Serial.printf("WLAN '%s' bereit, Seite: http://192.168.4.1\n", AP_SSID);
}

void loop() {
  dns.processNextRequest();
  server.handleClient();

  static uint32_t lastBtn = 0, lastLed = 0;
  if (digitalRead(BTN_PIN) == LOW && millis() - lastBtn > 1500) { zeroReq = true; lastBtn = millis(); }

  if (millis() - lastLed > 150) {               // LED: blau = bereit, gruen = Nullen, rot blinkt = Sensorfehler
    lastLed = millis();
    if (!sensorOk) led((millis() / 300) % 2 ? 40 : 0, 0, 0);
    else if (zeroReq) led(0, 40, 0);
    else led(0, 0, 25);
  }
  delay(1);
}
