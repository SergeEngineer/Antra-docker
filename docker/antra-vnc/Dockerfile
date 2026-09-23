###############################################################################
# Antra GUI in Docker
#
# Real Antra Linux AppImage
# Virtual X display
# Fluxbox
# x11vnc
# noVNC
#
# Browser:
#   http://<UNRAID-IP>:7337
###############################################################################

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

###############################################################################
# 1) Runtime dependencies
###############################################################################

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    ca-certificates \
    git \
    python3 \
    python3-pip \
    chromium \
    chromium-driver \
    iproute2 \
    procps \
    psmisc \
    file \
    dbus \
    dbus-x11 \
    x11vnc \
    xvfb \
    fluxbox \
    xterm \
    xfonts-base \
    xfonts-75dpi \
    xfonts-100dpi \
    libwebkit2gtk-4.1-0 \
    libgtk-3-0 \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcb-shm0 \
    libxcb-render0 \
    libxcb-render-util0 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-icccm4 \
    libxcb-xinerama0 \
    libxcb-xkb1 \
    libxcb-randr0 \
    libxcb-shape0 \
    libxcb-xfixes0 \
    libxext6 \
    libxi6 \
    libxtst6 \
    libfontconfig1 \
    libfreetype6 \
    libjpeg62-turbo \
    libpng16-16 \
    ffmpeg \
    fuse3 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*


###############################################################################
# 2) noVNC
###############################################################################

RUN git clone --depth 1 \
        https://github.com/novnc/noVNC.git \
        /opt/novnc \
    && \
    git clone --depth 1 \
        https://github.com/novnc/websockify.git \
        /opt/novnc/utils/websockify

RUN printf '%s\n' \
    '<!DOCTYPE html>' \
    '<html>' \
    '<head>' \
    '<meta http-equiv="refresh" content="0; url=/vnc.html">' \
    '<title>Antra</title>' \
    '</head>' \
    '<body>' \
    '<script>window.location.href="/vnc.html";</script>' \
    '</body>' \
    '</html>' \
    > /opt/novnc/index.html

###############################################################################
# 3) Download Antra Linux AppImage
#
# v1.1.8 is currently the latest release.
###############################################################################

ARG ANTRA_VERSION=1.1.8

RUN mkdir -p /opt/antra \
    && wget -q \
       "https://github.com/anandprtp/Antra/releases/download/v${ANTRA_VERSION}/Antra-Linux.AppImage" \
       -O /opt/antra/Antra.AppImage \
    && chmod +x /opt/antra/Antra.AppImage

###############################################################################
# 4) AppImage configuration
#
# We extract the AppImage instead of requiring FUSE at runtime.
###############################################################################

RUN cd /opt/antra \
    && ./Antra.AppImage --appimage-extract \
    && mv squashfs-root appimage \
    && rm -f Antra.AppImage


# Make Chromium the explicit HTTP/HTTPS handler
RUN xdg-settings set default-web-browser chromium.desktop \
    && xdg-mime default chromium.desktop x-scheme-handler/http \
    && xdg-mime default chromium.desktop x-scheme-handler/https \
    && xdg-mime default chromium.desktop text/html

###############################################################################
# 5) Persistent directories
###############################################################################

RUN mkdir -p \
    /config \
    /music \
    /tmp/runtime-root

###############################################################################
# 6) Startup script
###############################################################################

COPY start.sh /usr/local/bin/start.sh
COPY antra-browser /usr/local/bin/antra-browser

RUN sed -i 's/\r$//' /usr/local/bin/start.sh \
    && chmod +x /usr/local/bin/start.sh

###############################################################################
# 7) Ports
###############################################################################

EXPOSE 7337 5900

###############################################################################
# 8) Start
###############################################################################

ENTRYPOINT ["/usr/local/bin/start.sh"]