'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const SCRIPT_SRC_RE = /<script\b[^>]*\bsrc=["'](\/static\/[^"'?#]+\.js)(?:[?#][^"']*)?["']/gi;

function indexScriptPaths() {
  const html = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8').replace(/<!--[\s\S]*?-->/g, '');
  const out = [];
  const seen = new Set();
  let m;
  SCRIPT_SRC_RE.lastIndex = 0;
  while ((m = SCRIPT_SRC_RE.exec(html)) !== null) {
    const rel = m[1].replace(/^\/+/, '');
    const full = path.join(ROOT, rel);
    if (!seen.has(full)) {
      seen.add(full);
      out.push({ rel, full });
    }
  }
  return out;
}

function uiJsFiles() {
  const matched = indexScriptPaths()
    .filter(({ rel }) => rel === 'static/app.js' || rel.startsWith('static/js/'))
    .map(({ full }) => full);
  if (matched.length > 0) return matched;
  const fallback = path.join(ROOT, 'static', 'app.js');
  return fs.existsSync(fallback) ? [fallback] : [];
}

function allStaticJsFiles() {
  const seen = new Set();
  const files = [];
  for (const { full } of indexScriptPaths()) {
    if (fs.existsSync(full) && !seen.has(full)) {
      seen.add(full);
      files.push(full);
    }
  }
  const app = path.join(ROOT, 'static', 'app.js');
  if (fs.existsSync(app) && !seen.has(app)) {
    files.push(app);
  }
  return files;
}

function readAllUiJs({ normalizeNewlines = true } = {}) {
  const files = uiJsFiles();
  let text = files
    .map((p) => fs.readFileSync(p, 'utf8').replace(/^\uFEFF/, ''))
    .join('\n\n');
  if (normalizeNewlines) {
    text = text.replace(/\r\n?/g, '\n');
  }
  return text;
}

module.exports = {
  ROOT,
  uiJsFiles,
  allStaticJsFiles,
  readAllUiJs,
};
