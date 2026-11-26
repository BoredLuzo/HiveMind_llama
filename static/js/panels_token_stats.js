// Token Stats Viewer UI (extracted from panels_learning.js)
// ── Token Stats Viewer ─────────────────────────────────────────────────────
function _fmtTokens(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
  return String(n);
}

function _fmtDuration(s) {
  if (s >= 3600) return (s / 3600).toFixed(1) + 'h';
  if (s >= 60) return (s / 60).toFixed(1) + 'm';
  return s.toFixed(0) + 's';
}

function toggleRunDetail(id) {
  const el = document.getElementById(id);
  if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
}


async function loadTokenStats() {
  try {
    const res = await fetch('/soul/token-stats?days=14');
    const data = await res.json();
    const summaryEl = document.getElementById('token-stats-summary');
    const dailyEl = document.getElementById('token-daily-list');

    const totalTokens = data.total_tokens || 0;
    const totalRuns = data.total_runs || 0;
    const avgTokens = totalRuns > 0 ? Math.round(totalTokens / totalRuns) : 0;
    // TOKEN-TRACKER UI (2026-08-25): Input-/Cache-Dimension + Hit-Quote.
    const totalPrompt = data.total_prompt_tokens || 0;
    const totalCached = data.total_cached_tokens || 0;
    const hitPct = totalPrompt > 0 ? Math.round((totalCached / totalPrompt) * 100) : 0;
    const avgInPerRun = totalRuns > 0 ? Math.round(totalPrompt / totalRuns) : 0;
    // AUDIT-R2+ (2026-08-25): request count — Σ cached ÷ requests ≈ Ø
    // reused context per LLM call (~context size) → makes the
    // large cached sum comprehensible.
    const totalRequests = data.total_requests || 0;
    const avgCachedPerReq = totalRequests > 0 ? Math.round(totalCached / totalRequests) : 0;
    // The actual compute load: prompt minus KV hits (e.g. only 40K of 1.3M)
    const actualComputed = Math.max(0, totalPrompt - totalCached);

    // TOKEN LABELS CLARIFICATION (2026-08-25): total_tokens are only the
    // GENERATED output tokens — next to 1.3M input, "total tokens" looked
    // like a contradiction. New: input+output grand total + precise labels.
    const grandTotalTokens = totalPrompt + totalTokens;

    summaryEl.innerHTML =
      '<div class="soul-stat-row"><span class="soul-stat-label">Input + Output total</span><span class="soul-stat-val" style="color:#c8d4e0">' + _fmtTokens(grandTotalTokens) + '</span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">Output tokens (generated)</span><span class="soul-stat-val" style="color:#60a0e0">' + _fmtTokens(totalTokens) + '</span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">Input (prompt)</span><span class="soul-stat-val" style="color:#9a74dc">' + _fmtTokens(totalPrompt) + '</span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">of which Cached (Σ KV hits all requests)</span><span class="soul-stat-val" style="color:#3a9960">' + _fmtTokens(totalCached) + ' <span style="color:#5a6a7a;font-size:10px">(' + hitPct + '% hit)</span></span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">↳ actually recomputed</span><span class="soul-stat-val" style="color:#60c080">' + _fmtTokens(actualComputed) + ' <span style="color:#5a6a7a;font-size:10px">(' + (100 - hitPct) + '%)</span></span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">LLM requests</span><span class="soul-stat-val" style="color:#8fa4b8">' + (totalRequests || '–') + '</span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">\u00d8 cached/request</span><span class="soul-stat-val" style="color:#8fa4b8">' + (avgCachedPerReq ? _fmtTokens(avgCachedPerReq) : '–') + '</span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">\u00d8 input/run</span><span class="soul-stat-val" style="color:#8fa4b8">' + _fmtTokens(avgInPerRun) + '</span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">Total runs</span><span class="soul-stat-val" style="color:#8fa4b8">' + totalRuns + '</span></div>' +
      '<div class="soul-stat-row"><span class="soul-stat-label">\u00d8 output/run</span><span class="soul-stat-val" style="color:#8fa4b8">' + _fmtTokens(avgTokens) + '</span></div>' +
      '<div style="font-size:9px;color:#5a6a7a;padding-top:6px;line-height:1.5">Input/Cached = sum over ALL LLM requests: every tool run and every phase resends the context (agentic loop). "Cached" = KV-cache hits \u2014 a high share is good (fast, cheap), no consumption.</div>';

    const daily = data.daily || {};
    const days = Object.keys(daily).sort().reverse();

    // METRICS-EXPAND (2026-10-19): comparison per model, computed by the backend
    // (routers/soul.py -> infra.token_stats.aggregate_by_model). Rendered as
    // compact cards: a fixed-width table was unreadable at panel width.
    // By model = Bewertung der Modelle ueber das ganze Fenster. Eine Tages-Sicht
    // braucht es nicht: die Tagesliste darunter zeigt chronologisch, welches
    // Modell welchen Run beruehrt hat.
    const byModel = data.by_model || [];
    const _filterTag = S.tokenStatsModelFilter || '';
    let byModelHtml = '';
    if (byModel.length) {
      // Nur das Noetige: welches Modell laeuft (crash-frei) und wie schnell.
      // Rollen-Tokens/-Zeiten werden pro Modell zusammengefasst; die Rolle selbst
      // ist fuer die Frage "laeuft es stabil und wie schnell" nicht relevant.
      var _flat = {};
      byModel.forEach(function (m) {
        var f = _flat[m.model];
        if (!f) {
          f = _flat[m.model] = {model: m.model, runs: 0, aborts: 0, completed: 0,
                                timed: 0, secs: 0, tokens: 0, estimated: false};
        }
        f.runs += (m.runs || 0);
        // alles, was DU gestoppt hast (abort + graceful_stop) zaehlt nicht als Fail
        f.aborts += (typeof m.excluded === 'number' ? m.excluded : (m.aborted || 0));
        f.completed += ((m.stop || {})['completed'] || 0);
        f.timed += (m.timed_tokens || 0);
        f.secs += (m.seconds || 0);
        f.tokens += (m.tokens || 0);
        f.estimated = f.estimated || !!m.estimated;
        // Backend-Rate (Phasenmessung oder Log-Median) als Rueckfall merken:
        // ohne das rechnete die Liste nur aus timed/secs und zeigte "–"
        if ((m.tps || 0) > (f.apiTps || 0)) {
          f.apiTps = m.tps;
        }
        if (m.speed_source === 'log') {
          f.speedSource = 'log';
          f.speedSamples = Math.max(f.speedSamples || 0, m.speed_samples || 0);
        }
      });
      S.tokenStatsModels = Object.keys(_flat);
      var _flatRows = Object.keys(_flat).map(function (k, _i) {
        var f = _flat[k];
        f.idx = _i;
        var judged = f.runs - f.aborts;            // dein Abbruch ist kein Crash
        f.tps = (f.timed && f.secs) ? f.timed / f.secs : (f.apiTps || 0);
        f.crashFree = judged > 0 ? f.completed / judged : 0;
        f.judged = judged;
        return f;
      });
      // schnell oben; ohne Messwerte nach unten, dort nach Stabilitaet
      _flatRows.sort(function (a, b) {
        if ((b.tps || 0) !== (a.tps || 0)) return (b.tps || 0) - (a.tps || 0);
        return (b.crashFree || 0) - (a.crashFree || 0);
      });
      var _cards = _flatRows.map(function (f) {
        var _tpsColor = f.tps >= 20 ? '#3a9960' : (f.tps >= 10 ? '#c0a020' : '#a06060');
        var _logSpeed = f.speedSource === 'log';
        var _cfColor = f.crashFree >= 0.8 ? '#3a9960' : (f.crashFree >= 0.6 ? '#c0a020' : '#a06060');
        var _isActive = !!_filterTag && _filterTag === f.model;
        return '<div onclick="setTokenStatsModelFilter(' + f.idx + ')" title="click to filter the runs below" ' +
          'style="display:flex;align-items:baseline;gap:8px;padding:3px 0;border-bottom:1px solid #131c26;font-size:9px;cursor:pointer' +
          (_isActive ? ';background:#141c26' : '') + '">' +
          '<span title="' + esc(f.model) + '" style="flex:1 1 auto;min-width:0;color:#c8d4e0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' +
            esc(f.model) + '</span>' +
          '<span style="flex:0 0 70px;text-align:right;color:' + _tpsColor + ';font-weight:600" title="' +
            (_logSpeed ? 'median decode speed from the llama-server log (' + (f.speedSamples || 0) + ' samples)'
                       : (f.estimated ? 'no phase timing in part of these records - rate uses the available seconds'
                                      : 'output tokens per second')) + '">' +
            (f.tps ? f.tps.toFixed(1) + ' tok/s' : '\u2013') +
            (f.tps && (_logSpeed || f.estimated)
              ? '<sup style="font-size:6px;color:#5a6a7a">' + (_logSpeed ? '~' : '*') + '</sup>' : '') +
            '</span>' +
          '<span style="flex:0 0 92px;text-align:right;color:' + _cfColor + '" title="completed runs / runs without your own stops (abort + graceful)">' +
            Math.round(f.crashFree * 100) + '% <span style="color:#4a5a6a">(' + f.completed + '/' + f.judged + ')</span></span>' +
        '</div>';
      }).join('');
      var _head = '<div style="display:flex;gap:8px;padding:0 0 4px;margin-bottom:1px;border-bottom:1px solid #16202a;' +
        'font-family:\'IBM Plex Mono\',monospace;font-size:8px;color:#55657a">' +
        '<span style="flex:1 1 auto"></span>' +
        '<span style="flex:0 0 70px;text-align:right">speed</span>' +
        '<span style="flex:0 0 92px;text-align:right">crash-free</span>' +
      '</div>';
      byModelHtml =
        '<div style="margin:6px 0 10px;border:1px solid #1e2a38;border-radius:3px;padding:8px 10px">' +
          '<div style="display:flex;justify-content:space-between;align-items:baseline;padding:0 0 5px" ' +
            'title="tok/s = output tokens per coder-phase second. Runs before 2026-10-19 carry no phase timing and fall back to the whole run time.">' +
            '<span style="font-family:\'IBM Plex Mono\',monospace;font-size:9px;color:var(--tx2)">By model' +
              (_filterTag ? ' \u00b7 filtered' : '') + '</span>' +
            '<span style="font-family:\'IBM Plex Mono\',monospace;font-size:8px;color:#3f4e5c;letter-spacing:.4px">14 days</span>' +
          '</div>' +
          _head + _cards +
          '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:8px;color:#4a5a6a;' +
            'margin-top:6px;padding:6px 0 0;border-top:1px solid #1a2430;line-height:1.7">' +
            '<span style="color:#5a6a7a">crash-free</span> completed \u00f7 runs without your own stops (abort, graceful)' +
            '<br><span style="color:#5a6a7a">~</span> median decode speed from the llama-server log ' +
            '<span style="color:#5a6a7a">*</span> estimated from run time ' +
            '<br><span style="color:#5a6a7a">click a model</span> filters the runs below to that model' +
          '</div>' +
        '</div>';
    }
    // MODE-TRACKING (2026-10-20): welche Route lief wie oft/schnell/erfolgreich
    const byMode = data.by_mode || [];
    let byModeHtml = '';
    if (byMode.length) {
      var _modeOrder = {code_duo: 0, pipeline: 1, direct: 2};
      var _mRows = byMode.slice().sort(function (a, b) {
        var oa = _modeOrder[a.mode] === undefined ? 9 : _modeOrder[a.mode];
        var ob = _modeOrder[b.mode] === undefined ? 9 : _modeOrder[b.mode];
        return oa !== ob ? oa - ob : (b.runs || 0) - (a.runs || 0);
      }).map(function (m) {
        var _ok = Math.round((m.completed_share || 0) * 100);
        var _okColor = _ok >= 80 ? '#3a9960' : (_ok >= 60 ? '#c0a020' : '#a06060');
        return '<div style="display:flex;align-items:baseline;gap:8px;padding:3px 0;' +
          'border-bottom:1px solid #131c26;font-size:9px">' +
          '<span style="flex:1 1 auto;min-width:0;color:#c8d4e0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' +
            esc(m.mode || '\u2013') + '</span>' +
          '<span style="flex:0 0 52px;text-align:right;color:#7a8fa8">' + (m.runs || 0) + ' runs</span>' +
          '<span style="flex:0 0 66px;text-align:right;color:#60a0e0">' + _fmtTokens(m.tokens || 0) + ' out</span>' +
          '<span style="flex:0 0 62px;text-align:right;color:' + _okColor + '">' + _ok + '% ok</span>' +
          '<span style="flex:0 0 62px;text-align:right;color:#6a7a8a">\u00d8 ' + _fmtDuration(m.avg_elapsed_s || 0) + '</span>' +
        '</div>';
      }).join('');
      byModeHtml =
        '<div style="margin:0 0 10px;border:1px solid #1e2a38;border-radius:3px;padding:8px 10px">' +
          '<div style="display:flex;justify-content:space-between;align-items:baseline;padding:0 0 5px">' +
            '<span style="font-family:\'IBM Plex Mono\',monospace;font-size:9px;color:var(--tx2)">By mode</span>' +
            '<span style="font-family:\'IBM Plex Mono\',monospace;font-size:8px;color:#3f4e5c" ' +
              'title="route = what actually ran: code_duo (planner+coder loop), pipeline (analyst+synthesizer), direct (chat)">' +
              'route that ran</span>' +
          '</div>' +
          '<div style="display:flex;gap:8px;padding:0 0 4px;margin-bottom:1px;border-bottom:1px solid #16202a;' +
            'font-family:\'IBM Plex Mono\',monospace;font-size:8px;color:#55657a">' +
            '<span style="flex:1 1 auto"></span>' +
            '<span style="flex:0 0 52px;text-align:right">runs</span>' +
            '<span style="flex:0 0 66px;text-align:right">out</span>' +
            '<span style="flex:0 0 62px;text-align:right">ok</span>' +
            '<span style="flex:0 0 62px;text-align:right">\u00d8 time</span>' +
          '</div>' +
          _mRows +
        '</div>';
    }

    // Leer-Guard + Balkenskalierung (beim Karten-Umbau versehentlich entfernt,
    // Fehler war "maxDayTokens is not defined")
    if (!days.length) {
      dailyEl.innerHTML = byModelHtml + byModeHtml +
        '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:#7a8fa8;padding:8px">' +
        'No token data yet. Captured automatically per run.</div>';
      return;
    }
    let maxDayTokens = 0;
    days.forEach(function (d) { maxDayTokens = Math.max(maxDayTokens, daily[d].tokens || 0); });

    var _filterBar = '';
    if (_filterTag) {
      _filterBar = '<div style="display:flex;justify-content:space-between;align-items:center;gap:6px;margin:0 0 4px;' +
        'padding:2px 4px;border:1px solid #2a3a4a;border-radius:3px;font-family:\'IBM Plex Mono\',monospace;font-size:8px">' +
        '<span style="color:#8fa4b8;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">filter: ' + esc(_filterTag) + '</span>' +
        '<span onclick="setTokenStatsModelFilter(-1)" style="cursor:pointer;color:#a06060;white-space:nowrap">clear \u2715</span>' +
      '</div>';
    }
    dailyEl.innerHTML = byModelHtml + byModeHtml + _filterBar + days.map(function(day, di) {
      const entry = daily[day];
      const pct = maxDayTokens > 0 ? Math.round((entry.tokens / maxDayTokens) * 100) : 0;
      const avgPerRun = entry.runs > 0 ? Math.round(entry.tokens / entry.runs) : 0;
      const elapsed = entry.elapsed_s || 0;
      var runList = (entry.run_list || []).filter(function (r) {
        return !(r.smoke_test || ((r.requests || 0) === 0 && (r.prompt_tokens || 0) === 0
          && (!r.phases || !Object.keys(r.phases).length)
          && (r.tokens || 0) === 45 && (r.elapsed_s || 0) >= 20));
      });
      if (_filterTag) {
        // "welche Chats hat dieses Modell beruehrt" - Rolle egal, Tag muss passen
        runList = runList.filter(function (r) {
          var mm = r.models || {};
          return Object.keys(mm).some(function (k) {
            var v = mm[k];
            if (Array.isArray(v)) return v.indexOf(_filterTag) !== -1;
            return v === _filterTag;
          });
        });
      }
      const uid = 'tsd-' + di;
      let runsHtml = '';
      if (runList.length) {
        runsHtml = '<div id="' + uid + '" style="display:none;margin:4px 0 2px;padding-left:6px;border-left:1px solid #1e2a38">' +
          runList.map(function(r) {
            const sr = r.stop_reason || 'completed';
            const srColor = sr === 'completed' ? '#3a9960' : (sr === 'graceful_stop' ? '#8a8a8a' : '#c08020');
            // Ein Metrik-Stil fuer alle Zeilen (2026-10-20): eine Kopfzeile mit
            // Zeit/Stop/Modell + eine Zahlenzeile - vorher drei gestapelte
            // Mini-Zeilen mit unterschiedlicher Typografie.
            const models = r.models || {};
            var _tag = function (t) {
              var s = String(t || '').replace(/^.*[\\/]/, '').replace(/\.gguf$/i, '');
              return s.length > 30 ? s.slice(0, 29) + '\u2026' : s;
            };
            // Welche Rollen waren WIRKLICH aktiv? `models` enthaelt auch Rollen,
            // die nur konfiguriert waren (z.B. critic in 70 Runs, aber nie eine
            // Runde). Aktiv heisst: Tokens oder Zeit in phases/phases_ms.
            var _ph0 = r.phases || {};
            var _pm0 = r.phases_ms || {};
            var _act = [];
            [['planner', 'P', '#9a74dc'], ['coder', 'C', '#20b0a0'], ['critic', 'K', '#e09030'],
             ['analyst', 'A', '#4f9ad0'], ['refiner', 'R', '#7fa0c0'],
             ['synthesizer', 'S', '#4fc0a0'], ['vision', 'V', '#c09040']]
              .forEach(function (d) {
                if ((_ph0[d[0]] || 0) > 0 || (_pm0[d[0]] || 0) > 0) _act.push(d);
              });
            // Pipeline-Runs erkennt man an ihren Stages (analyst/refiner/synthesizer)
            var _isPipeline = _act.some(function (d) {
              return d[0] === 'analyst' || d[0] === 'refiner' || d[0] === 'synthesizer';
            });
            // Run-Typ aus der Aktivitaet ableiten (mode ist bei Alt-Runs leer)
            var _kind = '\u2013';
            var _kindColor = '#4a5a6a';
            if (_isPipeline) {
              _kind = 'pipeline'; _kindColor = '#4f9ad0';
            } else if (_act.some(function (d) { return d[0] === 'critic'; })) {
              _kind = 'critic'; _kindColor = '#e09030';
            } else if (_act.some(function (d) { return d[0] === 'coder'; })) {
              _kind = 'agentic'; _kindColor = '#20b0a0';
            } else if (_act.some(function (d) { return d[0] === 'planner'; })) {
              _kind = 'plan'; _kindColor = '#9a74dc';
            } else if (/direct|chat/i.test(String(r.mode || ''))) {
              _kind = 'chat'; _kindColor = '#60a0e0';
            }
            var _tag = function (t) {
              var s = String(t || '').replace(/^.*[\\/]/, '').replace(/\.gguf$/i, '');
              return s.length > 26 ? s.slice(0, 25) + '\u2026' : s;
            };
            var _present = _act.map(function (d) {
              var v = models[d[0]];
              if (Array.isArray(v)) v = v[0];
              return v ? [d[0], v, d[1], d[2]] : null;
            }).filter(Boolean);
            var _unique = {};
            _present.forEach(function (p) { _unique[p[1]] = 1; });
            var _modelLine = '';
            if (_present.length && Object.keys(_unique).length === 1) {
              _modelLine = '<div style="font-size:8px;color:#8fa4b8;margin-top:1px" title="all roles: ' + esc(_present[0][1]) + '">' +
                'all roles: ' + esc(_tag(_present[0][1])) + '</div>';
            } else if (_present.length) {
              _modelLine = '<div style="font-size:8px;margin-top:1px;display:flex;flex-wrap:wrap;gap:8px">' +
                _present.map(function (p) {
                  return '<span title="' + esc(p[0]) + ': ' + esc(p[1]) + '" style="white-space:nowrap">' +
                    '<span style="color:' + p[3] + ';font-weight:600">' + p[2] + '</span> ' +
                    '<span style="color:#9ab0c4">' + esc(_tag(p[1])) + '</span></span>';
                }).join('') + '</div>';
            }
            var _meta = [];
            var _rIn = r.prompt_tokens || 0, _rCa = r.cached_tokens || 0;
            if (_rIn > 0) _meta.push('<span style="color:#6a7a8a">in</span> <span style="color:#9a74dc">' + _fmtTokens(_rIn) + '</span>');
            if (_rCa > 0) {
              var _rHit = _rIn > 0 ? Math.round((_rCa / _rIn) * 100) : 0;
              _meta.push('<span style="color:#6a7a8a">cached</span> <span style="color:#3a9960">' + _fmtTokens(_rCa) + ' (' + _rHit + '%)</span>');
              var _rNew = Math.max(0, _rIn - _rCa);
              if (_rNew > 0) _meta.push('<span style="color:#6a7a8a">recomp</span> <span style="color:#60c080">' + _fmtTokens(_rNew) + '</span>');
            }
            if (r.requests) _meta.push('<span style="color:#6a7a8a">' + r.requests + ' reqs</span>');
            var _pm = r.phases_ms || {};
            var _cSec = (_pm.coder || 0) / 1000;
            if (r.tokens && _cSec > 0) {
              var _rtps = r.tokens / _cSec;
              _meta.push('<span style="color:' + (_rtps >= 20 ? '#3a9960' : (_rtps >= 10 ? '#c0a020' : '#a06060')) +
                         ';font-weight:600">' + _rtps.toFixed(1) + ' tok/s</span>');
            }
            var _ph = r.phases || {};
            if (_ph.planner) _meta.push('<span style="color:#6a7a8a">P</span> <span style="color:#9ab0c4">' + _fmtTokens(_ph.planner) + 't</span>');
            if (_ph.coder) _meta.push('<span style="color:#6a7a8a">C</span> <span style="color:#9ab0c4">' + _fmtTokens(_ph.coder) + 't</span>');
            if (_ph.critic) _meta.push('<span style="color:#6a7a8a">K</span> <span style="color:#9ab0c4">' + _fmtTokens(_ph.critic) + 't</span>');
            if (_pm.planner || _cSec) {
              var _dur = [];
              if (_pm.planner) _dur.push(_fmtDuration(_pm.planner / 1000));
              if (_cSec) _dur.push(_fmtDuration(_cSec));
              _meta.push('<span style="color:#4a6a8a">' + _dur.join(' / ') + '</span>');
            }
            return '<div style="padding:3px 0;border-bottom:1px solid #141c26">' +
              '<div style="display:flex;justify-content:space-between;gap:6px;font-size:9px">' +
                '<span style="color:var(--tx2)">' + (r.t || '') + ' \u00b7 <span style="color:' + srColor + '">' + esc(sr) + '</span>' +
                  ' \u00b7 <span style="color:' + _kindColor + '" title="run type derived from the phases that actually ran' +
                  (r.mode ? ' (mode=' + esc(r.mode) + ')' : '') + '">' + _kind + '</span>' +
                '</span>' +
                '<span style="color:#60a0e0;font-weight:600;white-space:nowrap;flex:0 0 auto">' + _fmtTokens(r.tokens) + ' \u00b7 ' + _fmtDuration(r.elapsed_s || 0) + '</span>' +
              '</div>' +
              _modelLine +
              (_meta.length ? '<div style="font-size:8px;color:#7a8fa8;display:flex;flex-wrap:wrap;gap:6px;margin-top:1px">' + _meta.join('') + '</div>' : '') +
            '</div>';
          }).join('') + '</div>';
      }
      return '<div style="border-bottom:1px solid #1e2a38;padding:4px 2px">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;user-select:none" onclick="toggleRunDetail(\'' + uid + '\')">' +
          '<span style="font-family:\'IBM Plex Mono\',monospace;font-size:9px;color:var(--tx2)">' + day + ' <span style="color:#4a5a6a">&#9656;</span></span>' +
          '<span style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:#60a0e0;font-weight:600">' + _fmtTokens(entry.tokens) + '</span>' +
        '</div>' +
        '<div style="height:4px;background:#1a2030;border-radius:2px;margin:3px 0;overflow:hidden">' +
          '<div style="height:100%;width:' + pct + '%;background:linear-gradient(90deg,#3a6090,#60a0e0);border-radius:2px"></div>' +
        '</div>' +
        '<span style="font-family:\'IBM Plex Mono\',monospace;font-size:8px;color:#5a6a7a">' + entry.runs + ' runs \u00b7 \u00d8 ' + _fmtTokens(avgPerRun) + '/run \u00b7 ' + _fmtDuration(elapsed) + ' total</span>' +
        runsHtml +
      '</div>';
    }).join('');
  } catch(e) {
    const summaryEl = document.getElementById('token-stats-summary');
    if (summaryEl) summaryEl.innerHTML = '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:#c05050;padding:8px">Error: ' + esc(e.message) + '</div>';
  }
}

function setTokenStatsModelFilter(idx) {
  // Klick auf ein Modell in der By-model-Liste filtert die Runs darunter
  if (idx === null || idx === undefined || idx === -1) {
    S.tokenStatsModelFilter = '';
  } else {
    const list = S.tokenStatsModels || [];
    const tag = list[idx];
    S.tokenStatsModelFilter = (S.tokenStatsModelFilter === tag) ? '' : (tag || '');
  }
  loadTokenStats();
}

async function resetTokenStats() {
  if (!confirm('Reset all token statistics?')) return;
  try {
    await fetch('/soul/token-stats', {method: 'DELETE'});
    await loadTokenStats();
  } catch(e) { alert('Error: ' + e.message); }
}

