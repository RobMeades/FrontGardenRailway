#!/bin/bash
# reset_logging.sh

DB_PATH="/mnt/fgr_data/logs.db"

echo "Stopping services..."
systemctl stop log_server
systemctl stop web_controller

echo "Backing up corrupted database..."
if [ -f "$DB_PATH" ]; then
    cp "$DB_PATH" "${DB_PATH}.corrupted.$(date +%Y%m%d_%H%M%S)"
fi

echo "Removing corrupted database..."
rm -f "$DB_PATH"
rm -f "${DB_PATH}-wal" "${DB_PATH}-shm"  # WAL files

echo "Clearing journal..."
journalctl --rotate
journalctl --vacuum-time=1s

echo "Starting services..."
systemctl start log_server
systemctl start web_controller

echo "Reset complete!"