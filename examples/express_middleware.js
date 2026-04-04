/**
 * Express.js middleware for SafeEye content scanning.
 * Scans uploaded files before they reach your route handler.
 *
 * Usage:
 *   npm install express multer node-fetch form-data
 *   SAFEEYE_URL=http://localhost:1985 SAFEEYE_TOKEN=xxx node express_middleware.js
 */
const express = require("express");
const multer = require("multer");
const fetch = require("node-fetch");
const FormData = require("form-data");
const fs = require("fs");

const SAFEEYE_URL = process.env.SAFEEYE_URL || "http://localhost:1985";
const SAFEEYE_TOKEN = process.env.SAFEEYE_TOKEN;

const upload = multer({ dest: "/tmp/uploads" });

// SafeEye middleware — blocks NSFW uploads
async function safeEyeGuard(req, res, next) {
  if (!req.file) return next();

  const form = new FormData();
  form.append("file", fs.createReadStream(req.file.path));

  try {
    const resp = await fetch(`${SAFEEYE_URL}/api/v1/scan/file`, {
      method: "POST",
      headers: { Authorization: `Bearer ${SAFEEYE_TOKEN}` },
      body: form,
    });
    const data = await resp.json();
    const result = data.result;

    if (result.is_nsfw) {
      fs.unlinkSync(req.file.path); // Delete unsafe file
      return res.status(403).json({
        error: "Content blocked by SafeEye",
        labels: result.labels,
        confidence: result.confidence,
      });
    }

    req.safeEyeResult = result; // Attach scan result for downstream use
    next();
  } catch (err) {
    console.error("SafeEye scan failed:", err.message);
    next(); // Allow on error (fail-open) — change to res.status(500) for fail-close
  }
}

// Example app
const app = express();

app.post("/upload", upload.single("image"), safeEyeGuard, (req, res) => {
  res.json({
    message: "Upload accepted",
    file: req.file.filename,
    safeEye: req.safeEyeResult,
  });
});

app.listen(3000, () => console.log("Server on :3000 with SafeEye guard"));
