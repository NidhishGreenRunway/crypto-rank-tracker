#!/bin/bash
# Crypto Rank Tracker — launch script
# Usage: ./launch.sh   (from the crypto_tracker directory)

set -e
cd "$(dirname "$0")"

echo "📦 Installing dependencies..."
pip install -r requirements.txt --quiet

echo "🚀 Starting Crypto Rank Tracker on http://localhost:5001"
echo "   Open your browser at http://localhost:5001"
echo "   Press Ctrl+C to stop."
echo ""
python app.py
