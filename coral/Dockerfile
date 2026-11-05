# Coral USB Accelerator: Kugelmodell ausfuehren (coral_server.py) + Hardware-Test (check_coral.py).
# Debian 11 (bullseye) mit Python 3.9, weil Googles Coral-Pakete (pycoral) nur dafuer
# fertig gebaut sind. Neuere Python-Versionen sind mit Coral ein bekanntes Minenfeld.
FROM debian:bullseye-slim

# Schritt 1: Grundpakete von Debian.
# Debian 11 ist seit Ende August 2026 aus der Langzeitpflege; die Pakete wandern dann
# von den normalen Servern nach archive.debian.org. Klappt der normale Weg nicht,
# wird automatisch das Archiv genommen (dessen Signaturen sind gueltig, nur "abgelaufen").
RUN if apt-get update; then \
        echo "Debian-Pakete: normale Server"; \
    else \
        echo "Debian 11 nicht mehr auf den normalen Servern - nehme archive.debian.org"; \
        printf '%s\n' \
            "deb http://archive.debian.org/debian bullseye main" \
            "deb http://archive.debian.org/debian-security bullseye-security main" \
            > /etc/apt/sources.list; \
        echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99archiv; \
        apt-get update; \
    fi \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg usbutils python3 python3-numpy \
    && rm -rf /var/lib/apt/lists/*

# Schritt 2: Googles Coral-Pakete (Treiber + pycoral)
RUN curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
        | gpg --dearmor -o /usr/share/keyrings/coral.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/coral.gpg] https://packages.cloud.google.com/apt coral-edgetpu-stable main" \
        > /etc/apt/sources.list.d/coral.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends libedgetpu1-std python3-pycoral \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /models && curl -fsSL -o /models/mobilenet_v2_1.0_224_quant_edgetpu.tflite \
    https://github.com/google-coral/test_data/raw/master/mobilenet_v2_1.0_224_quant_edgetpu.tflite

WORKDIR /app
COPY check_coral.py coral_server.py ./
ENV PYTHONUNBUFFERED=1
CMD ["python3", "coral_server.py"]
