#!/bin/bash
# Setup Scribe Database
set -e

PGUSER=${1:-postgres}
PGHOST=${2:-localhost}
DB_NAME=${3:-scribe}
DB_USER=${4:-scribe}
DB_PASS=${5:-scribe}

echo "🔧 Setting up Scribe database..."
echo "  Host: $PGHOST"
echo "  User: $PGUSER"
echo "  DB Name: $DB_NAME"
echo "  DB User: $DB_USER"

# Create User
if psql -h "$PGHOST" -U "$PGUSER" -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
    echo "👤 User '$DB_USER' already exists."
else
    echo "👤 Creating user '$DB_USER'..."
    psql -h "$PGHOST" -U "$PGUSER" -d postgres -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';"
fi

# Create Database
# Using SQL query instead of -lqt to avoid psql version mismatch errors (daticulocale)
if psql -h "$PGHOST" -U "$PGUSER" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
    echo "🗄️  Database '$DB_NAME' already exists."
else
    echo "🗄️  Creating database '$DB_NAME'..."
    createdb -h "$PGHOST" -U "$PGUSER" -O "$DB_USER" "$DB_NAME"
fi

# Grant Privileges
echo "🔑 Granting privileges..."
psql -h "$PGHOST" -U "$PGUSER" -d postgres -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;"

# Install TimescaleDB Extension
echo "⏱️  Installing TimescaleDB extension..."
psql -h "$PGHOST" -U "$PGUSER" -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"

echo "✅ Setup complete!"

echo "---------------------------------------------------"
echo "👥 List of Users:"
psql -h "$PGHOST" -U "$PGUSER" -d postgres -c "SELECT rolname FROM pg_roles;"

echo "---------------------------------------------------"
echo "🗂️  List of Databases:"
psql -h "$PGHOST" -U "$PGUSER" -d postgres -c "SELECT datname FROM pg_database;"

echo "---------------------------------------------------"
echo "📄 Content of '$DB_NAME':"
psql -h "$PGHOST" -U "$PGUSER" -d "$DB_NAME" -c "\dt+"
