#!/usr/bin/env bash
# Pulls the latest from origin and restarts the live bot service.
# Run this on the server after pushing changes from local dev.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

echo "==> Pulling latest from origin..."
git pull

echo "==> Restarting da-casino-bot..."
sudo systemctl restart da-casino-bot

sleep 2

echo "==> Status:"
sudo systemctl status da-casino-bot --no-pager | head -8

echo "==> Recent logs:"
sudo journalctl -u da-casino-bot --no-pager -n 10
