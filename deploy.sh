#!/bin/bash
# Deploy Scribe to Home Assistant and Restart

SOURCE_DIR="/home/jonathan/docker/scribe"
TARGET_DIR="/home/jonathan/docker/homeassistant/custom_components/scribe"
CONTAINER_NAME="homeassistant"

echo "🚀 Deploying Scribe..."

# 1. Sync Files
echo "📂 Syncing files..."
# Ensure target exists
mkdir -p "$TARGET_DIR"
# Copy files (excluding git, tests, etc if needed, but for now copy all)
# Clean target (optional, but good for removing old files)
rm -rf "$TARGET_DIR"/*

# Copy specific files
cp "$SOURCE_DIR"/*.py "$TARGET_DIR"/
cp "$SOURCE_DIR"/manifest.json "$TARGET_DIR"/
cp "$SOURCE_DIR"/services.yaml "$TARGET_DIR"/

# Copy directories
cp -r "$SOURCE_DIR"/translations "$TARGET_DIR"/

echo "✅ Files copied."

# 2. Restart Home Assistant
echo "🔄 Restarting Home Assistant..."
docker restart "$CONTAINER_NAME"

echo "✅ Deployment Complete!"
