#!/bin/bash

set -e
#set -x #make it verbose

# Set variables
SSH_KEY="$HOME/.ssh/pelias_id_rsa"
USER="oncoordadmin"
LOCAL_IMAGE_PATH="d:/images"
LOCAL_POSTGRES_PATH="/c/dev/oncoord-platform/oncoord-postgres"
REMOTE_HOME="/home/oncoordadmin"
REMOTE_IMAGE_PATH="$REMOTE_HOME/docker/images"
REMOTE_DOCKER_PROPERTY_SCOUT_PATH="$REMOTE_HOME/docker/property-scout"
REMOTE_IMAGE_PATH="$REMOTE_DOCKER_PROPERTY_SCOUT_PATH/images"

LOCAL_DATA_DIR="c:/data/property-scout"
REMOTE_DATA_DIR="/data/property-scout"

ZIP_EXE="/c/Program Files/7-Zip/7z.exe"

# Read IP from environment variable
if [ -z "$AZURE_VM_IP" ]; then
  echo "❌ Error: AZURE_VM_IP environment variable is not set."
  echo "➡️  Please set it like: export AZURE_VM_IP=4.151.234.176"
  exit 1
fi
HOST="$AZURE_VM_IP"
REMOTE_USER_HOST="$USER@$HOST"

ssh -i $SSH_KEY oncoordadmin@$HOST "mkdir -p" $REMOTE_DOCKER_PROPERTY_SCOUT_PATH/property-scout-manager
ssh -i $SSH_KEY oncoordadmin@$HOST "mkdir -p" $REMOTE_DOCKER_PROPERTY_SCOUT_PATH/property-scout-postgres

#################### Set up the postgres ####################

scp -i $SSH_KEY $LOCAL_POSTGRES_PATH/docker-compose.yml.cloud oncoordadmin@$HOST:$REMOTE_DOCKER_PROPERTY_SCOUT_PATH/property-scout-postgres/docker-compose.yml
scp -i $SSH_KEY $LOCAL_POSTGRES_PATH/.env_cloud oncoordadmin@$HOST:$REMOTE_DOCKER_PROPERTY_SCOUT_PATH/property-scout-postgres/.env



exit

#################### Set up the manager ####################
#files/setup required for property-scout manager
ssh -i $SSH_KEY oncoordadmin@$HOST "mkdir -p $REMOTE_DOCKER_PROPERTY_SCOUT_PATH/images"
scp -i $SSH_KEY ./docker-compose.yml.cloud oncoordadmin@$HOST:$REMOTE_DOCKER_PROPERTY_SCOUT_PATH/docker-compose.yml
scp -i $SSH_KEY ./src/main/resources/application.properties oncoordadmin@$HOST:$REMOTE_DOCKER_PROPERTY_SCOUT_PATH
#scp -i $SSH_KEY ./.env_cloud_test oncoordadmin@$HOST:$REMOTE_DOCKER_PROPERTY_SCOUT_PATH/.env
scp -i $SSH_KEY ./.env_cloud_prod oncoordadmin@$HOST:$REMOTE_DOCKER_PROPERTY_SCOUT_PATH/.env

scp -i $SSH_KEY setup_property_scout_environment_from_ubuntu_step2.sh oncoordadmin@$HOST:$REMOTE_DOCKER_PROPERTY_SCOUT_PATH

#exit


REMOTE_FILE="$REMOTE_DATA_DIR/auctionscout-manage.db"
REMOTE_SIZE=$(ssh -i "$SSH_KEY" oncoordadmin@"$HOST" "stat -c%s '$REMOTE_FILE' 2>/dev/null || echo 0")
if [ "$REMOTE_SIZE" -gt 0 ]; then
  echo "Remote file exists and is non-zero ($REMOTE_SIZE bytes) — skipping copy."
else
  scp -i "$SSH_KEY" "$LOCAL_DATA_DIR/auctionscout-manage.db" oncoordadmin@"$HOST:$REMOTE_DATA_DIR"
fi

#exit

declare -A FILES
# List of image files to transfer
FILES["property-scout-manager.tar"]="$REMOTE_IMAGE_PATH"

# Upload each tar file using scp
echo "Uploading tar/image files..."

for file in "${!FILES[@]}"; do
  LOCAL_PATH="$LOCAL_IMAGE_PATH/$file"
  REMOTE_DIR="${FILES[$file]}"
  REMOTE_PATH="$REMOTE_DIR/$file"
  USER_HOST_REMOTE_PATH="$USER@$HOST:$REMOTE_PATH"

  LOCAL_SIZE=$(stat -c%s "$LOCAL_PATH")

  REMOTE_SIZE=$(ssh -i "$SSH_KEY" "$REMOTE_USER_HOST" "
    if [ -f '$REMOTE_PATH' ]; then
      stat --format=%s '$REMOTE_PATH'
    else
      echo 0
    fi
  " 2>/dev/null)

  echo "Local Size: $LOCAL_SIZE, Remote Size: $REMOTE_SIZE, Remote Path: $REMOTE_PATH"

  #if [ "$LOCAL_SIZE" == "$REMOTE_SIZE" ]; then
  #  echo "✅ $file already exists with same size ($LOCAL_SIZE bytes). Skipping..."
  #  continue
  #fi

  for attempt in {1..5}; do
    scp -o ServerAliveInterval=60 -i "$SSH_KEY" "$LOCAL_PATH" "$USER_HOST_REMOTE_PATH" && break
    echo "⚠️ Attempt $attempt failed for $file. Retrying in 5 seconds..."
    sleep 5
  done

  if [ "$attempt" -eq 5 ]; then
    echo "❌ Upload failed for $file after 5 attempts."
    exit 1
  fi
done
echo "✅ All tar files uploaded."

exit

#
echo "setup_auction_scout_environment_from_windows_step1.sh complete."
