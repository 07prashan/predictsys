const express = require("express");
const path = require("path");
const { exec } = require("child_process");

const app = express();
const PORT = process.env.PORT || 80; // the browser's default port, so "localhost" alone (no :port) works

// The website is plain static files - HTML/JS/CSS plus the JSON snapshots predict.py writes into
// public/data - exactly what a static host (Vercel, Netlify, ...) serves in production. This
// server only exists for running it locally; there is no API and no database behind it.
app.use(express.static(path.join(__dirname, "public")));

app.listen(PORT, () => {
  const url = PORT === 80 ? "http://localhost" : `http://localhost:${PORT}`;
  console.log(`PredictSys running at ${url}`);
  if (process.platform === "win32") {
    exec(`start "" "${url}"`, (err) => {
      if (err) console.log(`(couldn't auto-open the browser - just open ${url} yourself)`);
    });
  }
});
