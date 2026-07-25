#!/bin/bash
# reset_logging.sh - Complete reset of FGR logging system
#
# WHAT THIS SCRIPT DOES:
# 1. Stops all FGR services (log_server, web_controller)
# 2. Creates a timestamped backup of the corrupted database
# 3. Deletes the main database and all SQLite WAL files
# 4. Clears systemd journal entries for FGR
# 5. Restarts all services to recreate a clean database
#
# WARNING: This will permanently delete ALL historical log data!
#          Only run this if you have confirmed the database is corrupted
#          and you have no need for the existing logs.
#
# USAGE: sudo ./reset_logging.sh

set -e  # Exit on error

# ============================================================
# CONFIGURATION - Specific to this system
# ============================================================

# Database path
DB_PATH="/mnt/fgr_data/logs.db"

# Service names
LOG_SERVER_SERVICE="log_server"
CONTROLLER_SERVICE="web_controller"

# Backup directory (same as database location)
BACKUP_DIR="/mnt/fgr_data"

# ============================================================
# SCRIPT STARTS HERE
# ============================================================

# Color codes for output
RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║     FGR LOGGING SYSTEM - COMPLETE RESET UTILITY         ║${NC}"
echo -e "${BLUE}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""

# ============================================================
# PRIVILEGE CHECK
# ============================================================

# Check if running with sufficient privileges
check_privileges() {
    local errors=0

    # Check if running as root
    if [ "$EUID" -ne 0 ]; then
        echo -e "${RED}✗ This script must be run with root privileges${NC}"
        echo -e "${YELLOW}  Please run: ${BLUE}sudo $0${NC}"
        echo ""
        echo -e "${YELLOW}  Why root is needed:${NC}"
        echo "    • Stop/start system services (systemctl)"
        echo "    • Delete database files in /mnt/fgr_data/"
        echo "    • Clear systemd journal (journalctl)"
        echo "    • Access systemd unit files"
        return 1
    fi

    # Check if sudo is actually available (should be, but just in case)
    if ! command -v sudo &> /dev/null; then
        echo -e "${YELLOW}⚠️  sudo command not found (but running as root, continuing)${NC}"
    fi

    # Check if user has write permission to backup directory
    if [ -d "$BACKUP_DIR" ]; then
        if [ ! -w "$BACKUP_DIR" ]; then
            echo -e "${RED}✗ No write permission to backup directory: $BACKUP_DIR${NC}"
            errors=$((errors + 1))
        fi
    else
        # Directory doesn't exist, check parent
        PARENT_DIR=$(dirname "$BACKUP_DIR")
        if [ ! -w "$PARENT_DIR" ]; then
            echo -e "${RED}✗ Cannot create backup directory: no write permission to $PARENT_DIR${NC}"
            errors=$((errors + 1))
        fi
    fi

    # Check if we can access systemd
    if ! command -v systemctl &> /dev/null; then
        echo -e "${RED}✗ systemctl not found - this script requires systemd${NC}"
        errors=$((errors + 1))
    fi

    if [ $errors -gt 0 ]; then
        echo ""
        echo -e "${RED}Cannot proceed due to permission issues.${NC}"
        return 1
    fi

    echo -e "${GREEN}✓ Running with sufficient privileges (root)${NC}"
    echo ""
    return 0
}

# Run privilege check
if ! check_privileges; then
    exit 1
fi

echo -e "${YELLOW}⚠️  WARNING: THIS WILL PERMANENTLY DELETE ALL LOG DATA${NC}"
echo ""
echo "This script will perform the following operations:"
echo "  ${GREEN}1.${NC} Stop all FGR services (log_server, web_controller)"
echo "  ${GREEN}2.${NC} Backup the current database (if it exists)"
echo "  ${GREEN}3.${NC} Delete the database and WAL files"
echo "  ${GREEN}4.${NC} Clear systemd journal entries for FGR"
echo "  ${GREEN}5.${NC} Restart services to recreate clean database"
echo ""
echo "Database: ${BLUE}$DB_PATH${NC}"
echo "Backups will be saved to: ${BLUE}$BACKUP_DIR${NC}"
echo ""

# Check disk space before proceeding
echo -e "${BLUE}Checking disk space...${NC}"
AVAILABLE_SPACE=$(df -BG "$BACKUP_DIR" 2>/dev/null | awk 'NR==2 {print $4}' | sed 's/G//')
if [ -n "$AVAILABLE_SPACE" ] && [ "$AVAILABLE_SPACE" -lt 1 ]; then
    echo -e "${RED}✗ Low disk space in $BACKUP_DIR (only ${AVAILABLE_SPACE}GB available)${NC}"
    echo "  Free up some space before proceeding."
    exit 1
else
    echo -e "${GREEN}✓ Disk space available: ${AVAILABLE_SPACE}GB${NC}"
fi
echo ""

echo -e "${RED}All historical log data will be permanently lost!${NC}"
echo ""

# Confirmation
echo -e "${RED}Type 'YES RESET' to confirm you want to proceed:${NC}"
read -p "> " confirmation

if [ "$confirmation" != "YES RESET" ]; then
    echo -e "${GREEN}Cancelled. No changes made.${NC}"
    exit 0
fi

echo ""
echo -e "${YELLOW}Proceeding with reset...${NC}"
echo ""

# Create backup directory if it doesn't exist
mkdir -p "$BACKUP_DIR"

# ============================================================
# STEP 1: Stop Services
# ============================================================
echo -e "${BLUE}[1/5] Stopping services...${NC}"

stop_service() {
    local service=$1
    if systemctl is-active --quiet "$service" 2>/dev/null; then
        echo "  Stopping $service..."
        systemctl stop "$service"
        sleep 1
    else
        echo "  $service not running (skipping)"
    fi
}

# Stop services in correct order (controller first, then log_server)
stop_service "$CONTROLLER_SERVICE"
stop_service "$LOG_SERVER_SERVICE"

echo "  Services stopped"
echo ""

# ============================================================
# STEP 2: Backup Database
# ============================================================
echo -e "${BLUE}[2/5] Backing up database...${NC}"

if [ -f "$DB_PATH" ]; then
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    BACKUP_FILE="$BACKUP_DIR/logs_db_backup_${TIMESTAMP}.db"

    # Check if backup already exists (avoid overwriting)
    if [ -f "$BACKUP_FILE" ]; then
        echo -e "${YELLOW}  Warning: Backup file already exists: $BACKUP_FILE${NC}"
        echo "  Adding unique suffix..."
        BACKUP_FILE="${BACKUP_FILE}_$(date +%N)"
    fi

    echo "  Copying $DB_PATH to $BACKUP_FILE..."
    cp "$DB_PATH" "$BACKUP_FILE"

    # Verify backup was created
    if [ -f "$BACKUP_FILE" ]; then
        BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
        echo -e "  ${GREEN}✓ Backup created: $BACKUP_FILE ($BACKUP_SIZE)${NC}"
    else
        echo -e "${RED}✗ Backup failed!${NC}"
        exit 1
    fi

    # Also backup WAL files if they exist
    if [ -f "${DB_PATH}-wal" ]; then
        cp "${DB_PATH}-wal" "${BACKUP_FILE}-wal"
        echo "  Backed up WAL file"
    fi
    if [ -f "${DB_PATH}-shm" ]; then
        cp "${DB_PATH}-shm" "${BACKUP_FILE}-shm"
        echo "  Backed up SHM file"
    fi
else
    echo "  No database found at $DB_PATH (skipping backup)"
fi
echo ""

# ============================================================
# STEP 3: Delete Database
# ============================================================
echo -e "${BLUE}[3/5] Deleting corrupted database...${NC}"

delete_file() {
    local file=$1
    if [ -f "$file" ]; then
        echo "  Removing $file..."
        rm -f "$file"
    fi
}

delete_file "$DB_PATH"
delete_file "${DB_PATH}-wal"
delete_file "${DB_PATH}-shm"
delete_file "${DB_PATH}-journal"

echo "  Database files removed"
echo ""

# ============================================================
# STEP 4: Clear Journal
# ============================================================
echo -e "${BLUE}[4/5] Clearing systemd journal...${NC}"

# Rotate journal to ensure we can vacuum
echo "  Rotating journal..."
journalctl --rotate

# Vacuum to remove old entries
echo "  Vacuuming journal..."
journalctl --vacuum-time=1s

# Clear specific FGR entries
echo "  Clearing FGR-specific journal entries..."
journalctl --rotate --vacuum-time=1s --unit="$LOG_SERVER_SERVICE" 2>/dev/null || true
journalctl --rotate --vacuum-time=1s --unit="$CONTROLLER_SERVICE" 2>/dev/null || true

echo -e "  ${GREEN}✓ Journal cleared${NC}"
echo ""

# ============================================================
# STEP 5: Restart Services
# ============================================================
echo -e "${BLUE}[5/5] Restarting services...${NC}"

start_service() {
    local service=$1
    if systemctl list-unit-files | grep -q "^$service.service"; then
        echo "  Starting $service..."
        systemctl start "$service"

        # Wait a moment for service to start
        sleep 2

        # Check if it started successfully
        if systemctl is-active --quiet "$service"; then
            echo -e "  ${GREEN}✓ $service started successfully${NC}"
        else
            echo -e "  ${RED}✗ Failed to start $service${NC}"
            echo "    Check status with: systemctl status $service"
        fi
    else
        echo "  $service not installed (skipping)"
    fi
}

# Start log_server first (creates database)
start_service "$LOG_SERVER_SERVICE"

# Give it a moment to create the database
sleep 3

# Start web_controller
start_service "$CONTROLLER_SERVICE"

echo ""

# ============================================================
# VERIFICATION
# ============================================================
echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}✓ Reset complete!${NC}"
echo ""

# Check if database was recreated
if [ -f "$DB_PATH" ]; then
    DB_SIZE=$(du -h "$DB_PATH" | cut -f1)
    echo -e "  ${GREEN}✓ Database recreated: $DB_PATH ($DB_SIZE)${NC}"

    # Check database permissions
    DB_OWNER=$(stat -c '%U' "$DB_PATH" 2>/dev/null || echo "unknown")
    DB_GROUP=$(stat -c '%G' "$DB_PATH" 2>/dev/null || echo "unknown")
    echo "  Database owner: $DB_OWNER:$DB_GROUP"
else
    echo -e "  ${YELLOW}⚠️  Database not found - check log_server service${NC}"
fi

# Show service status
echo ""
echo "Service status:"
for service in "$LOG_SERVER_SERVICE" "$CONTROLLER_SERVICE"; do
    if systemctl is-active --quiet "$service" 2>/dev/null; then
        echo -e "  ${GREEN}●${NC} $service is running"
    else
        echo -e "  ${RED}●${NC} $service is not running"
        if systemctl is-failed --quiet "$service" 2>/dev/null; then
            echo "    Service is in failed state"
            echo "    Check with: systemctl status $service"
        fi
    fi
done

echo ""
echo -e "${GREEN}You can monitor new logs with:${NC}"
echo "  sudo journalctl -u $LOG_SERVER_SERVICE -f"
echo ""

# List backup files
echo -e "${YELLOW}Backup(s) of corrupted database:${NC}"
BACKUP_FILES=$(ls -lh "$BACKUP_DIR"/logs_db_backup_*.db 2>/dev/null | head -5)
if [ -n "$BACKUP_FILES" ]; then
    echo "$BACKUP_FILES"
    BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/logs_db_backup_*.db 2>/dev/null | wc -l)
    if [ "$BACKUP_COUNT" -gt 5 ]; then
        echo "  ... and $((BACKUP_COUNT - 5)) more"
    fi
else
    echo "  (none found)"
fi

echo ""
echo -e "${GREEN}Done!${NC}"