/* Feature search core — pure helpers (2026-10-05).
 *
 * Extracted from the feature-search palette in app.js so the alias
 * expansion and dedupe rules are unit-testable without a browser
 * (tests/js/test_feature_search_core.mjs runs them under node; the pytest
 * wrapper tests/test_feature_search_core.py skips when node is absent).
 *
 * Exposed as (window||globalThis).FSCore = { expandTerms, dedupe }.
 */
(function(root) {
  'use strict';

  // Abbreviations people type are not substrings of the label text:
  // "ctx" never matches "Context". Expanding the query with aliases fixes
  // that without renaming the UI.
  var ALIASES = {
    ctx: 'context',
    kontext: 'context',
    temp: 'temperature',
    bild: 'image',
    foto: 'image',
    vram: 'vram'
  };

  function expandTerms(q, aliases) {
    q = String(q || '').toLowerCase().trim();
    if (!q) return [];
    var terms = [q];
    var map = aliases || ALIASES;
    Object.keys(map).forEach(function(a) {
      if (q === a || q.indexOf(a) >= 0) {
        var t = map[a];
        if (terms.indexOf(t) < 0) terms.push(t);
      }
    });
    return terms;
  }

  // Keep the FIRST occurrence per panel+label; later duplicates (the same
  // button rendered twice) drop out. Returns a new array, input untouched.
  function dedupe(idx) {
    var seen = {};
    var out = [];
    (idx || []).forEach(function(item) {
      var key = (item.panel || '') + '|' + String(item.label || '').toLowerCase();
      if (seen[key]) return;
      seen[key] = 1;
      out.push(item);
    });
    return out;
  }

  var api = { expandTerms: expandTerms, dedupe: dedupe, ALIASES: ALIASES };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.FSCore = api;
})(typeof window !== 'undefined' ? window : globalThis);
