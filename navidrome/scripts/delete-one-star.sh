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

# Remove duplicate files ending with (1).flac through (9).flac
DELETE_FILES=true

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
# DUPLICATE FILE CLEANUP
# ============================================================

log ""
log "============================================================"
log "DUPLICATE FILE CLEANUP"
log "============================================================"

DUPLICATE_COUNT=0
DUPLICATE_DELETED=0
DUPLICATE_SKIPPED=0

log "Scanning entire music library for duplicate FLAC files..."
log "Looking for files ending with (1).flac through (9).flac"

while IFS= read -r DUPLICATE_FILE; do

    [ -z "$DUPLICATE_FILE" ] && continue

    DUPLICATE_COUNT=$((DUPLICATE_COUNT + 1))

    log ""
    log "Duplicate found:"
    log "  $DUPLICATE_FILE"

    # --------------------------------------------------------
    # Safety check
    # --------------------------------------------------------

    REAL_ROOT=$(realpath -e "$HOST_MUSIC_DIR" 2>/dev/null)
    REAL_FILE=$(realpath -e "$DUPLICATE_FILE" 2>/dev/null)

    if [ -z "$REAL_ROOT" ] || [ -z "$REAL_FILE" ]; then
        log "  ERROR: Could not resolve path."
        DUPLICATE_SKIPPED=$((DUPLICATE_SKIPPED + 1))
        continue
    fi

    case "$REAL_FILE" in

        "$REAL_ROOT"/*)
            ;;

        *)
            log "  SECURITY: File is outside music directory!"
            log "  SKIPPED"

            DUPLICATE_SKIPPED=$((DUPLICATE_SKIPPED + 1))
            continue
            ;;

    esac

    # --------------------------------------------------------
    # Delete or dry run
    # --------------------------------------------------------

    if [ "$DELETE_FILES" = true ]; then

        log "  DELETE: $REAL_FILE"

        if rm -f -- "$REAL_FILE"; then

            log "  SUCCESS: Duplicate deleted."
            DUPLICATE_DELETED=$((DUPLICATE_DELETED + 1))

        else

            log "  ERROR: Failed to delete duplicate."
            DUPLICATE_SKIPPED=$((DUPLICATE_SKIPPED + 1))

        fi

    else

        log "  DRY RUN: Would delete $REAL_FILE"
        DUPLICATE_DELETED=$((DUPLICATE_DELETED + 1))

    fi

done < <(
    find "$HOST_MUSIC_DIR" \
        -type f \
        \( \
            -iname '* (1).flac' \
            -o -iname '* (2).flac' \
            -o -iname '* (3).flac' \
            -o -iname '* (4).flac' \
            -o -iname '* (5).flac' \
            -o -iname '* (6).flac' \
            -o -iname '* (7).flac' \
            -o -iname '* (8).flac' \
            -o -iname '* (9).flac' \
        \) \
        -print
)

log ""
log "Duplicate scan complete."
log "Duplicate files found : $DUPLICATE_COUNT"
log "Duplicates deleted     : $DUPLICATE_DELETED"
log "Duplicates skipped     : $DUPLICATE_SKIPPED"

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