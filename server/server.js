const express = require("express");
const path = require("path");
const { exec } = require("child_process");

const app = express();
const PORT = process.env.PORT || 80; // the browser's default port, so "localhost" alone (no :port) works
// Bind address - defaults to every interface (unchanged behaviour). Set HOST=127.0.0.1 to
// serve only this machine, e.g.  $env:HOST="127.0.0.1"; node server.js
const HOST = process.env.HOST || "0.0.0.0";

// The website is plain static files - HTML/JS/CSS plus the JSON snapshots predict.py writes into
// public/data - exactly what a static host (Vercel, Netlify, ...) serves in production. This
// server only exists for running it locally; there is no API and no database behind it.
app.use(express.static(path.join(__dirname, "public")));

app.listen(PORT, HOST, () => {
  const url = PORT === 80 ? "http://localhost" : `http://localhost:${PORT}`;
  console.log(`PredictSys running at ${url}`);
  if (process.platform === "win32") {
    exec(`start "" "${url}"`, (err) => {
      if (err) console.log(`(couldn't auto-open the browser - just open ${url} yourself)`);
    });
  }
});
