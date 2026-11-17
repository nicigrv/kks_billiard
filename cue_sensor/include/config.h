// Einstellungen fuer das Queue-Gyroskop.
// WLAN und Castcode werden NICHT hier eingetragen: Beim ersten Start oeffnet der Queue den
// Hotspot "Billard-Queue-...", dort WLAN waehlen, Castcode im Admin-Panel eintragen, fertig.
#pragma once

#define FW_VERSION       "3.2.0"
#define SETUP_AP_PREFIX  "Billard-Queue-"   // Hotspot-Name, dahinter die ersten 4 Zeichen des Castcodes
#define API_PORT         80                 // HTTP-API, ueber die der Pi die Lage abfragt
#define DISCOVERY_PORT   5005               // Pi sucht den Queue per UDP-Broadcast {"suche": "<castcode>"}
                                            // und abonniert dort die Lage {"abo": "<castcode>"}
#define ANGLE_INTERVAL_MS 20                // Lage alle 20 ms aus dem Filter lesen und pruefen ...
#define AVG_COUNT        3                  // ... und je 3 davon gemittelt senden (ca. 17x pro Sekunde)
#define OUTLIER_DEG      25.0f              // weicht ein Wert so weit von den anderen ab: Ausreisser, verworfen
#define ABO_TIMEOUT_MS   3000               // so lange ohne Lebenszeichen vom Pi -> nicht verbunden (LED rot)
#define CASTCODE_LEN     6

#define SAMPLE_HZ        200                // Sensor-Abtastrate

// Stossstaerke (frueher eigenes "Koe-Messgeraet"): Spitzenwert der Beschleunigung je Stoss.
// Ein Stoss beginnt, wenn die Beschleunigung mehr als STROKE_START_G von 1 g (Ruhe) abweicht,
// und endet STROKE_END_MS danach. Der Spitzenwert geht an den Pi und auf die LED-Matrix.
#define STROKE_START_G   1.5f
#define STROKE_END_MS    300
#define STROKE_SHOW_MS   2000               // so lange zeigt die LED-Matrix den Balken
#define CONNECT_TIMEOUT_MS 20000            // so lange wird das WLAN versucht, sonst Hotspot
#define PORTAL_RETRY_MS  300000             // im Hotspot: nach 5 min neu starten und WLAN erneut versuchen
#define WIFI_LOST_MS     60000              // im Betrieb so lange kein WLAN: neu starten -> ggf. Hotspot

// Einbaulage: Die X-Achse des MPU6050 (Pfeil auf der Platine) muss in Richtung
// Queue-SPITZE zeigen. Liegt die Platine andersherum, AXIS_SIGN_X auf -1 setzen.
#define AXIS_SIGN_X      1

// Akku-Spannung messen (Spannungsteiler 1:2 an einen ADC-Pin). -1 = keine Messung.
#ifndef BAT_PIN
#define BAT_PIN          -1
#endif
#define BAT_DIVIDER      2.0f

#ifndef SDA_PIN
#define SDA_PIN          21
#endif
#ifndef SCL_PIN
#define SCL_PIN          22
#endif

// RGB-LED (WS2812/SK6812) und Knopf. -1 = nicht vorhanden. Die M5Stack-Umgebungen in
// platformio.ini setzen das passend (Atom Lite/Matrix: LED G27, Knopf G39; AtomS3 Lite: LED G35, Knopf G41).
#ifndef LED_PIN
#define LED_PIN          -1
#endif
#ifndef LED_COUNT
#define LED_COUNT        1
#endif
#ifndef LED_BRIGHTNESS
#define LED_BRIGHTNESS   60                 // 0-255; Atom Matrix (25 LEDs) dunkler, sonst wird er warm
#endif
#ifndef BTN_PIN
#define BTN_PIN          -1
#endif
