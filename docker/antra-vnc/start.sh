#!/bin/bash

set -e

###############################################################################
# Environment
###############################################################################

export DISPLAY=:0
export HOME=/config
export BROWSER=/usr/local/bin/antra-browser

export XDG_CURRENT_DESKTOP=XFCE
export XDG_SESSION_TYPE=x11
export XDG_CONFIG_HOME=/config/.config
export XDG_DATA_HOME=/config/.local/share
export XDG_CACHE_HOME=/config/.cache
export XDG_RUNTIME_DIR=/tmp/runtime-root

export XDG_RUNTIME_DIR=/tmp/runtime-root

mkdir -p \
    "$HOME" \
    "$XDG_CONFIG_HOME" \
    "$XDG_DATA_HOME" \
    "$XDG_CACHE_HOME" \
    "$XDG_RUNTIME_DIR"

chmod 700 "$XDG_RUNTIME_DIR"

###############################################################################
# Virtual display
###############################################################################

echo "[Antra] Starting Xvfb..."

Xvfb :0 \
    -screen 0 1920x1080x24 \
    -ac \
    +extension GLX \
    +render \
    -noreset \
    > /tmp/xvfb.log 2>&1 &

sleep 2

###############################################################################
# Window manager
###############################################################################

echo "[Antra] Starting Fluxbox..."

fluxbox \
    > /tmp/fluxbox.log 2>&1 &

sleep 2

###############################################################################
# VNC server
###############################################################################

echo "[Antra] Starting x11vnc..."

x11vnc \
    -display :0 \
    -forever \
    -shared \
    -rfbport 5900 \
    -nopw \
    -listen 0.0.0.0 \
    > /tmp/x11vnc.log 2>&1 &

sleep 2

###############################################################################
# noVNC
###############################################################################

echo "[Antra] Starting noVNC..."

/opt/novnc/utils/novnc_proxy \
    --vnc localhost:5900 \
    --listen 7337 \
    --web /opt/novnc \
    > /tmp/novnc.log 2>&1 &

sleep 3

echo "[Antra] noVNC started on http://0.0.0.0:7337/vnc.html"

###############################################################################
# Start Antra
###############################################################################

echo "[Antra] Starting Antra..."

cd /opt/antra/appimage

exec ./AppRun