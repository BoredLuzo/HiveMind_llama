"""Lightweight stdlib-only GGUF v2/v3 header & metadata reader.

Reads architectural dimensions, MoE counts, chat-template, thinking tokens,
vision projector hints, and derived KV-cache size from any dropped GGUF file
without loading tensors or hardcoding model families.
"""
from __future__ import annotations

import glob
import logging
import re
import struct
from pathlib import Path
from typing import Any, BinaryIO

_logger = logging.getLogger(__name__)

_GGUF_MAGIC = b"GGUF"
_THINK_TOKEN_BYTES = frozenset({
    b"<think>",
    b"</think>",
    b"[THINK]",
    b"[/THINK]",
    b"<|thinking|>",
    b"<|thought|>",
    b"<thought>",
})
_FIXED_FMTS: dict[int, tuple[str, int]] = {
    0: ("<B", 1),   # UINT8
    1: ("<b", 1),   # INT8
    2: ("<H", 2),   # UINT16
    3: ("<h", 2),   # INT16
    4: ("<I", 4),   # UINT32
    5: ("<i", 4),   # INT32
    6: ("<f", 4),   # FLOAT32
    7: ("<?", 1),   # BOOL
    10: ("<Q", 8),  # UINT64
    11: ("<q", 8),  # INT64
    12: ("<d", 8),  # FLOAT64
}
_WANT_SUFFIXES = frozenset({
    ".block_count",
    ".context_length",
    ".embedding_length",
    ".attention.head_count",
    ".attention.head_count_kv",
    ".attention.key_length",
    ".attention.value_length",
    ".attention.sliding_window",
    ".attention.sliding_window_pattern",
    ".attention.shared_kv_layers",
    ".expert_count",
    ".expert_used_count",
    ".full_attention_interval",
    ".vision.block_count",
    ".loop.count",
    ".loop.start",
    ".loop.end",
})

# Cache keyed by resolved path string -> (mtime_ns, size, parsed dict)
_META_CACHE: dict[str, tuple[int, int, dict[str, Any]]] = {}
# Parent-dir listing cache for detect_sibling_files: parent_str -> (dir_mtime_ns, entries)
_SIBLING_DIR_CACHE: dict[str, tuple[int, list[tuple[Path, bool, str, str | None]]]] = {}

_TPL_COMMENT_RE = re.compile(r"\{#.*?#\}", re.S)
_TPL_DEFAULT_PATS = tuple(
    re.compile(p) for p in (
        r"_default_reasoning_effort\s*=\s*['\"]([a-zA-Z0-9_]+)['\"]",
        r"\breasoning_effort\s*\|\s*default\s*\(\s*['\"]([a-zA-Z0-9_]+)['\"]",
        r"\bdefault_reasoning_effort\s*=\s*['\"]([a-zA-Z0-9_]+)['\"]",
        r"\breasoning_effort\b[^\n]*\belse\s+['\"]([a-zA-Z0-9_]+)['\"]",
    )
)
_TPL_ELSE_NUM_RE = re.compile(r"\breasoning_effort[^\n]*\belse\s+([0-9]+(?:\.[0-9]+)?)")
_TPL_EFFORT_MAP_RE = re.compile(r"\beffort_map\s*=\s*\{([^}]+)\}")
_TPL_MAP_KEY_RE = re.compile(r"['\"]([a-zA-Z0-9_]+)['\"]\s*:")
_TPL_GUARD_RE = re.compile(r"reasoning_effort[^\n]*\bnot\s+in\s*[\(\[]([^\)\]]+)[\)\]]")
_TPL_QUOTED_STR_RE = re.compile(r"['\"]([a-zA-Z0-9_]+)['\"]")
_TPL_BRANCH_RE = re.compile(
    r"\{%-?\s*(?:el)?if\b([^\n]*?(?<!\.)\b(?:_effort_raw|reasoning_effort|resolved_reasoning_effort|effort)\b[^\n]*?)-?%\}",
    re.IGNORECASE,
)
_TPL_EQ_RE = re.compile(r"(?<!\.)\b(?:_effort_raw|reasoning_effort)\s*==\s*['\"]([a-zA-Z0-9_]+)['\"]")


def clear_cache() -> None:
    """Clear the cached GGUF metadata and reasoning-info maps (primarily for tests/rescan)."""
    _META_CACHE.clear()
    _SIBLING_DIR_CACHE.clear()
    _REASONING_INFO_CACHE.clear()


def _read_exact(f: BinaryIO, n: int) -> bytes:
    buf = f.read(n)
    if len(buf) != n:
        raise EOFError(f"unexpected EOF reading {n} bytes")
    return buf


def _read_str(f: BinaryIO, *, max_len: int = 16 * 1024 * 1024) -> str:
    (slen,) = struct.unpack("<Q", _read_exact(f, 8))
    if slen > max_len:
        raise ValueError(f"GGUF string exceeds guard ({slen})")
    return _read_exact(f, slen).decode("utf-8", errors="replace")


_MAX_KV_COUNT = 250_000
_MAX_ARRAY_COUNT = 2_000_000
_MAX_STR_LEN = 16 * 1024 * 1024
_SPLIT_SHARD_RE = re.compile(r'^(?P<base>.+)-(?P<idx>\d{5})-of-(?P<total>\d{5})$', re.IGNORECASE)
_MMPROJ_DTYPE_TOKENS = frozenset({
    "mmproj", "f16", "f32", "bf16", "q8", "q8_0", "q4", "q5", "q6", "vision", "projector",
})
_SIBLING_NOISE_TOKENS = _MMPROJ_DTYPE_TOKENS | frozenset({
    "0", "1", "k", "m", "s", "l", "xs", "xl", "xxl", "nl", "ud", "iq1", "iq2", "iq3", "iq4",
    "mtp", "dflash", "dspark", "draft", "spec", "sidecar", "encoder", "vl",
})
_SIZE_TOKEN_RE = re.compile(r"^(?:e?\d+(?:\.\d+)?b|a\d+(?:\.\d+)?b)$", re.IGNORECASE)
_TOOL_CALL_TEMPLATE_RE = re.compile(
    r"(?:"
    r"\{%[^%]*\btools\b"
    r"|\{\{\s*tools\b"
    r"|<tools>"
    r"|\bfor\s+tool\s+in\b"
    r"|\btool_calls?\b"
    r"|\btool_response\b"
    r"|\btool_declaration\b"
    r"|\[TOOL_CALLS\]"
    r"|\bfunction_call\b"
    r"|<function[=\s>]"
    r"|<\|tool"
    r"|<\|call:"
    r"|<\|plugin\|>"
    r"|\bavailable_tools\b"
    r")",
    re.IGNORECASE,
)


def _detect_has_tool_call(chat_tpl_str: str | None) -> bool:
    """Detect tool/function-calling capability from GGUF tokenizer.chat_template."""
    if not chat_tpl_str:
        return False
    return bool(_TOOL_CALL_TEMPLATE_RE.search(chat_tpl_str))


def _skip_value(f: BinaryIO, vtype: int, _depth: int = 0) -> None:
    if _depth > 8:
        raise ValueError("GGUF nested array depth exceeded")
    if vtype in _FIXED_FMTS:
        f.seek(_FIXED_FMTS[vtype][1], 1)
    elif vtype == 8:
        (slen,) = struct.unpack("<Q", _read_exact(f, 8))
        if slen > _MAX_STR_LEN:
            raise ValueError(f"GGUF string exceeds guard ({slen})")
        f.seek(slen, 1)
    elif vtype == 9:
        etype, count = struct.unpack("<IQ", _read_exact(f, 12))
        if count > _MAX_ARRAY_COUNT:
            raise ValueError(f"GGUF array exceeds guard ({count})")
        if etype in _FIXED_FMTS:
            f.seek(count * _FIXED_FMTS[etype][1], 1)
        else:
            for _ in range(count):
                _skip_value(f, etype, _depth + 1)
    else:
        raise ValueError(f"unknown GGUF value type {vtype}")


def _scan_tokens_for_think(f: BinaryIO, count: int) -> bool:
    if count > _MAX_ARRAY_COUNT:
        raise ValueError(f"GGUF token array exceeds guard ({count})")
    found = False
    for _ in range(count):
        (slen,) = struct.unpack("<Q", _read_exact(f, 8))
        if slen > _MAX_STR_LEN:
            raise ValueError(f"GGUF token string exceeds guard ({slen})")
        if slen <= 20:
            if _read_exact(f, slen) in _THINK_TOKEN_BYTES:
                found = True
        else:
            f.seek(slen, 1)
    return found


def _read_value(f: BinaryIO, vtype: int, key: str) -> Any:
    if vtype in _FIXED_FMTS:
        fmt, sz = _FIXED_FMTS[vtype]
        return struct.unpack(fmt, _read_exact(f, sz))[0]
    if vtype == 8:
        return _read_str(f)
    if vtype == 9:
        etype, count = struct.unpack("<IQ", _read_exact(f, 12))
        if count > _MAX_ARRAY_COUNT:
            raise ValueError(f"GGUF array exceeds guard ({count})")
        if key == "tokenizer.ggml.tokens" and etype == 8:
            return _scan_tokens_for_think(f, count)
        if (
            key.endswith(".attention.head_count")
            or key.endswith(".attention.head_count_kv")
            or key.endswith(".attention.sliding_window_pattern")
        ) and etype in _FIXED_FMTS and count <= 512:
            fmt, sz = _FIXED_FMTS[etype]
            vals = [struct.unpack(fmt, _read_exact(f, sz))[0] for _ in range(count)]
            return vals
        if etype in _FIXED_FMTS:
            f.seek(count * _FIXED_FMTS[etype][1], 1)
        else:
            for _ in range(count):
                _skip_value(f, etype)
        return None
    raise ValueError(f"unknown GGUF value type {vtype}")


def _needs_key(key: str) -> bool:
    if key in ("general.architecture", "general.name", "tokenizer.chat_template", "tokenizer.ggml.tokens"):
        return True
    return any(key.endswith(sfx) for sfx in _WANT_SUFFIXES)


def _is_helper_gguf_name(name: str) -> bool:
    """Return True if a .gguf filename is a sidecar/projector/drafter rather than a base model."""
    low = name.lower()
    return (
        "mmproj" in low
        or "projector" in low
        or "vision_encoder" in low
        or "vl-encoder" in low
        or low.startswith("mtp-")
        or "dflash" in low
        or "dspark" in low
    )


def _split_tokens(stem: str, strip_set: frozenset[str] = frozenset()) -> list[str]:
    return [
        t for t in stem.lower().replace("_", "-").split("-")
        if t and t not in strip_set
    ]


def detect_sibling_files(path: Path) -> dict[str, str | None]:
    """Auto-detect sibling projector/drafter .gguf files in `path.parent`.

    Detects `mmproj`, `mtp-*`, `*dflash*`, and `*dspark*` without any Python
    model allowlist while preserving multi-model directory isolation.
    """
    found: dict[str, str | None] = {
        "mmproj": None,
        "mtp": None,
        "dflash": None,
        "dspark": None,
    }
    if not path or not path.parent.is_dir():
        return found

    base_stem = _SPLIT_SHARD_RE.sub(r"\g<base>", path.stem)
    stem_tokens = _split_tokens(base_stem)
    stem_set = set(stem_tokens)

    try:
        dir_mtime = path.parent.stat().st_mtime_ns
    except OSError:
        return found

    dir_key = str(path.parent)
    cached_dir = _SIBLING_DIR_CACHE.get(dir_key)
    if cached_dir is not None and cached_dir[0] == dir_mtime:
        entries = cached_dir[1]
    else:
        try:
            all_ggufs = sorted(path.parent.glob("*.gguf"), key=lambda p: p.name.lower())
        except OSError:
            return found
        entries = []
        for p in all_ggufs:
            low_name = p.name.lower()
            kind: str | None = None
            if "mmproj" in low_name or "projector" in low_name:
                kind = "mmproj"
            elif low_name.startswith("mtp-") or "-mtp-draft" in low_name or "_mtp_draft" in low_name:
                kind = "mtp"
            elif "dflash" in low_name:
                kind = "dflash"
            elif "dspark" in low_name:
                kind = "dspark"
            if kind is not None:
                try:
                    if not p.is_file() or p.stat().st_size <= 0:
                        continue
                except OSError:
                    continue
            entries.append((
                p,
                _is_helper_gguf_name(p.name),
                _SPLIT_SHARD_RE.sub(r"\g<base>", p.stem),
                kind,
            ))
        _SIBLING_DIR_CACHE[dir_key] = (dir_mtime, entries)

    has_other_models = any(
        (not is_helper and p != path and p_base != base_stem)
        for p, is_helper, p_base, _kind in entries
    )

    best_scores: dict[str, int] = {k: -1 for k in found}

    for sib, _is_helper, _sib_base, kind in entries:
        if sib == path or kind is None:
            continue

        if kind == "mmproj":
            sib_tokens = _split_tokens(sib.stem, _MMPROJ_DTYPE_TOKENS)
            score = -1
            if sib_tokens:
                if (len(sib_tokens) >= 2 and all(tok in stem_set for tok in sib_tokens)) or (
                    len(sib_tokens) == 1 and sib_tokens[0] in stem_set
                ):
                    score = 100 + len(sib_tokens)
            elif not has_other_models:
                score = 10
        else:
            sib_tokens = _split_tokens(sib.stem, _SIBLING_NOISE_TOKENS)
            fam_tokens = [t for t in sib_tokens if not _SIZE_TOKEN_RE.match(t)]
            score = -1
            if sib_tokens and all(tok in stem_set for tok in sib_tokens):
                score = 100 + len(sib_tokens)
            elif fam_tokens and all(tok in stem_set for tok in fam_tokens):
                score = 50 + len(fam_tokens)
            elif not has_other_models:
                score = 10 if (not sib_tokens or not fam_tokens) else 5

        if score > best_scores[kind]:
            best_scores[kind] = score
            found[kind] = str(sib)

    return found


def _sibling_has_mmproj(path: Path) -> bool:
    """Return True if a matching vision projector exists alongside `path`."""
    return bool(detect_sibling_files(path).get("mmproj"))


def _derive_kv_bytes(
    block_count: int | None,
    head_count: int | None,
    head_count_kv: float | int | None,
    embedding_length: int | None,
    key_length: int | None,
    value_length: int | None,
    full_attention_interval: int | None,
    sliding_window_pattern: list[Any] | None = None,
    shared_kv_layers: int | None = None,
) -> tuple[int | None, int | None]:
    if not block_count or not head_count_kv:
        return None, None
    kv_heads = float(head_count_kv)
    if kv_heads <= 0:
        return None, None
    k_dim = key_length or (
        (embedding_length // head_count) if (embedding_length and head_count) else 128
    )
    v_dim = value_length or k_dim
    # Architectures with KV sharing (e.g. Gemma-4 shared_kv_layers) only allocate
    # unique KV buffers on the leading non-shared layers.
    own_layers = (
        max(1, block_count - int(shared_kv_layers))
        if (isinstance(shared_kv_layers, int) and 0 < shared_kv_layers < block_count)
        else block_count
    )
    # Sliding-window attention layers use a small fixed ring buffer that does not
    # grow with num_ctx; only non-SWA full-attention layers scale linearly with ctx.
    if isinstance(sliding_window_pattern, list) and sliding_window_pattern:
        full_layers = sum(1 for is_swa in sliding_window_pattern[:own_layers] if not bool(is_swa))
        attn_layers = full_layers if full_layers > 0 else own_layers
    elif full_attention_interval and full_attention_interval > 1:
        # Hybrid linear/full attention architectures (e.g. Qwen3.5) store growing KV only on full layers.
        attn_layers = max(1, own_layers // full_attention_interval)
    else:
        attn_layers = own_layers
    elems_per_tok = attn_layers * kv_heads * (k_dim + v_dim)
    return round(elems_per_tok * (18.0 / 32.0)), round(elems_per_tok * 2.0)


def _parse_gguf_file(path: Path, size_bytes: int) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    think_token_found = False
    with path.open("rb") as f:
        if _read_exact(f, 4) != _GGUF_MAGIC:
            return {}
        (version,) = struct.unpack("<I", _read_exact(f, 4))
        if version not in (2, 3):
            return {}
        tensor_count, kv_count = struct.unpack("<QQ", _read_exact(f, 16))
        if kv_count > _MAX_KV_COUNT:
            raise ValueError(f"GGUF kv_count exceeds guard ({kv_count})")
        for _ in range(kv_count):
            key = _read_str(f)
            (vtype,) = struct.unpack("<I", _read_exact(f, 4))
            if _needs_key(key):
                val = _read_value(f, vtype, key)
                if key == "tokenizer.ggml.tokens":
                    think_token_found = bool(val)
                elif val is not None:
                    raw[key] = val
            else:
                _skip_value(f, vtype)

    arch = str(raw.get("general.architecture") or "").strip().lower()

    def _arch_get(suffix: str) -> Any:
        if arch and f"{arch}{suffix}" in raw:
            return raw[f"{arch}{suffix}"]
        for k, v in raw.items():
            if k.endswith(suffix) and not k.endswith(f".vision{suffix}"):
                return v
        return None

    head_kv_raw = _arch_get(".attention.head_count_kv")
    head_kv: float | int | None = None
    if isinstance(head_kv_raw, list):
        nz = [float(x) for x in head_kv_raw if isinstance(x, (int, float)) and x > 0]
        head_kv = round(sum(nz) / len(nz), 3) if nz else None
    elif isinstance(head_kv_raw, (int, float)):
        head_kv = head_kv_raw

    block_count = _arch_get(".block_count")
    head_count_raw = _arch_get(".attention.head_count")
    head_count: int | None = None
    if isinstance(head_count_raw, list):
        _hc_nz = [float(x) for x in head_count_raw if isinstance(x, (int, float)) and x > 0]
        head_count = int(round(sum(_hc_nz) / len(_hc_nz))) if _hc_nz else None
    elif isinstance(head_count_raw, (int, float)):
        head_count = int(head_count_raw)
    emb_len = _arch_get(".embedding_length")
    k_len = _arch_get(".attention.key_length")
    v_len = _arch_get(".attention.value_length")
    full_attn_int = _arch_get(".full_attention_interval")
    swa_pattern = _arch_get(".attention.sliding_window_pattern")
    shared_kv_raw = _arch_get(".attention.shared_kv_layers")
    shared_kv_layers = int(shared_kv_raw) if isinstance(shared_kv_raw, int) and shared_kv_raw > 0 else None
    loop_count = int(_arch_get(".loop.count") or 0)
    loop_start_raw = _arch_get(".loop.start")
    loop_end_raw = _arch_get(".loop.end")
    loop_start = int(loop_start_raw) if isinstance(loop_start_raw, int) else None
    loop_end = int(loop_end_raw) if isinstance(loop_end_raw, int) else None
    expert_count = int(_arch_get(".expert_count") or 0)
    expert_used = int(_arch_get(".expert_used_count") or 0)
    vision_blocks = next(
        (int(v) for k, v in raw.items() if k.endswith(".vision.block_count") and isinstance(v, int)),
        0,
    )
    chat_tpl = raw.get("tokenizer.chat_template")
    chat_tpl_str = chat_tpl if isinstance(chat_tpl, str) else None
    tpl_low = (chat_tpl_str or "").lower()
    has_thinking = bool(
        think_token_found
        or "enable_thinking" in tpl_low
        or "reasoning_effort" in tpl_low
        or "<|content_thinking|>" in tpl_low
        or "<think>" in tpl_low
        or "[think]" in tpl_low
    )
    has_tool_call = _detect_has_tool_call(chat_tpl_str)
    siblings = detect_sibling_files(path)
    has_embedded_vision = vision_blocks > 0
    has_vision = bool(has_embedded_vision or siblings.get("mmproj"))
    eff_blocks = block_count
    eff_swa = swa_pattern if isinstance(swa_pattern, list) else None
    _MAX_LOOP_COUNT = 256
    _MAX_BLOCK_COUNT = 4096
    if (
        1 < loop_count <= _MAX_LOOP_COUNT
        and isinstance(block_count, int)
        and 0 < block_count <= _MAX_BLOCK_COUNT
        and loop_start is not None
        and loop_end is not None
        and 0 <= loop_start < loop_end <= block_count
    ):
        eff_blocks = block_count + (loop_end - loop_start) * (loop_count - 1)
        if isinstance(eff_swa, list) and len(eff_swa) == block_count:
            eff_swa = eff_swa[:loop_start] + eff_swa[loop_start:loop_end] * loop_count + eff_swa[loop_end:]
    kv_q4, kv_f16 = _derive_kv_bytes(
        eff_blocks, head_count, head_kv, emb_len, k_len, v_len, full_attn_int,
        sliding_window_pattern=eff_swa,
        shared_kv_layers=shared_kv_layers,
    )
    return {
        "path": str(path),
        "file_size_gb": round(size_bytes / (1024 ** 3), 3),
        "version": version,
        "tensor_count": tensor_count,
        "architecture": arch,
        "name": str(raw.get("general.name") or ""),
        "block_count": block_count,
        "context_length": _arch_get(".context_length"),
        "embedding_length": emb_len,
        "head_count": head_count,
        "head_count_kv": head_kv,
        "key_length": k_len,
        "value_length": v_len,
        "expert_count": expert_count,
        "expert_used_count": expert_used,
        "loop_count": loop_count,
        "loop_start": loop_start,
        "loop_end": loop_end,
        "sliding_window": _arch_get(".attention.sliding_window"),
        "full_attention_interval": full_attn_int,
        "chat_template": chat_tpl_str,
        "has_chat_template": bool(chat_tpl_str),
        "has_tool_call": has_tool_call,
        "has_thinking": has_thinking,
        "has_embedded_vision": has_embedded_vision,
        "has_vision": has_vision,
        "siblings": siblings,
        "is_moe": expert_count > 0,
        "kv_bytes_per_token_q4": kv_q4,
        "kv_bytes_per_token_f16": kv_f16,
    }


def _total_gguf_size_bytes(path: Path, single_size: int) -> int:
    m = _SPLIT_SHARD_RE.match(path.stem)
    if not m:
        return single_size
    base = m.group("base")
    total_sfx = m.group("total")
    try:
        shards = list(path.parent.glob(f"{glob.escape(base)}-*-of-{glob.escape(total_sfx)}{glob.escape(path.suffix)}"))
    except OSError:
        return single_size
    if len(shards) <= 1:
        return single_size
    total = 0
    for s in shards:
        try:
            total += s.stat().st_size
        except OSError:
            pass
    return total or single_size


def read_gguf_metadata(path_or_model: str | Path) -> dict[str, Any]:
    """Return cached GGUF header metadata for a file path or registered model name."""
    raw = str(path_or_model or "").strip()
    if not raw:
        return {}
    p = Path(path_or_model) if isinstance(path_or_model, Path) else Path(raw)
    if not p.is_file():
        try:
            from backend.llama_models import resolve_model_path
            try:
                resolved = resolve_model_path(raw, quiet=True)
            except TypeError:
                resolved = resolve_model_path(raw)
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            _logger.debug("[GGUF-META] model path lookup skipped for %r: %s", raw, exc)
            return {}
        if not resolved or not resolved.is_file():
            return {}
        p = resolved
    try:
        st = p.stat()
        key = str(p.resolve())
    except OSError:
        return {}
    total_size = _total_gguf_size_bytes(p, st.st_size)
    cached = _META_CACHE.get(key)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == total_size:
        out = dict(cached[2])
        siblings = detect_sibling_files(p)
        out["siblings"] = siblings
        out["has_vision"] = bool(out.get("has_embedded_vision") or siblings.get("mmproj"))
        return out
    try:
        parsed = _parse_gguf_file(p, total_size)
    except (OSError, struct.error, ValueError, EOFError, UnicodeDecodeError, OverflowError, TypeError) as exc:
        _logger.debug("[GGUF-META] failed to parse %s: %s", p, exc)
        return {}
    _META_CACHE[key] = (st.st_mtime_ns, total_size, parsed)
    return dict(parsed)


_REASONING_CANONICAL_ORDER = ("none", "minimal", "low", "medium", "high", "max")
_REASONING_INFO_CACHE: dict[str, tuple[str, int, str, int, int, bool | None, dict[str, Any]]] = {}


def _norm_tpl_effort_token(tok: str, *, strict: bool = False) -> str:
    t = tok.strip().lower()
    if t == "none":
        return "none"
    if not strict and t in ("off", "disabled", "false", "0"):
        return "none"
    if t == "med":
        return "medium"
    if t in ("max", "maximum"):
        return "max"
    if not strict and t == "xhigh":
        return "max"
    if t in ("minimal", "low", "medium", "high"):
        return t
    return ""


def _pick_group_canonical_level(tokens: list[str]) -> list[str]:
    """Collapse multi-alias branch checks (e.g. ('high','xhigh','max')) to one level."""
    normed = [_norm_tpl_effort_token(t) for t in tokens]
    normed = [x for x in normed if x]
    if not normed:
        return []
    uniq = list(dict.fromkeys(normed))
    if "low" in uniq and "minimal" in uniq and "medium" not in uniq and "high" not in uniq:
        return ["low"]
    if "high" in uniq and "max" in uniq and "medium" not in uniq and "low" not in uniq:
        return ["high"]
    return uniq


def _extract_template_default_effort(tpl: str) -> str | None:
    for rx in _TPL_DEFAULT_PATS:
        m = rx.search(tpl)
        if m:
            norm = _norm_tpl_effort_token(m.group(1))
            if norm:
                return norm
    m_num = _TPL_ELSE_NUM_RE.search(tpl)
    if m_num:
        try:
            val = float(m_num.group(1))
            if 0.0 <= val <= 1.0:
                if val <= 0.0:
                    return "none"
                if val <= 0.12:
                    return "minimal"
                if val <= 0.4:
                    return "low"
                if val <= 0.75:
                    return "medium"
                if val < 0.95:
                    return "high"
                return "max"
        except ValueError:
            pass
    return None


def inspect_template_reasoning(template_text: str, *, has_thinking: bool = False) -> dict[str, Any]:
    """Inspect a Jinja chat template to determine exact supported reasoning controls."""
    tpl = _TPL_COMMENT_RE.sub("", str(template_text or ""))
    supports_enable_thinking = "enable_thinking" in tpl
    has_effort = "reasoning_effort" in tpl
    if not has_effort:
        if has_thinking or supports_enable_thinking:
            return {
                "mode": "binary",
                "levels": ["none", "on"],
                "accepts_none_effort": False,
                "supports_enable_thinking": supports_enable_thinking,
                "template_default": None,
            }
        return {
            "mode": "none",
            "levels": [],
            "accepts_none_effort": False,
            "supports_enable_thinking": False,
            "template_default": None,
        }

    tpl_default = _extract_template_default_effort(tpl)
    found: set[str] = set()
    accepts_none_effort = False

    m_map = _TPL_EFFORT_MAP_RE.search(tpl)
    if m_map:
        raw_keys = [k.strip().lower() for k in _TPL_MAP_KEY_RE.findall(m_map.group(1))]
        accepts_none_effort = "none" in raw_keys
        for k in raw_keys:
            norm = _norm_tpl_effort_token(k, strict=True)
            if norm:
                found.add(norm)
    else:
        m_guard = _TPL_GUARD_RE.search(tpl)
        _guard_pos: list[str] = []
        _guard_raw: list[str] = []
        if m_guard:
            _guard_raw = [v.strip().lower() for v in _TPL_QUOTED_STR_RE.findall(m_guard.group(1))]
            _guard_pos = [
                n for n in (_norm_tpl_effort_token(v, strict=True) for v in _guard_raw)
                if n and n != "none"
            ]
        if m_guard and _guard_pos:
            accepts_none_effort = "none" in _guard_raw
            for v in _guard_raw:
                norm = _norm_tpl_effort_token(v, strict=True)
                if norm:
                    found.add(norm)
        else:
            for m_br in _TPL_BRANCH_RE.finditer(tpl):
                cond = m_br.group(1)
                if "<|" in cond:
                    continue
                vals = [v.strip().lower() for v in _TPL_QUOTED_STR_RE.findall(cond)]
                if not vals:
                    continue
                if "none" in vals:
                    accepts_none_effort = True
                for lvl in _pick_group_canonical_level(vals):
                    found.add(lvl)
            if not any(lvl != "none" for lvl in found):
                for _eq_v in _TPL_EQ_RE.findall(tpl):
                    _eq_low = _eq_v.strip().lower()
                    _eq_norm = _norm_tpl_effort_token(_eq_low)
                    if _eq_norm == "none":
                        if _eq_low == "none":
                            accepts_none_effort = True
                        found.add("none")
                    elif _eq_norm:
                        found.add(_eq_norm)
            if tpl_default:
                found.add(tpl_default)
            # TwIL-style single-toggle templates (`reasoning_effort == "low"` only):
            # expose explicit low + standard high (plus Off via enable_thinking).
            if found == {"low"} or found == {"none", "low"}:
                found.add("high")
                if not tpl_default:
                    tpl_default = "high"
            if not any(lvl != "none" for lvl in found):
                found.update(("low", "medium", "high"))

    if supports_enable_thinking:
        found.add("none")

    levels = [lvl for lvl in _REASONING_CANONICAL_ORDER if lvl in found]
    if not any(lvl != "none" for lvl in levels):
        levels = (["none"] if (supports_enable_thinking or accepts_none_effort) else []) + ["low", "medium", "high"]
    return {
        "mode": "effort",
        "levels": levels,
        "accepts_none_effort": accepts_none_effort,
        "supports_enable_thinking": supports_enable_thinking,
        "template_default": tpl_default,
    }


def _gguf_stat_key(model_name: str) -> tuple[str, int, int]:
    """Cheaply locate GGUF file stat (path, mtime_ns, size) for cache validation."""
    raw = str(model_name or "").strip()
    if not raw:
        return ("", 0, 0)
    p = Path(raw)
    if not p.is_file():
        try:
            from backend.llama_models import resolve_model_path
            try:
                resolved = resolve_model_path(raw, quiet=True)
            except TypeError:
                resolved = resolve_model_path(raw)
        except (ImportError, OSError, RuntimeError, ValueError):
            return ("", 0, 0)
        if not resolved or not resolved.is_file():
            return ("", 0, 0)
        p = resolved
    try:
        st = p.stat()
        return (str(p.resolve()), st.st_mtime_ns, st.st_size)
    except OSError:
        return ("", 0, 0)


def get_effective_reasoning_info(model_name: str) -> dict[str, Any]:
    """Return effective reasoning capability/levels from external .jinja or GGUF metadata."""
    name = str(model_name or "").strip()
    if not name:
        return {
            "mode": "none",
            "levels": [],
            "accepts_none_effort": False,
            "supports_enable_thinking": False,
            "template_default": None,
            "template_source": "none",
        }

    reg_tpl: str | None = None
    reg_caps: dict[str, Any] | None = None
    try:
        from model_configs.models_registry import (
            get_capabilities as _reg_caps_fn,
            get_chat_template as _reg_tpl_fn,
        )
        reg_tpl = _reg_tpl_fn(name)
        reg_caps = _reg_caps_fn(name)
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        _logger.debug("[GGUF-META] registry lookup skipped for %r: %s", name, exc)

    ext_path: Path | None = None
    try:
        from backend.manager_load import resolve_chat_template_file
        _no_alias = name.split("#", 1)[0]
        model_base = _no_alias.split(":", 1)[0].lower()
        ext_path = resolve_chat_template_file(model_base, reg_tpl)
    except (ImportError, AttributeError, TypeError, ValueError, OSError) as exc:
        _logger.debug("[GGUF-META] external template lookup skipped for %r: %s", name, exc)

    ext_mtime = 0
    ext_key = str(ext_path or "")
    if ext_path is not None and ext_path.is_file():
        try:
            ext_mtime = ext_path.stat().st_mtime_ns
        except OSError:
            ext_mtime = 0

    explicit_think: bool | None = None
    try:
        from core.model_sampling import _model_profile as _mp_cap_fn
        _mp_caps = _mp_cap_fn(name)
        if isinstance(_mp_caps, dict) and "thinking" in _mp_caps:
            explicit_think = bool(_mp_caps["thinking"])
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        _logger.debug("[GGUF-META] model_profile lookup skipped for %r: %s", name, exc)

    if explicit_think is None and isinstance(reg_caps, dict) and "thinking" in reg_caps:
        explicit_think = bool(reg_caps["thinking"])
    if explicit_think is None:
        try:
            from routing.model_automap import get_model_capabilities
            auto_caps = get_model_capabilities(name)
            if isinstance(auto_caps, dict) and "thinking" in auto_caps:
                explicit_think = bool(auto_caps["thinking"])
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            _logger.debug("[GGUF-META] automap caps skipped for %r: %s", name, exc)

    gguf_key, gguf_mtime, gguf_size = _gguf_stat_key(name)
    cache_key = name.lower()
    cached = _REASONING_INFO_CACHE.get(cache_key)
    if (
        cached
        and cached[0] == ext_key
        and cached[1] == ext_mtime
        and cached[2] == gguf_key
        and cached[3] == gguf_mtime
        and cached[4] == gguf_size
        and cached[5] == explicit_think
    ):
        _ret_info = dict(cached[6])
        if "levels" in _ret_info and isinstance(_ret_info["levels"], list):
            _ret_info["levels"] = list(_ret_info["levels"])
        return _ret_info

    tpl_text = ""
    tpl_source = "none"
    if ext_path is not None and ext_path.is_file():
        try:
            tpl_text = ext_path.read_text(encoding="utf-8", errors="replace")
            tpl_source = f"external:{ext_path.name}"
        except OSError:
            tpl_text = ""

    meta = read_gguf_metadata(name)
    if not tpl_text and meta:
        tpl_text = str(meta.get("chat_template") or "")
        if tpl_text:
            tpl_source = "gguf"

    has_thinking = explicit_think if explicit_think is not None else bool(meta.get("has_thinking")) if meta else False

    # Honor explicit thinking=False capability/override unconditionally so UI & backend never split-brain.
    if explicit_think is False:
        info: dict[str, Any] = {
            "mode": "none",
            "levels": [],
            "accepts_none_effort": False,
            "supports_enable_thinking": False,
            "template_default": None,
            "template_source": tpl_source,
        }
    else:
        info = inspect_template_reasoning(tpl_text, has_thinking=has_thinking)
        info["template_source"] = tpl_source

    _REASONING_INFO_CACHE[cache_key] = (
        ext_key, ext_mtime, gguf_key, gguf_mtime, gguf_size, explicit_think, dict(info),
    )
    return info
