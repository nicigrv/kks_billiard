# Billard-System: Erkennung + Scoreboard (HDMI) + Admin-Portal
# ffmpeg: Videos der Spielaufzeichnung (H.264, laeuft auf iPad/iPhone)
# Debian-Pakete statt pip: fertig fuer ARM gebaut, SDL2 mit KMS/DRM-Unterstuetzung.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pygame python3-opencv python3-numpy ffmpeg \
        libgl1-mesa-dri libegl1 libgles2 libgbm1 libdrm2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY *.py admin.html ./
COPY sim/ ./sim/

ENV PYTHONUNBUFFERED=1 \
    SDL_VIDEODRIVER=kmsdrm \
    DATA_DIR=/data

EXPOSE 8080
EXPOSE 5005/udp
CMD ["python3", "main.py"]
