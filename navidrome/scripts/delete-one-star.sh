#!/bin/bash

# ============================================================
# Navidrome Smart Playlist Cleanup
# ============================================================

# --- CONFIGURATION ---

# 1. Name of your Navidrome Docker container
CONTAINER_NAME="navidrome"

# 2. Path to the physical music folder ON YOUR HOST MACHINE
HOST_MUSIC_DIR="/mnt/user/media/music"

# 3. Temporary location on your host to save the exported playlist
PLAYLIST_TMP="/tmp/to_delete.m3u"

# 4. Log directory
LOG_DIR="/mnt/user/appdata/navidrome/scripts/logs"

# 5. Log file - one log per day
LOG_FILE="$LOG_DIR/delete-one-star-rating-$(date '+%Y-%m-%d').log"

# ------------------------------------------------------------
# Create log directory
# ------------------------------------------------------------

mkdir -p "$LOG_DIR"

# ------------------------------------------------------------
# Logging function
# ------------------------------------------------------------

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# ============================================================
# START
# ============================================================

log "============================================================"
log "Navidrome Smart Playlist Cleanup"
log "============================================================"
log "Playlist: Smart: To Delete"
log "Container: $CONTAINER_NAME"
log "Host music directory: $HOST_MUSIC_DIR"
log "Log file: $LOG_FILE"
log ""

# ============================================================
# Check configuration
# ============================================================

if [ ! -d "$HOST_MUSIC_DIR" ]; then
    log "ERROR: Host music directory does not exist:"
    log "       $HOST_MUSIC_DIR"
    exit 1
fi

if ! docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    log "ERROR: Docker container '$CONTAINER_NAME' does not exist."
    exit 1
fi

# ============================================================
# Export playlist
# ============================================================

log "Exporting playlist 'Smart: To Delete' from Docker container..."

if ! docker exec "$CONTAINER_NAME" \
    navidrome pls -n -l error -p "Smart: To Delete" \
    > "$PLAYLIST_TMP" 2>>"$LOG_FILE"; then

    log "ERROR: Failed to export playlist."
    rm -f "$PLAYLIST_TMP"
    exit 1
fi

# ============================================================
# Check playlist
# ============================================================

if [ -s "$PLAYLIST_TMP" ]; then

    log "Playlist exported successfully."

    # Count actual music entries
    TRACK_COUNT=$(grep -v '^#' "$PLAYLIST_TMP" | grep -v '^[[:space:]]*$' | wc -l)

    log "Tracks found: $TRACK_COUNT"
    log ""
    log "Processing deletions..."

    DELETED=0
    MISSING=0
    ERRORS=0

    # ========================================================
    # Process playlist
    # ========================================================

    while IFS= read -r line || [[ -n "$line" ]]; do

        # Clean Windows carriage returns if any
        line=$(echo "$line" | tr -d '\r')

        # Skip empty lines
        [[ -z "$line" ]] && continue

        # Skip M3U headers/comments
        [[ "$line" =~ ^# ]] && continue

        # ----------------------------------------------------
        # Build absolute path on host
        # ----------------------------------------------------

        # Remove Navidrome's /music/ prefix before mapping to the host
       # Remove the /music/ prefix returned by Navidrome
        RELATIVE_PATH="${line#/music/}"

        # Build the actual host path
        FULL_PATH="$HOST_MUSIC_DIR/$RELATIVE_PATH"

        log "Host path:     $FULL_PATH"

        # ----------------------------------------------------
        # Verify file exists
        # ----------------------------------------------------

        if [ -f "$FULL_PATH" ]; then

            log "Deleting: $FULL_PATH"

            if rm -f -- "$FULL_PATH"; then
                log "SUCCESS: File deleted."
                DELETED=$((DELETED + 1))
            else
                log "ERROR: Failed to delete file."
                ERRORS=$((ERRORS + 1))
            fi

        else

            log "MISSING: File not found on host."
            MISSING=$((MISSING + 1))

        fi

    done < "$PLAYLIST_TMP"

    # ========================================================
    # Clean up
    # ========================================================

    rm -f "$PLAYLIST_TMP"

    # ========================================================
    # Summary
    # ========================================================

    log ""
    log "============================================================"
    log "SUMMARY"
    log "============================================================"
    log "Tracks found : $TRACK_COUNT"
    log "Deleted      : $DELETED"
    log "Missing      : $MISSING"
    log "Errors       : $ERRORS"
    log "============================================================"
    log "Deletion complete."

else

    log "Playlist 'Smart: To Delete' was empty or not found."

    rm -f "$PLAYLIST_TMP"

fi

log ""
log "Script finished."
log "============================================================"


# ============================================================
# PURGE LOG FILES - OLDER THAN 90 DAYS
# ============================================================

LOG_RETENTION_DAYS=90

log ""
log "Purging log files older than $LOG_RETENTION_DAYS days..."

OLD_LOGS=$(find "$LOG_DIR" \
    -type f \
    -name "*.log" \
    -mtime +"$LOG_RETENTION_DAYS" \
    -print)

if [ -n "$OLD_LOGS" ]; then

    PURGED_COUNT=0

    while IFS= read -r OLD_LOG; do
        [ -z "$OLD_LOG" ] && continue

        log "Purging: $OLD_LOG"

        if rm -f -- "$OLD_LOG"; then
            PURGED_COUNT=$((PURGED_COUNT + 1))
        else
            log "ERROR: Could not delete log: $OLD_LOG"
        fi

    done <<< "$OLD_LOGS"

    log "Old logs purged: $PURGED_COUNT"

else

    log "No logs older than $LOG_RETENTION_DAYS days."

fi

log "Cleanup job finished."