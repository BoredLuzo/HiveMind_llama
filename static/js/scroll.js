// HiveMind UI module: Sticky chat follow & batched scroll helpers.
// ── Batched scroll: coalesce multiple scrollBtm() calls per frame ─────────
// STICKY-FIX (2026-08-10): double rAF — rAF callbacks run BEFORE the layout pass,
// freshly appended content (token flush, tool chips) was not yet in the
// scrollHeight when measuring → you did not stick to the bottom.
var _scrollRafId = null;
function scrollBtm() {
  // Batch: instead of forcing layout per call, schedule a single rAF
  if (!_scrollRafId) {
    _scrollRafId = requestAnimationFrame(function() {
      requestAnimationFrame(function() {
        _scrollRafId = null;
        const c = document.getElementById('chat');
        if (c) c.scrollTop = c.scrollHeight;
      });
    });
  }
}

// STICKY-FOLLOW (2026-09-18): chat intent is tracked by a scroll listener —
// while the user sits at the bottom, every append pins the view (no matter
// how much ONE render grew); scrolling up pauses the follow, returning to
// the bottom resumes it. The old per-call delta guess (delta <= threshold)
// died permanently the first time a single render grew past the threshold.

var _chatFollow = true;

function _chatEnsureFollowListener() {
  var c = document.getElementById('chat');
  if (!c || c._followWired) return;
  c._followWired = true;
  c._userUpTs = 0;
  c.addEventListener('scroll', function() {
    var dist = c.scrollHeight - (c.scrollTop + c.clientHeight);
    if (c._progScrollGuard && c.scrollTop >= (c._progScrollTarget || 0) - 2) {
      c._progScrollGuard = false;
      return;
    }
    c._progScrollGuard = false;
    if (!_chatFollow) {
      if (dist <= 24 && (Date.now() - (c._userUpTs || 0)) > 180) _chatFollow = true;
    } else if (dist > 60) {
      _chatFollow = false;
    }
  }, { passive: true });
  c.addEventListener('wheel', function(e) {
    if (!e || e.deltaY >= 0) return;
    var _inner = e.target && e.target.closest ? e.target.closest('.think-body, #planner-think-content') : null;
    if (_inner && _inner.scrollTop > 0) return;
    _chatFollow = false;
    c._userUpTs = Date.now();
  }, { passive: true });
}

function scrollBtmIfNearBottom(thresholdPx) {
  // thresholdPx kept for the 50+ call sites; intent now lives in _chatFollow.
  _chatEnsureFollowListener();
  if (!_chatFollow) return;
  if (!window._scrollNearRaf) {
    window._scrollNearRaf = requestAnimationFrame(function() {
      requestAnimationFrame(function() {
        window._scrollNearRaf = null;
        if (!_chatFollow) return;
        const c = document.getElementById('chat');
        if (!c) return;
        c._progScrollGuard = true;
        c.scrollTop = c.scrollHeight;
        c._progScrollTarget = c.scrollTop;
      });
    });
  }
}

function _elEnsureFollowListener(el) {
  if (!el || el._followWired) return;
  el._followWired = true;
  el._followBottom = true;
  el._userUpTs = 0;
  if (typeof el.addEventListener !== 'function') return;
  el.addEventListener('scroll', function() {
    var dist = el.scrollHeight - (el.scrollTop + el.clientHeight);
    if (el._progScrollGuard && el.scrollTop >= (el._progScrollTarget || 0) - 2) {
      el._progScrollGuard = false;
      return;
    }
    el._progScrollGuard = false;
    if (el._followBottom === false) {
      if (dist <= 12 && (Date.now() - (el._userUpTs || 0)) > 180) el._followBottom = true;
    } else if (dist > 24) {
      el._followBottom = false;
    }
  }, { passive: true });
  el.addEventListener('wheel', function(e) {
    if (e && e.deltaY < 0) {
      el._followBottom = false;
      el._userUpTs = Date.now();
    }
  }, { passive: true });
  el.addEventListener('pointerdown', function() {
    el._userUpTs = Date.now();
  }, { passive: true });
  el.addEventListener('touchstart', function() {
    el._userUpTs = Date.now();
  }, { passive: true });
}

function _scrollElToBottomSoon(el) {
  if (!el) return;
  _elEnsureFollowListener(el);
  if (el._followBottom === false || el._scrollRaf) return;
  el._scrollRaf = requestAnimationFrame(function() {
    el._scrollRaf = null;
    if (_chatFollow && el._followBottom !== false && el.isConnected) {
      el._progScrollGuard = true;
      el.scrollTop = el.scrollHeight;
      el._progScrollTarget = el.scrollTop;
    }
  });
}
// END_SCROLL_HELPERS
