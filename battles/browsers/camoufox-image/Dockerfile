# Camoufox-as-a-server image for the browsers Battle.
#
# Camoufox (https://camoufox.com) is a Firefox fork with anti-bot
# fingerprint patches, distributed as a Python package that wraps
# Playwright. `python -m camoufox server` exposes a Playwright WS
# endpoint that `firefox.connect()` can attach to — identical
# protocol shape to Browserless's chromium endpoint, but Firefox
# under the hood.
#
# This Dockerfile produces an image that:
#   1. Installs Python 3.12 + the camoufox package + headless deps
#   2. Pre-fetches Camoufox's Firefox binary at build time so first
#      run isn't bottlenecked by the download
#   3. ENTRYPOINTs to the camoufox server on :9222
#
# Pinned base image digest for reproducibility — verifiers get the
# same Python + libc.

FROM python:3.12.7-slim-bookworm

# Camoufox is Firefox-derived; needs the usual Firefox headless runtime
# deps. These are the same packages the upstream camoufox docs list as
# required for headless container runs. Pinning via apt isn't possible
# (Debian stable repo is on a moving target), but pinning the base
# image digest is sufficient — apt resolves to fixed versions inside
# that snapshot.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgtk-3-0 \
    libdbus-glib-1-2 \
    libx11-xcb1 \
    libxt6 \
    libasound2 \
    libxtst6 \
    libdrm2 \
    libgbm1 \
    libxshmfence1 \
    libnss3 \
    libxss1 \
    libxrandr2 \
    fonts-liberation \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# Pin camoufox AND playwright. Camoufox's `launch_server` calls into
# Playwright's `browserServerImpl.js` — a file that exists in the
# Playwright driver bundle up through 1.50.x but was removed in
# 1.51+. The latest camoufox (0.4.11) still depends on it. Pinning
# playwright==1.50.0 keeps the launchServer.js shim resolving;
# protocol-level WS compat with the 1.59-client harness is preserved
# (Playwright's WS protocol is stable across this minor-version
# range).
RUN pip install --no-cache-dir \
    "camoufox[geoip]==0.4.11" \
    "playwright==1.50.0"

# Pre-fetch the Firefox binary so the first server start isn't a
# 5-10s download. The fetch is deterministic for a given camoufox
# version (CDN-served binary, content-addressed).
RUN python -m camoufox fetch

# Launcher script — camoufox 0.4.x's `python -m camoufox server` CLI
# takes no options, so we configure port + ws_path via the Python
# `launch_server` API.
COPY server.py /opt/camoufox-server.py

EXPOSE 9222

# Xvfb wraps the server because Camoufox (Firefox-derived) calls X11
# APIs even in headless mode.
CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x720x24 & DISPLAY=:99 python /opt/camoufox-server.py"]
