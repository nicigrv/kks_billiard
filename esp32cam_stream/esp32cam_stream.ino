/*
 * ESP32-CAM (AI-Thinker) -> MJPEG-Stream fuer das Billard-System
 *
 * KEINE WLAN-Daten im Code: Beim ersten Start (oder wenn das WLAN nicht klappt)
 * oeffnet die Kamera den Hotspot "Billard-Cam-Setup". Mit dem Handy verbinden,
 * die Einrichtungsseite oeffnet sich (sonst http://192.168.4.1), WLAN eintragen, fertig.
 * Die Daten liegen im Flash (NVS) und ueberleben Firmware-Updates.
 *
 * Endpunkte (Port 80, ausser Stream):
 *   /          Uebersicht + Einrichtung (WLAN, feste IP, Admin-Passwort)
 *   /update    Firmware-Update (.bin)
 *   /status    JSON-Status
 *   /capture   Einzelbild
 *   :81/stream MJPEG-Livestream
 *
 * Arduino IDE / arduino-cli:
 *   Board "AI Thinker ESP32-CAM", PSRAM Enabled,
 *   Partition "Regular/Default 4MB with spiffs (1.2MB APP/1.5MB SPIFFS)"  (MUSS OTA haben!)
 *   FQBN: esp32:esp32:esp32cam:PartitionScheme=default
 *   Hochladen wird nur die *.ino.bin (nicht merged/bootloader/partitions).
 */

#include "esp_camera.h"
#include "esp_http_server.h"
#include <WiFi.h>
#include <Update.h>
#include <Preferences.h>
#include <DNSServer.h>
#include <ESPmDNS.h>

// ======================= KONFIGURATION =======================
#define FW_VERSION      "1.0.0"
#define HOSTNAME        "billard-cam"           // im Router + http://billard-cam.local
#define SETUP_AP_SSID   "Billard-Cam-Setup"
// Admin-Passwort ab Werk (Einrichtung, Updates, Hotspot). Auf der Einrichtungsseite aenderbar.
#define DEFAULT_ADMIN_PW "billard1234"

// Aufloesung: FRAMESIZE_VGA (640x480) ~20-25 fps
//             FRAMESIZE_SVGA (800x600) ~15-20 fps
//             FRAMESIZE_HD (1280x720) ~10-12 fps
#define FRAME_SIZE   FRAMESIZE_SVGA
// JPEG-Qualitaet: 0-63, kleiner = besser/groesser. 10-15 ist ein guter Bereich.
#define JPEG_QUALITY 12
// =============================================================

// Pinbelegung AI-Thinker ESP32-CAM
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22
#define LED_FLASH_GPIO     4

#define PART_BOUNDARY "billardframeboundary"
static const char* STREAM_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;
static const char* STREAM_BOUNDARY     = "\r\n--" PART_BOUNDARY "\r\n";
static const char* STREAM_PART         = "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

// ---------------- gespeicherte Einstellungen ----------------
struct Settings {
  String ssid, pass;
  String ip, gw, mask;     // leer = DHCP
  String admin_pw;
};
static Settings cfg;
static Preferences prefs;

static httpd_handle_t control_httpd = NULL;
static httpd_handle_t stream_httpd  = NULL;
static DNSServer dns;
static bool camera_ok = false;
static bool ap_mode   = false;
static String scan_options;   // <option>-Liste der gefundenen WLANs fuer die Einrichtungsseite
static volatile uint32_t restart_at = 0;  // geplanter Neustart (nach Antwort an den Browser)

static void load_settings() {
  prefs.begin("billard", true);
  cfg.ssid     = prefs.getString("ssid", "");
  cfg.pass     = prefs.getString("pass", "");
  cfg.ip       = prefs.getString("ip", "");
  cfg.gw       = prefs.getString("gw", "");
  cfg.mask     = prefs.getString("mask", "");
  cfg.admin_pw = prefs.getString("admin", DEFAULT_ADMIN_PW);
  prefs.end();
}

static void save_settings() {
  prefs.begin("billard", false);
  prefs.putString("ssid", cfg.ssid);
  prefs.putString("pass", cfg.pass);
  prefs.putString("ip", cfg.ip);
  prefs.putString("gw", cfg.gw);
  prefs.putString("mask", cfg.mask);
  prefs.putString("admin", cfg.admin_pw);
  prefs.end();
}

// ---------------- kleine Helfer ----------------
static String html_escape(const String& s) {
  String o;
  o.reserve(s.length() + 8);
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

static String url_decode(const char* s) {
  String o;
  for (size_t i = 0; s[i]; i++) {
    if (s[i] == '+') o += ' ';
    else if (s[i] == '%' && isxdigit((unsigned char)s[i + 1]) && isxdigit((unsigned char)s[i + 2])) {
      char hex[3] = {s[i + 1], s[i + 2], 0};
      o += (char)strtol(hex, NULL, 16);
      i += 2;
    } else o += s[i];
  }
  return o;
}

static String form_field(const char* body, const char* key) {
  char raw[200] = {0};
  if (httpd_query_key_value(body, key, raw, sizeof(raw)) != ESP_OK) return "";
  return url_decode(raw);
}

static bool check_pw_header(httpd_req_t* req) {
  char pw[80] = {0};
  return httpd_req_get_hdr_value_str(req, "X-OTA-Password", pw, sizeof(pw)) == ESP_OK &&
         cfg.admin_pw == pw;
}

static bool valid_ip(const String& s) {
  IPAddress a;
  return a.fromString(s);
}

// ---------------- Stream ----------------
static esp_err_t stream_handler(httpd_req_t* req) {
  if (!camera_ok) {
    httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Kamera nicht initialisiert");
    return ESP_FAIL;
  }
  char part_buf[64];
  esp_err_t res = httpd_resp_set_type(req, STREAM_CONTENT_TYPE);
  if (res != ESP_OK) return res;
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_hdr(req, "Cache-Control", "no-cache");

  while (true) {
    camera_fb_t* fb = esp_camera_fb_get();
    if (!fb) {
      Serial.println("[stream] Frame konnte nicht gelesen werden");
      res = ESP_FAIL;
      break;
    }
    size_t hlen = snprintf(part_buf, sizeof(part_buf), STREAM_PART, fb->len);
    res = httpd_resp_send_chunk(req, STREAM_BOUNDARY, strlen(STREAM_BOUNDARY));
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, part_buf, hlen);
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, (const char*)fb->buf, fb->len);
    esp_camera_fb_return(fb);
    if (res != ESP_OK) break;  // Client hat Verbindung getrennt
  }
  return res;
}

// ---------------- Einzelbild ----------------
static esp_err_t capture_handler(httpd_req_t* req) {
  camera_fb_t* fb = camera_ok ? esp_camera_fb_get() : NULL;
  if (!fb) {
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }
  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  esp_err_t res = httpd_resp_send(req, (const char*)fb->buf, fb->len);
  esp_camera_fb_return(fb);
  return res;
}

// ---------------- Status ----------------
static esp_err_t status_handler(httpd_req_t* req) {
  sensor_t* s = camera_ok ? esp_camera_sensor_get() : NULL;
  char json[260];
  snprintf(json, sizeof(json),
           "{\"version\":\"%s\",\"camera_ok\":%s,\"ap_mode\":%s,\"framesize\":%d,\"quality\":%d,"
           "\"free_heap\":%u,\"rssi\":%d,\"ip\":\"%s\",\"uptime_s\":%lu}",
           FW_VERSION, camera_ok ? "true" : "false", ap_mode ? "true" : "false",
           s ? s->status.framesize : -1, s ? s->status.quality : -1,
           (unsigned)ESP.getFreeHeap(), ap_mode ? 0 : WiFi.RSSI(),
           (ap_mode ? WiFi.softAPIP() : WiFi.localIP()).toString().c_str(),
           (unsigned long)(millis() / 1000));
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, json, strlen(json));
}

// ---------------- Seiten ----------------
static const char PAGE_HEAD[] = R"HTML(<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Billard-Cam</title>
<style>body{font-family:sans-serif;max-width:480px;margin:24px auto;padding:0 16px;color:#222}
input,button,select{font-size:16px;margin:4px 0 12px;width:100%;box-sizing:border-box;padding:8px}
label{font-size:14px;color:#555}fieldset{border:1px solid #ccc;border-radius:8px;margin:16px 0}
.ok{color:#080}.bad{color:#c00}small{color:#777}progress{width:100%}a{color:#06c}</style></head><body>)HTML";

static esp_err_t root_handler(httpd_req_t* req) {
  String ip = (ap_mode ? WiFi.softAPIP() : WiFi.localIP()).toString();
  String h;
  h.reserve(4096);
  h += PAGE_HEAD;
  h += "<h2>Billard-Cam</h2><p>Firmware " FW_VERSION " &middot; ";
  h += camera_ok ? "<span class=ok>Kamera ok</span>" : "<span class=bad>Kamera nicht erkannt</span>";
  h += "<br>";
  if (ap_mode) {
    h += cfg.ssid.length() ? "<span class=bad>WLAN \"" + html_escape(cfg.ssid) +
                             "\" nicht erreichbar.</span> Daten pruefen:"
                           : "Noch kein WLAN eingerichtet.";
  } else {
    h += "WLAN <b>" + html_escape(cfg.ssid) + "</b>, IP <b>" + ip + "</b>, Signal " +
         String(WiFi.RSSI()) + " dBm<br>MAC " + WiFi.macAddress();
    h += "<br><a href='http://" + ip + ":81/stream'>Livebild</a> &middot; <a href='/update'>Firmware-Update</a>";
  }
  h += "</p>";

  h += "<form method=post action='/setup'><fieldset><legend>WLAN</legend>"
       "<label>Netzwerk</label><input name=ssid list=nets required value=\"" + html_escape(cfg.ssid) +
       "\"><datalist id=nets>" + scan_options + "</datalist>"
       "<label>WLAN-Passwort</label><input name=pass type=password placeholder='"
       + String(cfg.pass.length() ? "(unveraendert lassen = leer)" : "") + "'>"
       "</fieldset><fieldset><legend>Feste IP <small>(leer = automatisch per DHCP)</small></legend>"
       "<label>IP-Adresse</label><input name=ip placeholder='z. B. 192.168.178.50' value=\"" + html_escape(cfg.ip) + "\">"
       "<label>Gateway (Router)</label><input name=gw placeholder='z. B. 192.168.178.1' value=\"" + html_escape(cfg.gw) + "\">"
       "<label>Subnetzmaske</label><input name=mask placeholder='255.255.255.0' value=\"" + html_escape(cfg.mask) + "\">"
       "</fieldset><fieldset><legend>Admin</legend>"
       "<label>Aktuelles Admin-Passwort" + String(ap_mode ? " (= Hotspot-Passwort)" : "") +
       "</label><input name=admin type=password required>"
       "<label>Neues Admin-Passwort <small>(optional, min. 8 Zeichen)</small></label><input name=newadmin type=password>"
       "</fieldset><button>Speichern &amp; neu starten</button></form></body></html>";

  httpd_resp_set_type(req, "text/html");
  return httpd_resp_send(req, h.c_str(), h.length());
}

static esp_err_t setup_post_handler(httpd_req_t* req) {
  char body[1024] = {0};
  if (req->content_len >= sizeof(body)) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Formular zu gross");
    return ESP_FAIL;
  }
  size_t got = 0;
  while (got < req->content_len) {
    int r = httpd_req_recv(req, body + got, req->content_len - got);
    if (r == HTTPD_SOCK_ERR_TIMEOUT) continue;
    if (r <= 0) return ESP_FAIL;
    got += r;
  }

  String msg;
  String admin = form_field(body, "admin");
  if (admin != cfg.admin_pw) msg = "Falsches Admin-Passwort.";

  Settings n = cfg;
  n.ssid = form_field(body, "ssid");
  String pass = form_field(body, "pass");
  if (pass.length() || n.ssid != cfg.ssid) n.pass = pass;   // leer + gleiches WLAN = Passwort behalten
  n.ip = form_field(body, "ip");
  n.gw = form_field(body, "gw");
  n.mask = form_field(body, "mask");
  String newadmin = form_field(body, "newadmin");

  if (msg.isEmpty() && n.ssid.isEmpty()) msg = "WLAN-Name fehlt.";
  if (msg.isEmpty() && n.pass.length() && n.pass.length() < 8) msg = "WLAN-Passwoerter haben mindestens 8 Zeichen.";
  if (msg.isEmpty() && (n.ip.length() || n.gw.length() || n.mask.length())) {
    if (n.mask.isEmpty()) n.mask = "255.255.255.0";
    if (!valid_ip(n.ip) || !valid_ip(n.gw) || !valid_ip(n.mask))
      msg = "Feste IP: IP, Gateway und Maske muessen gueltige Adressen sein (oder alles leer).";
  }
  if (msg.isEmpty() && newadmin.length()) {
    if (newadmin.length() < 8) msg = "Neues Admin-Passwort braucht mindestens 8 Zeichen.";
    else n.admin_pw = newadmin;
  }

  String h = PAGE_HEAD;
  if (msg.length()) {
    h += "<h2>Nicht gespeichert</h2><p class=bad>" + msg + "</p><p><a href='/'>Zurueck</a></p></body></html>";
  } else {
    cfg = n;
    save_settings();
    h += "<h2>Gespeichert</h2><p>Die Kamera startet neu und verbindet sich mit <b>" + html_escape(cfg.ssid) + "</b>.</p>";
    if (cfg.ip.length())
      h += "<p>Danach erreichbar unter <b>http://" + cfg.ip + "</b></p>";
    else
      h += "<p>Die IP vergibt der Router. Du findest sie dort unter dem Namen <b>" HOSTNAME "</b> "
           "oder unter <b>http://" HOSTNAME ".local</b>.</p>";
    h += "<p>Klappt die Verbindung nicht, erscheint nach ca. 30 s wieder der Hotspot <b>" SETUP_AP_SSID "</b>.</p></body></html>";
    restart_at = millis() + 1500;
  }
  httpd_resp_set_type(req, "text/html");
  return httpd_resp_send(req, h.c_str(), h.length());
}

// ---------------- Firmware-Update ----------------
static const char UPDATE_PAGE[] = R"HTML(<h2>Firmware-Update</h2>
<p>Nur die Datei <b>esp32cam_stream.ino.bin</b> hochladen.</p>
<input type="file" id="f" accept=".bin">
<input type="password" id="pw" placeholder="Admin-Passwort">
<button onclick="go()">Hochladen</button>
<progress id="p" value="0" max="100"></progress>
<p id="msg"></p><p><a href="/">Zurueck</a></p>
<script>
function go(){
  var f=document.getElementById('f').files[0], m=document.getElementById('msg');
  if(!f){m.textContent='Keine Datei gewaehlt';return;}
  var x=new XMLHttpRequest();
  x.open('POST','/update');
  x.setRequestHeader('X-OTA-Password',document.getElementById('pw').value);
  x.setRequestHeader('Content-Type','application/octet-stream');
  x.upload.onprogress=function(e){document.getElementById('p').value=e.loaded/e.total*100;};
  x.onload=function(){m.textContent=x.status+': '+x.responseText;};
  x.onerror=function(){m.textContent='Verbindung abgebrochen (normal, falls gerade neu gestartet wird)';};
  x.send(f);
  m.textContent='Lade hoch ...';
}
</script></body></html>)HTML";

static esp_err_t update_page_handler(httpd_req_t* req) {
  String h = PAGE_HEAD;
  h += UPDATE_PAGE;
  httpd_resp_set_type(req, "text/html");
  return httpd_resp_send(req, h.c_str(), h.length());
}

static esp_err_t update_post_handler(httpd_req_t* req) {
  if (!check_pw_header(req)) {
    httpd_resp_set_status(req, "403 Forbidden");
    return httpd_resp_sendstr(req, "Falsches Passwort");
  }

  size_t total = req->content_len;
  Serial.printf("[ota] Update gestartet, %u Bytes\n", (unsigned)total);

  // Update.begin prueft selbst, ob es eine OTA-Partition gibt und ob die Datei passt.
  if (total == 0 || !Update.begin(total, U_FLASH)) {
    httpd_resp_set_status(req, "500 Internal Server Error");
    return httpd_resp_sendstr(req, total == 0 ? "Leere Datei" : Update.errorString());
  }

  static uint8_t buf[2048];  // static: nicht auf dem knappen Task-Stack
  size_t remaining = total;
  while (remaining > 0) {
    int r = httpd_req_recv(req, (char*)buf, remaining < sizeof(buf) ? remaining : sizeof(buf));
    if (r == HTTPD_SOCK_ERR_TIMEOUT) continue;
    if (r <= 0) {
      Update.abort();
      Serial.println("[ota] Verbindung abgebrochen, altes Programm bleibt aktiv");
      return ESP_FAIL;
    }
    // Update.write prueft am ersten Byte, ob das ueberhaupt ein ESP32-Programm ist
    if (Update.write(buf, r) != (size_t)r) {
      String err = Update.errorString();
      Update.abort();
      httpd_resp_set_status(req, "500 Internal Server Error");
      return httpd_resp_sendstr(req, err.c_str());
    }
    remaining -= r;
  }

  if (!Update.end(true)) {
    httpd_resp_set_status(req, "500 Internal Server Error");
    return httpd_resp_sendstr(req, Update.errorString());
  }

  Serial.println("[ota] Erfolgreich, Neustart");
  restart_at = millis() + 1000;
  return httpd_resp_sendstr(req, "OK, Kamera startet neu ...");
}

// Captive Portal: unbekannte Adressen (Handy prueft "Internet vorhanden?") -> Einrichtungsseite
static esp_err_t not_found_handler(httpd_req_t* req, httpd_err_code_t err) {
  if (!ap_mode) {
    httpd_resp_send_err(req, HTTPD_404_NOT_FOUND, "Nicht gefunden");
    return ESP_FAIL;
  }
  httpd_resp_set_status(req, "302 Found");
  httpd_resp_set_hdr(req, "Location", "http://192.168.4.1/");
  return httpd_resp_send(req, NULL, 0);
}

static void start_servers() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.recv_wait_timeout = 20;   // grosszuegig fuer Uploads ueber schwaches WLAN
  config.send_wait_timeout = 20;
  config.stack_size = 8192;        // Seiten werden als String gebaut
  config.max_uri_handlers = 10;
  config.lru_purge_enable = true;  // Handys halten viele Verbindungen offen

  config.server_port = 80;
  config.ctrl_port   = 32768;
  httpd_uri_t uris[] = {
    { .uri = "/",        .method = HTTP_GET,  .handler = root_handler,        .user_ctx = NULL },
    { .uri = "/setup",   .method = HTTP_POST, .handler = setup_post_handler,  .user_ctx = NULL },
    { .uri = "/update",  .method = HTTP_GET,  .handler = update_page_handler, .user_ctx = NULL },
    { .uri = "/update",  .method = HTTP_POST, .handler = update_post_handler, .user_ctx = NULL },
    { .uri = "/status",  .method = HTTP_GET,  .handler = status_handler,      .user_ctx = NULL },
    { .uri = "/capture", .method = HTTP_GET,  .handler = capture_handler,     .user_ctx = NULL },
  };
  if (httpd_start(&control_httpd, &config) == ESP_OK) {
    for (auto& u : uris) httpd_register_uri_handler(control_httpd, &u);
    httpd_register_err_handler(control_httpd, HTTPD_404_NOT_FOUND, not_found_handler);
  }

  // Port 81: Stream (eigener Server, weil der Stream-Handler den Task blockiert)
  httpd_config_t sconf = HTTPD_DEFAULT_CONFIG();
  sconf.server_port = 81;
  sconf.ctrl_port   = 32769;
  httpd_uri_t stream_uri = { .uri = "/stream", .method = HTTP_GET, .handler = stream_handler, .user_ctx = NULL };
  if (httpd_start(&stream_httpd, &sconf) == ESP_OK) {
    httpd_register_uri_handler(stream_httpd, &stream_uri);
  }
}

static bool init_camera() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer   = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM;  c.pin_d1 = Y3_GPIO_NUM;
  c.pin_d2 = Y4_GPIO_NUM;  c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM;  c.pin_d5 = Y7_GPIO_NUM;
  c.pin_d6 = Y8_GPIO_NUM;  c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM;
  c.pin_pclk = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM;
  c.pin_href = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM;
  c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM;
  c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;
  c.frame_size   = FRAME_SIZE;
  c.jpeg_quality = JPEG_QUALITY;
  c.fb_count     = 2;                    // Doppelpuffer: Sensor schreibt, waehrend gesendet wird
  c.fb_location  = CAMERA_FB_IN_PSRAM;
  c.grab_mode    = CAMERA_GRAB_LATEST;   // immer das neueste Bild = weniger Latenz

  if (!psramFound()) {
    Serial.println("[cam] Kein PSRAM gefunden! Reduziere auf VGA, 1 Puffer.");
    c.frame_size  = FRAMESIZE_VGA;
    c.fb_count    = 1;
    c.fb_location = CAMERA_FB_IN_DRAM;
  }

  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK) {
    Serial.printf("[cam] Init fehlgeschlagen: 0x%x\n", err);
    return false;
  }
  sensor_t* s = esp_camera_sensor_get();
  s->set_vflip(s, 0);
  s->set_hmirror(s, 0);
  return true;
}

static void scan_networks() {
  WiFi.mode(WIFI_STA);
  int n = WiFi.scanNetworks();
  scan_options = "";
  for (int i = 0; i < n && i < 15; i++) {
    String ss = WiFi.SSID(i);
    if (ss.length() && scan_options.indexOf("\"" + html_escape(ss) + "\"") < 0)
      scan_options += "<option value=\"" + html_escape(ss) + "\">";
  }
  WiFi.scanDelete();
}

static bool connect_wifi() {
  if (cfg.ssid.isEmpty()) return false;
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(HOSTNAME);
  if (cfg.ip.length()) {
    IPAddress ip, gw, mask;
    ip.fromString(cfg.ip); gw.fromString(cfg.gw); mask.fromString(cfg.mask);
    WiFi.config(ip, gw, mask, gw);   // DNS = Router
  }
  WiFi.setSleep(false);  // WICHTIG: Modem-Sleep aus, sonst Latenzspitzen von 100+ ms
  WiFi.begin(cfg.ssid.c_str(), cfg.pass.c_str());

  Serial.printf("[wifi] Verbinde mit %s", cfg.ssid.c_str());
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    Serial.print(".");
    if (millis() - t0 > 30000) {
      Serial.println("\n[wifi] Timeout");
      return false;
    }
  }
  Serial.printf("\n[wifi] Verbunden, IP: %s, RSSI: %d dBm\n",
                WiFi.localIP().toString().c_str(), WiFi.RSSI());
  if (MDNS.begin(HOSTNAME)) MDNS.addService("http", "tcp", 80);
  return true;
}

static void start_setup_ap() {
  scan_networks();
  WiFi.disconnect(true);
  WiFi.mode(WIFI_AP);
  WiFi.softAP(SETUP_AP_SSID, cfg.admin_pw.c_str());
  dns.start(53, "*", WiFi.softAPIP());   // alle Namen -> Kamera (Captive Portal)
  ap_mode = true;
  Serial.printf("[wifi] Einrichtungs-Hotspot: %s, Passwort = Admin-Passwort, http://%s/\n",
                SETUP_AP_SSID, WiFi.softAPIP().toString().c_str());
}

void setup() {
  Serial.begin(115200);
  Serial.println("\n[boot] Billard-Cam " FW_VERSION);

  pinMode(LED_FLASH_GPIO, OUTPUT);
  digitalWrite(LED_FLASH_GPIO, LOW);  // Blitz-LED aus (blendet sonst und wird heiss)

  load_settings();
  // Kamera-Fehler fuehrt NICHT zum Neustart: Einrichtung/Update muss erreichbar bleiben
  camera_ok = init_camera();

  if (!connect_wifi()) start_setup_ap();
  start_servers();
}

void loop() {
  static uint32_t lost_since = 0;
  static uint32_t ap_since = millis();

  if (restart_at && (int32_t)(millis() - restart_at) >= 0) ESP.restart();

  if (ap_mode) {
    dns.processNextRequest();
    // Ist schon ein WLAN gespeichert (z. B. Router war nur aus), alle 5 min neu versuchen,
    // aber nicht, solange jemand mit dem Hotspot verbunden ist oder ein Update laeuft.
    if (cfg.ssid.length() && millis() - ap_since > 300000 &&
        WiFi.softAPgetStationNum() == 0 && !Update.isRunning())
      ESP.restart();
    delay(10);
    return;
  }

  if (WiFi.status() != WL_CONNECTED) {
    // WLAN-Watchdog: bei Verbindungsverlust neu starten statt tot herumzuhaengen
    if (lost_since == 0) lost_since = millis();
    if (millis() - lost_since > 30000 && !Update.isRunning()) ESP.restart();
  } else {
    lost_since = 0;
  }
  delay(100);
}
