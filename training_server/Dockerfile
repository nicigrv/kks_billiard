# Billard-Training im Netz: Python-Standardbibliothek + Pillow/numpy (Einzelkugel-Erkennung)
# + OpenCV (Farben lernen fuer den Pi, kugel_merkmale.py)
# + pygame/ffmpeg (Videos der Spielaufzeichnung, gleiche Grafik wie der Beamer), Daten im Volume /data
# ballnum/geometry/notices/planner/scene/replay/kugel_merkmale.py sind Kopien aus app/ (scripts/kopien.sh).
FROM python:3.12-slim-bookworm
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-freefont-ttf fontconfig \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pillow==11.* numpy==2.* opencv-python-headless==4.10.0.84 pygame==2.6.*
COPY server.py einzelkugel.py modelle.py aufzeichnung.py kugel_merkmale.py start_merkmale.json \
     ballnum.py geometry.py notices.py planner.py scene.py replay.py \
     trainieren.html admin.html notify.js ui.css ./
RUN useradd -u 10001 -M -d /app billard && mkdir /data && chown 10001 /data
USER 10001
# pygame ohne Bildschirm; Schrift-Cache im beschreibbaren /tmp (Container ist sonst schreibgeschuetzt)
ENV DATA_DIR=/data PORT=8000 PYTHONUNBUFFERED=1 SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy \
    PYGAME_HIDE_SUPPORT_PROMPT=1 XDG_CACHE_HOME=/tmp
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --start-interval=2s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/gesund')"
CMD ["python3", "server.py"]
