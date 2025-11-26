#!/bin/bash
# Deploy Scribe to Home Assistant and Restart

SOURCE_DIR="$(cd "$(dirname "$0")/../custom_components/scribe" && pwd)"
TARGET_BASE="$1"
CONTAINER_NAME=${2:-homeassistant}

if [ -z "$TARGET_BASE" ]; then
    echo "Usage: $0 <target_directory> [container_name]"
    echo "Example: $0 /path/to/custom_components/ homeassistant"
    exit 1
fi

# Ensure the target ends with 'scribe' subdirectory
if [[ "$TARGET_BASE" == */scribe ]]; then
    TARGET_DIR="$TARGET_BASE"
else
    TARGET_DIR="$TARGET_BASE/scribe"
fi

echo "🚀 Deploying Scribe..."
echo "📁 Source: $SOURCE_DIR"
echo "📁 Target: $TARGET_DIR"

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
