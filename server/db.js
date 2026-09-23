// Read-only connection to the SQLite database predict.py (Python) writes to.
// Node never writes predictions - Python is the only writer, so opening this
// read-only is a cheap guarantee the two sides can't step on each other.

const Database = require("better-sqlite3");
const path = require("path");

const DB_PATH = path.join(__dirname, "..", "data", "predictions.db");

const db = new Database(DB_PATH, { readonly: true, fileMustExist: true });

module.exports = db;
