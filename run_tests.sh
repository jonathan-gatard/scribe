#!/bin/bash
# Setup test environment
mkdir -p custom_components/scribe
cp *.py *.json *.yaml custom_components/scribe/
cp -r translations custom_components/scribe/

# Run tests
if [ -d "venv" ]; then
    venv/bin/pytest tests -v
else
    pytest tests -v
fi
