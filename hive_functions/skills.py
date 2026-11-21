


from __future__ import annotations

import fnmatch
import logging
import os
import re
from pathlib import Path

from utils.file import _is_junction

logger = logging.getLogger("hivemind.skills")

SKILLS_DIR_NAME = ".hivemind/skills"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _default_skill() -> dict:
    return {
        "name": "", "description": "", "trigger_keywords": [],
        "trigger_paths": [], "priority": 5, "enabled": True,
        "source": "user", "version": "1.0.0", "body": "", "_path": "",
    }


def parse_skill(text: str, path: str = "") -> dict | None:
    """Parse a skill .md file (YAML frontmatter + body)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    raw_fm, body = m.group(1), m.group(2)
    try:
        import yaml as _yaml
        fm = _yaml.safe_load(raw_fm) or {}
    except Exception as _e:
        logger.warning("[SKILLS] Malformed YAML frontmatter in %s: %s", path, _e)
        return None
    if not isinstance(fm, dict) or not fm.get("name"):
        return None
    skill = _default_skill()
    skill["name"] = str(fm.get("name", "")).strip()
    skill["description"] = str(fm.get("description", "")).strip()
    skill["trigger_keywords"] = [str(k).strip().lower() for k in (fm.get("trigger_keywords") or []) if str(k).strip()]
    skill["trigger_paths"] = [str(p).strip() for p in (fm.get("trigger_paths") or []) if str(p).strip()]
    try:
        skill["priority"] = int(fm.get("priority", 5))
    except Exception:
        skill["priority"] = 5
    skill["enabled"] = bool(fm.get("enabled", True))
    skill["source"] = str(fm.get("source", "user")).strip() or "user"
    skill["version"] = str(fm.get("version", "1.0.0")).strip() or "1.0.0"
    skill["body"] = body.strip()
    skill["_path"] = path
    return skill


def _skill_dirs(workspace_root: str | Path) -> list[Path]:
    dirs: list[Path] = []
    try:
        dirs.append(Path(workspace_root) / SKILLS_DIR_NAME)
    except Exception:
        pass
    try:
        dirs.append(Path.home() / SKILLS_DIR_NAME)
    except Exception:
        pass
    return dirs


def load_skills(workspace_root: str | Path, include_disabled: bool = False) -> list[dict]:

    skills: dict[str, dict] = {}
    for _d in _skill_dirs(workspace_root):
        if not _d.exists():
            continue
        for _f in sorted(_d.glob("*.md")):
            try:
                _text = _f.read_text(encoding="utf-8")
            except Exception:
                continue
            _skill = parse_skill(_text, str(_f))
            if _skill is None:
                continue
            if _skill["name"] not in skills:
                skills[_skill["name"]] = _skill
    if include_disabled:
        return list(skills.values())
    return [s for s in skills.values() if s.get("enabled")]


def _glob_match(pattern: str, path: str) -> bool:
    norm = path.replace("\\", "/").lower()
    pat = pattern.replace("\\", "/").lower().strip()
    if fnmatch.fnmatch(norm, pat):
        return True
    return fnmatch.fnmatch(norm, "**/" + pat.lstrip("/"))


_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".next", ".nuxt"}


def workspace_file_paths(workspace_root: str | Path, max_files: int = 200) -> list[str]:
    try:
        _root = Path(workspace_root)
        _abs_root = _root.resolve()
    except (OSError, ValueError, TypeError):
        return []
    _skip_lower = {d.lower() for d in _SKIP_DIRS}
    _out: list[str] = []
    try:
        for dirpath, dirnames, filenames in os.walk(_root, followlinks=False):
            dirnames[:] = sorted(
                d for d in dirnames
                if d.lower() not in _skip_lower
                and not (Path(dirpath) / d).is_symlink()
                and not _is_junction(Path(dirpath) / d)
            )
            for fn in sorted(filenames):
                if len(_out) >= max_files:
                    return _out
                _p = Path(dirpath) / fn
                if _p.is_symlink() or _is_junction(_p):
                    continue
                try:
                    _rel = _p.resolve().relative_to(_abs_root).as_posix()
                except (OSError, ValueError):
                    continue
                _out.append(_rel)
    except OSError:
        pass
    return _out


def match_skills(skills: list[dict], user_input: str, accessed_paths: list[str] | None = None) -> list[dict]:
    """Deterministic matching: keywords against the prompt, paths against accessed files."""
    accessed = [str(p) for p in (accessed_paths or [])]
    user_low = (user_input or "").lower()
    matched: list[dict] = []
    for s in skills:
        _kw_hit = any(k in user_low for k in s.get("trigger_keywords", []))
        _path_hit = any(
            _glob_match(p, a)
            for p in s.get("trigger_paths", [])
            for a in accessed
        )
        if _kw_hit or _path_hit:
            matched.append(s)
    matched.sort(key=lambda s: s.get("priority", 5), reverse=True)
    return matched


def format_skill_coder(skill: dict) -> str:
    return f"### Skill: {skill['name']}\n{skill['body']}"


def format_skill_planner(skill: dict) -> str:
    _desc = skill.get("description", "").replace("\n", " ").strip()
    return f"- {skill['name']}: {_desc[:200]}"


def set_skill_enabled(skill_path: str, enabled: bool) -> bool:
    """Schreibt das `enabled:`-Feld im Frontmatter der Skill-.md um (UI-Toggle)."""
    try:
        p = Path(skill_path)
        if not p.exists():
            return False
        text = p.read_text(encoding="utf-8")
        new_text = re.sub(
            r"^enabled:\s*(true|false)",
            f"enabled: {'true' if enabled else 'false'}",
            text, count=1, flags=re.MULTILINE | re.IGNORECASE,
        )
        if new_text == text:
            return False
        p.write_text(new_text, encoding="utf-8")
        return True
    except Exception as e:
        logger.warning("[SKILLS] Toggle failed for %s: %s", skill_path, e)
        return False


# ── Phase 2: Destillation aus Erfolgsmustern ────────────────────────────────


def select_skill_candidates(insights: list[dict], max_candidates: int = 3) -> list[dict]:


    candidates: list[dict] = []
    for ins in insights:
        if not isinstance(ins, dict):
            continue
        if ins.get("distilled_skill"):
            continue
        src = str(ins.get("source", ""))
        if src == "critic_fail_fix_loop":
            continue
        try:
            score = float(ins.get("relevance_score", 0) or 0)
        except Exception:
            score = 0.0
        try:
            merge_count = int(ins.get("merge_count", 1) or 1)
        except Exception:
            merge_count = 1
        is_pattern = (src == "insight_extractor:pattern") and score >= 0.7
        is_recurring = (merge_count >= 2) and score >= 0.7
        if is_pattern or is_recurring:
            candidates.append(ins)
    candidates.sort(
        key=lambda i: (int(i.get("merge_count", 1) or 1), float(i.get("relevance_score", 0) or 0)),
        reverse=True,
    )
    return candidates[:max_candidates]


def _safe_skill_slug(name: str) -> str:
    raw = str(name or "").strip().lower().replace(" ", "-")
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw).strip(".-")
    if not slug or ".." in slug:
        return ""
    return slug[:80]


def skill_file_exists(workspace_root: str | Path, name: str) -> bool:
    try:
        _slug = _safe_skill_slug(name)
        if not _slug:
            return False
        for _d in _skill_dirs(workspace_root):
            _candidate = _d / f"{_slug}.md"
            try:
                _candidate.resolve().relative_to(_d.resolve())
            except (OSError, ValueError):
                continue
            if _candidate.exists():
                return True
        return False
    except Exception:
        return False


def write_skill_md(workspace_root: str | Path, skill: dict) -> bool:
    try:
        ws = Path(workspace_root)
        skills_dir = ws / SKILLS_DIR_NAME
        skills_dir.mkdir(parents=True, exist_ok=True)

        _name = _safe_skill_slug(skill.get("name", ""))
        if not _name:
            return False

        _desc = str(skill.get("description", "") or "").strip()
        _kws = [str(k) for k in (skill.get("trigger_keywords") or []) if str(k).strip()]
        _paths = [str(p) for p in (skill.get("trigger_paths") or []) if str(p).strip()]
        try:
            _priority = int(skill.get("priority", 5) or 5)
        except Exception:
            _priority = 5

        _fm = [
            f"name: {_name}",
            "description: |",
        ]
        for _dl in (_desc.split("\n") if _desc else [""]):
            _fm.append(f"  {_dl}".rstrip())
        _fm.append(f"trigger_keywords: [{', '.join(_kws)}]")
        _fm.append(f"trigger_paths: [{', '.join('\"' + p + '\"' for p in _paths)}]")
        _fm.append(f"priority: {_priority}")
        _fm.append(f"enabled: {'true' if skill.get('enabled') else 'false'}")
        _fm.append(f"source: {skill.get('source', 'distilled')}")
        _fm.append(f"version: {skill.get('version', '1.0.0')}")

        _instructions = [str(i) for i in (skill.get("instructions") or []) if str(i).strip()]
        _anti = [str(a) for a in (skill.get("anti_patterns") or []) if str(a).strip()]
        _example = str(skill.get("example", "") or "").strip()

        _body = ["## Instructions"]
        _body.extend(f"- {i}" for i in _instructions)
        if _anti:
            _body.append("")
            _body.append("## Anti-Patterns (Do NOT do this)")
            _body.extend(f"- {a}" for a in _anti)
        if _example:
            _body.append("")
            _body.append("## Examples")
            _body.append("```")
            _body.append(_example)
            _body.append("```")

        _fname = skills_dir / f"{_name}.md"
        try:
            _fname.resolve().relative_to(skills_dir.resolve())
        except (OSError, ValueError):
            logger.warning("[SKILLS] Rejected skill write outside skills dir: %s", _name)
            return False
        _fname.write_text(
            "---\n" + "\n".join(_fm) + "\n---\n\n" + "\n".join(_body) + "\n",
            encoding="utf-8",
        )
        return True
    except Exception as e:
        logger.warning("[SKILLS] Skill write failed: %s", e)
        return False
