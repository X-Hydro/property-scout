#!/bin/bash
# Build property-scout-manager, package it as a Docker image, and save a tar
# for upload to the VM.
#
#   ./buildAndDeployImage.sh          build + save tar
#   ./buildAndDeployImage.sh --run    build + save tar + start locally for testing

set -e
set -x # make it verbose

PROJECT_DIR=/c/dev/property-scout/property-scout-manager
IMAGE=property-scout-manager
TAR_DIR=d:/images

cd "$PROJECT_DIR" || { echo "❌ Failed to change directory to $PROJECT_DIR"; exit 1; }

# Step 1: make sure the application will build
mvn.cmd clean install -DskipTests=true

# Step 2: Remove old image
echo "🧹 Removing old image '$IMAGE' (if exists)..."
docker rmi -f "$IMAGE" 2>/dev/null || echo "No existing image to remove."

# Step 3: Build new image
echo "🔨 Building new image $IMAGE..."
docker build -t "$IMAGE" .

# Step 4: Save the image for uploading to the cloud
mkdir -p "$TAR_DIR"
docker save -o "$TAR_DIR/$IMAGE.tar" "$IMAGE:latest"

# Step 5 (optional): start locally against the local Postgres container
if [[ "$1" == "--run" ]]; then
  echo "🚀 Starting $IMAGE locally..."
  docker compose -f docker-compose.local.yml down
  docker compose --env-file .env_local -f docker-compose.local.yml up -d
  docker compose -f docker-compose.local.yml logs -f
fi

echo "✅ Done! Image saved to $TAR_DIR/$IMAGE.tar"