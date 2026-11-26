// Automations Panel UI (extracted from panels_learning.js)
var _autoJobs = [];
var _autoEditingId = null;
var _autoCapabilities = {};

function _autoScheduleLabel(s) {
  s = s || {};
  if (s.kind === 'daily') {
    var ds = Array.isArray(s.days) && s.days.length ? ' (' + s.days.map(function(d){ return ['Mo','Tu','We','Th','Fr','Sa','Su'][d] || d; }).join(',') + ')' : '';
    return 'daily ' + (s.at_hm || '09:00') + ds;
  }
  if (s.kind === 'once') return 'once in ' + Math.round((s.interval_s || 0) / 60) + 'm';
  return 'every ' + Math.round((s.interval_s || 0) / 60) + 'm';
}

function _autoCountdown(ts) {
  if (!ts) return 'never';
  var diff = Math.round(Number(ts) - Date.now() / 1000);
  if (diff <= 0) return 'due now';
  if (diff < 60) return 'in ' + diff + 's';
  if (diff < 3600) return 'in ' + Math.round(diff / 60) + 'm';
  var h = Math.floor(diff / 3600);
  var m = Math.round((diff % 3600) / 60);
  return 'in ' + h + 'h' + (m > 0 ? ' ' + m + 'm' : '');
}

function _autoNextLabel(ts) {
  if (!ts) return 'never';
  try {
    return new Date(Number(ts) * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) + ' (' + _autoCountdown(ts) + ')';
  } catch (e) { return '?'; }
}

function _autoModeBadgeInfo(mode) {
  var m = String(mode || 'simple').toLowerCase();
  if (m === 'agentic' || m === 'code_duo') return {label: '\u26A1 Agentic', cls: 'mode-agentic'};
  if (m === 'code_critic') return {label: '\u21C4 Code+Critic', cls: 'mode-critic'};
  if (m === 'pipeline') return {label: '\uD83D\uDD17 Pipeline', cls: ''};
  if (m === 'auto') return {label: '\uD83E\uDDED Auto', cls: ''};
  return {label: '\uD83D\uDCAC Direct', cls: 'mode-direct'};
}

function _autoTierBadgeInfo(tier) {
  var t = String(tier || 'readonly').toLowerCase();
  if (t === 'mcp') return {label: '\uD83D\uDD0C MCP + Web', cls: 'tier-mcp'};
  if (t === 'full') return {label: '\uD83D\uDD27 Full + MCP', cls: 'tier-full'};
  if (t === 'python') return {label: '\uD83D\uDC0D Python', cls: ''};
  if (t === 'research') return {label: '\uD83D\uDD0D Research', cls: ''};
  if (t === 'off') return {label: '\uD83D\uDEAB No Tools', cls: ''};
  return {label: '\uD83C\uDF10 Web Only', cls: ''};
}

function _renderAutoCapBar(caps) {
  caps = caps || {};
  var mcpEl = document.getElementById('auto-cap-mcp');
  var tgEl = document.getElementById('auto-cap-tg');
  if (mcpEl) {
    var srvs = Array.isArray(caps.mcp_servers) ? caps.mcp_servers : [];
    if (caps.mcp_enabled && srvs.length) {
      mcpEl.className = 'auto-cap-pill ok';
      mcpEl.textContent = '\uD83D\uDD0C MCP: ' + srvs.length + ' (' + srvs.slice(0, 2).join(', ') + (srvs.length > 2 ? '…' : '') + ')';
    } else if (caps.mcp_enabled) {
      mcpEl.className = 'auto-cap-pill warn';
      mcpEl.textContent = '\uD83D\uDD0C MCP: ON (no ready servers)';
    } else {
      mcpEl.className = 'auto-cap-pill';
      mcpEl.textContent = '\uD83D\uDD0C MCP: OFF';
    }
  }
  if (tgEl) {
    if (caps.telegram_configured && caps.telegram_paired) {
      tgEl.className = 'auto-cap-pill ok';
      tgEl.textContent = '\uD83D\uDCF1 Telegram: Ready';
    } else if (caps.telegram_configured) {
      tgEl.className = 'auto-cap-pill warn';
      tgEl.textContent = '\uD83D\uDCF1 Telegram: Not paired';
    } else {
      tgEl.className = 'auto-cap-pill';
      tgEl.textContent = '\uD83D\uDCF1 Telegram: Not configured';
    }
  }
}

function automationSyncModeHint() {
  var box = document.getElementById('auto-mode-hint');
  if (!box) return;
  var mode = (_autoField('auto-mode') || 'simple').toLowerCase();
  var tier = (_autoField('auto-tier') || 'mcp').toLowerCase();
  var modeDesc = mode === 'agentic'
    ? '\u26A1 Agentic: Autonomous multi-step loop for research, scripts, files & complex MCP workflows.'
    : mode === 'code_critic'
      ? '\u21C4 Code+Critic: Coder + Reviewer pair (experimental for automations).'
      : mode === 'pipeline'
        ? '\uD83D\uDD17 Pipeline: Multi-model analyst \u2192 refiner \u2192 critic \u2192 synthesizer.'
        : mode === 'auto'
          ? '\uD83E\uDDED Auto: Router picks Direct vs Agentic dynamically.'
          : '\uD83D\uDCAC Direct: Fast single-model turn — ideal for recurring MCP summaries & notifications.';
  var tierDesc = tier === 'mcp'
    ? '\uD83D\uDD0C MCP + Web + Read enabled (Mail, Calendar, connected MCP APIs; no shell/write).'
    : tier === 'full'
      ? '\uD83D\uDD27 Full access: MCP tools + file edits + Python + bash.'
      : tier === 'off'
        ? '\uD83D\uDEAB Tools OFF: pure text generation only.'
        : tier === 'python'
          ? '\uD83D\uDC0D Python + workspace read + web (no MCP).'
          : tier === 'research'
            ? '\uD83D\uDD0D Read workspace + web search (no MCP).'
            : '\uD83C\uDF10 Websearch + dialog only (no MCP — switch to MCP/Full for Mail/API tools).';
  box.textContent = modeDesc + ' ' + tierDesc;
}

function automationApplyTemplate(tplKey) {
  automationNew();
  var tpls = {
    mail_mcp_tg: {
      name: 'MCP Mail Summary',
      kind: 'every', interval: '30',
      mode: 'simple', tier: 'mcp', notify_tg: true,
      prompt: 'Check my unread emails from the last 30 minutes using the connected MCP mail tools. Summarize important messages with sender, subject, and concrete action items concisely.'
    },
    agentic_mcp_tg: {
      name: 'Agentic MCP Triage',
      kind: 'every', interval: '60',
      mode: 'agentic', tier: 'full', notify_tg: true,
      prompt: 'Inspect incoming tasks/messages via connected MCP tools, cross-check relevant workspace notes/files, execute any safe follow-up checks, and send a concise status briefing.'
    },
    daily_digest_tg: {
      name: 'Morning Briefing',
      kind: 'daily', time: '08:00',
      mode: 'simple', tier: 'mcp', notify_tg: true,
      prompt: 'Create my morning briefing using MCP calendar/mail tools and web search for today. Highlight urgent deadlines, meetings, and key updates.'
    },
    repo_health: {
      name: 'Daily Repo Health',
      kind: 'daily', time: '09:00',
      mode: 'agentic', tier: 'full', notify_tg: false,
      prompt: 'Run the test suite and check git status/recent logs in the workspace. Summarize any failing tests, regressions, or uncommitted changes.'
    }
  };
  var t = tpls[tplKey];
  if (!t) return;
  _autoField('auto-name', t.name);
  _autoField('auto-prompt', t.prompt);
  _autoField('auto-kind', t.kind);
  if (t.interval) _autoField('auto-interval', t.interval);
  if (t.time) _autoField('auto-time', t.time);
  _autoField('auto-mode', t.mode);
  _autoField('auto-tier', t.tier);
  var tg = document.getElementById('auto-notify-tg');
  if (tg) tg.checked = !!t.notify_tg;
  automationScheduleChanged();
  automationSyncModeHint();
}

async function loadAutomations() {
  const list = document.getElementById('automation-list');
  if (!list) return;
  try {
    const d = await (await fetch('/automations')).json();
    _autoJobs = d.automations || [];
    _autoCapabilities = d.capabilities || {};
    _renderAutoCapBar(_autoCapabilities);
    if (!_autoJobs.length) {
      list.innerHTML = '<div class="empty">No automations yet. Pick a template above or create one below.</div>';
      return;
    }
    list.innerHTML = '';
    _autoJobs.forEach(function(job) {
      const card = document.createElement('div');
      card.className = 'auto-card' + (job.enabled ? '' : ' paused');

      const head = document.createElement('div');
      head.className = 'auto-card-head';
      const dot = document.createElement('span');
      dot.className = 'auto-status-dot';
      dot.style.background = !job.enabled ? 'var(--tx3)'
        : (job.last_status === 'ok' ? 'var(--green)'
        : (job.last_status === 'needs_input' || job.last_status === 'partial' || job.last_status === 'queued' ? '#e0a030'
        : (job.last_status === 'failed' || job.last_status === 'failed_chat' ? '#c05050' : 'var(--tx2)')));
      const title = document.createElement('div');
      title.className = 'auto-card-title';
      title.textContent = job.name || 'Automation';
      head.appendChild(dot);
      head.appendChild(title);

      const prev = document.createElement('div');
      prev.className = 'auto-card-prompt';
      prev.textContent = job.prompt || '';

      const badges = document.createElement('div');
      badges.className = 'auto-badges';
      function addBadge(text, extraCls) {
        const b = document.createElement('span');
        b.className = 'auto-badge' + (extraCls ? ' ' + extraCls : '');
        b.textContent = text;
        badges.appendChild(b);
      }
      var mb = _autoModeBadgeInfo(job.mode);
      addBadge(mb.label, mb.cls);
      var tb = _autoTierBadgeInfo(job.tools_tier);
      addBadge(tb.label, tb.cls);
      addBadge(job.notify_telegram ? '\uD83D\uDCF1 Telegram' : '\uD83D\uDCAC Local Chat', job.notify_telegram ? 'tg-on' : '');
      addBadge('\u23F1 ' + _autoScheduleLabel(job.schedule), '');
      if (job.workspace) {
        var wsName = String(job.workspace).replace(/\\/g, '/').split('/').filter(Boolean).pop() || job.workspace;
        addBadge('\uD83D\uDCC1 ' + wsName, '');
      }

      const meta = document.createElement('div');
      meta.className = 'auto-meta-row';
      var durTxt = (job.last_duration_s !== null && job.last_duration_s !== undefined) ? (' (' + job.last_duration_s + 's)') : '';
      var lastTxt = job.last_status ? (' \u00B7 last: ' + job.last_status + durTxt) : '';
      var firesTxt = job.fires ? (' \u00B7 ' + job.fires + 'x') : '';
      meta.textContent = (job.enabled ? ('Next: ' + _autoNextLabel(job.next_ts)) : 'Paused') + lastTxt + firesTxt;

      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:4px;margin-top:4px;flex-wrap:wrap';
      function mk(label, fn, cls, titleTxt) {
        const b = document.createElement('button');
        b.textContent = label;
        b.className = cls || 'pload';
        b.style.marginRight = '0';
        if (titleTxt) b.title = titleTxt;
        b.onclick = function(e) { e.stopPropagation(); fn(); };
        actions.appendChild(b);
      }
      mk('\u25B6 Run', function() { automationRun(job.id); }, null, 'Run now');
      if (job.chat_id) {
        mk('\uD83D\uDCAC Chat', function() { loadChat(job.chat_id); }, null, 'Open automation chat transcript');
      }
      mk('\u270E Edit', function() { automationEdit(job.id); });
      mk('Runs', function() { automationShowRuns(job.id); });
      mk(job.enabled ? '\u23F8 Pause' : '\u25B6 Enable', function() { automationToggle(job.id); });
      mk('\u00D7', function() { automationDelete(job.id); }, 'pdel', 'Delete automation');

      card.appendChild(head);
      card.appendChild(prev);
      card.appendChild(badges);
      card.appendChild(meta);
      card.appendChild(actions);
      list.appendChild(card);
    });
  } catch (e) {
    list.innerHTML = '<div class="empty">Could not load automations.</div>';
  }
}

var _autoDays = [];

function _buildDayChips() {
  const row = document.getElementById('auto-days-row');
  if (!row) return;
  row.innerHTML = '';
  ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'].forEach(function(label, i) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.textContent = label;
    chip.className = 'pload';
    chip.style.marginRight = '0';
    chip.style.color = _autoDays.indexOf(i) >= 0 ? 'var(--amber)' : '';
    chip.onclick = function() {
      const at = _autoDays.indexOf(i);
      if (at >= 0) _autoDays.splice(at, 1); else _autoDays.push(i);
      chip.style.color = _autoDays.indexOf(i) >= 0 ? 'var(--amber)' : '';
      automationPreview();
    };
    row.appendChild(chip);
  });
}

function automationScheduleChanged() {
  const kind = (document.getElementById('auto-kind') || {}).value;
  const ir = document.getElementById('auto-interval-row');
  const tr = document.getElementById('auto-time-row');
  if (ir) ir.style.display = kind === 'daily' ? 'none' : 'flex';
  if (tr) tr.style.display = kind === 'daily' ? 'block' : 'none';
  if (kind === 'daily') { _buildDayChips(); }
  automationPreview();
}

function _readAutoSchedule() {
  const kind = _autoField('auto-kind') || 'every';
  if (kind === 'daily') {
    return {kind: 'daily', at_hm: _autoField('auto-time') || '09:00', days: _autoDays.slice()};
  }
  const minutes = Math.max(1, parseInt(_autoField('auto-interval'), 10) || 60);
  return {kind: kind, interval_s: minutes * 60};
}

async function automationPreview() {
  const box = document.getElementById('auto-preview');
  if (!box) return;
  const schedule = _readAutoSchedule();
  try {
    const r = await fetch('/automations/preview', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({schedule: schedule}),
    });
    if (!r.ok) { box.textContent = ''; return; }
    const d = await r.json().catch(function() { return {}; });
    const times = (d.next || []).map(function(ts) {
      return new Date(Number(ts) * 1000).toLocaleString();
    });
    box.textContent = times.length
      ? ('next: ' + times.join(' · ')) : '';
  } catch (e) { box.textContent = ''; }
}

function _autoShowEditor(show) {
  const el = document.getElementById('automation-editor');
  if (!el) return;
  el.style.display = show ? 'block' : 'none';
  // EDITOR-BELOW-FOLD (audit 2026-11-05): with a long job list the editor
  // sits under the fold - "+ New automation" looked like a no-op.
  if (show) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function _autoField(id, value) {
  const e = document.getElementById(id);
  if (e && arguments.length > 1) e.value = value;
  return e ? e.value : '';
}

function automationNew() {
  _autoEditingId = null;
  _autoField('auto-name', '');
  _autoField('auto-prompt', '');
  _autoField('auto-kind', 'every');
  _autoField('auto-interval', '30');
  _autoField('auto-time', '09:00');
  _autoDays = [];
  _autoField('auto-mode', 'simple');
  _autoField('auto-tier', 'mcp');
  var tg = document.getElementById('auto-notify-tg'); if (tg) tg.checked = false;
  _autoField('auto-workspace', '');
  _autoField('auto-policy', 'skip');
  const en = document.getElementById('auto-enabled'); if (en) en.checked = true;
  const title = document.getElementById('automation-editor-title');
  if (title) title.textContent = 'New automation';
  automationScheduleChanged();
  automationSyncModeHint();
  _autoShowEditor(true);
}

function automationEdit(jobId) {
  const job = _autoJobs.find(function(j) { return j.id === jobId; });
  if (!job) return;
  _autoEditingId = jobId;
  const s = job.schedule || {};
  _autoField('auto-name', job.name || '');
  _autoField('auto-prompt', job.prompt || '');
  _autoField('auto-kind', s.kind === 'daily' ? 'daily' : (s.kind === 'once' ? 'once' : 'every'));
  _autoField('auto-interval', String(Math.max(1, Math.round((s.interval_s || 3600) / 60))));
  _autoField('auto-time', s.at_hm || '09:00');
  _autoDays = Array.isArray(s.days) ? s.days.slice() : [];
  var m = String(job.mode || 'simple').toLowerCase();
  if (m === 'code_duo') m = 'agentic';
  if (m === 'direct') m = 'simple';
  _autoField('auto-mode', m);
  _autoField('auto-tier', job.tools_tier || 'readonly');
  var tg = document.getElementById('auto-notify-tg'); if (tg) tg.checked = !!job.notify_telegram;
  _autoField('auto-workspace', job.workspace || '');
  _autoField('auto-policy', job.policy || 'skip');
  const en = document.getElementById('auto-enabled'); if (en) en.checked = !!job.enabled;
  const title = document.getElementById('automation-editor-title');
  if (title) title.textContent = 'Edit automation';
  automationScheduleChanged();
  automationSyncModeHint();
  _autoShowEditor(true);
}

function automationCancel() {
  _autoEditingId = null;
  _autoShowEditor(false);
}

async function automationSave() {
  const schedule = _readAutoSchedule();
  const prompt = _autoField('auto-prompt').trim();
  if (!prompt) { showStatus('⚠ A prompt is required.'); return; }
  const payload = {
    name: _autoField('auto-name') || 'Automation',
    prompt: prompt,
    schedule: schedule,
    mode: _autoField('auto-mode') || 'simple',
    tools_tier: _autoField('auto-tier') || 'mcp',
    notify_telegram: !!(document.getElementById('auto-notify-tg') || {}).checked,
    workspace: _autoField('auto-workspace') || '',
    policy: _autoField('auto-policy') || 'skip',
    enabled: !!(document.getElementById('auto-enabled') || {}).checked,
  };
  try {
    const url = _autoEditingId ? '/automations/' + _autoEditingId : '/automations';
    const r = await fetch(url, {
      method: _autoEditingId ? 'PUT' : 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const d = await r.json().catch(function() { return {}; });
    if (!r.ok) { showStatus('⚠ ' + (d.error || ('HTTP ' + r.status))); return; }
    _autoEditingId = null;
    _autoShowEditor(false);
    showStatus('Automation saved.');
    loadAutomations();
  } catch (e) {
    showStatus('Saving automation failed: ' + e.message);
  }
}

async function automationRun(jobId) {
  try {
    const r = await fetch('/automations/' + jobId + '/run', {method: 'POST'});
    const d = await r.json().catch(function() { return {}; });
    showStatus(d.ok ? '▶ Automation started.' : ('⚠ ' + (d.note || 'not started')));
    loadAutomations();
  } catch (e) { showStatus('Run failed: ' + e.message); }
}

async function automationToggle(jobId) {
  try {
    const r = await fetch('/automations/' + jobId + '/toggle', {method: 'POST'});
    if (!r.ok) {
      const d = await r.json().catch(function() { return {}; });
      showStatus('⚠ ' + (d.error || ('HTTP ' + r.status)));
      return;
    }
    loadAutomations();
  } catch (e) { showStatus('Toggle failed: ' + e.message); }
}

async function automationDelete(jobId) {
  if (!confirm('Delete this automation? Its chat history stays.')) return;
  try {
    const r = await fetch('/automations/' + jobId, {method: 'DELETE'});
    if (!r.ok) {
      const d = await r.json().catch(function() { return {}; });
      showStatus('⚠ ' + (d.error || ('HTTP ' + r.status)));
      return;
    }
    showStatus('Automation deleted.');
    const _runsBox = document.getElementById('automation-runs');
    if (_runsBox) _runsBox.innerHTML = '';
    loadAutomations();
  } catch (e) { showStatus('Delete failed: ' + e.message); }
}

async function automationShowRuns(jobId) {
  const box = document.getElementById('automation-runs');
  if (!box) return;
  try {
    const d = await (await fetch('/automations/' + jobId + '/runs')).json();
    const runs = d.runs || [];
    if (!runs.length) { box.innerHTML = '<div class="empty">No runs yet.</div>'; return; }
    box.innerHTML = '<div class="sec">Recent runs</div>';
    runs.slice().reverse().forEach(function(run) {
      const row = document.createElement('div');
      row.className = 'chat-hist-meta';
      row.style.marginBottom = '4px';
      var icon = run.status === 'ok' ? '\u2705' : (run.status === 'needs_input' ? '\u2753' : '\u26A0');
      var dur = (run.duration_s !== undefined && run.duration_s !== null)
        ? (' (' + run.duration_s + 's)')
        : (run.finished && run.started ? (' (' + (Number(run.finished) - Number(run.started)).toFixed(1) + 's)') : '');
      row.textContent = icon + ' ' + (run.status || '?') + dur + ' \u00B7 '
        + (run.stop_reason || '') + ' \u00B7 '
        + new Date(Number(run.started || run.ts || 0) * 1000).toLocaleString();
      box.appendChild(row);
    });
  } catch (e) { box.innerHTML = '<div class="empty">Could not load runs.</div>'; }
}


