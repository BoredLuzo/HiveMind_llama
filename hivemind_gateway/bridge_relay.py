"""Engine-run mirror, approval-card callback, and steering mixin for RunBridge."""
from __future__ import annotations

import json
import time

from httpx import HTTPError

from . import render
from .hive_client import HiveUnreachable
from .telegram_api import TelegramApiError

# JOURNAL-AFTER (audit A2): a cursor past the window makes the server return
# the response meta with EMPTY frames — used by the second-run peek, which
# reads only run_id/active/done/aborted.
JOURNAL_META_ONLY_AFTER = 1 << 30

def _now() -> float:
    return time.monotonic()


def _card_did_matches(expected_did: str, card_did: str) -> bool:
    """Fail-closed check: require exact card_did when expected_did is non-empty."""
    if expected_did:
        return bool(card_did) and card_did == expected_did
    return not card_did




class _BridgeRelayMixin:
    # -- P10: run takeover - mirror a UI/engine run to the phone ---------

    async def _flush_mirror_thinking_pair(self, m: dict) -> None:
        _tp = [m.get("planner_think") or "", m.get("tick_think") or ""]
        try:
            await self._flush_thinking_pair(_tp)
        finally:
            m["planner_think"], m["tick_think"] = _tp

    def _mirror(self) -> dict:
        if not hasattr(self, "_mirror_state"):
            self._mirror_state = {"run_id": None, "after": 0,
                                  "status_msg_id": None,
                                  "last_status": "", "approval_sig": None,
                                  "approval_decision_id": "",
                                  "approval_card_did": "",
                                  "approval_tool": "",
                                  "other_run_noted": "", "last_phase": "",
                                  "tool_relay_seen": {},
                                  "answer_parts": [],
                                  "tick_think": "",
                                  "planner_think": "",
                                  "last_tick": 0.0}
        return self._mirror_state

    def _mirror_reset(self, m: dict) -> None:
        """End a mirror session - every reset site must clear the approval
        card metadata too (G2: decision_id/tool of the relayed card)."""
        m.update({"run_id": None, "after": 0, "status_msg_id": None,
                  "last_status": "", "approval_sig": None,
                  "approval_decision_id": "", "approval_card_did": "",
                  "approval_tool": "",
                  "other_run_noted": "", "last_phase": "",
                  "tool_relay_seen": {}, "answer_parts": [],
                  "tick_think": "", "planner_think": "",
                  "ctx_marks": set(), "ctx_compression_noted": False,
                  "answer_sent_upto": 0})
        # A new mirror run may legitimately send the same filename again.
        self._sent_files.clear()

    def _own_run_id(self) -> str | None:
        run = self.state.data.get("active_run") or {}
        return run.get("run_id")

    def _mirror_enabled(self, engine_settings: dict) -> bool:
        """The UI toggle (telegram_mirror_enabled) gates the takeover.
        Key absent = off (fail-closed, same rule as the veto)."""
        return bool(engine_settings.get("telegram_mirror_enabled"))

    async def mirror_tick(self) -> None:
        """One mirror step (called every few seconds from the mirror
        task). Watches the engine journal for an active run that is NOT
        the gateway's own, relays it to the phone, relays approval
        cards. Never raises."""
        try:
            await self._mirror_tick_inner()
        except (HiveUnreachable, OSError) as exc:
            self._log_note(f"mirror tick: engine unreachable ({exc})")
        except (TelegramApiError, ValueError, TypeError, KeyError,
                AttributeError) as exc:
            # deep audit N3: a malformed frame must not silently kill the
            # mirror task - log and continue with the next tick
            self._log_note(f"mirror tick failed: {type(exc).__name__}: {exc}")

    def _parse_done(self, frames: list | None) -> str | None:
        """stop_reason of the LAST done frame in the journal, if any."""
        for raw in reversed(frames or []):
            if isinstance(raw, str) and '"type": "done"' in raw:
                try:
                    payload = json.loads(raw[6:])
                    return str(payload.get("stop_reason") or "completed")
                except (ValueError, TypeError):
                    continue
        return None

    async def _mirror_tick_inner(self) -> None:
        m = self._mirror()
        try:
            s = await self.hive.settings()
        except (HiveUnreachable, OSError, ValueError):
            return  # engine unreachable: nothing to mirror, keep quiet
        # UI toggle telegram_phone_restricted: stash for /status + the
        # supervisor heartbeat (best-effort view, refreshed every tick);
        # missing key = restricted (safe default)
        if hasattr(self.ms, "phone_restricted"):
            self.ms.phone_restricted = bool(s.get("telegram_phone_restricted",
                                                  True))
        if not self._mirror_enabled(s):
            if m["run_id"]:
                self._mirror_reset(m)
                await self.ms.send_message(
                    "📴 Mirror ended (setting off in the UI).")
            return
        # audit G7: once a session is active, fetch THAT run's journal -
        # the unscoped endpoint answers with the most recently ACTIVE run,
        # which can be the wrong one when a second run starts.
        # JOURNAL-AFTER (audit A2, 2026-10-23): m["after"] is the logical
        # frame cursor this mirror already maintains — send it so the server
        # slices and the tick stops re-downloading the whole window. The
        # done/abort branch below still receives the tail frames it parses
        # (they lie AFTER the cursor by definition).
        from . import bridge as _b
        j = await self.hive.journal(m["run_id"], after=int(m.get("after") or 0)) \
            if m["run_id"] else await self.hive.journal(after=_b.JOURNAL_META_ONLY_AFTER)
        if not isinstance(j, dict) or not j.get("active") \
                or j.get("done") or j.get("aborted"):
            # nothing active: end a session, report how it ended
            if m["run_id"]:
                rid, frames = m["run_id"], j.get("frames") \
                    if isinstance(j, dict) else None
                reason = self._parse_done(frames) or "finished"
                # R6 FAZIT ON EARLY DONE (owner report, live 10:2x): a
                # run that finished between two ticks landed HERE and
                # the phone got a bare "finished" line - the produced
                # answer was discarded. Drain accumulated tokens; fall
                # back to the full journal frames when empty.
                _acc = (self._accumulate_frames(frames or [])[0]
                        if isinstance(j, dict) else "")
                _ans = "".join(m.get("answer_parts") or []).strip() or _acc
                self._mirror_reset(m)
                await self._mirror_fazit(rid, _ans, reason)
            elif isinstance(j, dict) and j.get("done") and not j.get("aborted"):
                # R6 LATE RESULT (user feedback: messages must arrive): a run
                # that started AND finished while the mirror was not
                # attached still delivers its result - once per run id,
                # only for runs finished during THIS gateway process.
                _lr_id = str(j.get("run_id") or "")
                if (_lr_id and float(j.get("ts") or 0) > self._proc_start
                        and _lr_id not in self._late_relayed
                        and _lr_id != str(self._own_run_id() or "")):
                    self._late_relayed.add(_lr_id)
                    if not j.get("frames") and int(j.get("n") or 0) > 0:
                        _full = await self.hive.journal(_lr_id, after=0)
                        if isinstance(_full, dict):
                            j = _full
                    _lr_ans, _ = self._accumulate_frames(j.get("frames") or [])
                    await self._mirror_fazit(
                        _lr_id, _lr_ans,
                        self._parse_done(j.get("frames")) or "completed")
            return
        rid = str(j.get("run_id") or "")
        # deep audit N7 (confirmed as G7): while OUR run sits between
        # POST /stream and its run_id frame, active_run exists with
        # run_id None - the unscoped journal is then necessarily our own
        # run. Skip the takeover decision this tick instead of announcing
        # the gateway's own run as a takeover.
        _own = self.state.data.get("active_run")
        if not m["run_id"] and isinstance(_own, dict) \
                and not _own.get("run_id"):
            return
        if rid == str(self._own_run_id() or ""):
            self._late_relayed.add(rid)  # own path relays it
            return  # our own run reports natively; do not double-mirror
        fresh = m["run_id"] != rid
        if fresh:
            if not j.get("frames") and int(j.get("n") or 0) > 0:
                _full = await self.hive.journal(rid, after=0)
                if isinstance(_full, dict):
                    j = _full
            # VOICE-PRELOAD for mirrored runs: the phone gets a voice note for a
            # desktop run too, so the sidecar must be warm BEFORE the answer is
            # done (a cold start here cost 48 s on 2026-10-18).
            await self._preload_voice()
        if fresh:
            m.update({"run_id": rid, "after": int(j.get("n") or 0),
                      "status_msg_id": None, "last_status": "",
                      "approval_sig": None, "approval_decision_id": "",
                      "approval_card_did": "",
                      "approval_tool": "", "other_run_noted": "",
                      "last_phase": "", "answer_parts": [],
                      "tick_think": "", "planner_think": "",
                      "ctx_marks": set(), "ctx_compression_noted": False,
                      "answer_sent_upto": 0})
            # R6 CATCH-UP (user feedback: messages must arrive): process the
            # pre-adoption frames SILENTLY - the answer accumulates so
            # the fazit relay at done carries the FULL result, and tool
            # repeats dedupe against pre-adoption calls. No sends here.
            _catchup_pct = 0
            _catchup_comp = False
            _catchup_lim = 0
            _duo_label = ""
            _plan_catchup = ""
            for _raw in (j.get("frames") or []):
                if not isinstance(_raw, str):
                    continue
                try:
                    _cev = json.loads(
                        _raw[6:] if _raw.startswith("data: ") else _raw)
                except ValueError:
                    continue
                if _cev.get("type") == "token":
                    m["answer_parts"].append(str(_cev.get("content") or ""))
                elif _cev.get("type") == "agent" and m["answer_parts"]:
                    m["answer_parts"] = []  # phase switch: last phase wins
                elif _cev.get("type") == "tool_call":
                    # pre-count so a repeat loop already running before
                    # the adoption dedupes instead of re-flooding
                    _tl = self._tool_line(_cev)
                    if _tl:
                        _saw = m.setdefault("tool_relay_seen", {})
                        _saw[_tl] = _saw.get(_tl, 0) + 1
                        # CATCHUP-TOOL-DIGEST (2026-10-11): tool calls made
                        # before the mirror bound were relayed never (user request:
                        # "alle tool calls, nicht nur write/read"). The last
                        # ones ride the adoption note below.
                        _ctl = m.setdefault("catchup_tool_lines", [])
                        if not _ctl or _ctl[-1] != _tl:
                            _ctl.append(_tl)
                elif _cev.get("type") == "ctx_meter":
                    # TG-AUDIT: STATE events are silently skipped in the
                    # replay (no spam), but the mirror must still land on
                    # the CURRENT state - milestones already passed are
                    # pre-marked and a live compression is announced once.
                    try:
                        _e = int(_cev.get("est_tokens") or 0)
                        _l = int(_cev.get("ctx_limit") or 0)
                    except (TypeError, ValueError):
                        _e = _l = 0
                    if _e > 0 and _l > 0:
                        _catchup_pct = max(_catchup_pct, _e * 100 // _l)
                        _catchup_lim = _l
                        _catchup_comp = bool(_cev.get("compressing"))
                elif _cev.get("type") == "duo_start":
                    _dl = str(_cev.get("label") or "").strip()
                    if _dl:
                        _duo_label = _dl
                elif _cev.get("type") == "planner_result":
                    # late adoption: the plan finished before the takeover -
                    # summarize it into the note instead of dropping it
                    _pch = _cev.get("chunks") or []
                    if isinstance(_pch, list) and _pch:
                        _titles = [str((c.get("title") or c.get("raw") or "")).strip()
                                   for c in _pch if isinstance(c, dict)]
                        _titles = [t for t in _titles if t]
                        if _titles:
                            _plan_catchup = ("Plan (" + str(len(_titles))
                                             + " steps): " + " | ".join(_titles))
                    if not _plan_catchup:
                        _pth = str(_cev.get("thinking") or "").strip()
                        if _pth:
                            _plan_catchup = "Plan: " + _pth
            if _catchup_pct:
                m["ctx_marks"] = {t for t in (50, 75, 90)
                                  if _catchup_pct >= t}

            _note = (f"📢 Engine run taken over (mirror): {rid}\n"
                     "Your texts are queued (steering), approvals come "
                     "to you here. /stop aborts it.")
            if len(j.get("frames") or []) > 80:
                _note = ("📢 Re-attached to engine run "
                         f"{rid} (gateway restarted) - mid-run context "
                         "was rebuilt silently.\n"
                         "Steering queued, approvals come here. "
                         "/stop aborts it.")
            if _duo_label:
                _note += "\n🤖 " + _duo_label[:200]
            if _plan_catchup:
                _note += "\n🧠 " + _plan_catchup[:600]
            _catchup_tools = (m.get("catchup_tool_lines") or [])[-6:]
            if _catchup_tools:
                _nlc = "\n"
                _note += (_nlc + "🔧" + _nlc.join(
                    "🔧" + " " + _ct for _ct in _catchup_tools)
                    + _nlc + "(earlier tool activity, newest last)")
            await self.ms.send_message(_note)
            if _catchup_comp and _catchup_lim:
                await self.ms.send_message(
                    "🗜 Context is compressing "
                    f"(~{_catchup_pct}% of {_catchup_lim // 1024}k) - "
                    "started before the takeover.")
            m["after"] = int(j.get("n") or 0)
            return  # next tick streams the delta

        # SECOND-RUN VISIBILITY (2026-10-06, audit T2): while bound to one
        # run the mirror never looks at another one - a second engine run
        # used to run entirely unseen on the phone. Peek the unscoped
        # journal (most-recently-active) once per tick; when a DIFFERENT
        # active run surfaces, say so once per foreign run id.
        if m["run_id"]:
            try:
                # META-ONLY PEEK (audit A2, 2026-10-23): this peek reads only
                # run_id/active/done/aborted — a cursor past the window makes
                # the server return empty frames, so the second-run check
                # stops hauling the full journal every tick.
                from . import bridge as _b
                _j_all = await self.hive.journal(after=_b.JOURNAL_META_ONLY_AFTER)
                _other = str((_j_all or {}).get("run_id") or "")
                if (_other and _other != m["run_id"]
                        and _other != str(self._own_run_id() or "")
                        and (_j_all or {}).get("active")
                        and not (_j_all or {}).get("done")
                        and not (_j_all or {}).get("aborted")
                        and m.get("other_run_noted") != _other):
                    m["other_run_noted"] = _other
                    await self.ms.send_message(
                        f"ℹ️ Another engine run is active ({_other[:18]}…) - "
                        f"still mirroring {m['run_id'][:18]}… (only one run "
                        "mirrors at a time).")
            except (HiveUnreachable, OSError, ValueError, TypeError,
                    KeyError, TelegramApiError):
                pass  # the mirrored run's own relay continues regardless

        # delta frames since last tick (TG-AUDIT 2026-10-07): `n` is the
        # LOGICAL frame count and `base` the window offset once the
        # engine's 20k-frame cap sliced old frames away. The old
        # len(frames)-tracking went BLIND at the cap: the length stayed
        # pinned while logical frames kept flowing, so every delta - tool
        # lines, statuses, the fazit answer - was lost forever.
        frames = j.get("frames") or []
        _base = int(j.get("base") or 0)
        after = int(m["after"] or 0)
        _n_total = int(j.get("n") or (_base + len(frames)))
        # server.py /run/journal?after=N already returns the pre-sliced delta
        # frames[max(0, after - _base):] while reporting the window's _base.
        # Only slice client-side when the server returned the unsliced window
        # (e.g. legacy/unit-test stubs where _base + len(frames) == _n_total
        # and after > _base).
        if after > _base and (_base + len(frames)) == _n_total and len(frames) > max(0, _n_total - after):
            _start = after - _base
            new_frames = frames[max(0, _start):] if _start < len(frames) else []
        else:
            new_frames = list(frames)
        m["after"] = _n_total
        done_reason = self._parse_done(new_frames)
        last_status = ""
        tool_lines: list[str] = []
        # R4 relay: phase/plan/thinking visibility on the phone (persisted in m across ticks)
        last_phase = m.get("last_phase", "")
        for raw in new_frames:
            if not isinstance(raw, str):
                continue
            try:
                ev = json.loads(raw[6:]) if raw.startswith("data: ") else {}
            except ValueError:
                continue
            if ev.get("type") == "status":
                last_status = str(ev.get("content") or "")[:120]
            elif ev.get("type") == "token":
                await self._flush_mirror_thinking_pair(m)
                # FAZIT RELAY (2026-10-06, owner report): the takeover never
                # delivered the run's RESULT - the phone watched status lines
                # and then got a bare "🏁 completed". Accumulate the answer
                # tokens; an agent event starts a new phase, so only the LAST
                # phase's tokens survive (planner/critic tokens use their own
                # event types and never land here).
                m["answer_parts"].append(str(ev.get("content") or ""))
            elif ev.get("type") == "agent":
                await self._flush_mirror_thinking_pair(m)
                if m["answer_parts"]:
                    m["answer_parts"] = []
                # R4: phase switches (Planner / Code / Answer) as own lines
                _ph = str(ev.get("content") or "").strip()
                if _ph and _ph != last_phase:
                    await self.ms.send_message(f"→ {_ph}")
                    last_phase = _ph
            elif ev.get("type") == "steer":
                _note = self._format_steer_note(ev)
                if _note:
                    await self.ms.send_message(_note)
            elif ev.get("type") == "planner_result":
                if (m.get("planner_think") or "").strip():
                    await self._flush_thinking(m["planner_think"], label="Planning")
                    m["planner_think"] = ""
                # Combine structured chunks preview with content/thinking fallbacks
                _pch = ev.get("chunks") or []
                _plan = ""
                if isinstance(_pch, list) and _pch:
                    _titles = [
                        str((c.get("title") or c.get("raw") or "")).strip()
                        for c in _pch if isinstance(c, dict)
                    ]
                    _titles = [t for t in _titles if t]
                    if _titles:
                        _plan = "Plan (" + str(len(_titles)) + " steps): " + " | ".join(_titles)
                if not _plan:
                    _plan = str(ev.get("content") or "")
                    if not _plan and isinstance(_pch, list):
                        _plan = "".join(str(c) for c in _pch)
                if not _plan:
                    _plan = str(ev.get("thinking") or "").strip()
                _plan = " ".join(_plan.split())
                if _plan:
                    await self.ms.send_message(
                        "🧠 " + _plan[:600] + ("…" if len(_plan) > 600 else ""))
            elif ev.get("type") == "duo_round":
                _rn = ev.get("n")
                _rt = ev.get("total")
                _st = str(ev.get("subtask") or "").strip()
                _msg = f"⚙️ Coder round {_rn}/{_rt}"
                if _st:
                    _msg += f": {_st[:110]}"
                await self.ms.send_message(_msg)
            elif ev.get("type") == "duo_start":
                # MODEL VISIBILITY (2026-10-07, owner): WHICH model with
                # which settings runs - the label carries model, VRAM, ctx.
                _lbl = str(ev.get("label") or "").strip()
                if _lbl:
                    await self.ms.send_message("🤖 " + _lbl[:200])
            elif ev.get("type") == "ctx_meter":
                # CONTEXT/COMPRESSION VISIBILITY (user feedback: the plan did not reach the phone again): milestone lines at 50/75/90 % (once each) and
                # a one-shot note while the engine compresses context.
                try:
                    _est = int(ev.get("est_tokens") or 0)
                    _lim = int(ev.get("ctx_limit") or 0)
                except (TypeError, ValueError):
                    _est = _lim = 0
                if _est > 0 and _lim > 0:
                    _pct = _est * 100 // _lim
                    _marks = m.setdefault("ctx_marks", set())
                    for _th in (50, 75, 90):
                        if _pct >= _th and _th not in _marks:
                            _marks.add(_th)
                            await self.ms.send_message(
                                f"🧮 Context {_pct}% "
                                f"({_est // 1024}k/{_lim // 1024}k tokens)")
                    _comp = bool(ev.get("compressing"))
                    if _comp and not m.get("ctx_compression_noted"):
                        # once per RUN (the flag can flip every frame while
                        # the engine hovers at the limit - no re-notes)
                        m["ctx_compression_noted"] = True
                        await self.ms.send_message(
                            "🗜 Context compressing (older turns are being "
                            "summarized to stay under the limit)…")
            elif ev.get("type") == "file_change":
                # FILE ACTIVITY (owner): what the coder actually writes.
                _fc = self._format_file_change_line(ev)
                if not tool_lines or tool_lines[-1] != _fc:
                    tool_lines.append(_fc)
            elif ev.get("type") == "send_file":
                # MIRROR-SEND (2026-11-04): a UI-started run that calls
                # send_file must reach the owner's phone too; the engine
                # already validated the path, the gateway gates it again.
                await self._deliver_send_file(ev)
            elif ev.get("type") == "usage_meta":
                # CODER OUTPUT (user feedback: relay real coder text instead of a bare
                # "round finished"): when a coder LLM call ends, relay the
                # free text it produced since the last relay (token events
                # only - tool-call payloads are separate events).
                if str(ev.get("phase") or "") == "coder":
                    _parts = m.get("answer_parts") or []
                    _upto = int(m.get("answer_sent_upto") or 0)
                    _delta = "".join(str(x) for x in _parts[_upto:]).strip()
                    if len(_delta) >= 40:  # short coder remarks too
                        m["answer_sent_upto"] = len(_parts)
                        # AUDIT FIX (1.3.3 release audit): the fazit path
                        # filters secrets BEFORE sending - the mid-run
                        # coder relay must not leak past that guard.
                        _txt = " ".join(
                            render.filter_secrets(_delta).split())
                        await self.ms.send_message(
                            "💬 " + _txt[:700] + ("…" if len(_txt) > 700 else ""))
            elif ev.get("type") == "planner_thinking_token":
                m["planner_think"] = (m.get("planner_think") or "") + str(ev.get("content") or "")
            elif ev.get("type") == "thinking_token":
                # R4: coder/direct reasoning - buffered across ticks in mirror state
                m["tick_think"] = (m.get("tick_think") or "") + str(ev.get("content") or "")
            elif ev.get("type") == "tool_call":
                if (m.get("tick_think") or "").strip():
                    await self._flush_thinking(m["tick_think"], label="Reasoning")
                    m["tick_think"] = ""
                # Tool-Call-Relay (2026-10-05) + REPEAT-DEDUPE (R6, live
                # flood 10:21): identical lines (same tool, same args -
                # model retry loops) relay once, then a compact counter
                # every 5th repeat. Cross-tick via the mirror state.
                line = self._tool_line(ev)
                if not line:
                    continue
                _seen = m.setdefault("tool_relay_seen", {})
                if len(_seen) > 40:
                    _seen.clear()
                _cnt = _seen.get(line, 0) + 1
                _seen[line] = _cnt
                if _cnt == 1:
                    tool_lines.append(line)
                elif _cnt % 5 == 0:
                    tool_lines.append(f"{line} (repeat #{_cnt})")
        for tl in tool_lines[-20:]:  # cap per tick, newest win
            # (user feedback: the plan did not reach the phone again - 6 was too tight once the
            # file-change lines share the budget with the tool lines)
            await self.ms.send_message(tl)
        from . import bridge as _b
        if last_status and last_status != m["last_status"] \
                and (_b._now() - m["last_tick"]) >= self.cfg.status_min_interval_s:
            if m["status_msg_id"] is None:
                res = await self.ms.send_message(f"⏳ {last_status} …")
                m["status_msg_id"] = res.get("message_id")
            else:
                await self._edit_status(m["status_msg_id"],
                                        f"⏳ {last_status} …")
            m["last_status"] = last_status
            m["last_tick"] = _b._now()
        m["last_phase"] = last_phase  # F5: cross-tick dedupe state
        if done_reason:
            await self._flush_mirror_thinking_pair(m)
            # FAZIT RELAY (2026-10-06, R6-hardened): deliver what the run
            # PRODUCED, not just the fact that it ended. Empty accumulator
            # falls back to the full journal frames (late adoption, phase
            # switches) so the phone always gets the final answer.
            _ans = "".join(m.get("answer_parts") or []).strip()
            if not _ans:
                _ans, _ = self._accumulate_frames(frames)
            self._mirror_reset(m)
            await self._mirror_fazit(rid, _ans, done_reason)
            return

        # approval relay (the point of the takeover)
        pend = await self.hive.pending_approval(rid)
        body = pend.json() if hasattr(pend, "json") else {}
        if getattr(pend, "status_code", 200) == 200 and body.get("active"):
            if (
                str(body.get("kind") or "approval") != "ask"
                and not body.get("decision_id")
                and not body.get("approval_key")
                and not body.get("args_hash")
                and str(body.get("preview") or "").strip().lower().startswith("(generating")
            ):
                return
            sig = f"{body.get('decision_id') or ''}|{body.get('preview') or ''}"
            if sig != m["approval_sig"]:
                m["approval_sig"] = sig
                # G2: remember which card is on the phone, so the phone
                # decision echoes decision_id + tool and the engine can
                # drop duplicates / tool mismatches.
                m["approval_decision_id"] = str(body.get("decision_id") or "")
                m["approval_card_did"] = ""
                m["approval_tool"] = str(body.get("tool") or "")
                m["approval_kind"] = str(body.get("kind") or "approval")
                # deep audit N2: the pending preview is truncated to 300
                # chars by the engine; the full command lives in the
                # journal's tool_call frames - dig it out so the owner
                # never approves blind.
                full = self._full_command_from_frames(
                    j.get("frames"), str(body.get("tool") or ""))
                preview = full or (str(body.get("preview") or "") + "\n"
                                   "(server preview truncated - full "
                                   "command in the UI)")
                if len(preview) > 2500:
                    preview = preview[:2500] + " …"
                did_tok = self._compute_card_did(
                    rid, m["approval_decision_id"], preview,
                    kind=m["approval_kind"], ui_mirror=True,
                )
                m["approval_card_did"] = did_tok
                await self._send_approval_or_ask_card(
                    rid, m["approval_decision_id"], str(body.get("tool") or ""),
                    preview, kind=m["approval_kind"], ui_mirror=True, did_tok=did_tok,
                )
        elif m["approval_sig"] and not body.get("active"):
            # R5 (owner): show WHAT happened, not just that the card
            # closed. Evidence in the journal tail: a fail-closed timeout
            # denies with a status line; an approved call leaves a
            # tool_result for the same tool.
            _appr_tool = str(body.get("tool") or "")
            _tail = [f for f in (j.get("frames") or [])[-40:]
                     if isinstance(f, str)]
            _timed_out = any("DENIED (fail closed)" in f for f in _tail)
            _ran = any('"type": "tool_result"' in f and _appr_tool in f
                       for f in _tail)
            if _timed_out:
                await self.ms.send_message(
                    "🛡 The call was auto-DENIED (no answer within the "
                    "timeout, fail-closed). The model picks another way.")
            elif _ran and _appr_tool:
                await self.ms.send_message(
                    f"✅ Approved in the UI: {_appr_tool} ran on the PC.")
            else:
                await self.ms.send_message(
                    "ℹ️ Card closed (answered in the UI, or timed out).")
            m["approval_sig"] = None
            m["approval_decision_id"] = ""
            m["approval_card_did"] = ""

    async def _mirror_fazit(self, rid: str, answer: str, reason: str) -> None:
        """FAZIT RELAY (2026-10-06): deliver what the run PRODUCED, not
        just the fact that it ended. Chunked messages, .txt document past
        the chunk cap, transcript persistence for handed-over chats;
        marks the run relayed so the late-result path never re-sends."""
        from . import bridge as _b
        _ans = render.filter_secrets((answer or "").strip())  # R3: filter
        # BEFORE split - per-chunk filtering missed secrets straddling
        # hard-split boundaries (each half matched no pattern alone)
        text = _b.STOP_REASON_TEXT.get(
            reason, f"🏁 Mirror run finished ({reason}).")
        if reason == "completed":
            text = "🏁 Mirror run completed."
        # RESULT-MODE (1.4.2): the mirror path is what the phone gets when a run
        # was started in the browser. It used to relay text only, so "answers as
        # voice notes" never fired for a desktop run (owner report: paired, no
        # audio). Same contract as _finish: both = text + voice, text = text only,
        # voice = voice only (with a text fallback if the synthesis failed).
        _mode = (await self._result_mode()) if _ans else "text"
        if _mode == "voice":
            await self._maybe_voice_answer(_ans)
            if not self._voice_sent:
                _mode = "both"          # no audio -> the text MUST still arrive
            elif reason not in ("completed", ""):
                # a non-completed mirror run keeps its status line even when the
                # answer itself went out as audio
                await self.ms.send_message(text)
        if _mode != "voice":
            _full = (_ans + "\n\n" + text) if _ans else text
            _chunks = render.split_message(_full)
            if len(_chunks) > _b.FINAL_CHUNK_LIMIT:
                await self.ms.send_document(
                    _ans.encode("utf-8"), "mirror_result.txt")
                await self.ms.send_message(text)
            else:
                for _c in _chunks:
                    await self.ms.send_message(_c)
            if _mode == "both" and _ans:
                await self._maybe_voice_answer(_ans)
        # PHONE-RUNS-IN-UI: persist the answer into the chat transcript
        # so the run exists in the UI, not only on the phone.
        _ho_chat = self._handed_over.pop(rid, None)
        if _ho_chat and _ans:
            try:
                await self._write_turn(_ho_chat, {
                    "role": "assistant", "content": _ans})
            except (HiveUnreachable, OSError, HTTPError) as _tw:
                self._log_note(f"handover transcript write failed: {_tw}")
        self._late_relayed.add(rid)

    @staticmethod
    def _accumulate_frames(frames: list) -> tuple[str, str]:
        """R6 late relay: accumulate (answer, last phase) from journal
        frames without any sends - used for runs that finished while the
        mirror was not attached."""
        parts: list[str] = []
        phase = ""
        for raw in frames or []:
            if not isinstance(raw, str):
                continue
            try:
                ev = json.loads(raw[6:] if raw.startswith("data: ") else raw)
            except ValueError:
                continue
            et = ev.get("type")
            if et == "token":
                parts.append(str(ev.get("content") or ""))
            elif et == "agent" and parts:
                parts = []  # phase switch: last phase wins
            elif et == "phase":
                phase = str(ev.get("content") or phase)
        return "".join(parts).strip(), phase

    @staticmethod
    def _full_command_from_frames(frames: list | None, tool: str) -> str:
        """Last tool_call event for `tool` in the journal frames, with
        the untruncated command from its extra (sse/events.py keeps
        extra['cmd'] whole). Empty string when not found."""
        for raw in reversed(frames or []):
            if not isinstance(raw, str) or '"type": "tool_call"' not in raw:
                continue
            try:
                ev = json.loads(raw[6:] if raw.startswith("data: ") else raw)
            except ValueError:
                continue
            if ev.get("type") != "tool_call":
                continue
            if tool and ev.get("name") != tool:
                continue
            extra = ev.get("extra") or {}
            for key in ("cmd", "path", "url", "query", "code", "package"):
                v = extra.get(key) or ev.get("label")
                if isinstance(v, str) and v.strip():
                    return f"{ev.get('name', tool)}: {v}"
        return ""

    def mirror_intercept(self, text: str) -> bool:
        """True = this text belongs to the mirrored engine run (approval
        answer or steering); the caller must await mirror_send(text)
        instead of starting a run."""
        m = self._mirror()
        return bool(m.get("run_id")) and bool((text or "").strip())

    async def approval_callback(self, callback_id: str, data: str) -> str:
        """Tappable approval buttons (2026-10-05): callback_data is
        'appr:<rid>:<1|2|3>' from a card's inline keyboard - mirrored UI
        runs AND own phone runs (ask mode). The decision rides the SAME
        path as text answers (decision_id + tool echoed for the engine's
        duplicate/tool guards); '2' uses the engine's chat-scoped
        exact-call memory. Returns the toast text for
        answerCallbackQuery."""
        from . import bridge as _b
        m = self._mirror()
        card_did = ""
        try:
            _parts = data.split(":", 3)
            if len(_parts) == 4:
                _, rid, answer, card_did = _parts
            else:
                _, rid, answer = _parts
        except ValueError:
            return "malformed button"
        if data.startswith("ask:"):
            # ASK BUTTONS (2026-10-07, user feedback: the plan did not reach the phone again): yes/no cards (destructive gate + agent
            # yes/no questions) ride the SAME decide_approval path as a
            # text answer; decision_id/tool from the mirrored card guard
            # duplicates. Free text stays possible alongside the buttons.
            # 'later' (owner, 2nd round) does NOT consume the card: the
            # run keeps waiting and the free-text answer stays open.
            if answer not in ("yes", "no", "later") \
                    and not answer.startswith(("y", "n", "l")):
                return "unknown button"
            if rid != m.get("run_id") or not m.get("approval_sig"):
                return "card already gone"
            expected_did = str(
                m.get("approval_card_did") or m.get("approval_decision_id") or "")
            if not _b._card_did_matches(expected_did, card_did):
                return "ℹ️ card outdated - nothing changed"
            if answer.startswith("l"):
                return "⏳ later - the run waits; answer with text anytime"
            ask_ans = "yes" if answer.startswith("y") else "no"
            resp = await self.hive.decide_approval(
                rid, ask_ans,
                decision_id=m.get("approval_decision_id") or "",
                tool=m.get("approval_tool") or "")
            code = getattr(resp, "status_code", 500)
            m["approval_sig"] = None
            m["approval_decision_id"] = ""
            m["approval_card_did"] = ""
            if code >= 300:
                return f"not accepted (HTTP {code})"
            try:
                routed = str((resp.json() or {}).get("routed") or "")
            except (ValueError, TypeError, AttributeError):
                routed = ""
            if routed == "duplicate":
                return "already answered (UI) - nothing changed"
            if routed == "expired":
                return "⏱ card already timed out - denied (fail closed)"
            if routed == "stale":
                return "ℹ️ card outdated - nothing changed"
            try:
                await self.ms.send_message(
                    "✅ Confirmed (yes) - answer delivered." if ask_ans == "yes"
                    else "⛔ Declined (no) - answer delivered.")
            except (TelegramApiError, OSError):
                pass  # the decision itself is delivered
            return f"{ask_ans} - delivered"
        if answer not in ("1", "2", "3"):
            return "unknown button"

        def _fmt_routed(resp, *, dup_msg: str) -> str:
            try:
                routed = str((resp.json() or {}).get("routed") or "")
            except (ValueError, TypeError, AttributeError):
                routed = ""
            if routed == "duplicate":
                return dup_msg
            if routed == "expired":
                return "⏱ card already timed out - the call was denied (fail closed)"
            if routed == "stale":
                return "ℹ️ That card is outdated (a newer one replaced it) - nothing changed."
            if answer == "2":
                return "always (this chat, this exact call) - delivered"
            return "allowed (once) - delivered" if answer == "1" else "denied - delivered"

        own = self._open_own_approval or {}
        if rid == own.get("rid"):
            expected_own_did = str(
                own.get("card_did") or own.get("decision_id") or "")
            if not _b._card_did_matches(expected_own_did, card_did):
                return "ℹ️ card outdated - nothing changed"
            resp = await self.hive.decide_approval(
                rid, answer,
                decision_id=own.get("decision_id") or "",
                tool=own.get("tool") or "")
            code = getattr(resp, "status_code", 500)
            if code >= 300:
                return f"not accepted (HTTP {code})"
            self._open_own_approval = None
            self.state.data.setdefault("open_approvals", {}).pop(rid, None)
            self.state.save()
            return _fmt_routed(resp, dup_msg="already answered - nothing changed")
        if rid != m.get("run_id") or not m.get("approval_sig"):
            return "card already gone"
        expected_did = str(
            m.get("approval_card_did") or m.get("approval_decision_id") or "")
        if not _b._card_did_matches(expected_did, card_did):
            return "ℹ️ card outdated - nothing changed"
        resp = await self.hive.decide_approval(
            rid, answer,
            decision_id=m.get("approval_decision_id") or "",
            tool=m.get("approval_tool") or "")
        code = getattr(resp, "status_code", 500)
        if code >= 300:
            if code == 404:
                m["approval_sig"] = None
                m["approval_decision_id"] = ""
                m["approval_card_did"] = ""
            return f"not accepted (HTTP {code})"
        m["approval_sig"] = None
        m["approval_decision_id"] = ""
        m["approval_card_did"] = ""
        return _fmt_routed(resp, dup_msg="already answered (UI) - nothing changed")

    async def mirror_send(self, text: str) -> str:
        """Route a phone message into the mirrored run: while an APPROVAL
        card is open, 1/3 answers it ('2'/always does not exist from the
        phone); while an ASK-USER pause is open, ANY text is the answer
        (free text beats yes/no for open questions); otherwise steering."""
        m = self._mirror()
        rid = m.get("run_id")
        _pending_kind = ""
        _pending_did = ""
        if m.get("approval_sig"):
            try:
                _pk = await self.hive.pending_approval(rid)
                _pk_body = _pk.json() if hasattr(_pk, "json") else {}
                if _pk_body.get("active"):
                    _pending_kind = str(_pk_body.get("kind") or "approval")
                    _pending_did = str(_pk_body.get("decision_id") or "")
            except (HiveUnreachable, OSError, ValueError):
                _pending_kind = ""
        if m.get("approval_sig"):
            t = (text or "").strip()
            _kind_now = _pending_kind or (m.get("approval_kind") or "")
            if _kind_now == "ask" and t not in ("1", "3"):
                # ASK-USER FREE TEXT (2026-10-06, owner): the phone answer
                # IS the reply to the agent's question. F3 (audit r3): the
                # LIVE decision_id from the fetched pending body is used,
                # not the relay-time one; a stale card routes "stale" and
                # says so instead of claiming delivery.
                resp = await self.hive.decide_approval(
                    rid, t,
                    decision_id=_pending_did or m.get("approval_decision_id") or "",
                    tool=m.get("approval_tool") or "")
                code = getattr(resp, "status_code", 500)
                if code >= 300:
                    if code == 404:
                        m["approval_sig"] = None
                        m["approval_decision_id"] = ""
                        m["approval_card_did"] = ""
                    return (f"❌ Answer not accepted (HTTP {code}) - maybe "
                            "answered in the UI.")
                m["approval_sig"] = None
                m["approval_decision_id"] = ""
                m["approval_card_did"] = ""
                try:
                    _routed = str((resp.json() or {}).get("routed") or "")
                except (ValueError, TypeError, AttributeError):
                    _routed = ""
                if _routed == "stale":
                    return ("ℹ️ That card is outdated (a newer one "
                            "replaced it) - nothing changed.")
                return ("✅ Answer delivered to the agent: " + t[:200])
            if t == "2":
                return ("❌ '2' (always allow) deliberately does not "
                        "exist from the phone - 1 or 3.")
            if t in ("1", "3"):
                resp = await self.hive.decide_approval(
                    rid, t,
                    decision_id=m.get("approval_decision_id") or "",
                    tool=m.get("approval_tool") or "")
                code = getattr(resp, "status_code", 500)
                if code >= 300:
                    if code == 404:
                        m["approval_sig"] = None
                        m["approval_decision_id"] = ""
                        m["approval_card_did"] = ""
                    return (f"❌ Decision not accepted "
                            f"(HTTP {code}) - maybe answered in the UI.")
                m["approval_sig"] = None
                m["approval_decision_id"] = ""
                m["approval_card_did"] = ""
                try:
                    routed = str((resp.json() or {}).get("routed") or "")
                except (ValueError, TypeError, AttributeError):
                    routed = ""
                if routed == "duplicate":
                    # G2: the UI already answered this card - be honest
                    # instead of confirming a decision that was dropped.
                    return ("ℹ️ This card was already answered (UI) - "
                            "nothing changed.")
                if routed == "expired":
                    return ("⏱ card already timed out - the call was "
                            "denied (fail closed).")
                if routed == "stale":
                    return ("ℹ️ That card is outdated (a newer one "
                            "replaced it) - nothing changed.")
                return ("✅ allowed (once) - delivered."
                        if t == "1" else "🛡 denied - delivered.")
            return ("🛡 An approval is waiting - reply 1 or 3 "
                    "(or answer it in the UI).")
        # audit G8: steering bypassed the max_text_chars cap (the intercept
        # runs before the run-start length check) - cap at the same limit.
        text = (text or "").strip()[: self.cfg.max_text_chars]
        resp = await self.hive.steer(rid, text)
        if getattr(resp, "status_code", 500) == 404:
            return (f"❌ Run {rid} is no longer active - the mirror "
                    "ends on the next tick.")
        return ("🧭 Queued - injected at the next boundary "
                "(mode dependent; pipeline accepts no steering).")

