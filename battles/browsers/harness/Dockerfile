# Harness image for the browsers Battle.
#
# We can't use mcr.microsoft.com/playwright directly: it ships the
# browser binaries (/ms-playwright/...) and Node, but not the
# `playwright` npm package — Microsoft expects you to bring your own
# project and install it. So we build a thin layer that adds the
# package, then bench-kit's runners launch THIS image (not the raw
# upstream). NODE_PATH points at /harness/node_modules so a
# read-only-mounted /task/harness/drive.js can `require("playwright")`
# without a writable working tree.

FROM mcr.microsoft.com/playwright@sha256:b0ab6f3cb99aa7803adbc14d9027ec1785fc6e433b97e134e0f8fe61683b6b53

WORKDIR /harness

# Install playwright via the lockfile so the image is byte-deterministic
# for a given (Dockerfile, package.json, package-lock.json).
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

# Make `require("playwright")` resolve from /task/harness/drive.js
# even though /task is mounted read-only.
ENV NODE_PATH=/harness/node_modules
