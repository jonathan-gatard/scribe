#!/bin/bash
# Script to drop the 'homeassistant' database
# Usage: ./drop_ha_db.sh [postgres_user] [postgres_host]

PGUSER=${1:-postgres}
PGHOST=${2:-localhost}

echo "⚠️  WARNING: This will DELETE the 'homeassistant' database!"
read -p "Are you sure? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]
then
    echo "Aborted."
    exit 1
fi

echo "Dropping database 'homeassistant'..."
dropdb -h "$PGHOST" -U "$PGUSER" --if-exists homeassistant

if [ $? -eq 0 ]; then
    echo "✅ Database 'homeassistant' dropped successfully."
else
    echo "❌ Failed to drop database."
fi
