FROM debian:bookworm-slim

LABEL org.opencontainers.image.title="LipLock" \
      org.opencontainers.image.description="Sync and mux audio tracks between releases of the same film" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-numpy python3-flask python3-waitress ffmpeg mkvtoolnix tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV LANG=C.UTF-8 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    PORT=47650 \
    CONFIG_DIR=/config \
    WATCH_DIR=/watch \
    OUTPUT_DIR=/output \
    MEDIA_DIRS=/media,/media2 \
    UI_LANG=ru \
    PUID=99 PGID=100 UMASK=000

COPY app /app
WORKDIR /app
EXPOSE 47650
VOLUME ["/config"]
CMD ["python3", "/app/server.py"]
