# All seven test pages served from a single Node/Express container.
# Six routes (/static, /form, /feed, /xhr, /canvas, /router) plus
# /healthz are mounted at port 80 on the `bench` internal network.

FROM node:22-alpine@sha256:8ea2348b068a9544dae7317b4f3aafcdc032df1647bb7d768a05a5cad1a7683f

WORKDIR /app
# package-lock.json pins the full transitive dep graph so the fixture
# builds bit-identically across hosts and over time. `npm ci` refuses
# to install if the lock and manifest disagree.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

COPY server.js ./
COPY public ./public

EXPOSE 80
CMD ["node", "server.js"]
