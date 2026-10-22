#!/usr/bin/env bash
# ==============================================================================
#   IISER Bhopal - 100% Laptop-Hosted Exam Scanner & AI Grading Hub
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=============================================================================="
echo "  IISER Bhopal - 100% Laptop-Hosted Exam Scanner & AI Grading Hub"
echo "=============================================================================="
echo ""

# Find Python 3.10+
PYTHON_BIN=""
for cmd in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON_BIN="$cmd"
        break
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "[ERROR] Python 3 not found. Please install Python 3.10 or higher." >&2
    exit 1
fi

# Initialize virtual environment if absent
if [ ! -f "venv/bin/python" ]; then
    echo "[INFO] Virtual environment not found at ./venv."
    echo "[INFO] Creating virtual environment with $PYTHON_BIN..."
    "$PYTHON_BIN" -m venv venv
    echo "[INFO] Installing dependencies from requirements.txt..."
    ./venv/bin/pip install --upgrade pip
    ./venv/bin/pip install -r requirements.txt
    echo "[SUCCESS] Virtual environment initialized successfully!"
    echo ""
fi

echo "Starting Hub on http://localhost:8000 and opening browser..."
echo ""
./venv/bin/python local_hub.py --port 8000 --open-browser "$@"
