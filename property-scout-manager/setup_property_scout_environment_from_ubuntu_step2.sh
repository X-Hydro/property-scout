#!/bin/bash
set -e
set -x

# Define directories
USER_HOME="/home/oncoordadmin"
DOCKER_DIR="$USER_HOME/docker/property-scout"
MANAGER_DIR="$USER_HOME/docker/property-scout/property-scout-manager"
IMG_DIR="$DOCKER_DIR/images"

# Confirm the image tar exists before touching the running service
if [ ! -f "$IMG_DIR/property-scout-manager.tar" ]; then
  echo "❌ File not found: $IMG_DIR/property-scout-manager.tar"
  exit 1
fi

# Stop the existing service before loading the new image
echo "Stopping existing PropertyScout Manager service..."
cd "$MANAGER_DIR"
docker-compose stop property-scout-manager
docker image prune -f

echo "Loading PropertyScout Manager Docker image..."
docker load -i "$IMG_DIR/property-scout-manager.tar"
docker-compose rm -f property-scout-manager


echo "setup_property_scout_environment_from_ubuntu_step2.sh complete."
echo running: docker-compose --env-file ./.env up -d property-scout-manager
docker-compose --env-file ./.env up -d property-scout-manager

# Give the container a moment to either come up cleanly or crash (e.g. the
# placeholder-resolution failure we hit when ADMIN_SECRET_TOKEN was missing)
# before checking its logs.
echo "Waiting for property-scout-manager to start..."
sleep 10

echo "----- property-scout-manager: last 50 log lines -----"
docker-compose logs --tail=50 property-scout-manager
echo "----- property-scout-manager: container status -----"
docker-compose ps property-scout-manager
curl -i http://127.0.0.1:8061/health