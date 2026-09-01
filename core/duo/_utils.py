import re
from pathlib import Path

from core.duo_helpers import _RE_CRITIC_APPROVED, _RE_CRITIC_VERDICT, _RE_CRITIC_ISSUES


def _merge_down(parts: list, target_n: int) -> list:
    parts = [dict(p) for p in parts]
    while len(parts) > target_n:
        parts.sort(key=lambda p: len(p.get("paths", [])))
        tiny = parts[0]
        tiny_lbl = str(tiny.get("label", "")).lower()
        best_target = None
        best_common = -1
        for other in parts[1:]:
            o_lbl = str(other.get("label", "")).lower()
            common = sum(1 for a, b in zip(tiny_lbl.split("/"), o_lbl.split("/")) if a == b)
            if common > best_common:
                best_common = common
                best_target = other
        if best_target is None:
            best_target = max(parts[1:], key=lambda p: len(p.get("paths", [])))
        seen = set(best_target.get("paths", []))
        for fp in tiny.get("paths", []):
            if fp not in seen:
                best_target.setdefault("paths", []).append(fp)
                seen.add(fp)
        parts.pop(0)
    parts.sort(key=lambda p: -len(p.get("paths", [])))
    return parts


def _split_paths_by_parent(_paths: list[str]) -> tuple[list[str], list[str]]:
    _by_parent: dict[str, list[str]] = {}
    for _fp in _paths:
        _p = str(_fp or "").replace("\\", "/")
        _parent = _p.rsplit("/", 1)[0] if "/" in _p else "__root__"
        _by_parent.setdefault(_parent, []).append(_fp)
    if len(_by_parent) < 2:
        if len(_paths) >= 4:
            _mid = len(_paths) // 2
            return list(_paths[:_mid]), list(_paths[_mid:])
        return [], []
    _items = sorted(
        _by_parent.items(),
        key=lambda kv: (-len(kv[1]), kv[0]),
    )
    _a: list[str] = []
    _b: list[str] = []
    _a_n = 0
    _b_n = 0
    for _, _chunk in _items:
        if _a_n <= _b_n:
            _a.extend(_chunk)
            _a_n += len(_chunk)
        else:
            _b.extend(_chunk)
            _b_n += len(_chunk)
    return _a, _b


def _parse_critic_tune(text: str) -> dict:
    d: dict = {}
    am = _RE_CRITIC_APPROVED.search(text)
    if am: d["approved"] = am.group(1).lower() == "true"
    vm = _RE_CRITIC_VERDICT.search(text)
    if vm: d["verdict"] = vm.group(1).replace("_", " ")
    im = _RE_CRITIC_ISSUES.search(text)
    if im:
        raw = im.group(1).strip()
        d["issues"] = [p.strip().replace("_"," ") for p in raw.split(";") if p.strip()] if raw else []
    return d


def _is_retryable_ollama_err(e: Exception) -> bool:
    if type(e).__name__ in ("ReadError", "RemoteProtocolError", "ConnectError"):
        return True
    s = str(e).lower()
    return any(x in s for x in ("500", "internal server error", "input stream",
                                 "connection", "timeout", "503", "502", "read"))


def _build_soft_check(file_changes: dict, ws: str) -> str:
    lines = []
    for path in sorted(file_changes.keys())[:5]:
        try:
            content = Path(ws, path).read_text(encoding="utf-8", errors="replace")
            todos = [l.strip() for l in content.splitlines()
                     if "TODO" in l or "FIXME" in l][:3]
            if todos:
                lines.append(f"  {path}: {len(todos)} TODOs/FIXMEs offen")
        except Exception:
            pass
    if not lines:
        return ""
    lines.insert(0, "Before you finish this subtask — please check:")
    lines.append("\nAre these points intentional (future work) or must they be solved now?")
    lines.append("Decide yourself and proceed accordingly.")
    return "\n".join(lines)
