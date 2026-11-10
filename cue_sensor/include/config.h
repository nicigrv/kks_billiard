// Einstellungen fuer das Queue-Gyroskop.
// WLAN und Castcode werden NICHT hier eingetragen: Beim ersten Start oeffnet der Queue den
// Hotspot "Billard-Queue-...", dort WLAN waehlen, Castcode im Admin-Portal eintragen, fertig.
#pragma once

#define FW_VERSION       "2.0.0"
#define SETUP_AP_PREFIX  "Billard-Queue-"   // Hotspot-Name, dahinter die ersten 4 Zeichen des Castcodes
#define API_PORT         80                 // HTTP-API, ueber die der Pi die Lage abfragt
#define DISCOVERY_PORT   5005               // Pi sucht den Queue per UDP-Broadcast {"suche": "<castcode>"}
#define CASTCODE_LEN     6

#define SAMPLE_HZ        200                // Sensor-Abtastrate
#define CONNECT_TIMEOUT_MS 20000            // so lange wird das WLAN versucht, sonst Hotspot
#define PORTAL_RETRY_MS  300000             // im Hotspot: nach 5 min neu starten und WLAN erneut versuchen

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
