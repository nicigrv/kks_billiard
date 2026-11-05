# Billard-System: Erkennung + Scoreboard (HDMI) + Admin-Portal
# ffmpeg: Videos der Spielaufzeichnung (H.264, laeuft auf iPad/iPhone)
# Debian-Pakete statt pip: fertig fuer ARM gebaut, SDL2 mit KMS/DRM-Unterstuetzung.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pygame python3-opencv python3-numpy ffmpeg \
        libgl1-mesa-dri libegl1 libgles2 libgbm1 libdrm2 \
    && rm -rf /var/lib/apt/lists/*

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
COPY *.py admin.html beamer.html ./
COPY sim/ ./sim/

ENV PYTHONUNBUFFERED=1 \
    SDL_VIDEODRIVER=kmsdrm \
    DATA_DIR=/data

EXPOSE 8080
EXPOSE 5005/udp
CMD ["python3", "main.py"]
