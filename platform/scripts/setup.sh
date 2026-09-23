#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

echo "=========================================="
echo " Syda Platform Development Setup"
echo "=========================================="

# 1. Verify parent syda core exists
if [ -f "../pyproject.toml" ]; then
    echo "✓ Detected parent syda core package."
else
    echo "! Warning: Expected parent syda repository at .."
fi

# 2. Python environment & backend setup
if command -v uv >/dev/null 2>&1; then
    echo "✓ uv detected ($(uv --version))"
    echo "→ Syncing backend environment with uv..."
    (cd backend && uv sync)
else
    echo "! uv not detected. Falling back to python3 venv..."
    if [ ! -d "backend/.venv" ]; then
        python3 -m venv backend/.venv
    fi
    source backend/.venv/bin/activate
    pip install -U pip
    pip install -e ../..
    pip install -e ./backend
fi
echo "✓ Backend dependencies configured."

# 3. Frontend setup
echo "→ Installing frontend dependencies..."
npm --prefix frontend install
echo "✓ Frontend dependencies installed."

# 4. Root dependencies
echo "→ Installing root development dependencies..."
npm install
echo "✓ Root dependencies installed."

echo ""
echo "=========================================="
echo " Setup complete! 🎉"
echo " Start the development environment with:"
echo "   npm run dev"
echo "=========================================="
