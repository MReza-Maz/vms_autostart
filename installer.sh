#!/bin/bash
set -euo pipefail

APP_DIR="/opt/vms_autostart"
LOG_DIR="/var/log/vms_autostart"
CRON_FILE="/etc/cron.d/vms_autostart"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Run this installer as root."
    exit 1
fi

# This release supports Ubuntu 24.04 LTS only.
if [ ! -f /etc/os-release ]; then
    echo "ERROR: Cannot identify the operating system."
    exit 1
fi

. /etc/os-release

if [ "${ID:-}" != "ubuntu" ] || [ "${VERSION_ID:-}" != "24.04" ]; then
    echo "ERROR: This release supports Ubuntu 24.04 LTS only."
    echo "Detected: ${PRETTY_NAME:-unknown}"
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive

echo "==> Ubuntu 24.04 LTS detected"
echo "==> Updating APT package lists"
apt-get update

echo "==> Ensuring Ubuntu Universe repository is available"
apt-get install -y software-properties-common
add-apt-repository -y universe
apt-get update

echo "==> Installing required packages"
apt-get install -y docker.io docker-compose-v2 util-linux ca-certificates cron

echo "==> Enabling Docker and cron"
systemctl enable --now docker
systemctl enable --now cron

echo "==> Checking Docker Compose"
docker compose version

echo "==> Creating application directories"
mkdir -p "$APP_DIR/state" "$LOG_DIR"

echo "==> Installing application files"
for file in docker-compose.yml vms_autostart.py config.json vcenter.pass run.sh; do
    if [ ! -f "$SCRIPT_DIR/$file" ]; then
        echo "ERROR: Missing required file: $file"
        exit 1
    fi
    cp "$SCRIPT_DIR/$file" "$APP_DIR/$file"
done

chmod 750 "$APP_DIR" "$APP_DIR/run.sh"
chmod 644 "$APP_DIR/docker-compose.yml" "$APP_DIR/vms_autostart.py" "$APP_DIR/config.json"
chmod 600 "$APP_DIR/vcenter.pass"
chmod 755 "$LOG_DIR"

echo "==> Validating Docker Compose configuration"
cd "$APP_DIR"
docker compose config >/dev/null

echo "==> Installing cron jobs"
cat > "$CRON_FILE" <<EOF
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# vCenter VM auto-start transition check every 30 minutes.
*/30 * * * * root /usr/bin/flock -n /run/vms_autostart_power.lock $APP_DIR/run.sh power

# Daily VM snapshot and count-based retention at 02:00.
0 2 * * * root /usr/bin/flock -n /run/vms_autostart_snapshot.lock $APP_DIR/run.sh snapshot
EOF
chmod 644 "$CRON_FILE"
systemctl restart cron

echo
echo "=============================================="
echo "vms_autostart installation completed"
echo "OS: Ubuntu 24.04 LTS"
echo "Docker Compose: $(docker compose version)"
echo "Application: $APP_DIR"
echo "Log: $LOG_DIR/vms_autostart.log"
echo "=============================================="
echo
echo "The installer does not build or load the Docker image."
echo "Load the required image separately with:"
echo "  docker load -i vms_autostart_2.1.tar"
echo
echo "Test commands:"
echo "  cd $APP_DIR"
echo "  ./run.sh power"
echo "  ./run.sh snapshot"
