#!/bin/bash
# LipLock — build the image locally on Unraid and run it, without GitHub.
# Usage:  bash /mnt/user/appdata/liplock-src/scripts/install-local.sh
# Re-running rebuilds the image and recreates the container; settings and drafts are kept.
#
# Prefer the Unraid template (templates/liplock.xml) if the image is published on GHCR.

# ============================ SETTINGS — edit these ============================
LIBRARY="/mnt/user/media"                      # shown in the UI as /media
LIBRARY2=""                                    # optional, shown as /media2 (e.g. /mnt/crap_download)
WATCH_DIR="/mnt/user/media/liplock/input"      # main/ + donors/ per film
OUTPUT_DIR="/mnt/user/media/liplock/output"
CONFIG_DIR="/mnt/user/appdata/liplock"
PORT=47650
UI_LANG=ru                                     # en | ru
TRACK_ORDER="rus,eng"
PUID=99 PGID=100 UMASK=000
# ===============================================================================

set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"

for p in "$LIBRARY" "$WATCH_DIR" "$OUTPUT_DIR" "$CONFIG_DIR" ${LIBRARY2:+"$LIBRARY2"}; do
    top="$(echo "$p" | cut -d/ -f1-3)"
    if [ ! -d "$top" ]; then
        echo "Error: $top does not exist. Fix the paths at the top of this script." >&2
        exit 1
    fi
done

mkdir -p "$WATCH_DIR" "$OUTPUT_DIR" "$CONFIG_DIR"
chown "$PUID:$PGID" "$WATCH_DIR" "$OUTPUT_DIR" "$CONFIG_DIR"

echo "==> Building the image"
docker build -t liplock:local "$SRC"

echo "==> Recreating the container"
docker rm -f liplock >/dev/null 2>&1 || true
EXTRA=()
[ -n "$LIBRARY2" ] && EXTRA+=(-v "$LIBRARY2:/media2")

docker run -d --name liplock --restart unless-stopped \
    -p "$PORT:47650" \
    -e TZ="${TZ:-Europe/London}" -e PUID="$PUID" -e PGID="$PGID" -e UMASK="$UMASK" \
    -e UI_LANG="$UI_LANG" -e TRACK_ORDER="$TRACK_ORDER" \
    -v "$LIBRARY:/media" "${EXTRA[@]}" \
    -v "$WATCH_DIR:/watch" -v "$OUTPUT_DIR:/output" -v "$CONFIG_DIR:/config" \
    -l net.unraid.docker.webui="http://[IP]:[PORT:$PORT]/" \
    liplock:local >/dev/null

docker image prune -f >/dev/null 2>&1 || true
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Done! Open http://${IP:-<server-ip>}:$PORT"
