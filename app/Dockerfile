# Billard-System: Kugelerkennung (KI-Chip), Scoreboard (HDMI) + Admin-Portal
#
# Schlank fuer den Pi:
#  - pygame/SDL2 aus Debian (KMS/DRM fuer den Beamer), numpy aus Debian
#  - OpenCV als "headless"-Paket (nur Bildfunktionen). Das Debian-Paket python3-opencv zieht
#    Qt, VTK, GDAL & Co. nach und laedt alles beim Start in den Speicher.
#  - ffmpeg: Videos der Spielaufzeichnung (H.264, laeuft auf iPad/iPhone)
#  - alsa-utils (aplay): Ding-Ton ueber HDMI
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pygame python3-numpy python3-pip ffmpeg ca-certificates curl libusb-1.0-0 alsa-utils \
        libgl1-mesa-dri libegl1 libgles2 libgbm1 libdrm2 \
    && pip3 install --no-cache-dir --no-deps --break-system-packages opencv-python-headless==4.10.0.84 \
    && rm -rf /var/lib/apt/lists/*

# KI-Chip (Coral USB Accelerator): Treiber libedgetpu + tflite_runtime fuer Python 3.11.
# Googles eigene Paketquelle gibt es nicht mehr; das sind die gepflegten Builds von
# github.com/feranick (beide gegen TensorFlow 2.17.1 gebaut, passen also zusammen).
# Pruefsummen festgelegt: eine veraenderte Datei bricht den Bau ab.
ARG CORAL=1
RUN if [ "$CORAL" = "1" ] && [ "$(dpkg --print-architecture)" = "arm64" ]; then \
        cd /tmp \
        && curl -fsSL -o libedgetpu1-std.deb \
            https://github.com/feranick/libedgetpu/releases/download/16.0TF2.17.1-1/libedgetpu1-std_16.0tf2.17.1-1.bookworm_arm64.deb \
        && echo "f05bd2b2fd03f376495ecc2290dd6c5e0fcc147b03744dacc411499fdde1d690  libedgetpu1-std.deb" | sha256sum -c - \
        && curl -fsSL -o tflite_runtime-2.17.1-cp311-cp311-linux_aarch64.whl \
            https://github.com/feranick/TFlite-builds/releases/download/v2.17.1/tflite_runtime-2.17.1-cp311-cp311-linux_aarch64.whl \
        && echo "dd3768be23a44eb59b59b686cce943a1eef62c0160911fec4d3251d9c8ba97ab  tflite_runtime-2.17.1-cp311-cp311-linux_aarch64.whl" | sha256sum -c - \
        && dpkg -i libedgetpu1-std.deb \
        && pip3 install --no-cache-dir --no-deps --break-system-packages tflite_runtime-2.17.1-cp311-cp311-linux_aarch64.whl \
        && rm -f /tmp/*.deb /tmp/*.whl \
        && echo "KI-Chip: Treiber + tflite_runtime installiert"; \
    else echo "KI-Chip-Software gibt es hier nur fuer den Raspberry Pi (arm64) - ohne Coral keine Erkennung"; \
    fi

# Pi-Kameramodul (Flachbandkabel): picamera2/libcamera gibt es nur in der Paketquelle von
# Raspberry Pi. Klappt das nicht (z. B. auf einem PC), wird der Bau trotzdem fertig - dann
# gehen nur ESP32-CAM, USB-Kamera und Demo. Abschalten: PI_CAMERA=0 in docker-compose.yml.
ARG PI_CAMERA=1
RUN if [ "$PI_CAMERA" = "1" ]; then ( \
        apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
        && curl -fsSL https://archive.raspberrypi.com/debian/raspberrypi.gpg.key \
            | gpg --dearmor -o /usr/share/keyrings/raspberrypi.gpg \
        && echo "deb [signed-by=/usr/share/keyrings/raspberrypi.gpg] http://archive.raspberrypi.com/debian/ bookworm main" \
            > /etc/apt/sources.list.d/raspi.list \
        && apt-get update && apt-get install -y --no-install-recommends python3-picamera2 \
        && echo "Pi-Kameramodul: picamera2 installiert" \
    ) || (rm -f /etc/apt/sources.list.d/raspi.list; echo "Pi-Kameramodul: picamera2 NICHT verfuegbar"); \
    fi; rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY *.py *.html ./
COPY models/ ./models/
COPY sim/ ./sim/

# MALLOC_ARENA_MAX: Python mit mehreren Threads reserviert sonst je Thread eigenen Speicher
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MALLOC_ARENA_MAX=2 \
    SDL_VIDEODRIVER=kmsdrm \
    DATA_DIR=/data

EXPOSE 8080
EXPOSE 5005/udp
CMD ["python3", "main.py"]
