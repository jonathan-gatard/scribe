#!/bin/bash
# Run tests

# Ensure we are in the project root
cd "$(dirname "$0")/.."

# Run tests
if [ -d "venv" ]; then
    venv/bin/pytest tests -v
else
    pytest tests -v
fi
