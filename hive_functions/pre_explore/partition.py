"""Pre-Explore: Partition-Exploration (explore_partition, worker_drain) (Teil von hive_functions/pre_explore)."""

from __future__ import annotations

from pathlib import Path
import asyncio
from hive_functions.prompts import build_partition_explore_prompt
from core.model_sampling import get_sampling_profile
import json
from hive_functions.tree_scout import parse_contract_summary
import re

_CODE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".cpp",
    ".c", ".h", ".cs", ".kt", ".rb", ".php", ".swift", ".vue", ".svelte",
    ".json", ".yaml", ".yml", ".toml", ".md", ".html", ".css", ".sh",
    ".sql", ".env", ".cfg", ".ini", ".xml",
    ".scss", ".less", ".bat", ".ps1", ".r", ".lua", ".vim", ".el",
    ".dockerfile", ".makefile", ".cmake",
    ".txt", ".rst", ".tex", ".csv",
}

_EXCLUDE_MSG = "[EXCLUDED DIR — node_modules, .git, dist, build, etc. are never readable. Read only your assigned files.]"

_EXPLORE_TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file from the workspace",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_contract",
        "description": (
            "Submit the completed partition contract after reading all assigned files. "
            "Call this EXACTLY ONCE when all files have been read. "
            "Write a valid JSON object string. "
            "Required fields: partition, role, exports (list), files_read (list), "
            "touched_by_task (true/false), complexity_score (0.0-1.0), data_flow (string). "
            "Use forward slashes in all file paths."
        ),
        "parameters": {"type": "object",
                       "properties": {"contract": {"type": "object",
                       "description": "JSON object with contract fields: partition, role, exports, files_read, skipped_files, imports_internal, imports_external, data_flow, complexity_score, touched_by_task, hint."}},
                       "required": ["contract"]},
    }},
]

_RE_STRIP_THINK = re.compile(r"<think[^>]*>[\s\S]*?</think(?:ing)?>", re.DOTALL)

from .tooling import _DummyTx
from .context import _MAX_FALLBACK_FILES
from .tooling import _SKIP_DIRS
from .context import _SKIP_FILE_PREFIXES
from .contracts import _build_contract_dict
from .llm import _compress_msgs
from .contracts import _contract_fail_count
from .tooling import _exec_tool
from .contracts import _extract_contract
from .context import _extract_paths_from_tree
from .llm import _llm
from .llm import _needs_no_think
from .tooling import _path_has_excluded_dir
from .llm import _resolve_worker_thinking
from .contracts import _sanitize_toml
from .contracts import logger

async def _explore_partition(
    *,
    part: dict,
    slot: dict,
    worker_idx: int,
    worker_total: int,
    sid: str,
    workspace: str,
    tree_ctx: str,
    msg_cap: int,
    aborted_fn,
    emit_fn,
    max_unique_reads: int = 0,
    thinking_override: bool | None = None,
    llm_read_timeout: float = 300.0,
) -> dict:
    """
    Process one partition with a completely fresh context.
    Returns a result dict.  Never raises — errors are returned as partial results.

    Context lifecycle:
      msgs = [system]                   ← fresh per partition
      msgs += [user: file list]
      loop:
        _llm(msgs, tools) → tool_calls | content
        if tool_calls:  execute, append results as user message, continue
        if content:     append as assistant, check for [contract]
        _compress_msgs() every COMPRESS_EVERY rounds to prevent ctx explosion
      contract extracted → msgs = []    ← context cleared, GC'd
    """
    READ_TIMEOUT = 8    # Bail if no reads after this many rounds

    label   = part.get("label", f"?{worker_idx}")
    paths   = part.get("paths", [])
    paths   = [str(Path(workspace) / p) if not Path(p).is_absolute() else p for p in paths]
    mdl     = slot["model"]
    prt     = slot["port"]
    wk      = f"W{worker_idx}"
    _is_thinking = _resolve_worker_thinking(thinking_override, mdl)
    _no_think  = _needs_no_think(mdl)
    _pctx      = int(slot.get("num_ctx") or slot.get("ctx") or 16384)
    _tbudget   = max(50, min(800, _pctx // 24))   # thinking budget
    # P3: Tool-Calls brauchen wenig Output (~50 Token JSON), write_contract TOML ~400 Token.
    _tool_toks = max(400, min(800, _pctx // 8))

    # ── Model-aware sampling profile (pre-explore = always non-thinking text) ──
    _sampling = get_sampling_profile(mdl, mode="sampling_text")
    _s_temp   = _sampling.get("temperature", 0.7)
    _s_top_p  = _sampling.get("top_p", 1.0)
    _s_top_k  = _sampling.get("top_k", 20)
    _s_min_p  = _sampling.get("min_p", 0.0)
    _s_penalty = _sampling.get("repetition_penalty", 1.0)
    _s_presence = _sampling.get("presence_penalty", 1.5)

    # ── Ctx-aware parameters (scale with model ctx, not hardcoded) ────────────
    # Compression fires when accumulated chars exceed ~75% of ctx budget (est. 4 chars/token).
    # This means a 8k model compresses at ~24k chars, a 32k model at ~96k chars.
    COMPRESS_CHAR_THRESHOLD = int(_pctx * 4 * 0.75)
    # Per-file read cap: leave room for system prompt + contract output
    MAX_FILE_CHARS = min(20000, max(3000, _pctx // 3))
    # How many tail messages to keep when compressing (more ctx → keep more history)
    COMPRESS_KEEP_TAIL = max(6, min(20, _pctx // 1500))

    if not paths:
        logger.warning("[%s] %s SKIP - empty partition '%s' (after dedupe?)", sid, wk, label)
        if emit_fn:
            try:
                await emit_fn({"type": "partition_done", "label": label,
                               "files_read": 0, "files_total": 0,
                               "worker_key": wk, "skipped": True})
            except Exception:
                pass
        return {"label": label, "ctx": "", "contract": None,
                "files_read": 0, "files_total": 0, "skipped": True}

    logger.info("[%s] %s → %s (%d files)", sid, wk, label, len(paths))

    if emit_fn:
        try:
            await emit_fn({
                "type": "partition_start", "label": label,
                "n_files": len(paths), "worker_key": wk,
                "worker_model": mdl, "worker_port": prt,
                "worker_idx": worker_idx, "worker_total": worker_total,
            })
        except Exception:
            pass

    async def _emit_tok(content: str):
        if emit_fn:
            try:
                await emit_fn({"type": "partition_token", "label": label,
                               "content": content, "worker_key": wk})
            except Exception:
                pass

    # ── Fresh context ─────────────────────────────────────────────────────────
    sys_p = build_partition_explore_prompt(label, workspace, paths, tree_ctx)
    msgs: list[dict] = [{"role": "system", "content": sys_p}]

    path_list_str = "\n".join(f"  {i+1}. {p}" for i, p in enumerate(paths))
    msgs.append({"role": "user", "content": (
        f"Explore partition [{label}]. Read ALL files using read_file.\n"
        f"Files:\n{path_list_str}\n\nStart with file 1."
    )})

    remaining      = list(paths)
    _read_set:     set[str] = set()        # absolute paths for UI display
    _seen_files:   dict[str, str] = {}     # norm-path → cached content
    _read_counts:  dict[str, int] = {}     # norm-path → repeated-read counter
    _failed_reads: set[str] = set()        # P2: paths that failed — deadlock detection
    # Pre-compute normalised partition paths for in-partition check.
    # read_n only counts files that belong to THIS partition — prevents
    # n_read > n_total when model wanders outside its assigned paths.
    _partition_norms: set[str] = set()
    _partition_roots: set[str] = set()
    for _pp in paths:
        try:
            _pp_abs = str((Path(workspace) / _pp).resolve()) if not Path(_pp).is_absolute() else str(Path(_pp).resolve())
        except Exception:
            _pp_abs = _pp
        _pp_norm = _pp_abs.replace("\\", "/").lower()
        _partition_norms.add(_pp_norm)
        # extract top-level directory root for cross-partition boundary enforcement
        _pp_rel = _pp_norm
        if _pp_norm.startswith(workspace.replace("\\", "/").lower()):
            _pp_rel = _pp_norm[len(workspace.replace("\\", "/").lower()):].lstrip("/")
        _slash = _pp_rel.find("/")
        if _slash > 0:
            _partition_roots.add(_pp_rel[:_slash + 1])
        else:
            _partition_roots.add("")
    # Safety: derive root from partition label if roots are empty/only ""
    _non_empty_roots = _partition_roots - {""}
    if not _non_empty_roots and label and label.split(":sz")[0] != "__root__":
        _label_root = label.replace("\\", "/")
        _ls = _label_root.find("/")
        if _ls > 0:
            _partition_roots.add(_label_root[:_ls + 1])
        elif "/" not in _label_root:
            _partition_roots.add(_label_root + "/")
    # P5+R1: Pre-filter remaining — remove paths outside this partition's root.
    _p5_before = len(remaining)
    _ws_root = Path(workspace).resolve()
    _p5_filtered = []
    for p in remaining:
        try:
            _p_abs = Path(p).resolve() if not Path(p).is_absolute() else Path(p)
            _rel = str(_p_abs.relative_to(_ws_root)).replace("\\", "/")
        except (ValueError, OSError):
            _p5_filtered.append(p)
            continue
        _in_root = any(
            _rel.lower().startswith(root.lower().strip("/"))
            for root in (_partition_roots - {""})
        )
        if _in_root or not (_partition_roots - {""}):
            _p5_filtered.append(p)
    remaining = _p5_filtered
    if len(remaining) < _p5_before:
        logger.info("[PRE-EXPLORE] Pre-filtered %d out-of-partition paths for worker %s",
                     _p5_before - len(remaining), wk)
    content_msgs: list[str] = []
    contract_done = False
    _direct_contract: dict | None = None  # T2: set when write_contract parses OK via JSON
    _wc_nudge_count = 0  # Tracks repeated write_contract nudges for aggressive escalation
    _hallucinated_paths: set[str] = set()  # HALLUCINATION FIX: Paths that don't exist on disk
    _error_nudge_sent = False  # Bug 8 guard: max 1 error-nudge per worker
    _prose_only_rounds = 0     # Bug 9: rounds without tool calls (prose-only)
    read_n   = 0
    rounds   = 0
    max_r    = max(8, len(paths) * 2 + 4)
    # Running char total — updated incrementally instead of recomputed every round
    _total_chars = sum(len(m.get("content") or "") for m in msgs)

    # ── Main loop ─────────────────────────────────────────────────────────────
    while not contract_done and rounds < max_r:
        if aborted_fn and aborted_fn():
            break
        rounds += 1

        # Compress context when accumulated chars exceed ctx budget.
        # Char-based so large-ctx models keep full history; small models get pruned.
        if _total_chars > COMPRESS_CHAR_THRESHOLD:
            msgs = _compress_msgs(msgs, keep_system=True, keep_tail=COMPRESS_KEEP_TAIL)
            _total_chars = sum(len(m.get("content") or "") for m in msgs)  # recount after compress

        result = await _llm(
            mdl, prt, msgs,
            temp=_s_temp, max_tok=_tool_toks,
            top_p=_s_top_p, top_k=_s_top_k, min_p=_s_min_p,
            penalty=_s_penalty, presence_penalty=_s_presence,
            msg_cap=0,   # _compress_msgs manages the window; don't double-truncate
            tools=_EXPLORE_TOOLS,
            thinking=_is_thinking, thinking_budget=_tbudget,
            no_think=_no_think,
            read_timeout=llm_read_timeout,
        )

        tc_list = result["tool_calls"]
        content = result["content"]

        # ── Tool calls ────────────────────────────────────────────────────────
        if tc_list:
            _prose_only_rounds = 0
            msgs.append({"role": "assistant", "content": content or None, "tool_calls": [
                {"type": "function", "id": f"call_{i}", "function": {"name": tc.get("name", ""), "arguments": json.dumps(tc.get("args", {}))}}
                for i, tc in enumerate(tc_list)
            ]})
            if content:
                content_msgs.append(content)
            tool_results: list[str] = []
            for tc in tc_list:
                tn = tc.get("name", "")
                ta = tc.get("args") or {}
                if tn == "read_file":
                    fp = str(ta.get("path", "")).strip()
                    # M5 FIX: /workspace/<project>/ prefix normalization (llama.cpp context mount)
                    _ws_base = Path(workspace).name
                    _fp_raw = fp
                    if _fp_raw.startswith("/workspace/"):
                        _fp_parts = _fp_raw[len("/workspace/"):].lstrip("/").split("/", 1)
                        if _fp_parts and _fp_parts[0].lower() == _ws_base.lower():
                            _fp_raw = _fp_parts[1] if len(_fp_parts) > 1 else _fp_parts[0]
                    # Already-read guard: return short notice instead of full content
                    # so the model doesn't loop re-reading the same file.
                    _fp_check = str((Path(workspace) / _fp_raw).resolve())
                    _fp_check_norm = _fp_check.replace("\\", "/").lower()
                    _abs_ws = Path(workspace).resolve()
                    try:
                        Path(_fp_check).resolve().relative_to(_abs_ws)
                    except ValueError:
                        fc = f"[ERROR: '{fp}' is outside workspace — not readable]"
                        tool_results.append(f"[read_file: {fp}]\n{fc}")
                        await _emit_tok(f"skip({fp}): outside workspace")
                        _failed_reads.add(fp)
                        _hallucinated_paths.add(fp)
                        continue
                    # --- EXCLUDE DIRS CHECK (before boundary) ---
                    if _path_has_excluded_dir(_fp_check):
                        _failed_reads.add(fp)
                        _hallucinated_paths.add(fp)
                        _to_rm_excl = next(
                            (_rp for _rp in remaining
                             if _rp.replace("\\", "/").lower().rstrip("/") == _fp_check_norm.rstrip("/")),
                            None
                        )
                        if _to_rm_excl:
                            remaining.remove(_to_rm_excl)
                        fc = _EXCLUDE_MSG
                        tool_results.append(f"[read_file: {fp}]\n{fc}")
                        await _emit_tok(f"skip({fp}): excluded dir")
                        continue
                    # --- BOUNDARY CHECK ---
                    if _partition_roots and _fp_check_norm not in _partition_norms:
                        _in_bounds = False
                        # Convert absolute path to workspace-relative for root matching
                        try:
                            _ws_norm = Path(workspace).resolve()
                            _fp_rel = str(Path(_fp_check_norm).resolve().relative_to(_ws_norm)).replace("\\", "/").lower()
                        except (ValueError, OSError):
                            _fp_rel = _fp_check_norm
                        for _pr in _partition_roots:
                            if _pr and (_fp_rel.startswith(_pr.lower()) or _fp_check_norm.startswith(_pr)):
                                _in_bounds = True
                                break
                        if not _in_bounds:
                            fc = "[out of partition — skipped]"
                            tool_results.append(f"[read_file: {fp}]\n{fc}")
                            await _emit_tok(f"skip({fp}): out of partition")
                            continue
                    # --- DEDUP + CAP CHECK ---
                    if _fp_check_norm in _seen_files:
                        _read_counts[_fp_check_norm] = (
                            _read_counts.get(_fp_check_norm, 0) + 1
                        )
                        _content = _seen_files[_fp_check_norm]
                        _cached_lines = _content.count('\n') + 1
                        _cached_chars = len(_content)
                        if _read_counts[_fp_check_norm] == 1:
                            _preview = _content[:200].rstrip()
                            fc = (
                                f"[ALREADY READ — do NOT read again]\n"
                                f"{_cached_lines} lines | {_cached_chars} chars\n"
                                f"Preview:\n{_preview}\n"
                                f"Full content is already in context above. "
                                f"Move on to the next task."
                            )
                        else:
                            fc = (
                                f"[SKIP: '{fp}' already read "
                                f"({_cached_lines} lines). "
                                f"Do NOT call read_file on this path again.]"
                            )
                    elif max_unique_reads > 0 and len(_seen_files) >= max_unique_reads:
                        fc = f"[read cap reached ({max_unique_reads}) — skipped]"
                    else:
                        fc = await _exec_tool("read_file", ta, workspace, _DummyTx(),
                                             max_read=MAX_FILE_CHARS)
                    # Resolve to absolute path — model may return relative or absolute.
                    # _read_set stores absolute paths for display; _seen_files is
                    # the lowercase dedup key with cached content.
                    try:
                        _fp_abs = str((Path(workspace) / fp).resolve()) if not Path(fp).is_absolute() else str(Path(fp).resolve())
                    except Exception:
                        _fp_abs = fp
                    _norm_fp = _fp_abs.replace("\\", "/").lower()
                    if _norm_fp not in _seen_files:
                        _seen_files[_norm_fp] = fc
                        _read_set.add(_fp_abs)   # absolute path for UI display
                        # Only count reads that belong to this partition.
                        if _norm_fp in _partition_norms:
                            read_n += 1
                    # Remove from remaining: match by suffix because the model may
                    # return an absolute path while remaining holds relative paths.
                    _norm_fp_strip = _norm_fp.strip("/")
                    _to_remove = None
                    for _rp in remaining:
                        _norm_rp = _rp.replace("\\", "/").lower().strip("/")
                        if (
                            _norm_rp == _norm_fp_strip                          # exact
                            or _norm_fp_strip.endswith("/" + _norm_rp)          # abs ends with rel
                            or _norm_rp.endswith("/" + _norm_fp_strip)          # rel ends with abs (rare)
                            or _norm_fp_strip.endswith(_norm_rp)                # abs ends with rel (no leading slash)
                        ):
                            _to_remove = _rp
                            break
                    if _to_remove:
                        _read_ok = not any(
                            err in str(fc or "")
                            for err in ("[ERROR", "[TOOL_ERROR", "Permission denied", "not found", "No such file")
                        )
                        if _read_ok:
                            remaining.remove(_to_remove)
                        else:
                            logger.warning(
                                "[PRE-EXPLORE] read_file failed for %s — keeping in remaining", _to_remove
                            )
                            _failed_reads.add(_to_remove)  # P2: track for deadlock detection
                            # HALLUCINATION FIX: track paths the model invents that don't exist
                            _err_match = re.search(r'\[ERROR:READ\].*?No such file.*?[\'"]([^\'"]+)[\'"]', fc or "")
                            if _err_match:
                                _hallucinated_paths.add(_err_match.group(1))
                    tool_results.append(f"[read_file: {fp}]\n{fc}")
                    # partition_token: live file list in UI card
                    await _emit_tok(f"read_file({_fp_abs})")
                    # file_read: counter badge update (n_read / n_total)
                    if emit_fn:
                        try:
                            await emit_fn({
                                "type":     "file_read",
                                "label":    label,
                                "path":     _fp_abs,
                                "n_read":   read_n,
                                "n_total":  len(paths),
                                "worker_key": wk,
                            })
                        except Exception:
                            pass
                elif tn == "list_dir":
                    fc = await _exec_tool("list_dir", ta, workspace, _DummyTx())
                    tool_results.append(f"[list_dir]\n{fc}")
                elif tn == "write_contract":
                    _ta = ta or {}
                    _toml_content = ""
                    for _key in ("contract", "toml", "parameter", "data", "content"):
                        _val = _ta.get(_key)
                        if _val:
                            if _key != "contract":
                                logger.warning(
                                    "[CONTRACT-KEY-FALLBACK] partition=%s used key='%s' instead of 'contract'",
                                    label, _key,
                                )
                            _toml_content = _val
                            break
                    _still = len(remaining) if remaining else 0
                    if not _toml_content:
                        _msg = (
                            "[write_contract REJECTED] contract parameter is empty. "
                            + (f"Read {_still} remaining file(s) first, then call write_contract with JSON content."
                               if _still > 0
                               else "Call write_contract now with the completed JSON contract.")
                        )
                        tool_results.append(_msg)
                    elif _still > 0:
                        _rem_names = ", ".join(str(Path(r).name) for r in remaining[:8])
                        _rem_more = f" +{_still - 8} more" if _still > 8 else ""
                        tool_results.append(
                            f"[write_contract REJECTED — {_still} file(s) still unread: "
                            f"{_rem_names}{_rem_more}. Read ALL of them first (including config/"
                            f"docs files like README, docker-compose, *.d.ts), then call write_contract again.]"
                        )
                    elif isinstance(_toml_content, dict):
                        _actually_read = [p for p in (paths or []) if p not in remaining]
                        _contract_dict = _build_contract_dict(_toml_content, label)
                        if _actually_read:
                            _contract_dict["files_read"] = _actually_read
                        _direct_contract = _contract_dict
                        content = json.dumps(_toml_content)
                        contract_done = True
                        logger.info(
                            "[CONTRACT] Parsed OK via dict-direct: partition=%s, actual_files=%d/%d",
                            _contract_dict.get("partition"), len(_actually_read), len(paths or []),
                        )
                        tool_results.append(f"[write_contract accepted — dict-direct]")
                        await _emit_tok(f"contract(dict-direct)")
                    else:
                        logger.debug(
                            "[RAW-WORKER-OUTPUT] partition=%s len=%d output=%r",
                            label, len(_toml_content), _toml_content[:2000],
                        )
                        _contract_dict = _extract_contract(_toml_content, label)
                        if _contract_dict:
                            _actually_read = [p for p in (paths or []) if p not in remaining]
                            if _actually_read:
                                _contract_dict["files_read"] = _actually_read
                            _direct_contract = _contract_dict
                            content = _toml_content
                            contract_done = True
                            logger.info(
                                "[CONTRACT] Parsed OK via %s: partition=%s, actual_files=%d/%d",
                                "JSON" if _toml_content.strip().startswith("{") else "TOML",
                                _contract_dict.get("partition"), len(_actually_read), len(paths or []),
                            )
                            tool_results.append(f"[write_contract accepted — {len(_toml_content)} chars]")
                            await _emit_tok(f"contract({len(_toml_content)} chars)")
                        else:
                            content_msgs.append(f"```toml\n{_sanitize_toml(_toml_content.strip())}\n```")
                            content = _toml_content
                            contract_done = True
                            tool_results.append(f"[write_contract accepted (fallback parse) — {len(_toml_content)} chars]")
                            await _emit_tok(f"contract({len(_toml_content)} chars)")
            if tool_results:
                _msg_tr = {"role": "user", "content": "\n\n".join(tool_results)}
                msgs.append(_msg_tr)
                _total_chars += len(_msg_tr["content"])
            # P2: Deadlock detection — all remaining files have failed reads → force nudge
            if remaining and _failed_reads and set(remaining).issubset(_failed_reads):
                logger.warning(
                    "[PRE-EXPLORE] All remaining files have failed reads — clearing remaining to unlock write_contract nudge"
                )
                remaining.clear()
            if not remaining and not contract_done:
                _msg_allread = {"role": "user", "content": "All files read. Call write_contract now with your JSON contract."}
                msgs.append(_msg_allread)
                _total_chars += len(_msg_allread["content"])

        # ── Text content ──────────────────────────────────────────────────────
        elif content:
            _prose_only_rounds += 1
            # Strip stray <think> blocks (safeguard: /no_think should prevent these)
            content = _RE_STRIP_THINK.sub("", content).strip()
            if not content:
                msgs.append({"role": "user", "content": "Continue."})
                _total_chars += 9
                continue
            await _emit_tok(content[:300])
            msgs.append({"role": "assistant", "content": content})
            _total_chars += len(content)
            content_msgs.append(content)

            # Token-Hardcap: break if model writes prose without writing contract
            _total_content_toks = sum(len(c) for c in content_msgs) // 3
            if _total_content_toks > 700 and not contract_done:
                logger.warning(
                    "[%s] %s token budget exceeded (%d tok) without contract — forcing prose fallback",
                    sid, wk, _total_content_toks
                )
                break

            _DONE_MARKERS = (
                "<function=write_contract",
                "{write_contract(",
                '{"write_contract"',
            )
            if any(m in content for m in _DONE_MARKERS):
                if not remaining:
                    logger.debug(
                        "[RAW-WORKER-OUTPUT-PROSE] partition=%s len=%d output=%r",
                        label, len(content), content[:2000],
                    )
                    _prose_contract = _extract_contract(content, label)
                    if _prose_contract:
                        _direct_contract = _prose_contract
                        contract_done = True
                else:
                    # Model wrote contract too early — still has unread files.
                    # Reject the early contract and force remaining reads.
                    _still = len(remaining)
                    _next  = remaining[0]
                    msgs.append({"role": "user", "content": (
                        f"STOP — you wrote the contract before reading all files. "
                        f"You still have {_still} file(s) unread. "
                        f"Read them ALL first, then rewrite the contract.\n"
                        f"Next file to read: {_next}"
                    )})
            _ERROR_PATTERNS_CONTENT = (
                '{"error"',
                '{**error**',
                '"error":',
                'no tool_call response',
                'expected JSON object',
            )
            _is_error = any(p in content for p in _ERROR_PATTERNS_CONTENT)
            if (_prose_only_rounds >= 3 or _is_error) and not contract_done and not _error_nudge_sent:
                _error_nudge_sent = True
                msgs.append({
                    "role": "user",
                    "content": (
                        "You have written 3 responses without tool calls. "
                        "Stop explaining. You have already read all files. "
                        "Call write_contract NOW with the information you have gathered.\n"
                        "Use the exact format:\n"
                        '<function=write_contract><parameter=contract>\n'
                        '{"partition": "...", "exports": [...], "files_read": [...], "role": "...", '
                        '"touched_by_task": true, "complexity_score": 0.5}\n'
                        "</parameter></function>"
                    )
                })
            elif read_n == 0 and rounds <= READ_TIMEOUT:
                _first = paths[0] if paths else "(no files assigned)"
                msgs.append({"role": "user", "content": f"Use read_file now: {_first}"})
            elif remaining:
                _nudge = f"Now read: {remaining[0]}"
                if _hallucinated_paths:
                    _nudge += (
                        f"\nWARNING: These paths do NOT exist in the project: "
                        f"{', '.join(sorted(_hallucinated_paths)[:6])}. "
                        f"Do NOT attempt to read them again. "
                        f"Only read files from your assigned list."
                    )
                msgs.append({"role": "user", "content": _nudge})
            else:
                # All files read, no contract yet — aggressive escalation nudge
                _wc_nudge_count += 1
                if _wc_nudge_count >= 2:
                    _nudge = (
                        "CRITICAL: You MUST call write_contract() NOW. No prose, no text — ONLY a tool call.\n"
                        "Write your contract as JSON object. Example:\n"
                        'write_contract(contract={"partition":"' + label + '","role":"...","exports":["..."],"files_read":["..."],"complexity_score":0.5,"touched_by_task":true})\n'
                        "Do NOT write paragraphs. Do NOT explain. Just the tool call."
                    )
                    if _hallucinated_paths:
                        _nudge += (
                            f"\nWARNING: These paths do NOT exist: "
                            f"{', '.join(sorted(_hallucinated_paths)[:6])}. "
                            f"Do NOT read them again."
                        )
                    msgs.append({"role": "user", "content": _nudge})
                else:
                    msgs.append({"role": "user", "content": "All files read. Call write_contract now with your JSON contract."})

        # ── Empty response ────────────────────────────────────────────────────
        else:
            if remaining:
                _nudge = f"Use read_file now: {remaining[0]}"
                if _hallucinated_paths:
                    _nudge += (
                        f"\nWARNING: These paths do NOT exist: "
                        f"{', '.join(sorted(_hallucinated_paths)[:6])}. "
                        f"Do NOT read them again."
                    )
                msgs.append({"role": "user", "content": _nudge})
            elif not contract_done:
                _wc_nudge_count += 1
                if _wc_nudge_count >= 2:
                    msgs.append({"role": "user", "content": (
                        "You MUST call write_contract() NOW with your findings. Tool call ONLY — no text."
                    )})
                else:
                    msgs.append({"role": "user", "content": "Call write_contract now with your JSON contract."})

    if not contract_done and rounds >= max_r:
        logger.warning("[PRE-EXPLORE] Worker hit max_r=%d without contract — partition %s incomplete", max_r, label)

    # ── Force remaining reads if loop exited without finishing ──────────────
    # This handles the case where max_r was hit while files remain unread.
    if remaining and not (aborted_fn and aborted_fn()):
        _force_read_rounds = min(len(remaining) * 2 + 4, 20)
        for _fr in range(_force_read_rounds):
            if not remaining or (aborted_fn and aborted_fn()):
                break
            msgs.append({"role": "user", "content": (
                f"You missed {len(remaining)} file(s). Read now: {remaining[0]}"
            )})
            _fr_result = await _llm(
                mdl, prt, msgs,
                temp=_s_temp, max_tok=_tool_toks,
                top_p=_s_top_p, top_k=_s_top_k, min_p=_s_min_p,
                penalty=_s_penalty, presence_penalty=_s_presence,
                msg_cap=0,
                tools=_EXPLORE_TOOLS,
                thinking=_is_thinking, thinking_budget=_tbudget,
                no_think=_no_think,
                read_timeout=llm_read_timeout,
            )
            if _fr_result["tool_calls"]:
                _fr_tc_list = _fr_result["tool_calls"]
                msgs.append({"role": "assistant", "content": _fr_result.get("content") or None, "tool_calls": [
                    {"type": "function", "id": f"call_{i}", "function": {"name": tc.get("name", ""), "arguments": json.dumps(tc.get("args", {}))}}
                    for i, tc in enumerate(_fr_tc_list)
                ]})
                _fr_results: list[str] = []
                for _tc in _fr_result["tool_calls"]:
                    if _tc.get("name") == "read_file":
                        _fp2 = str(_tc.get("args", {}).get("path", "")).strip()
                        # M5 FIX: /workspace/<project>/ prefix normalization
                        if _fp2.startswith("/workspace/"):
                            _ws_base2 = Path(workspace).name
                            _fp2_parts = _fp2[len("/workspace/"):].lstrip("/").split("/", 1)
                            if _fp2_parts and _fp2_parts[0].lower() == _ws_base2.lower():
                                _fp2 = _fp2_parts[1] if len(_fp2_parts) > 1 else _fp2_parts[0]
                        try:
                            _fp2_abs = str((Path(workspace) / _fp2).resolve()) if not Path(_fp2).is_absolute() else str(Path(_fp2).resolve())
                        except Exception:
                            _fp2_abs = _fp2
                        _norm2 = _fp2_abs.replace("\\", "/").lower()
                        _abs_ws2 = Path(workspace).resolve()
                        try:
                            Path(_fp2_abs).resolve().relative_to(_abs_ws2)
                        except ValueError:
                            _fc2 = f"[ERROR: '{_fp2}' is outside workspace — not readable]"
                            _fr_results.append(f"[read_file: {_fp2}]\n{_fc2}")
                            await _emit_tok(f"skip({_fp2}): outside workspace")
                            _hallucinated_paths.add(_fp2)
                            continue
                        # --- EXCLUDE DIRS CHECK (force-read) ---
                        if _path_has_excluded_dir(_fp2_abs):
                            _fc2 = _EXCLUDE_MSG
                            _fr_results.append(f"[read_file: {_fp2}]\n{_fc2}")
                            await _emit_tok(f"skip({_fp2}): excluded dir")
                            continue
                        # --- BOUNDARY CHECK ---
                        _fp2_in_bounds = _norm2 in _partition_norms
                        if _partition_roots and not _fp2_in_bounds:
                            try:
                                _ws_norm2 = Path(workspace).resolve()
                                _fp2_rel = str(Path(_norm2).resolve().relative_to(_ws_norm2)).replace("\\", "/").lower()
                            except (ValueError, OSError):
                                _fp2_rel = _norm2
                            for _pr in _partition_roots:
                                if _pr and (_fp2_rel.startswith(_pr.lower()) or _norm2.startswith(_pr)):
                                    _fp2_in_bounds = True
                                    break
                            if not _fp2_in_bounds:
                                _fc2 = "[out of partition — skipped]"
                                _fr_results.append(f"[read_file: {_fp2}]\n{_fc2}")
                                await _emit_tok(f"skip({_fp2}): out of partition")
                                continue
                        # --- DEDUP + CAP CHECK ---
                        if _norm2 in _seen_files:
                            _read_counts[_norm2] = (
                                _read_counts.get(_norm2, 0) + 1
                            )
                            _content = _seen_files[_norm2]
                            _cached_lines = _content.count('\n') + 1
                            _cached_chars = len(_content)
                            if _read_counts[_norm2] == 1:
                                _preview = _content[:200].rstrip()
                                _fc2 = (
                                    f"[ALREADY READ — do NOT read again]\n"
                                    f"{_cached_lines} lines | {_cached_chars} chars\n"
                                    f"Preview:\n{_preview}\n"
                                    f"Full content is already in context above. "
                                    f"Move on to the next task."
                                )
                            else:
                                _fc2 = (
                                    f"[SKIP: '{_fp2}' already read "
                                    f"({_cached_lines} lines). "
                                    f"Do NOT call read_file on this path again.]"
                                )
                        elif max_unique_reads > 0 and len(_seen_files) >= max_unique_reads:
                            _fc2 = f"[read cap reached ({max_unique_reads}) — skipped]"
                        else:
                            _fc2 = await _exec_tool("read_file", _tc.get("args", {}),
                                                   workspace, _DummyTx(), max_read=MAX_FILE_CHARS)
                        if _norm2 not in _seen_files:
                            _seen_files[_norm2] = _fc2
                            if _norm2 in _partition_norms:
                                _read_set.add(_fp2_abs)
                                read_n += 1
                        _norm2_strip = _norm2.strip("/")
                        _rem2 = None
                        for _rp2 in remaining:
                            _nrp2 = _rp2.replace("\\", "/").lower().strip("/")
                            if _nrp2 == _norm2_strip or _norm2_strip.endswith("/" + _nrp2) or _norm2_strip.endswith(_nrp2):
                                _rem2 = _rp2; break
                        if _rem2:
                            remaining.remove(_rem2)
                        _fr_results.append(f"[read_file: {_fp2}]\n{_fc2}")
                        content_msgs.append(_fc2[:200])
                        await _emit_tok(f"read_file({_fp2_abs})")
                if _fr_results:
                    msgs.append({"role": "user", "content": "\n\n".join(_fr_results)})
            elif _fr_result["content"]:
                content_msgs.append(_fr_result["content"])
                if (any(f in _fr_result["content"] for f in ("```toml", "```json", "```")) or "[contract]" in _fr_result["content"].lower()) and not remaining:
                    contract_done = True
                    break

    # ── Force contract if still missing ──────────────────────────────────────
    if not contract_done and not (aborted_fn and aborted_fn()):
        # Compress hard before force-write to give the model clean ctx
        msgs = _compress_msgs(msgs, keep_system=True, keep_tail=4)
        msgs.append({"role": "user", "content": (
            f"Read {read_n}/{len(paths)} files. Write CONTRACT now.\n"
            "Output raw JSON only — no code fences, no markdown:\n"
            '{"partition": "' + label + '", "role": "what this partition does", '
            '"exports": ["ClassName", "function_name"], '
            '"files_read": ["file1.py", "file2.py"], '
            '"skipped_files": [], "imports_internal": [], '
            '"imports_external": ["lib1", "lib2"], '
            '"data_flow": "how data moves", '
            '"complexity_score": 0.5, "touched_by_task": true, '
            '"hint": "one key insight"}'
        )})
        _s_nothink = get_sampling_profile(mdl, mode="sampling_text")
        r2 = await _llm(mdl, prt, msgs,
                        temp=_s_nothink.get("temperature", 0.7),
                        max_tok=_tool_toks,
                        top_p=_s_nothink.get("top_p", 1.0),
                        top_k=_s_nothink.get("top_k", 20),
                        min_p=_s_nothink.get("min_p", 0.0),
                        penalty=_s_nothink.get("repetition_penalty", 1.0),
                        presence_penalty=_s_nothink.get("presence_penalty", 1.5),
                        msg_cap=msg_cap, thinking=False, no_think=_no_think,
                        read_timeout=llm_read_timeout)
        if r2["content"]:
            content_msgs.append(r2["content"])
        elif r2.get("tool_calls"):
            for _fc_tc in r2["tool_calls"]:
                _fc_name = _fc_tc.get("name", "")
                _fc_ta = _fc_tc.get("args", {})
                if _fc_name == "write_contract":
                    _fc_toml = ""
                    for _fk in ("contract", "toml", "parameter", "data", "content"):
                        _fv = _fc_ta.get(_fk)
                        if _fv:
                            if _fk != "contract":
                                logger.warning("[FORCE-CONTRACT-KEY-FALLBACK] partition=%s key='%s'", label, _fk)
                            _fc_toml = _fv
                            break
                    if _fc_toml:
                        _fc_str = json.dumps(_fc_toml) if isinstance(_fc_toml, dict) else _fc_toml
                        content_msgs.append(f"```toml\n{_fc_str}\n```")
                        contract_done = True
                        break

    # ── Extract contract + preserve messages for coder bridge ────────────────
    full_text = "\n".join(content_msgs)
    # Filter to real chat messages (role + content) for the coder bridge.
    # Drop system messages — per-partition system prompts are Worker-Anweisungen,
    _coder_msgs = [m for m in msgs if isinstance(m, dict) and "role" in m
                   and m.get("role") != "system"
                   and (m.get("content") is not None or m.get("tool_calls"))]
    msgs.clear()
    del msgs

    contract = None
    # T2: If write_contract was parsed directly, skip parse_contract_summary roundtrip
    if _direct_contract:
        contract = _direct_contract
        logger.info("[%s] %s contract via direct parse: partition=%s files=%d",
                     sid, wk, contract.get("partition", "?"), len(contract.get("files_read", [])))
    elif parse_contract_summary:
        try:
            parsed = parse_contract_summary(full_text)
            if parsed:
                contract = parsed[0]
                logger.info("[%s] %s contract OK: partition=%s exports=%s",
                            sid, wk, contract.get("partition", "?"), contract.get("exports", []))
            else:
                global _contract_fail_count
                _contract_fail_count += 1
                _log_fn = logger.warning if _contract_fail_count <= 3 else logger.debug
                _log_fn(
                    "[%s] %s contract NOT PARSED (fail #%d)%s",
                    sid, wk, _contract_fail_count,
                    "" if _contract_fail_count <= 3 else " — suppressed to debug",
                )
        except Exception as e:
            logger.warning("[%s] %s parse_contract error: %s", sid, wk, e)

    # Try _extract_contract on full_text before falling back to prose
    if contract is None and full_text:
        logger.debug(
            "[RAW-WORKER-OUTPUT-FALLBACK] partition=%s len=%d output=%r",
            label, len(full_text), full_text[:2000],
        )
        _direct_attempt = _extract_contract(full_text, label)
        if _direct_attempt:
            contract = _direct_attempt
            logger.info("[%s] %s contract via extract-from-prose: partition=%s exports=%d",
                        sid, wk, contract.get("partition", "?"), len(contract.get("exports", [])))

    # Error-response nudge: model returned an error instead of write_contract
    _ERROR_PATTERNS = (
        '{"error"',
        '{**error**',
        '"error":',
        'no tool_call response',
        'expected JSON object',
    )
    if contract is None and full_text and not contract_done:
        _is_error_response = any(p in full_text for p in _ERROR_PATTERNS)
        if _is_error_response:
            logger.warning("[%s] %s error response detected in full_text", sid, wk)

    # Prose-fallback: model wrote text but never called write_contract — try to extract
    if contract is None and full_text and read_n > 0:
        _prose_exports: list[str] = []
        # Priority 1: explicit "Exports: name, name" prose-lists (model names them directly)
        _ex_m = re.findall(r'[Ee]xports?\s*[=:]\s*\[?([^\]\n]+)', full_text)
        for _eg in _ex_m:
            _prose_exports.extend(re.findall(r'(\w+)', _eg))
        # Priority 2: Node.js patterns — module.exports = {name} / exports.name = ...
        _node_m = re.findall(r'(?:module\.|exports?\.)\s*(\w+)', full_text, re.IGNORECASE)
        _prose_exports.extend(_node_m)
        # Priority 3: TS/JS — export function/class/const name
        _ts_m = re.findall(r'\b(?:export\s+(?:default\s+)?(?:function|class|const|let)\s+|export\s*\{\s*)(\w+)', full_text, re.IGNORECASE)
        _prose_exports.extend(_ts_m)
        # Priority 4: Python def/class
        _py_m = re.findall(r'\b(?:def|class)\s+(\w+)', full_text)
        _prose_exports.extend(_py_m)
        _prose_exports = list(dict.fromkeys(_prose_exports))  # deduplicate, preserve order
        _prose_files = [p for p in (paths or []) if p not in (remaining or [])]
        if _prose_exports or _prose_files:
            _prose_role = ""
            # Priority 1: explicit "Role: ..." / "role: ..." line
            _role_m = re.search(
                r'(?:^|\n)\s*[Rr]ole\s*[:\-=]\s*(.+?)(?:\n|$)', full_text
            )
            if _role_m:
                _prose_role = _role_m.group(1).strip()[:120]
            # Priority 2: first non-trivial line (len > 20, not a path, not a system message)
            if not _prose_role:
                _SKIP_ROLE_PREFIXES = (
                    "[ALREADY", "[SKIP:", "[EXCLUDED", "[out of", "[CONTRACT",
                    "[System", "[ERROR", "Do NOT", "You must", "I have",
                    "--", "/*", "//", "CREATE", "INSERT", "SELECT", "import ", "const ", "{",
                )
                for _l in full_text.splitlines():
                    _ls = _l.strip()
                    if len(_ls) > 20 and "/" not in _ls and "\\" not in _ls \
                       and not _ls.startswith("#") and not _ls.startswith(_SKIP_ROLE_PREFIXES) \
                       and not _ls.startswith(("[", "{", "<")):
                        _prose_role = _ls[:120]
                        break
            # Priority 3: file names from actually-read files (blacklist non-code names)
            if not _prose_role:
                _ROLE_BLACKLIST = {"package-lock", "cache", "hash", "lock", "manifest", ".env", "dockerfile", "docker-compose", ".gitignore", "readme", "license"}
                _prose_files_names = [
                    Path(p).stem for p in _prose_files[:5]
                    if Path(p).stem.lower() not in _ROLE_BLACKLIST
                    and not any(b in Path(p).stem.lower() for b in ("v6", "v5", "v4", "v7"))
                ]
                _prose_role = f"{label} partition ({', '.join(_prose_files_names)}...)" if _prose_files_names else f"{label} partition (prose-extracted)"
            # Fallback: partition|module|component patterns from prose
            if not _prose_role or _prose_role == f"{label} partition (prose-extracted)":
                _role_m2 = re.search(r'(?:partition|module|component|service|database|server).*?[:\-=]\s*(.+?)(?:\.|\n)', full_text, re.IGNORECASE)
                if _role_m2 and len(_role_m2.group(1).strip()) > 3:
                    _prose_role = _role_m2.group(1).strip()[:120]
            _df_m = re.search(
                r'(?:data.flow|flows?\s*(?:through|via|→)|pipeline|processes?)\s*[:\-]?\s*(.{20,120})',
                full_text, re.IGNORECASE
            )
            _data_flow = _df_m.group(1).strip() if _df_m else f"{label} partition data flow"
            _imp_m = re.findall(
                r'(?:import|require|from)\s+["\'](\./|\.\./|(?!https?://)[a-z][^"\']*)["\']',
                full_text, re.IGNORECASE
            )
            _imports_internal = list(dict.fromkeys(
                p for p in _imp_m
                if not any(x in p.lower() for x in ("node_modules", "http", "@types"))
            ))[:8]
            contract = {
                "partition": label,
                "role": _prose_role or f"{label} partition (prose-extracted)",
                "exports": _prose_exports[:10],
                "files_read": _prose_files,
                "touched_by_task": "yes",
                "complexity_score": 0.5,
                "data_flow": _data_flow,
                "imports_internal": _imports_internal,
                "_fallback": True,
            }
            logger.warning("[%s] %s contract extracted from prose: %d exports, %d files",
                           sid, wk, len(_prose_exports), len(_prose_files))

    _zero_reads = read_n == 0 and len(paths) > 0 and not (aborted_fn and aborted_fn())
    if _zero_reads:
        logger.warning(
            "[%s] %s ZERO-READS: 0/%d files read (label=%s) - "
            "path problem or model did not issue tool calls. Contract may be hallucinated.",
            sid, wk, len(paths), label,
        )
        if emit_fn:
            try:
                await emit_fn({
                    "type": "status",
                    "content": (
                        f"⚠️ Pre-Explore Worker {wk} ({label}): "
                        f"0 of {len(paths)} files read — "
                        "path problem or no tool call from the model. "
                        "The contract may be hallucinated."
                    ),
                })
            except Exception:
                pass

    # ── Build summary (compact — what the coder gets) ─────────────────────────
    lines = [f"## {label}"]
    if contract:
        c = contract
        lines += [
            f"Hint: {c.get('hint', 'N/A')}",
            f"Exports: {', '.join(c.get('exports', []))}",
            f"Entry: {', '.join(c.get('entry_points', []))}",
            f"Imports: {', '.join(c.get('imports_needed', []))}",
            f"Complexity: {c.get('complexity_score', 'N/A')}",
            f"Touched: {c.get('touched_by_task', False)}",
        ]
        if c.get("plan"):
            lines.append(f"Plan:\n{c['plan']}")
    else:
        lines.append(f"Files: {read_n}/{len(paths)}")
        if full_text:
            lines.append(full_text[-800:])

    result = {
        "label":       label,
        "summary":     "\n".join(lines),
        "contract":    contract,
        "files_read":  read_n,
        "files_total": len(paths),
        "messages":    _coder_msgs,   # raw chat messages for coder bridge
    }

    if emit_fn:
        try:
            _c = contract or {}
            # Build plan_steps: list of {step, file, action} dicts for UI display
            _raw_plan = _c.get("plan") or ""
            _plan_steps: list[dict] = []
            if isinstance(_raw_plan, list):
                _plan_steps = _raw_plan
            elif isinstance(_raw_plan, str) and _raw_plan.strip():
                # Parse "step=N file=X action=Y" lines from raw plan string
                for _pl in _raw_plan.splitlines():
                    _pm = re.match(
                        r'step\s*=\s*(\d+).*?file\s*=\s*["\']?([^"\']+)["\']?.*?action\s*=\s*["\']?(.+)',
                        _pl.strip(), re.IGNORECASE
                    )
                    if _pm:
                        _plan_steps.append({
                            "step": int(_pm.group(1)),
                            "file": _pm.group(2).strip(),
                            "action": _pm.group(3).strip().rstrip('"\''),
                        })
            await emit_fn({
                "type": "partition_done", "label": label,
                "n_files_read": read_n,
                "files_total":  len(paths),               # ← total so UI can show skipped count
                "paths":        list(paths),              # ← original paths for skipped-file diff
                "files_read": sorted(_read_set),          # ← full path list for UI file tree
                "touched": _c.get("touched_by_task", False),
                "complexity": _c.get("complexity_score", 0.5),
                "hint": _c.get("hint", ""),
                "exports": _c.get("exports", []),
                "entry_points": _c.get("entry_points", []),
                "imports_needed": _c.get("imports_needed", []),
                "plan_steps": _plan_steps,                # ← structured plan for UI
                "zero_reads": _zero_reads,
                "worker_key": wk, "worker_model": mdl, "worker_port": prt,
                "worker_idx": worker_idx, "worker_total": worker_total,
            })
        except Exception:
            pass

    logger.info("[%s] %s DONE: %s (%d/%d files, contract=%s)",
                sid, wk, label, read_n, len(paths), contract is not None)
    return result


def _infer_importance(contract: dict) -> int:
    """Leitet Wichtigkeit aus Contract-Feldern ab."""
    if not contract:
        return 1
    score = 3  # Default
    if contract.get("entry_points"):
        score = max(score, 4)
    if contract.get("touched_by_task") == "yes":
        score = max(score, 4)
    if len(contract.get("exports", [])) > 5:
        score = max(score, 4)
    if contract.get("role", "").lower() in ("entry", "config"):
        score = max(score, 5)
    return min(5, score)


async def _worker_drain(
    *,
    worker_model: str,
    worker_port:  int,
    worker_key:   str,
    worker_idx:   int,
    worker_total: int,
    queue:        asyncio.Queue,
    results:      list,
    workspace:    str,
    tree_ctx:     str,
    pctx:         int,
    task:         str = "",
    sibling_map:  dict[str, list[str]] | None = None,
    max_tool_rounds: int = 20,
    max_unique_reads: int = 200,
    emit_fn=None,
    aborted_fn=None,
    thinking_override: bool | None = None,
):


    while True:
        try:
            partition = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        if partition is None:
            queue.task_done()
            continue
        label = partition.get("label", "?")
        paths = partition.get("paths") or partition.get("files") or []
        if not paths:
            queue.task_done()
            continue

        # Sibling-Labels berechnen
        base = label.split(":sz")[0]
        sibling_labels = []
        if sibling_map:
            sibling_labels = [s for s in sibling_map.get(base, []) if s != label]


        if aborted_fn and aborted_fn():
            queue.task_done()
            continue

        # ── LLM-Worker Exploration ──────────────────────────────────
        # Signature: part, slot, worker_idx, worker_total, sid, workspace, tree_ctx, msg_cap
        _part_dict = partition if isinstance(partition, dict) else {"label": label, "paths": paths}
        _slot_dict = {
            "model": worker_model,
            "port":  worker_port,
            "key":   worker_key,
            "num_ctx": pctx,
            "ctx":   pctx,
        }
        result = await _explore_partition(
            part=_part_dict,
            slot=_slot_dict,
            worker_idx=worker_idx,
            worker_total=worker_total,
            sid=worker_key,
            workspace=workspace,
            tree_ctx=tree_ctx,
            msg_cap=14,
            aborted_fn=aborted_fn,
            emit_fn=emit_fn,
            max_unique_reads=max_unique_reads,
            thinking_override=thinking_override,
            llm_read_timeout=llm_read_timeout,
        )
        # Normalize result keys to match expected format
        result.setdefault("n_files_read", result.get("files_read", 0)
                          if isinstance(result.get("files_read"), int) else
                          len(result.get("files_read", [])))
        result.setdefault("read_calls", 0)
        result.setdefault("importance", _infer_importance(result.get("contract") or {}))
        result.setdefault("exports", (result.get("contract") or {}).get("exports", []))
        result.setdefault("entry_points", (result.get("contract") or {}).get("entry_points", []))
        result.setdefault("imports_needed", (result.get("contract") or {}).get("imports_needed", []))
        result.setdefault("hint", (result.get("contract") or {}).get("hint", ""))
        result.setdefault("touched", (result.get("contract") or {}).get("touched_by_task", False))
        result.setdefault("complexity", (result.get("contract") or {}).get("complexity_score", 0.5))
        # files_read: working version returns int, our result dict expects list for emit
        if isinstance(result.get("files_read"), int):
            result["files_read"] = []  # emit uses this as list; int was read_n

        results.append(result)
        queue.task_done()

        logger.info(
            "[worker %s] %s done — %d/%d files (%d reads), imp=%d",
            worker_key, label, result["n_files_read"], len(paths),
            result.get("read_calls", 0), result.get("importance", _infer_importance(result.get("contract") or {})),
        )


async def _make_fallback_partition(workspace: str, tree_ctx: str, user_input: str) -> list[dict]:


    ws = Path(workspace)

    # Strategie 1 — tree_ctx parsen
    if tree_ctx:
        paths = _extract_paths_from_tree(tree_ctx, workspace)
        if paths:
            logger.info(
                "Gap-1-FIX: fallback partition from tree_ctx (%d files)", len(paths)
            )
            return [{"label": "workspace", "paths": paths}]

    # Strategie 2 — direkter Filesystem-Scan
    try:
        found: list[str] = []
        for ext in sorted(_CODE_EXTS):
            for f in ws.rglob(f"*{ext}"):
                if f.is_file() and not f.name.startswith(_SKIP_FILE_PREFIXES) and not any(s in (p.lower() for p in f.parts) for s in _SKIP_DIRS):
                    try:
                        found.append(str(f.relative_to(ws)))
                    except ValueError:
                        found.append(str(f))
                if len(found) >= _MAX_FALLBACK_FILES:
                    break
            if len(found) >= _MAX_FALLBACK_FILES:
                break
        if found:
            logger.info(
                "Gap-1-FIX: fallback partition via rglob (%d files)", len(found)
            )
            return [{"label": "workspace", "paths": found}]
    except Exception as e:
        logger.warning("Gap-1-FIX: rglob failed: %s", e)

    return []
