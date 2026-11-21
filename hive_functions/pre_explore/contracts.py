"""Pre-Explore: Contract-Extraktion & TOML/JSON-Parsing (Teil von hive_functions/pre_explore)."""

from __future__ import annotations

import json
import logging
import math
from hive_functions.tree_scout import parse_contract_summary
import re

_contract_fail_count = 0

logger = logging.getLogger("hivemind.pre_explore")


def _sanitize_toml(raw: str) -> str:
    lines = raw.splitlines()
    result = []
    for line in lines:
        stripped = line.strip()
        # Fix 1: "key: value" → "key = value" (model confuses YAML with TOML)
        if ":" in line and "=" not in line and \
           not stripped.startswith("#") and not stripped.startswith("["):
            line = line.replace(":", " =", 1)
        # Fix 2: Unclosed inline array — "exports = [\"a\", \"b\"" → "exports = [\"a\", \"b\"]"
        if re.search(r'=\s*\[', line):
            opens = line.count("[")
            closes = line.count("]")
            if opens > closes:
                line = line.rstrip() + "]" * (opens - closes)
        result.append(line)
    toml = "\n".join(result)
    # Fix B3: Unclosed string in array — '@types/fabric*,  "@astrojs/react"' → missing closing quote
    toml = re.sub(r'"([^"\n]{2,}),\s*"', r'"\1", "', toml)
    return toml


def _parse_toml_safe(raw: str) -> dict | None:
    """Try to parse TOML via tomllib/stdlib, fallback to parse_contract_summary."""
    raw = raw.strip()
    if not raw:
        return None
    # tomllib (Python 3.11+)
    try:
        import tomllib
        return tomllib.loads(raw)
    except Exception:
        pass
    # tomli (fallback)
    try:
        import tomli
        return tomli.loads(raw)
    except Exception:
        pass
    # parse_contract_summary (fenced TOML)
    _fenced = f"```toml\n{raw}\n```"
    _parsed = parse_contract_summary(_fenced)
    if _parsed:
        return _parsed[0]
    return None


def _normalize_hybrid_contract(raw: str) -> str:
    """Normalize TOML/JSON hybrid output from small models to valid JSON.

    Handles: = instead of :, stray quotes after ]/}/numbers/booleans,
    markdown code fences, trailing commas.
    """
    text = re.sub(r'^```\w*\s*\n?', '', raw)
    text = re.sub(r'\n?```\s*$', '', text)
    text = text.strip()
    if not text or text[0] != '{':
        return text
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass
    text_tc = re.sub(r',\s*([}\]])', r'\1', text)
    try:
        json.loads(text_tc)
        return text_tc
    except json.JSONDecodeError:
        pass
    text = re.sub(r'("\s*)\s*=\s*([\"\d\[\{tfn])', r'\1: \2', text)
    text = re.sub(r'(:\s*-?\d+(?:\.\d+)?)\s*"(?=\s*[,}\]\n])', r'\1', text)
    text = re.sub(r'(:\s*(?:true|false))\s*"(?=\s*[,}\]\n])', r'\1', text)
    text = re.sub(r',\s*([}\]])', r'\1', text)
    text = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', text)
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass
    text = re.sub(r'(\])\s*"', r'\1', text)
    text = re.sub(r'(\})\s*"', r'\1', text)
    return text


def _extract_contract(raw: str, partition_label: str) -> dict | None:
    raw = _normalize_hybrid_contract(raw.strip())

    # Fix B: <tool_call>-wrapper pre-strip (non-destructive)
    _tool_call_match = re.search(
        r'<parameter[^>]*=toml[^>]*>\s*(.*?)\s*</parameter>',
        raw,
        re.DOTALL
    )
    if _tool_call_match:
        raw = _tool_call_match.group(1).strip()

    if not _tool_call_match:
        _func_match = re.search(
            r'<function=write_contract[^>]*>\s*([\s\S]*?)(?:\s*</function>|\Z)',
            raw,
            re.DOTALL
        )
        if _func_match:
            _candidate = _func_match.group(1).strip()
            _repaired = _try_repair_json_mixed(_candidate)
            if isinstance(_repaired, dict) and _repaired.get("exports"):
                raw = json.dumps(_repaired)

    # Detect markdown-fenced JSON — model writes ```json{...}``` instead of raw JSON
    if raw.startswith("```json") or raw.startswith("```toml"):
        _fence_m = re.search(r'```(?:json|toml)\s*([\s\S]*?)```', raw)
        if _fence_m:
            _inner = _fence_m.group(1).strip()
            _inner = re.sub(r'^\s*//.*$', '', _inner, flags=re.MULTILINE)
            _inner = _inner.strip()
            try:
                data = json.loads(_inner)
                if isinstance(data, dict):
                    return _build_contract_dict(data, partition_label)
            except json.JSONDecodeError:
                _repaired = _try_repair_json_mixed(_inner)
                if isinstance(_repaired, dict):
                    return _build_contract_dict(_repaired, partition_label)

    # Strip {json}/{toml} format-annotation prefix before JSON object
    _pfx = re.compile(r'^\{(?:json|toml|contract|content)\}\s*')
    if _pfx.match(raw):
        raw = _pfx.sub('', raw)

    # Strip preamble before any known function-call marker
    _fn2 = raw.find("{write_contract(")
    _tc  = raw.find("<tool_call>")
    _fn  = raw.find("<function=write_contract")
    _fw  = raw.find('{"write_contract"')
    _cut = min(
        _tc  if _tc  >= 0 else len(raw),
        _fn  if _fn  >= 0 else len(raw),
        _fn2 if _fn2 >= 0 else len(raw),
        _fw  if _fw  >= 0 else len(raw),
    )
    if 0 < _cut < len(raw):
        raw = raw[_cut:]
    # Strip // inline comments outside quoted strings (preserves URLs inside strings)
    raw = re.sub(
        r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|(//[^\n]*)''',
        lambda m: m.group(0) if m.group(1) is None else '',
        raw,
    )
    # {write_contract(param="...")} — curly-brace format (no <function= prefix)
    _wc_pos = raw.find("write_contract(")
    if _wc_pos >= 0:
        _brace_start = raw.find("{", _wc_pos)
        if _brace_start >= 0:
            _depth, _json_end = 0, -1
            for _i, _c in enumerate(raw[_brace_start:], _brace_start):
                if _c == '{': _depth += 1
                elif _c == '}':
                    _depth -= 1
                    if _depth == 0:
                        _json_end = _i
                        break
            if _json_end > 0:
                _inner = raw[_brace_start:_json_end + 1]
                _inner = re.sub(
                    r'(?<!["\w])([a-zA-Z_]\w*)(\s*:)', r'"\1"\2', _inner
                )
                try:
                    data = json.loads(_inner)
                    return _build_contract_dict(data, partition_label)
                except json.JSONDecodeError:
                    _repaired = _try_repair_json_mixed(_inner)
                    if _repaired:
                        return _build_contract_dict(_repaired, partition_label)

    # Fix A: tool_call-wrapper — model wraps contract in {"tool_call": {"name":..., "arguments": {"contract": {...}}}}
    if '"tool_call"' in raw or "'tool_call'" in raw:
        _w = re.search(r'"(?:contract|toml)"\s*:\s*"(.*?)",\s*"plan"', raw, re.DOTALL)
        if not _w:
            _w = re.search(r'"(?:contract|toml)"\s*:\s*"(.*?)"\s*\}\s*\}', raw, re.DOTALL)
        if _w:
            _toml_raw = _w.group(1)
            _toml_raw = _toml_raw.replace("\\n", "\n").replace('\\"', '"')
            result = _parse_toml_safe(_sanitize_toml(_toml_raw))
            if result:
                return _build_contract_dict(result, partition_label)
        # Last resort: bracket-count JSON-wrapper parse
        _w2 = re.search(r'"arguments"\s*:\s*(\{.*?"plan"\s*:\s*\[)', raw, re.DOTALL)
        if not _w2:
            _w2 = re.search(r'"arguments"\s*:\s*(\{.*?\})\s*\}', raw, re.DOTALL)
        if _w2:
            try:
                _inner = json.loads(_w2.group(1))
                _inner_toml = _inner.get("contract") or _inner.get("toml", "")
                if _inner_toml:
                    _inner_toml = _inner_toml.replace("\\n", "\n").replace('\\"', '"')
                    result = _parse_toml_safe(_sanitize_toml(_inner_toml))
                    if result:
                        return _build_contract_dict(result, partition_label)
            except Exception:
                pass

    # Fix 4: <|tool_call_start|>[write_contract(contract={...})]<|tool_call_end|>  — LFM2.5 format
    if "<|tool_call_start|>" in raw:
        _m = re.search(r'<\|tool_call_start\|>(.*?)<\|tool_call_end\|>', raw, re.DOTALL)
        if _m:
            _body = _m.group(1).strip()
            if _body.startswith("[") and _body.endswith("]"):
                _body = _body[1:-1].strip()
            _tm = re.search(r'write_contract\(\s*(?:contract|toml)\s*=\s*["\'](.*?)["\']\s*\)', _body, re.DOTALL)
            if not _tm:
                _jm = re.search(r'write_contract\(\s*(?:contract|json)\s*=\s*(\{.*?)\s*\)', _body, re.DOTALL)
                if _jm:
                    _json_str = _jm.group(1).strip()
                    try:
                        data = json.loads(_json_str)
                        return _build_contract_dict(data, partition_label)
                    except Exception:
                        pass
            if _tm:
                _arg = _tm.group(1)
                _arg = _arg.replace("\\n", "\n").replace('\\"', '"')
                result = _parse_toml_safe(_sanitize_toml(_arg))
                if result:
                    return _build_contract_dict(result, partition_label)

    # Fix 5: <function=write_contract>\n<parameter=toml>\n{...} — XML-attribute format
    if "<function=write_contract" in raw:
        _PARAM_NAMES = r'(?:toml|json|content|data|value|contract)'
        _m = re.search(
            r'<parameter[=\s]*["\']?' + _PARAM_NAMES + r'["\']?>\s*([\s\S]*?)</parameter>',
            raw
        )
        if not _m:
            _m = re.search(
                r'<parameter[=\s]*["\']?' + _PARAM_NAMES + r'["\']?>\s*([\s\S]*?)(?=<[a-z/]|$)',
                raw, re.IGNORECASE
            )
        if _m:
            _arg = _m.group(1).strip()
            try:
                data = json.loads(_arg)
                return _build_contract_dict(data, partition_label)
            except Exception:
                result = _parse_toml_safe(_sanitize_toml(_arg))
                if result:
                    return _build_contract_dict(result, partition_label)

    raw = re.sub(r'\[(json|toml)\](\s*[\)>])', r'\2', raw)

    # Fix 3: <function=write_contract(json={...}) syntax — bracket-counting for JSON body
    _fn_start = raw.find("<function=write_contract")
    if _fn_start != -1:
        _json_start = raw.find("{", _fn_start)
        if _json_start != -1:
            _depth = 0
            _json_end = -1
            for _i, _ch in enumerate(raw[_json_start:], _json_start):
                if _ch == "{":
                    _depth += 1
                elif _ch == "}":
                    _depth -= 1
                    if _depth == 0:
                        _json_end = _i
                        break
            if _json_end != -1:
                try:
                    data = json.loads(raw[_json_start:_json_end + 1])
                    return _build_contract_dict(data, partition_label)
                except Exception:
                    pass

    # Path A: JSON object (preferred — 2B models produce this reliably)
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                if "write_contract" in data and isinstance(data["write_contract"], dict):
                    data = data["write_contract"]
                elif data.get("type") == "write_contract" and isinstance(data.get("json"), dict):
                    data = data["json"]
                return _build_contract_dict(data, partition_label)
        except Exception as _je:
            logger.debug("[CONTRACT] JSON strict parse failed: %s — trying repair", _je)
            # U0: Repair mixed JSON+TOML (semicolon-separated trailing garbage)
            data = _try_repair_json_mixed(raw)
            if isinstance(data, dict):
                return _build_contract_dict(data, partition_label)
            # U1: Fallback to lenient parser for common 2B model mistakes
            data = _try_parse_json_lenient(raw)
            if isinstance(data, dict):
                return _build_contract_dict(data, partition_label)
    # Path B: TOML (sanitized) → parse_contract_summary
    _sanitized = _sanitize_toml(raw)
    _fenced    = f"```toml\n{_sanitized}\n```"
    try:
        _parsed = parse_contract_summary(_fenced)
    except (ValueError, TypeError):
        _parsed = []
        try:
            import tomllib
            _raw_toml = tomllib.loads(_sanitized)
            if isinstance(_raw_toml, dict):
                _c = _raw_toml.get("contract") if isinstance(_raw_toml.get("contract"), dict) else _raw_toml
                if isinstance(_c, dict) and _c:
                    _parsed = [_build_contract_dict(_c, partition_label)]
        except (ImportError, ValueError, TypeError):
            _parsed = []
    if _parsed:
        _out = _parsed[0]
        if isinstance(_out, dict):
            _out["complexity_score"] = _sanitize_complexity_score(_out.get("complexity_score", 0.5))
        return _out
    global _contract_fail_count
    _contract_fail_count += 1
    _log_fn = logger.warning if _contract_fail_count <= 3 else logger.debug
    _log_fn(
        "[CONTRACT] Both JSON and TOML parse failed for partition=%s (fail #%d)%s",
        partition_label, _contract_fail_count,
        "" if _contract_fail_count <= 3 else " — suppressed to debug",
    )
    return None


_COMPLEXITY_LABELS: dict[str, float] = {
    "trivial": 0.1,
    "very_low": 0.1,
    "very low": 0.1,
    "config": 0.1,
    "low": 0.3,
    "simple": 0.3,
    "easy": 0.3,
    "utility": 0.3,
    "medium": 0.5,
    "moderate": 0.5,
    "mid": 0.5,
    "normal": 0.5,
    "standard": 0.5,
    "average": 0.5,
    "high": 0.7,
    "complex": 0.7,
    "hard": 0.7,
    "very_high": 0.9,
    "very high": 0.9,
    "critical": 0.9,
    "extreme": 0.9,
}


def _sanitize_complexity_score(val: object, default: float = 0.5) -> float:
    """Coerce complexity_score (numeric or qualitative label) into [0.0, 1.0]."""
    if val is None or isinstance(val, bool):
        return default
    if isinstance(val, (int, float)):
        f = float(val)
        return max(0.0, min(1.0, f)) if math.isfinite(f) else default
    if isinstance(val, str):
        s = val.strip().lower()
        if not s:
            return default
        if s in _COMPLEXITY_LABELS:
            return _COMPLEXITY_LABELS[s]
        try:
            f = float(s)
            return max(0.0, min(1.0, f)) if math.isfinite(f) else default
        except ValueError:
            return default
    return default


def _coerce_str(val: object) -> str:
    """Coerce a contract text field to string safely."""
    if val is None or isinstance(val, bool):
        return ""
    if isinstance(val, (list, tuple)):
        return ", ".join(str(x).strip() for x in val if x is not None and str(x).strip())
    return str(val)


def _coerce_str_list(val: object) -> list[str]:
    """Coerce a contract list field into a clean list of non-empty strings."""
    if val is None or isinstance(val, (bool, int, float, dict)):
        return []
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return []
        if "," in s:
            return [part.strip() for part in s.split(",") if part.strip()]
        return [s]
    if isinstance(val, (list, tuple, set)):
        items = sorted(val, key=str) if isinstance(val, set) else val
        return [str(x).strip() for x in items if x is not None and str(x).strip()]
    return []


def _validate_contract(data: dict, partition_label: str = "") -> dict:
    """Sanitize and normalize parsed contract fields without raising."""
    if not isinstance(data, dict):
        data = {}
    _part_raw = data.get("partition")
    _partition = str(_part_raw).strip() if _part_raw not in (None, "") else str(partition_label or "")
    _tbt_raw = data.get("touched_by_task", "yes")
    _tbt_str = str(_tbt_raw).strip().lower() if _tbt_raw is not None else "no"
    _df_raw = data.get("data_flow")
    if _df_raw in (None, ""):
        _df_raw = data.get("hint", "")
    out = {
        "partition":        _partition,
        "role":             _coerce_str(data.get("role", "")),
        "exports":          _coerce_str_list(data.get("exports")),
        "files_read":       _coerce_str_list(data.get("files_read")),
        "touched_by_task":  "yes" if _tbt_str in ("yes", "true", "1", "likely") else "no",
        "complexity_score": _sanitize_complexity_score(data.get("complexity_score", 0.5)),
        "data_flow":        _coerce_str(_df_raw),
        "imports_internal": _coerce_str_list(data.get("imports_internal")),
        "imports_external": _coerce_str_list(data.get("imports_external")),
        "config":           _coerce_str(data.get("config", "")),
        "hint":             _coerce_str(data.get("hint", "")),
        "entry_points":     _coerce_str_list(data.get("entry_points")),
        "imports_needed":   _coerce_str_list(data.get("imports_needed")),
        "skipped_files":    _coerce_str_list(data.get("skipped_files")),
    }
    return out


def _build_contract_dict(data: dict, partition_label: str) -> dict:
    """Build standardized contract dict from parsed data."""
    return _validate_contract(data, partition_label)


def _try_repair_json_mixed(raw: str) -> dict | None:
    # Fix B3: "key" = "value" → "key": "value"
    raw = re.sub(r'"([^"]+)"\s*=\s*"', r'"\1": "', raw)
    # Fix B4: trailing " after non-string values (0.7", → 0.7, / true" → true)
    raw = re.sub(r'(\b(?:true|false|null|\d[\d.]*))"\s*([,}])', r'\1\2', raw)
    depth = 0
    in_str = False
    esc_next = False
    json_end = -1
    for i, ch in enumerate(raw):
        if esc_next:
            esc_next = False
            continue
        if ch == "\\" and in_str:
            esc_next = True
            continue
        if ch == '"':
            in_str = not in_str
        if not in_str:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    json_end = i
                    break
    if json_end > 0:
        try:
            return json.loads(raw[: json_end + 1])
        except json.JSONDecodeError:
            pass
    return None


def _try_parse_json_lenient(raw: str) -> dict | None:
    raw = raw.strip()
    start = raw.find('{')
    if start == -1:
        return None
    # Fix regex: },KEY": → },"KEY":  (model forgets " after nested close)
    raw = re.sub(r'\},\s*([a-zA-Z_][a-zA-Z_0-9]*)":', r'},"\1":', raw)
    raw = re.sub(r',\s*([a-zA-Z_][a-zA-Z_0-9]*)":', r',"\1":', raw)
    raw = re.sub(r'"(\w+)":\s*\[([^\]]*)\}', r'"\1": [\2]', raw)
    # Bracket-count to find first valid JSON object (stops at depth 0)
    depth, first_end = 0, -1
    in_string, escape = False, False
    for i, ch in enumerate(raw[start:], start):
        if escape:
            escape = False; continue
        if ch == '\\' and in_string:
            escape = True; continue
        if ch == '"':
            in_string = not in_string; continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                first_end = i; break
    if first_end == -1:
        return None
    candidate = raw[start:first_end + 1]
    # U1a: Python-literals → JSON-literals (2B model writes True/False/None)
    candidate = re.sub(r'\bTrue\b',  'true',  candidate)
    candidate = re.sub(r'\bFalse\b', 'false', candidate)
    candidate = re.sub(r'\bNone\b',  'null',  candidate)
    try:
        result = json.loads(candidate)
    except Exception as _je:
        logger.debug("[CONTRACT] JSON lenient parse failed: %s | candidate[:80]=%s",
                     _je, candidate[:80])
        return None
    # Merge trailing key-value pairs: {"a":1},"b":2,"c":3} → merge b,c into result
    trailing = raw[first_end + 1:].strip()
    # Match "KEY":value patterns (value is any JSON value terminated by , or })
    _kv_re = re.compile(r',?"\s*([a-zA-Z_][a-zA-Z_0-9]*)"\s*:\s*((?:[^,{}"]|"(?:[^"\\]|\\.)*"|\[[^\]]*\])+)')
    for _m in _kv_re.finditer(trailing):
        _k = _m.group(1)
        _v = _m.group(2).strip()
        if _k not in result:
            try:
                result[_k] = json.loads(_v)
            except Exception:
                result[_k] = _v
    return result
