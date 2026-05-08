// Single Express server hosting all six fixture routes plus a
// /healthz probe. Behaviour is fully deterministic so the same
// Battle commit produces the same task outputs across every host.

"use strict";

const path = require("path");
const express = require("express");

const PORT = 80;
const app = express();

app.use(express.urlencoded({ extended: false }));
app.use(express.json());

app.get("/healthz", (_req, res) => res.status(200).send("ok\n"));

// Serve static fixture pages (one HTML per task) under the page's
// canonical path.
const PAGES = {
  static: "static.html",
  feed: "feed.html",
  xhr: "xhr.html",
  canvas: "canvas.html",
  router: "router.html",
};
for (const [route, file] of Object.entries(PAGES)) {
  app.get(`/${route}`, (_req, res) =>
    res.sendFile(path.join(__dirname, "public", file)),
  );
}

// /form is GET (the form) + POST (handler that redirects to /form/ok).
app.get("/form", (_req, res) =>
  res.sendFile(path.join(__dirname, "public", "form.html")),
);
app.post("/form", (req, res) => {
  const username = (req.body && req.body.username) || "anon";
  res.redirect(302, `/form/ok?u=${encodeURIComponent(username)}`);
});
app.get("/form/ok", (req, res) => {
  const u = String(req.query.u || "anon").replace(/[^a-zA-Z0-9_-]/g, "");
  res.type("html").send(
    `<!doctype html><html><body><h1>Welcome ${u}</h1></body></html>`,
  );
});

// /feed serves a paginated infinite-scroll feed via fetch().
const FEED_TOTAL = 200;
app.get("/feed/items", (req, res) => {
  const offset = Number.parseInt(req.query.offset || "0", 10) || 0;
  const limit = Math.min(Number.parseInt(req.query.limit || "20", 10) || 20, 50);
  const items = [];
  for (let i = offset; i < Math.min(offset + limit, FEED_TOTAL); i++) {
    items.push({ id: i, text: `feed-item-${i.toString().padStart(4, "0")}` });
  }
  res.json({ items, has_more: offset + limit < FEED_TOTAL });
});

// /xhr serves a one-shot fetch endpoint that returns 25 items.
app.get("/xhr/items", (_req, res) => {
  const items = [];
  for (let i = 0; i < 25; i++) {
    items.push({ id: i, text: `xhr-item-${i}` });
  }
  res.json({ items });
});

app.listen(PORT, "0.0.0.0", () => {
  console.log(`krabarena fixture listening on :${PORT}`);
});
