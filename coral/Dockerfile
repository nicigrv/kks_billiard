# Coral USB Accelerator: Kugelmodell ausfuehren (coral_server.py) + Hardware-Test (check_coral.py).
# Debian 11 (bullseye) mit Python 3.9, weil Googles Coral-Pakete (pycoral) nur dafuer
# fertig gebaut sind. Neuere Python-Versionen sind mit Coral ein bekanntes Minenfeld.
FROM debian:bullseye-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg usbutils python3 python3-numpy \
    && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
        | gpg --dearmor -o /usr/share/keyrings/coral.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/coral.gpg] https://packages.cloud.google.com/apt coral-edgetpu-stable main" \
        > /etc/apt/sources.list.d/coral.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        libedgetpu1-std python3-pycoral \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /models && curl -fsSL -o /models/mobilenet_v2_1.0_224_quant_edgetpu.tflite \
    https://github.com/google-coral/test_data/raw/master/mobilenet_v2_1.0_224_quant_edgetpu.tflite

WORKDIR /app
COPY check_coral.py coral_server.py ./
ENV PYTHONUNBUFFERED=1
CMD ["python3", "coral_server.py"]
