#!/bin/bash

# Exit on error
set -e

# Configuration
SERVER_DIR="$HOME"
PORT=8888
HOST="0.0.0.0"

echo "=== JupyterLab (uv) Installation Script ==="

# 1. Source uv environment if it exists
if [ -f "$HOME/.local/bin/env" ]; then
    echo "Sourcing uv environment..."
    . "$HOME/.local/bin/env"
fi

# 2. Create and Enter Server Directory
echo "[1/3] Preparing server directory: $SERVER_DIR..."
mkdir -p "$SERVER_DIR"
cd "$SERVER_DIR"

# 3. Initialize Environment and Install JupyterLab
echo "[2/3] Initializing environment and installing JupyterLab..."
# Remove existing .venv to avoid interactive prompt
rm -rf .venv
uv venv
. .venv/bin/activate
uv pip install jupyterlab ipywidgets

# 4. Generate Initial Configuration (optional)
echo "[3/3] Generating configuration..."
jupyter lab --generate-config

echo "=== Installation Complete ==="
echo ""
echo "Location: $SERVER_DIR"
echo ""
echo "To start JupyterLab manually:"
echo "  cd $SERVER_DIR"
echo "  . .venv/bin/activate"
echo "  jupyter lab --ip=$HOST --port=$PORT --no-browser"
echo ""
echo "To set a password (highly recommended):"
echo "  cd $SERVER_DIR"
echo "  . .venv/bin/activate"
echo "  jupyter lab password"
