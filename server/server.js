const express = require("express");
const path = require("path");
const { exec } = require("child_process");
const apiRoutes = require("./routes/api");
const teamRoutes = require("./routes/teams");

const app = express();
const PORT = process.env.PORT || 80; // the browser's default port, so "localhost" alone (no :port) works

app.use("/api", apiRoutes);
app.use("/api", teamRoutes);
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
