# Viewer: ESP32-CAM-Stream -> HDMI des Raspberry Pi (KMS/DRM, ohne Desktop)
# Debian-Pakete statt pip: fertig fuer ARM gebaut, SDL2 mit KMS/DRM-Unterstuetzung.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pygame python3-opencv python3-numpy \
        libgl1-mesa-dri libegl1 libgles2 libgbm1 libdrm2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY stream_reader.py viewer.py fake_cam.py ./

ENV PYTHONUNBUFFERED=1 \
    SDL_VIDEODRIVER=kmsdrm \
    CAM_URL=http://192.168.178.50:81/stream

CMD ["python3", "viewer.py"]
