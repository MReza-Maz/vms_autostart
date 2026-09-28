#!/bin/bash
set -euo pipefail

IMAGE="vms_autostart:2.1"
TAR="vms_autostart_2.1.tar"

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: Docker is not installed."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "==> Building $IMAGE"
docker build -t "$IMAGE" .

echo "==> Verifying image"
docker image inspect "$IMAGE" >/dev/null

echo "==> Saving $IMAGE to $TAR"
docker save -o "$TAR" "$IMAGE"

echo "==> Build completed"
ls -lh "$TAR"
