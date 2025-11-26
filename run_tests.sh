#!/bin/bash
# Run tests

# Run tests
if [ -d "venv" ]; then
    venv/bin/pytest tests -v
else
    pytest tests -v
fi
