# -*- coding: utf-8 -*-
"""Wire the agent roles to the models that are actually installed.

Run by setup_models.bat after a download, and by install.bat at the end.
Fresh installs ship with empty model names on purpose (a hardcoded name that
is not on disk fails at the first run); this script fills them from
models.json, so a default install is configured the moment the models land.

Rules
-----
- Only EMPTY or UNINSTALLED slots are touched. A model the user picked is
  never replaced, and an existing settings.json keeps everything else.
- The engine must not run while this writes settings.json, or its next save
  overwrites the file. setup_models.bat runs before the first start.
- Prints a small table so the user sees the resulting configuration.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Preference per role, best first. The first entry that is installed wins.
# Kept in one place so the installer and the docs cannot drift.
ROLE_PREFERENCE: dict[str, list[str]] = {
    # chat / direct answers: all-rounder with vision
    "direct": ["gemma-4:e4b-it-qat", "minicpm5:2b-sharp", "qwen3.5:4b-mtp"],
    # pipeline roles: the all-rounder, the cheap one as fallback
    "analyst": ["gemma-4:e4b-it-qat", "qwen3.5:4b-mtp", "minicpm5:2b-sharp"],
    "critic": ["gemma-4:e4b-it-qat", "qwen3.5:4b-mtp", "minicpm5:2b-sharp"],
    "synthesizer": ["gemma-4:e4b-it-qat", "qwen3.5:4b-mtp", "minicpm5:2b-sharp"],
    # short rewrite of constraints: fast + cheap
    "refiner": ["minicpm5:2b-sharp", "qwen3.5:4b-mtp", "gemma-4:e4b-it-qat"],
    # 120-token verdict: the smallest is enough
    "judge": ["minicpm5:2b-sharp", "qwen3.5:4b-mtp", "gemma-4:e4b-it-qat"],
    # coding: the Duo coder; the critic shares the same model (one slot)
    "duo_coder": ["qwen3.5:4b-mtp", "gemma-4:e4b-it-qat", "minicpm5:2b-sharp"],
    "duo_critic": ["qwen3.5:4b-mtp", "gemma-4:e4b-it-qat", "minicpm5:2b-sharp"],
    # the image preprocessor needs vision
    "vision_agent": ["gemma-4:e4b-it-qat", "qwen3.5:4b-mtp"],
}

# Subagent ladder: smallest first - the ladder walks up only when a tier
# fails, so the cheap model belongs at the front.
SUBAGENT_LADDER = ["lfm3:1.7b-qad", "minicpm5:2b-sharp", "qwen3.5:4b-mtp"]


def _installed_models(root: Path) -> set[str]:
    """Model tags from models.json (keys are tags plus _mmproj/_drafter keys)."""
    path = root / "models.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    tags: set[str] = set()
    for key in data:
        if not isinstance(key, str):
            continue
        if key.endswith("_mmproj") or key.endswith("_drafter"):
            continue
        tags.add(key)
    return tags


def apply(root: Path, *, dry_run: bool = False) -> list[tuple[str, str, str]]:
    """Fill empty/uninstalled slots. Returns [(role, old, new)] for the report."""
    installed = _installed_models(root)
    if not installed:
        return []
    settings_path = root / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    if not isinstance(settings, dict):
        settings = {}

    agents = settings.get("agents")
    if not isinstance(agents, dict):
        agents = {}

    changes: list[tuple[str, str, str]] = []

    def pick(role: str) -> str:
        for cand in ROLE_PREFERENCE.get(role, []):
            if cand in installed:
                return cand
        return ""

    for role in ROLE_PREFERENCE:
        cfg = agents.get(role)
        cfg = cfg if isinstance(cfg, dict) else {}
        current = str(cfg.get("model") or "").strip()
        if current and current in installed:
            continue                       # a working choice stays
        new = pick(role)
        if not new or new == current:
            continue
        cfg = dict(cfg)
        cfg["model"] = new
        agents[role] = cfg
        changes.append((role, current, new))

    if changes:
        settings["agents"] = agents

    # planner follows the coder unless it has its own model
    planner = str(settings.get("duo_planner_model") or "").strip()
    if (not planner or planner not in installed):
        coder = str((agents.get("duo_coder") or {}).get("model") or "").strip()
        if coder and planner != coder:
            settings["duo_planner_model"] = coder
            changes.append(("duo_planner_model", planner, coder))

    # vision agent model (only if that role is enabled later)
    vision = str(settings.get("vision_agent_model") or "").strip()
    if not vision or vision not in installed:
        new = pick("vision_agent")
        if new and new != vision:
            settings["vision_agent_model"] = new
            changes.append(("vision_agent_model", vision, new))

    # subagent ladder: only installed entries, order preserved
    ladder = settings.get("subagent_lite_model_ladder")
    ladder = ladder if isinstance(ladder, list) else []
    keep = [m for m in ladder if isinstance(m, str) and m in installed]
    if not keep:
        keep = [m for m in SUBAGENT_LADDER if m in installed]
        if keep and keep != ladder:
            settings["subagent_lite_model_ladder"] = keep
            changes.append(("subagent_lite_model_ladder",
                            ",".join(map(str, ladder)) or "-", ",".join(keep)))

    if changes and not dry_run:
        settings_path.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
    return changes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.root)

    installed = _installed_models(root)
    print(f"Models found     : {len(installed)}")
    if not installed:
        print("Nothing to do - models.json is missing or empty. Run the model")
        print("download first (setup_models.bat), then this script again.")
        return 0

    changes = apply(root, dry_run=args.dry_run)
    if not changes:
        print("Configuration   : already wired to installed models.")
        return 0
    print("Configuration   : " + ("would be updated" if args.dry_run else "updated"))
    print()
    print(f"  {'role':<28} {'was':<26} now")
    print("  " + "-" * 74)
    for role, old, new in changes:
        print(f"  {role:<28} {(old or '-'):<26} {new}")
    print()
    print("Change any of it in the AGENTS tab; these are only the empty slots.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
