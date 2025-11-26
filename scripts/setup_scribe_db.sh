#!/bin/bash
# Script to create the 'scribe' database and user
# Usage: ./setup_scribe_db.sh [postgres_user] [postgres_host]

PGUSER=${1:-postgres}
PGHOST=${2:-localhost}
DB_NAME="scribe"
DB_USER="scribe"
DB_PASS="scribe" # Change this in production!

echo "🚀 Setting up Scribe database..."

# Create User
echo "Creating user '$DB_USER'..."
psql -h "$PGHOST" -U "$PGUSER" -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';" || echo "User might already exist."

# Create Database
echo "Creating database '$DB_NAME'..."
createdb -h "$PGHOST" -U "$PGUSER" -O "$DB_USER" "$DB_NAME" || echo "Database might already exist."

# Grant Privileges (Superuser-like for TimescaleDB usually requires it, or at least specific grants)
# For simplicity in this script we grant ALL on database.
# Note: To use TimescaleDB, the user often needs to be a superuser or the extension must be installed by a superuser.
echo "Granting privileges..."
psql -h "$PGHOST" -U "$PGUSER" -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;"

# Install TimescaleDB Extension (Must be done as superuser)
echo "Installing TimescaleDB extension..."
psql -h "$PGHOST" -U "$PGUSER" -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"

echo "✅ Setup complete!"
echo "Database: $DB_NAME"
echo "User: $DB_USER"
echo "Password: $DB_PASS"
