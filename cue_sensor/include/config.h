// Einstellungen fuer das Queue-Gyroskop. Vor dem Hochladen anpassen.
#pragma once

#define WIFI_SSID      "MeinWLAN"        // <- WLAN, in dem auch der Pi haengt
#define WIFI_PASSWORD  "geheim"          // <-
#define DEVICE_ID      "koe1"            // Name des Queues (bei mehreren Queues unterschiedlich)

#define UDP_PORT       5005              // muss zu CUE_PORT in docker-compose.yml passen
// Ziel: leer = Broadcast an alle im WLAN (kein Pi-IP noetig). Sonst z. B. "192.168.88.10"
#define TARGET_IP      ""

#define SEND_HZ        30                // Pakete pro Sekunde
#define SAMPLE_HZ      200               // Sensor-Abtastrate

// Einbaulage: Die X-Achse des MPU6050 (Pfeil auf der Platine) muss in Richtung
// Queue-SPITZE zeigen. Liegt die Platine andersherum, AXIS_SIGN_X auf -1 setzen.
#define AXIS_SIGN_X    1

// Akku-Spannung messen (Spannungsteiler 1:2 an einen ADC-Pin). -1 = keine Messung.
#ifndef BAT_PIN
#define BAT_PIN        -1
#endif
#define BAT_DIVIDER    2.0f

#ifndef SDA_PIN
#define SDA_PIN        21
#endif
#ifndef SCL_PIN
#define SCL_PIN        22
#endif
