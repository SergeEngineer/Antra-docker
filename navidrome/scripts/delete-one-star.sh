#!/bin/bash

# ============================================================
# Navidrome Smart Playlist Cleanup
# ============================================================

# --- CONFIGURATION ---

# 1. Name of your Navidrome docker container
CONTAINER_NAME="navidrome"

# 2. Path to the physical music folder ON YOUR HOST MACHINE
HOST_MUSIC_DIR="/mnt/user/media/music"

# 3. Playlists to process
#    Separate multiple playlists with commas
PLAYLISTS="Smart: To Delete,!DELETE"

# 4. Temporary location on your host
PLAYLIST_TMP="/tmp/to_delete"

# 5. Log directory
LOG_DIR="/mnt/user/appdata/navidrome/scripts/logs"

# 6. Log file - one log per day
LOG_FILE="$LOG_DIR/delete-one-star-rating-$(date '+%Y-%m-%d').log"

# 7. Delete files
#    true  = actually delete
#    false = dry run
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
log "Playlists: $PLAYLISTS"
log "Container: $CONTAINER_NAME"
log "Host music directory: $HOST_MUSIC_DIR"
log "Delete enabled: $DELETE_FILES"
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
# Counters
# ============================================================

TOTAL_TRACKS=0
TOTAL_DELETED=0
TOTAL_MISSING=0
TOTAL_ERRORS=0
PLAYLISTS_PROCESSED=0
PLAYLISTS_FAILED=0

# ============================================================
# Process each playlist
# ============================================================

IFS=',' read -ra PLAYLIST_ARRAY <<< "$PLAYLISTS"

for PLAYLIST_NAME in "${PLAYLIST_ARRAY[@]}"; do

    # Remove leading/trailing spaces
    PLAYLIST_NAME="$(echo "$PLAYLIST_NAME" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"

    [ -z "$PLAYLIST_NAME" ] && continue

    PLAYLISTS_PROCESSED=$((PLAYLISTS_PROCESSED + 1))

    # --------------------------------------------------------
    # Create a unique temporary file for this playlist
    # --------------------------------------------------------

    PLAYLIST_TMP_FILE="${PLAYLIST_TMP}-$(date '+%s%N').m3u"

    log ""
    log "============================================================"
    log "PLAYLIST: $PLAYLIST_NAME"
    log "============================================================"

    # ========================================================
    # Export playlist
    # ========================================================

    log "Exporting playlist '$PLAYLIST_NAME' from Docker container..."

    if ! docker exec "$CONTAINER_NAME" \
        navidrome pls -n -l error -p "$PLAYLIST_NAME" \
        > "$PLAYLIST_TMP_FILE" 2>>"$LOG_FILE"; then

        log "ERROR: Failed to export playlist '$PLAYLIST_NAME'."

        rm -f "$PLAYLIST_TMP_FILE"

        PLAYLISTS_FAILED=$((PLAYLISTS_FAILED + 1))

        continue
    fi

    # ========================================================
    # Check playlist
    # ========================================================

    if [ ! -s "$PLAYLIST_TMP_FILE" ]; then

        log "Playlist '$PLAYLIST_NAME' was empty or not found."

        rm -f "$PLAYLIST_TMP_FILE"

        continue
    fi

    log "Playlist exported successfully."

    # Count actual music entries
    TRACK_COUNT=$(grep -v '^#' "$PLAYLIST_TMP_FILE" \
        | grep -v '^[[:space:]]*$' \
        | wc -l)

    log "Tracks found: $TRACK_COUNT"

    TOTAL_TRACKS=$((TOTAL_TRACKS + TRACK_COUNT))

    if [ "$TRACK_COUNT" -eq 0 ]; then
        rm -f "$PLAYLIST_TMP_FILE"
        continue
    fi

    log ""
    log "Processing deletions..."

    # ========================================================
    # Process playlist tracks
    # ========================================================

    while IFS= read -r line || [[ -n "$line" ]]; do

        # Clean Windows carriage returns
        line=$(echo "$line" | tr -d '\r')

        # Skip empty lines
        [[ -z "$line" ]] && continue

        # Skip M3U headers/comments
        [[ "$line" =~ ^# ]] && continue

        # ----------------------------------------------------
        # Build absolute path on host
        # ----------------------------------------------------

        log ""
        log "Playlist path: $line"

        # Remove Navidrome's /music/ prefix
        RELATIVE_PATH="${line#/music/}"

        # Build actual host path
        FULL_PATH="$HOST_MUSIC_DIR/$RELATIVE_PATH"

        log "Host path:     $FULL_PATH"

        # ----------------------------------------------------
        # Verify file exists
        # ----------------------------------------------------

        if [ -f "$FULL_PATH" ]; then

            if [ "$DELETE_FILES" = true ]; then

                log "Deleting: $FULL_PATH"

                if rm -f -- "$FULL_PATH"; then

                    log "SUCCESS: File deleted."

                    TOTAL_DELETED=$((TOTAL_DELETED + 1))

                else

                    log "ERROR: Failed to delete file."

                    TOTAL_ERRORS=$((TOTAL_ERRORS + 1))

                fi

            else

                log "DRY RUN: Would delete $FULL_PATH"

                TOTAL_DELETED=$((TOTAL_DELETED + 1))

            fi

        else

            log "MISSING: File not found on host."

            TOTAL_MISSING=$((TOTAL_MISSING + 1))

        fi

    done < "$PLAYLIST_TMP_FILE"

    # --------------------------------------------------------
    # Clean up playlist temporary file
    # --------------------------------------------------------

    rm -f "$PLAYLIST_TMP_FILE"

    log ""
    log "Playlist '$PLAYLIST_NAME' processing complete."

done

# ============================================================
# PLAYLIST SUMMARY
# ============================================================

log ""
log "============================================================"
log "PLAYLIST CLEANUP SUMMARY"
log "============================================================"
log "Playlists processed : $PLAYLISTS_PROCESSED"
log "Playlists failed    : $PLAYLISTS_FAILED"
log "Tracks found        : $TOTAL_TRACKS"
log "Deleted             : $TOTAL_DELETED"
log "Missing             : $TOTAL_MISSING"
log "Errors              : $TOTAL_ERRORS"
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


# ============================================================
# FINAL SUMMARY
# ============================================================

log ""
log "============================================================"
log "CLEANUP JOB FINISHED"
log "============================================================"
log "Playlist tracks      : $TOTAL_TRACKS"
log "Playlist files deleted: $TOTAL_DELETED"
log "Playlist files missing: $TOTAL_MISSING"
log "Playlist errors       : $TOTAL_ERRORS"
log "Duplicate files found : $DUPLICATE_COUNT"
log "Duplicates deleted    : $DUPLICATE_DELETED"
log "Duplicates skipped    : $DUPLICATE_SKIPPED"
log "============================================================"