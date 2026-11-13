# Billard-Training im Netz: nur Python-Standardbibliothek, Daten im Volume /data
FROM python:3.12-slim-bookworm
WORKDIR /app
COPY server.py trainieren.html admin.html notify.js ./
RUN useradd -u 10001 -M -d /app billard && mkdir /data && chown 10001 /data
USER 10001
ENV DATA_DIR=/data PORT=8000 PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --start-interval=2s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/gesund')"
CMD ["python3", "server.py"]
