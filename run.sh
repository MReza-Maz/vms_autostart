#!/bin/bash
set -u

APP_DIR="/opt/vms_autostart"
LOG_FILE="/var/log/vms_autostart/vms_autostart.log"
IMAGE="vms_autostart:2.1"

cd "$APP_DIR" || exit 1

MODE="${1:-}"
case "$MODE" in
    power|snapshot)
        ;;
    *)
        echo "Usage: $0 [power|snapshot]" >&2
        exit 2
        ;;
esac

mkdir -p "$(dirname "$LOG_FILE")"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Docker job: $MODE" >> "$LOG_FILE"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "ERROR: Docker image $IMAGE is not loaded." >> "$LOG_FILE"
    echo "Load it with: docker load -i vms_autostart_2.1.tar" >> "$LOG_FILE"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Docker job finished with exit code: 1" >> "$LOG_FILE"
    echo "---------------------------------------------------------------------" >> "$LOG_FILE"
    exit 1
fi

if ! docker compose config >/dev/null 2>&1; then
    echo "ERROR: Docker Compose configuration is invalid." >> "$LOG_FILE"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Docker job finished with exit code: 1" >> "$LOG_FILE"
    echo "---------------------------------------------------------------------" >> "$LOG_FILE"
    exit 1
fi

docker compose run --pull never --rm vms-autostart "$MODE" >> "$LOG_FILE" 2>&1
EXIT_CODE=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Docker job finished with exit code: $EXIT_CODE" >> "$LOG_FILE"
echo "---------------------------------------------------------------------" >> "$LOG_FILE"
exit "$EXIT_CODE"
