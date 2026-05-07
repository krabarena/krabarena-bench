# All seven test pages served from a single Node/Express container.
# Six routes (/static, /form, /feed, /xhr, /canvas, /router) plus
# /healthz are mounted at port 80 on the `bench` internal network.

FROM node:22-alpine@sha256:8ea2348b068a9544dae7317b4f3aafcdc032df1647bb7d768a05a5cad1a7683f

WORKDIR /app
COPY package.json server.js ./
COPY public ./public
RUN npm install --omit=dev --no-audit --no-fund

EXPOSE 80
CMD ["node", "server.js"]
