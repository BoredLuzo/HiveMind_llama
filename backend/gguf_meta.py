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
    ".expert_count",
    ".expert_used_count",
    ".full_attention_interval",
    ".vision.block_count",
})

# Cache keyed by resolved path string -> (mtime_ns, size, parsed dict)
_META_CACHE: dict[str, tuple[int, int, dict[str, Any]]] = {}


def clear_cache() -> None:
    """Clear the cached GGUF metadata map (primarily for tests)."""
    _META_CACHE.clear()


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
        if key.endswith(".attention.head_count_kv") and etype in _FIXED_FMTS and count <= 512:
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


def _sibling_has_mmproj(path: Path) -> bool:
    base_stem = _SPLIT_SHARD_RE.sub(r"\g<base>", path.stem)
    stem_tokens = [t for t in base_stem.lower().replace("_", "-").split("-") if t]
    stem_set = set(stem_tokens)
    try:
        siblings = list(path.parent.glob("*mmproj*.gguf"))
    except OSError:
        return False
    if not siblings:
        return False
    for sib in siblings:
        try:
            if sib.stat().st_size < 1024 * 1024:
                continue
        except OSError:
            continue
        sib_tokens = [
            t for t in sib.stem.lower().replace("_", "-").split("-")
            if t and t not in _MMPROJ_DTYPE_TOKENS
        ]
        # Named projector (e.g. mmproj-Qwen3.5-9B-F16.gguf): every model token must match this file's stem.
        if sib_tokens:
            if len(sib_tokens) >= 2 and all(tok in stem_set for tok in sib_tokens):
                return True
            if len(sib_tokens) == 1 and sib_tokens[0] in stem_set:
                return True
            continue
        # Generic projector (e.g. mmproj-F16.gguf): only match when this dir holds a single model.
        try:
            other_models = [
                p for p in path.parent.glob("*.gguf")
                if "mmproj" not in p.name.lower()
                and not p.name.lower().startswith("mtp-")
                and _SPLIT_SHARD_RE.sub(r'\g<base>', p.stem) != _SPLIT_SHARD_RE.sub(r'\g<base>', path.stem)
            ]
        except OSError:
            other_models = []
        if not other_models:
            return True
    return False


def _derive_kv_bytes(
    block_count: int | None,
    head_count: int | None,
    head_count_kv: float | int | None,
    embedding_length: int | None,
    key_length: int | None,
    value_length: int | None,
    full_attention_interval: int | None,
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
    # Hybrid linear/full attention architectures (e.g. Qwen3.5) store growing KV only on full layers.
    attn_layers = (
        max(1, block_count // full_attention_interval)
        if (full_attention_interval and full_attention_interval > 1)
        else block_count
    )
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
    head_count = _arch_get(".attention.head_count")
    emb_len = _arch_get(".embedding_length")
    k_len = _arch_get(".attention.key_length")
    v_len = _arch_get(".attention.value_length")
    full_attn_int = _arch_get(".full_attention_interval")
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
        or "<think>" in tpl_low
        or "[think]" in tpl_low
    )
    has_vision = bool(vision_blocks > 0 or _sibling_has_mmproj(path))
    kv_q4, kv_f16 = _derive_kv_bytes(
        block_count, head_count, head_kv, emb_len, k_len, v_len, full_attn_int
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
        "sliding_window": _arch_get(".attention.sliding_window"),
        "full_attention_interval": full_attn_int,
        "chat_template": chat_tpl_str,
        "has_chat_template": bool(chat_tpl_str),
        "has_thinking": has_thinking,
        "has_vision": has_vision,
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
        return dict(cached[2])
    try:
        parsed = _parse_gguf_file(p, total_size)
    except (OSError, struct.error, ValueError, EOFError, UnicodeDecodeError, OverflowError, TypeError) as exc:
        _logger.debug("[GGUF-META] failed to parse %s: %s", p, exc)
        return {}
    _META_CACHE[key] = (st.st_mtime_ns, total_size, parsed)
    return dict(parsed)
