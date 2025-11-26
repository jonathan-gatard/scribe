#!/bin/bash
# Deploy Scribe to Home Assistant and Restart

SOURCE_DIR="/home/jonathan/docker/scribe/custom_components/scribe"
TARGET_DIR="/home/jonathan/docker/homeassistant/custom_components/scribe"
CONTAINER_NAME="homeassistant"

echo "🚀 Deploying Scribe..."

# 1. Sync Files
echo "📂 Syncing files..."
# Ensure target exists
mkdir -p "$TARGET_DIR"
# Clean target
rm -rf "$TARGET_DIR"/*

# Copy all files from source to target
cp -r "$SOURCE_DIR"/* "$TARGET_DIR"/

echo "✅ Files copied."

# 2. Restart Home Assistant
echo "🔄 Restarting Home Assistant..."
docker restart "$CONTAINER_NAME"

echo "✅ Deployment Complete!"
