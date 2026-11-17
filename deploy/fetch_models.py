"""
fetch_models.py
===============
Downloads the recommended HiveMind models from Hugging Face and
populates models.json with all GGUFs found in the models folder.

Recommended set (as of release, = default-settings alignment 2026-08-26):
  gemma-4:e4b-it          Q4_K_M        — Direct/Vision/Allrounder
  qwen3.6:35b-a3b-mtp     UD-Q4_K_XL    — Coder/Planner (MoE+MTP, needs ~16GB RAM)
  (catalog is authoritative - this block lists only the highlights)
  lfm3-omni:2b            Q4_0          — Subagent/Vision-Worker (+mmproj)
  lfm3:1.7b-qad           QAD Q4_0      — text-only subagent
  lfm2.5:24b-a2b          Q4_K_M        — Coder/Writer (MoE, + DSpark drafter)
  spark-x2.6:4b           Q4_K_M        — fast direct helper (dense 4B)
  qwen3.5:0.8b-ud         UD-Q4_K_XL    — Subagent ladder (smallest tier)
  qwen3.5:2b              Q4_K_M        — Refiner
  qwen3.5:4b-mtp          Q4_K_M        — Analyst/Critic/Synthesizer/Speed/Fallback
  qwen3.5:9b-ud           UD-Q4_K_XL    — Direct/Duo-Coder/Quality
  gemma-4:…-hauhaucs-aggressive  Q4_K_P  — Uncensored all-rounder (2026-09-21)
  minicpm5:2b-sharp       Q6_K_XL (alt. Q4_K_XL) — Sharp-MiniCPM5-2B light
                          all-rounder, tool calls via --jinja (2026-09-26)

Usage:
  python deploy\\fetch_models.py --models-dir <path> [options]

  --models-dir     Target folder (default: env HIVEMIND_MODELS_DIR or <repo>/models)
  --only-missing   Do not re-download existing GGUFs
  --list-only      Only show what would be downloaded, download nothing
  --scan-only      Only scan local GGUFs + write models.json, NO download, NO network
  --yes            Skip confirmation for large-file warnings

Stdlib only — no huggingface_hub dependency.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import http.client
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    _reconf = getattr(_s, "reconfigure", None)
    if callable(_reconf):
        try:
            _reconf(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HF_API = "https://huggingface.co/api/models"
HF_RESOLVE = "https://huggingface.co/{repo}/resolve/main/{path}"
HF_API_TREE = "https://huggingface.co/api/models/{repo}/tree/main?recursive=1"
_LFS_OID_CACHE: dict[str, str] = {}
_LFS_TREE_CACHE: dict[str, dict[str, str]] = {}


def _hf_token() -> str:
    """HF token: env first, then the file `hf auth login` writes.

    No token is ever generated here. Public repos work anonymously; gated
    repos need a user-created token (env or the HF CLI token file).
    """
    _tok = (os.environ.get("HF_TOKEN")
            or os.environ.get("HUGGING_FACE_HUB_TOKEN") or "").strip()
    if _tok:
        return _tok
    _paths = []
    if os.environ.get("HF_TOKEN_PATH"):
        _paths.append(Path(os.environ["HF_TOKEN_PATH"]))
    if os.environ.get("HF_HOME"):
        _paths.append(Path(os.environ["HF_HOME"]) / "token")
    _paths.append(Path.home() / ".cache" / "huggingface" / "token")
    for _p in _paths:
        try:
            _v = _p.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if _v:
            return _v
    return ""


def _hf_auth_headers() -> dict:
    """HF token from the environment or the HF CLI, for gated repos."""
    _tok = _hf_token()
    return {"Authorization": f"Bearer {_tok}"} if _tok else {}


def _hf_lfs_sha256(repo: str, path: str) -> str:
    """LFS sha256 (``lfs.oid``) for one file, or "" when unavailable.

    B7 (audit 2026-11-03): the redirect's X-Linked-Etag is a 40-hex git blob
    id, NOT the content hash - verified against a real HF download. The file
    API exposes the true LFS oid (sha256). The tree is fetched ONCE per repo
    (P2) and the token, when set, authenticates gated repos; network failure
    degrades to the size check and says so in the log.
    """
    key = f"{repo}::{path}"
    if key in _LFS_OID_CACHE:
        return _LFS_OID_CACHE[key]
    tree = _LFS_TREE_CACHE.get(repo)
    if tree is None:
        tree = {}
        try:
            req = urllib.request.Request(
                HF_API_TREE.format(repo=repo),
                headers={"User-Agent": "HiveMind-Installer",
                         **_hf_auth_headers()})
            with urllib.request.urlopen(req, timeout=20) as r:
                entries = json.loads(r.read().decode("utf-8", "replace"))
            for entry in entries or []:
                _p = str((entry or {}).get("path") or "")
                if _p:
                    tree[_p] = str(((entry or {}).get("lfs") or {}).get("oid") or "")
        except (OSError, ValueError, urllib.error.URLError):
            tree = {}
        _LFS_TREE_CACHE[repo] = tree
    oid = tree.get(path, "")
    _LFS_OID_CACHE[key] = oid
    return oid


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a unique temp file + os.replace.

    models.json IS the model registry: a crash (or a second fetch run)
    mid-write used to leave a truncated file behind and the backend silently
    resolved nothing until the next scan.
    """
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)



def _persist_models_dir(mdir: Path) -> None:
    """Write the custom folder to settings.json "models_dir" so the server
    finds it even WITHOUT the HIVEMIND_MODELS_DIR env var (2026-09-01)."""
    try:
        from settings import load_settings, save_settings
        s = load_settings()
        s["models_dir"] = str(mdir)
        save_settings(s)
        print(f"    models_dir persisted -> {mdir}")
    except Exception as _e:
        print(f"    [WARNING] could not persist models_dir: {_e}")

# ── Download catalog (2026-09-11, user-curated) ──────────────────────────────
# Exactly the models the installer offers. file_regex: list of patterns, first
# priority first. "repo": PINNED repo (no HF fuzzy search — a search once
# pulled Jackrong/DeepSeek-V4-Pro... for the qwen3.5:4b-mtp spec because
# unsloth names its MTP files WITHOUT "mtp": unsloth/Qwen3.5-4B-MTP-GGUF →
# "Qwen3.5-4B-Q4_K_M.gguf". Pinned specs never fall back to search.)
# ESSENTIAL-SET: a WHITELIST of the default route, not a skip-list. The old
# skip-list shipped 15 models - including a 20.8 GB one - which is not
# "essential" on a small machine. The set is exactly the default route:
# one Duo coder/planner (qwen3.5:4b-mtp), an all-rounder with vision
# (gemma-4:e4b-it-qat), the light refiner/helper (minicpm5:2b-sharp, Q6_K_XL)
# and the text subagent (lfm3:1.7b-qad). Everything else is one menu entry
# away (S = select models), and setup_models.bat asks afterwards whether to
# add more.
# The judge role is deliberately not tied to a downloaded model: any
# installed LLM can be assigned to it.
ESSENTIAL_KEYS = {
    "gemma-4:e4b-it-qat",
    "qwen3.5:4b-mtp",
    "minicpm5:2b-sharp",
    "lfm3:1.7b-qad",
}
# kept for callers that only want to know the one optional extra
# DRAFTER-SKIP (2026-10-27, corrected): speculative decoding is ON again for
# the profiles that ship an MTP head, so a drafter sidecar is NOT dead weight.
# Gemma-4 uses its `mtp-*.gguf` and the DSpark models use their drafter; a load
# without the file falls back to plain decoding and loses the measured +54 %
# decode. So the skip is explicit only (`--no-drafters`), never implied by
# `--essential`, and it says out loud what it costs.
SKIP_DRAFTERS = False

SPECS: list[dict] = [
    {
        "key": "gemma-4:e4b-it-qat",
        "tag": "gemma-4:e4b-it-qat",
        "desc": "Gemma-4 E4B-IT QAT (All-rounder/Vision, ~4.2GB + mmproj, +MTP drafter)",
        "repo": "unsloth/gemma-4-E4B-it-qat-GGUF",
        "file_regex": [
            r"(?i)^gemma-4-e4b-it-qat-ud-q4_k_xl\.gguf$",
        ],
        "mmproj_regex": [
            r"(?i)^mmproj[-_.]bf16\.gguf$",
            r"(?i)^mmproj[-_.]f16\.gguf$",
        ],
        # unique dest: the e2b repo ships the same generic filename with a
        # DIFFERENT (smaller) projector inside - 1:1 downloads collided.
        "mmproj_dest": "mmproj-gemma-4-e4b-it-qat-f16.gguf",
        # MTP speculative-decoding drafter (~60MB, root of the repo)
        "sidecar": [
            {
                "repo": "unsloth/gemma-4-E4B-it-qat-GGUF",
                "path": "mtp-gemma-4-E4B-it.gguf",
                "note": "MTP spec-dec drafter (~60MB)",
            },
        ],
    },
    {
        "key": "gemma-4:e2b-it-qat",
        "tag": "gemma-4:e2b-it-qat",
        "desc": "Gemma-4 E2B-IT QAT (Small all-rounder/Vision, ~2.6GB + mmproj, +MTP drafter)",
        "repo": "unsloth/gemma-4-E2B-it-qat-GGUF",
        "file_regex": [
            r"(?i)^gemma-4-e2b-it-qat-ud-q4_k_xl\.gguf$",
        ],
        "mmproj_regex": [
            r"(?i)^mmproj[-_.]bf16\.gguf$",
            r"(?i)^mmproj[-_.]f16\.gguf$",
        ],
                # dest matches gemma-4_e2b-it-qat.json mmproj_filename (config wins
        # in resolve_mmproj_path) - the generic repo name collided with e4b.
        "mmproj_dest": "mmproj-Gemma-4-E2B-BF16.gguf",
        "sidecar": [
            {
                "repo": "unsloth/gemma-4-E2B-it-qat-GGUF",
                "path": "mtp-gemma-4-E2B-it.gguf",
                "note": "MTP spec-dec drafter (~60MB)",
            },
        ],
    },
    {
        "key": "qwen3.6:35b-a3b-mtp",
        "tag": "qwen3.6:35b-a3b-mtp",
        "desc": "Qwen3.6 35B A3B MTP unsloth UD (Coder/Planner MoE + MTP spec-decode, ~22.8GB download)",
        "repo": "unsloth/Qwen3.6-35B-A3B-MTP-GGUF",
        "file_regex": [
            r"(?i)^qwen3\.6-35b-a3b-ud-q4_k_xl\.gguf$",
        ],
        # Name locally so the MTP quant never collides if a non-MTP UD file is also present.
        "file_dest": "Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL.gguf",
        "mmproj_regex": [r"(?i)^mmproj-f16\.gguf$"],
        "mmproj_dest": "mmproj-qwen3.6-35b-a3b-mtp-f16.gguf",
    },
    {
        # LFM3 (2026-10-30, second attempt): b12001 carries arch `lfm3`
        # (PR #31252); b11889 answered "unknown model architecture: 'lfm3'".
        # Preferred entry is OMNI 2B (text + vision through its own mmproj),
        # the lean text-only 1.7B QAD follows. The 2.6B line is gone.
        "key": "lfm3-omni:2b",
        "tag": "lfm3-omni:2b",
        "desc": "LFM3-Omni 2B Q4_0 (Subagent/Vision-Worker, ~1GB + mmproj 270MB)",
        "repo": "LiquidAI/LFM3-Omni-2B-GGUF",
        "file_regex": [
            r"(?i)^lfm3-omni-2b-q4_0\.gguf$",
            r"(?i)^lfm3-omni-2b-q4_k_m\.gguf$",
        ],
        "mmproj_regex": [
            r"(?i)^mmproj-lfm3-omni-2b-q4_0\.gguf$",
            r"(?i)^mmproj-lfm3-omni-2b-q4_k_m\.gguf$",
        ],
        "mmproj_dest": "mmproj-LFM3-Omni-2B-Q4_0.gguf",
    },
    {
        "key": "lfm3:1.7b-qad",
        "tag": "lfm3:1.7b-qad",
        "desc": "LFM3 1.7B QAD Q4_0 (tool-calling subagent, ~1GB, needs llama.cpp b12001+)",
        "repo": "LiquidAI/LFM3-1.7B-GGUF",
        "file_regex": [
            r"(?i)^lfm3-1\.7b-qad-q4_0\.gguf$",
            r"(?i)^lfm3-1\.7b-q4_k_m\.gguf$",
        ],
        "mmproj_regex": [],
    },
    {
        "key": "lfm3:1.7b-uncensored-ad",
        "tag": "lfm3:1.7b-uncensored-ad",
        "desc": "LFM3 1.7B Uncensored Q4_K_M / Q6_K (tool-calling subagent, ~1.1GB, needs llama.cpp b12001+)",
        "repo": "AtomicChat/LFM3-1.7B-Uncensored-GGUF",
        "file_regex": [
            r"(?i)^lfm3-1\.7b-uncensored-ad-q4_k_m\.gguf$",
            r"(?i)^lfm3-1\.7b-uncensored-ad-q6_k\.gguf$",
        ],
        "mmproj_regex": [],
        # Seeded on disk only when downloaded, so no loose config ships by default.
        "default_profile": {
            "model": "lfm3:1.7b-uncensored-ad",
            "capabilities": {
                "thinking": True,
                "vision": False,
                "tool_call": True,
            },
            "vision_preprocessing": False,
            "num_ctx": 32768,
            "num_ctx_analyst": 16384,
            "num_ctx_duo_coder": 32768,
            "num_ctx_vision": None,
            "chat_template": None,
            "jinja": True,
            "reasoning": "on",
            "distilled": False,
            "moe_cpu_experts": 0,
            "mtp": False,
            "gpu_layers": 99,
            "mmproj_filename": None,
            "vram_gb_override": 1.6,
            "sampling": {
                "thinking": {
                    "temperature": 0.7,
                    "top_p": 0.9,
                    "top_k": 0,
                    "min_p": 0.0,
                    "presence_penalty": 0.0,
                    "repetition_penalty": 1.0,
                    "cache_prompt": True,
                },
                "non_thinking": {
                    "temperature": 0.7,
                    "top_p": 0.9,
                    "top_k": 0,
                    "min_p": 0.0,
                    "presence_penalty": 0.0,
                    "repetition_penalty": 1.0,
                    "cache_prompt": True,
                },
            },
        },
    },
    # ── Spark-X2.6 4B (XHToken, 2026-10) ──────────────────────────────────
    # Dense 4B, Apache-2.0, embedded chat template (jinja), thinking via the
    # template's enable_thinking switch. Replaces the Spark-X2.5 profiles
    # (4B/1.7B): same family, newer weights, one pinned repo. Sampling lives
    # in model_configs/models/spark-x2.6_4b.json (temp 1.0 / top_p 0.95 /
    # top_k off, no penalties) - matching the model card.
    {
        "key": "spark-x2.6:4b",
        "tag": "spark-x2.6:4b",
        "desc": "Spark-X2.6 4B Q4_K_M (dense, fast direct helper, ~3GB)",
        "repo": "XHToken/Spark-X2.6-4B-GGUF",
        "file_regex": [
            r"(?i)^spark-x2\.6-4b-q4_k_m\.gguf$",
        ],
        "mmproj_regex": [],
    },
    # ── LFM2.5 24B-A2B (LiquidAI, 2026-10) ───────────────────────────────
    # MoE 24B total / ~2B active. Q4_K_M is 13.5 GB, so on an 8 GB card the
    # experts live in RAM (moe_cpu_experts in the profile). Ships with the
    # DSpark drafter: a 0.62 GB sidecar that llama.cpp uses as --model-draft
    # with --spec-type draft-dspark (DSPARK_MIN_BUILD in llama_config.py).
    {
        "key": "lfm2.5:24b-a2b",
        "tag": "lfm2.5:24b-a2b",
        "desc": "LFM2.5 24B-A2B Q4_K_M (MoE 24B/2B active, strong writer, ~13.5GB)",
        "repo": "LiquidAI/LFM2.5-24B-A2B-GGUF",
        "file_regex": [
            r"(?i)^lfm2\.5-24b-a2b-q4_k_m\.gguf$",
        ],
        "mmproj_regex": [],
        # DSpark speculative-decoding drafter (F16, 0.62 GB) - never
        # registered as a model (dspark exclusion in write_models_json /
        # llama_models). The profile points at this exact filename.
        "sidecar": [
            {
                "repo": "LiquidAI/LFM2.5-24B-A2B-DSpark-GGUF",
                "path": "LFM2.5-24B-A2B-DSpark-F16.gguf",
                "note": "DSpark spec-dec drafter (F16, ~620MB)",
            },
        ],
        # Seeded automatically when downloaded so clean installs configure DSpark + CPU experts.
        "default_profile": {
            "model": "lfm2.5:24b-a2b",
            "capabilities": {"thinking": False, "vision": False, "tool_call": True},
            "vision_preprocessing": False,
            "num_ctx": 32768,
            "num_ctx_analyst": None,
            "num_ctx_duo_coder": 32768,
            "num_ctx_vision": None,
            "chat_template": None,
            "jinja": True,
            "reasoning": None,
            "distilled": False,
            "moe": True,
            "moe_cpu_experts": 24,
            "mtp": False,
            "gpu_layers": None,
            "dspark_draft_filename": "LFM2.5-24B-A2B-DSpark-F16.gguf",
            "mmproj_filename": None,
            "vram_gb_override": None,
            "dspark_draft_n_max": 2,
            "sampling": {
                "sampling_text": {
                    "temperature": 0.1,
                    "top_p": 1.0,
                    "top_k": 50,
                    "min_p": 0.0,
                    "presence_penalty": 0.0,
                    "repetition_penalty": 1.05,
                    "cache_prompt": True,
                },
                "non_thinking": {
                    "temperature": 0.1,
                    "top_p": 1.0,
                    "top_k": 50,
                    "min_p": 0.0,
                    "presence_penalty": 0.0,
                    "repetition_penalty": 1.05,
                    "cache_prompt": True,
                },
            },
        },
    },
    # ── Hermes3.6 Genesis Final "MTP-APEX-Compact" (2026-09-19) ───────────
    # Coder/Hermes agent: MoE 35B-A3B with MTP head, quantized as
    # "MTP-APEX-Compact" (~18 GB, experts offloaded to CPU). The per-model
    # config (model_configs/models/hermes3.6_..._final-mtp-apex-compact.json)
    # enables the MTP head + CPU experts and carries the author's agentic
    # coding sampling in the "thinking" profile key (temp 0.6, top_p 0.95,
    # top_k 20, seed 42, repeat 1.05). Link updated from the V13 repo to
    # the author's Final re-release.
    # ── Gemma-4 E4B "Uncensored HauhauCS Aggressive" (2026-09-21) ─────────
    # Uncensored fine-tune of Gemma-4-E4B-IT (ships with paired F16 mmproj).
    # Card sampling temp 1.0 / top_p 0.95 / top_k 64 lives in the gemma-4
    # family sampling profile; --jinja is enabled by the per-model config.
    {
        "key": "gemma-4:e4b-uncensored-hauhaucs-aggressive",
        "tag": "gemma-4:e4b-uncensored-hauhaucs-aggressive",
        "desc": "Gemma-4 E4B Uncensored HauhauCS Aggressive (uncensored all-rounder, ~5GB download)",
        "repo": "HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive",
        "file_regex": [
            r"(?i)^gemma-4-e4b-uncensored-hauhaucs-aggressive-q4_k_p\.gguf$",
        ],
        "mmproj_regex": [
            r"(?i)^mmproj-gemma-4-e4b-uncensored-hauhaucs-aggressive-f16\.gguf$",
        ],
    },
    # ── Sharp-MiniCPM5-2B (2026-09-26) ─────────────────────────────────────
    # Light dense all-rounder (plain llama arch, text-only, embedded jinja
    # template with OpenAI tool_calls). Q6_K_XL is near-lossless (99% KL 0.024
    # vs BF16).
    {
        "key": "minicpm5:2b-sharp",
        "tag": "minicpm5:2b-sharp",
        "desc": "Sharp-MiniCPM5-2B Q6_K_XL (light all-rounder, RECOMMENDED tier, near-lossless quant, ~2.1GB)",
        "repo": "peculiar-ragdoll/Sharp-MiniCPM5-2B-GGUF",
        "file_regex": [
            r"(?i)^sharp-minicpm5-2b-q6_k_xl\.gguf$",
        ],
        "mmproj_regex": [],
        # The Sharp chat template is a MUST for this model (its chat_template
        # config field points at the shipped file) — fetched into
        # model_configs/ alongside the GGUF.
        "extra_files": [
            {"path": "chat_template.jinja", "dest": "model_configs/sharp_minicpm5_chat_template.jinja"},
        ],
        # DSpark speculative-decoding drafter (openbmb, 324M / ~650MB, arch
        # "dflash"). Sidecar GGUF: never registered as a model (write_models_json
        # and llama_models exclude every *dspark* name). The model config
        # (minicpm5_2b-sharp.json) points at this exact filename.
        "sidecar": [
            {
                "repo": "openbmb/MiniCPM5-2B-DSpark-GGUF",
                "path": "MiniCPM5-2.6B-DSpark.gguf",
                "note": "DSpark spec-dec drafter (~650MB)",
            },
        ],
    },
    {
        # GROOT 19B-A3B (2026-10-30, user request): qwen35moe like Hermes,
        # 128 experts (8 used), ctx 262144, nextn head present. Profile:
        # model_configs/models/groot_19b-a3b-261023.json (ctx 131072,
        # ncmoe 32, KV q4_0, mmap+mlock, MTP on, native GGUF template).
        # ub is not a profile key: duo_ubatch/llama_ubatch_size (shipped 256).
        "key": "groot:19b-a3b-261023",
        "tag": "groot:19b-a3b-261023",
        "desc": "Groot 19B-A3B-261023 Q4_K_M (EXPERIMENTAL Agent-Coder, 128 Experten, ~11.7GB)",
        "repo": "GrootTeam/Groot-19B-A3B-261023-GGUF",
        "file_regex": [
            r"(?i)^groot-19b-a3b-261023-q4_k_m\.gguf$",
        ],
        "mmproj_regex": [],
    },
    # ── TwIL-8B (webAI-Official, Granite 4.2 8.8B dense reasoning) ─────────
    # Default balanced variant at the repo root:
    # TwIL-8B-Q4_K_M-balanced.gguf (~5.35 GB / 4.98 GiB). Text-only (no mmproj).
    # Profile model_configs/models/twil_8b.json carries the author's
    # defaults (-c 40960, --jinja, --reasoning on, temp=1.0, top_p=0.95).
    {
        "key": "twil:8b",
        "tag": "twil:8b",
        "desc": "TwIL-8B Q4_K_M Balanced (8.8B Granite 4.2 math/code/IF reasoning, ~5.3GB)",
        "repo": "webAI-Official/TwIL-8B",
        "file_regex": [
            r"(?i)^twil-8b-q4_k_m-balanced\.gguf$",
        ],
        "mmproj_regex": [],
    },
    # ── Air-26B-A4B (webAI-Official, Gemma 4 26B-A4B MoE reasoning) ────────
    # 25.2B total / ~4B active per token (128 experts, top-8, 30 blocks).
    # Text-only per model card; Q8_0 KV cache (-ctk/-ctv q8_0), 128K default ctx.
    # Profile model_configs/models/air_26b-a4b.json carries the author's
    # defaults (--jinja -c 131072 -ctk q8_0 -ctv q8_0, temp=1.0, top_p=0.95, top_k=64)
    # and MoE CPU expert offloading (moe=true, moe_cpu_experts=20, keeping 10/30 MoE blocks on GPU).
    {
        "key": "air:26b-a4b",
        "tag": "air:26b-a4b",
        "desc": "Air-26B-A4B MoE (25.2B/4B active Gemma 4 reasoning, 128K ctx, ~13.5GB)",
        "repo": "webAI-Official/Air-26B-A4B-gguf",
        "file_regex": [
            r"(?i)^air-26b-a4b\.gguf$",
        ],
        "mmproj_regex": [],
    },
    # ── Qwen3.5 ladder (subagent-lite smallest tier + MTP pair) ───────────
    # unsloth MTP repos name files WITHOUT "mtp" (verified 2026-09-11) — the
    # regexes are anchored to the exact filenames and the repos are pinned,
    # so no third-party repo (Jackrong etc.) can ever match.
    {
        "key": "qwen3.5:0.8b-ud",
        "tag": "qwen3.5:0.8b-ud",
        "desc": "Qwen3.5 0.8B UD (Subagent-lite ladder smallest tier, ~0.7GB VRAM)",
        "repo": "unsloth/Qwen3.5-0.8B-GGUF",
        "file_regex": [
            r"(?i)^qwen3\.5-0\.8b-ud-q4_k_xl\.gguf$",
        ],
        "mmproj_regex": [],
    },
    {
        "key": "qwen3.5:4b-mtp",
        "tag": "qwen3.5:4b-mtp",
        "desc": "Qwen3.5 4B MTP (Duo Coder/Planner default, MTP spec-decode, ~3.3GB VRAM)",
        "repo": "unsloth/Qwen3.5-4B-MTP-GGUF",
        "file_regex": [
            r"(?i)^qwen3\.5-4b-q4_k_m\.gguf$",
        ],
        # Vision projector: the unsloth MTP repos ship the GENERIC
        # mmproj-BF16/F16/F32 names (verified 2026-10-03) - without this
        # entry a fresh install had no projector for the default duo family
        # and every raw image was silently skipped. Dest embeds family+size
        # so write_models_json derives the size-keyed mmproj entry.
        "mmproj_regex": [r"(?i)^mmproj-f16\.gguf$"],
        "mmproj_dest": "mmproj-qwen3.5-4b-f16.gguf",
    },
    {
        "key": "qwen3.5:9b-mtp",
        "tag": "qwen3.5:9b-mtp",
        "desc": "Qwen3.5 9B MTP (Heavy Duo-Coder, MTP spec-decode, ~6GB VRAM)",
        "repo": "unsloth/Qwen3.5-9B-MTP-GGUF",
        "file_regex": [
            r"(?i)^qwen3\.5-9b-q4_k_m\.gguf$",
        ],
        "mmproj_regex": [r"(?i)^mmproj-f16\.gguf$"],
        "mmproj_dest": "mmproj-qwen3.5-9b-f16.gguf",
    },
    {
        "key": "qwen3.5:2b-mtp",
        "tag": "qwen3.5:2b-mtp",
        "desc": "Qwen3.5 2B MTP (Refiner, MTP spec-decode, ~1.3GB VRAM)",
        "repo": "unsloth/Qwen3.5-2B-MTP-GGUF",
        "file_regex": [
            r"(?i)^qwen3\.5-2b-q4_k_m\.gguf$",
        ],
        "mmproj_regex": [r"(?i)^mmproj-f16\.gguf$"],
        "mmproj_dest": "mmproj-qwen3.5-2b-f16.gguf",
    },
]


def http_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "HiveMind-Installer"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def pick_repos(spec: dict, max_candidates: int = 8) -> list[str]:
    """Candidate repos for a spec, sorted by author preference + downloads.
    Multiple candidates, so a top repo without a matching GGUF (e.g. google/QAT)
    can be skipped."""
    from urllib.parse import quote
    url = f"{HF_API}?search={quote(spec['search'])}&limit=50"
    try:
        results = http_json(url)
    except Exception as e:
        print(f"    [WARNING] HF search failed ({e})")
        return []

    gguf_repos = [r for r in results if "gguf" in r.get("id", "").lower()]
    if not gguf_repos:
        gguf_repos = results

    def rank(r: dict) -> tuple:
        author = r.get("id", "").split("/")[0].lower()
        pref = spec["author_pref"].index(author) if author in spec["author_pref"] else len(spec["author_pref"])
        return (pref, -int(r.get("downloads") or 0))

    gguf_repos.sort(key=rank)
    return [r["id"] for r in gguf_repos[:max_candidates]]


def list_repo_files(repo: str) -> list[dict]:
    return http_json(f"{HF_API}/{repo}/tree/main?recursive=true")


def pick_file(files: list[dict], regexes: list[str], exclude_mmproj: bool = True) -> dict | None:
    for rx in regexes:
        pat = re.compile(rx)
        for f in files:
            if f.get("type") != "file":
                continue
            name = Path(f["path"]).name
            if exclude_mmproj and "mmproj" in name.lower():
                continue
            if pat.search(name):
                return f
    return None


def disk_space_free_gb(path: Path) -> float:
    """Free space on the drive of `path` (in GB)."""
    try:
        return shutil.disk_usage(str(path)).free / (1024 ** 3)
    except OSError:
        return -1.0


class _EtagRedirectHandler(urllib.request.HTTPRedirectHandler):
    """B7: Hugging Face puts X-Linked-Etag on the 302, not on the CDN answer.

    urllib follows the redirect automatically; without this handler the
    checksum header was lost and the sha256 check silently became a size-only
    check. Capture it from the 3xx response before following.
    """

    def __init__(self):
        self.etag = ""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.etag:
            for _key in ("X-Linked-Etag", "ETag"):
                _val = headers.get(_key)
                if _val:
                    self.etag = str(_val).strip()
                    break
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _sha256_file(path: Path) -> str:
    """Streaming sha256 for a downloaded file (B7)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_file(repo: str, f: dict, dest_dir: Path, auto_yes: bool,
                  _no_resume: bool = False, dest_name: str | None = None) -> bool:
    url = HF_RESOLVE.format(repo=repo, path=urllib.parse.quote(str(f["path"]), safe="/"))
    size_gb = int(f.get("size") or 0) / (1024 ** 3)
    dest = dest_dir / (dest_name or f.get("dest_name") or Path(f["path"]).name)
    if size_gb > 8.0 and not auto_yes:
        # EOF (audit r4): a non-interactive run (systemd, piped stdin) used to
        # die with an EOFError traceback here instead of skipping cleanly.
        try:
            ans = input(f"    {Path(f['path']).name} is {size_gb:.1f} GB large. Download? [Y/n] ").strip().lower()
        except EOFError:
            print("    [WARNING] no interactive stdin - skipping the large file (use --yes)")
            return False
        if ans in ("n", "no"):
            return False
    # DISK-SPACE-CHECK (2026-08-26): check free space before every download
    # (download + 1GB buffer). Live finding: a 25GB set on a nearly full disk
    # only failed mid-write and left partial files behind.
    free_gb = disk_space_free_gb(dest_dir)
    if 0 <= free_gb < size_gb + 1.0:
        print(f"    [ERROR] Not enough disk space: {size_gb:.1f} GB needed, only {free_gb:.1f} GB free on {dest_dir}.")
        return False
    # RESUME (2026-10-27, audit): an 18 GB download that died at 12.6 GB started
    # from zero. A partial file is continued with a Range request, and the result
    # is checked against the expected size before it counts as done.
    _have = 0 if _no_resume else (dest.stat().st_size if dest.exists() else 0)
    _had_partial = bool(_have)
    _expected = int(f.get("size") or 0)
    if _have and _expected and _have > _expected:
        print(f"    [WARNING] oversized file on disk ({_have} > {_expected}) - restarting clean")
        dest.unlink(missing_ok=True)
        _have = 0
        _had_partial = False
    if _have and _expected and _have == _expected:
        _oid_pre = _hf_lfs_sha256(repo, str(f.get("path") or ""))
        if re.fullmatch(r"[a-f0-9]{64}", _oid_pre.lower()):
            if _sha256_file(dest) != _oid_pre.lower():
                print(f"    [WARNING] checksum mismatch on existing {dest.name} - re-downloading")
                dest.unlink(missing_ok=True)
                _have = 0
                _had_partial = False
            else:
                print(f"    Already complete (verified): {dest.name} ({_have / (1024**3):.2f} GB)")
                return True
        else:
            print(f"    Already complete: {dest.name} ({_have / (1024**3):.2f} GB)")
            return True
    _etag_raw = ""
    _is_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    for _attempt in range(1, 5):
        _have = 0 if (_no_resume and _attempt == 1) else (dest.stat().st_size if dest.exists() else 0)
        _had_partial = _had_partial or bool(_have)
        print(f"    Downloading -> {dest} ({size_gb:.1f} GB)"
              + (f" - resuming at {_have / (1024**3):.2f} GB" if _have else "") + "...")
        headers = {"User-Agent": "HiveMind-Installer", **_hf_auth_headers()}
        if _have:
            headers["Range"] = f"bytes={_have}-"
        req = urllib.request.Request(url, headers=headers)
        _redirect = _EtagRedirectHandler()
        _opener = urllib.request.build_opener(_redirect)
        try:
            resp = _opener.open(req, timeout=120)
        except urllib.error.HTTPError as he:
            if he.code in (401, 403):
                print("    [ERROR] access denied (gated/private repo). Create a "
                      "token at huggingface.co/settings/tokens and run "
                      "`hf auth login` or set HF_TOKEN.")
                return False
            if he.code == 416 and _have:
                if not _expected:
                    _remote = 0
                    try:
                        with urllib.request.urlopen(
                                urllib.request.Request(url, headers={"User-Agent": "HiveMind-Installer", **_hf_auth_headers()}, method="HEAD"),
                                timeout=30) as _hr:
                            _remote = int(_hr.headers.get("Content-Length") or 0)
                    except (urllib.error.URLError, OSError, ValueError):
                        _remote = 0
                    if _remote and _have == _remote:
                        print(f"    Already complete: {dest.name}")
                        return True
                    if _remote and _have != _remote:
                        print(f"    [WARNING] 416 with {_have} of {_remote} bytes "
                              f"(HEAD) - restarting clean")
                        dest.unlink(missing_ok=True)
                        return download_file(repo, f, dest_dir, auto_yes, _no_resume=True, dest_name=dest_name)
                    print(f"    [WARNING] 416 with an unknown expected size - "
                          f"keeping {dest.name} as-is; verify it before use")
                    return True
                if _expected and _have < _expected:
                    print(f"    [WARNING] 416 with {_have} of {_expected} bytes on "
                          f"disk - upstream file changed; restarting clean")
                    dest.unlink(missing_ok=True)
                    return download_file(repo, f, dest_dir, auto_yes, _no_resume=True, dest_name=dest_name)
                print(f"    Already complete: {dest.name}")
                return True
            if _attempt < 4:
                time.sleep(min(15, 2 ** _attempt))
                continue
            raise
        try:
            with resp as r:
                _cr = r.headers.get("Content-Range") or ""
                if _have and r.status == 206 and _cr:
                    _crm = re.match(r"bytes\s+(\d+)-", _cr)
                    if _crm and int(_crm.group(1)) != _have:
                        print(f"    [WARNING] server resumed at byte {_crm.group(1)}, "
                              f"partial is {_have} - restarting from scratch")
                        dest.unlink(missing_ok=True)
                        return download_file(repo, f, dest_dir, auto_yes, _no_resume=True, dest_name=dest_name)
                _append = bool(_have and r.status == 206)
                if not _append:
                    _have = 0
                total = int(r.headers.get("Content-Length") or 0) + _have
                done = _have
                _last_pct = -1
                _last_print_t = 0.0
                with open(dest, "ab" if _append else "wb") as out:
                    while True:
                        chunk = r.read(1024 * 512)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        if total:
                            _pct = done * 100 // max(total, 1)
                            _now = time.monotonic()
                            if _is_tty:
                                if _pct > _last_pct or (_now - _last_print_t) >= 0.5 or done >= total:
                                    sys.stdout.write(f"\r    {done / (1024**3):.2f} / {total / (1024**3):.2f} GB ({_pct}%)")
                                    sys.stdout.flush()
                                    _last_pct = _pct
                                    _last_print_t = _now
                            else:
                                if _pct >= _last_pct + 5 or (_last_pct < 0) or done >= total:
                                    print(f"    {done / (1024**3):.2f} / {total / (1024**3):.2f} GB ({_pct}%)", flush=True)
                                    _last_pct = _pct
                                    _last_print_t = _now
                _etag_raw = (_redirect.etag
                             or r.headers.get("ETag")
                             or r.headers.get("X-Linked-Etag") or "").strip()
                _etag_raw = re.sub(r'^W/', "", _etag_raw).strip('"')
                if _is_tty:
                    sys.stdout.write("\n")
                if total and done == total:
                    break
                if not total:
                    break
        except (OSError, http.client.HTTPException) as exc:
            if _is_tty:
                sys.stdout.write("\n")
            if _attempt >= 4:
                raise
            _wait = min(15, 2 ** _attempt)
            print(f"    [WARNING] interrupted ({exc}) - retrying in {_wait}s (attempt {_attempt + 1}/4)")
            time.sleep(_wait)
    _final = dest.stat().st_size if dest.exists() else 0
    if _expected and _final != _expected:
        print(f"    [WARNING] size mismatch: {_final} bytes on disk, {_expected} expected "
              f"- re-run to resume")
        return False
    # B7: Hugging Face serves the LFS sha256 as ETag/X-Linked-Etag. A hybrid
    # file (old partial + new bytes) has the right size but the wrong hash.
    # B7: prefer the file API's LFS oid; a 64-hex redirect ETag (local mirrors,
    # other CDNs) is the fallback. A 40-hex git blob id is NOT a content hash.
    _oid = _hf_lfs_sha256(repo, str(f.get("path") or ""))
    if not re.fullmatch(r"[a-f0-9]{64}", _oid.lower()):
        _oid = (_etag_raw.lower()
                if re.fullmatch(r"[a-f0-9]{64}", _etag_raw.lower()) else "")
    if _oid:
        print("    [INFO] checksum: sha256 available - verifying")
        if _sha256_file(dest) != _oid:
            print("    [WARNING] checksum mismatch: file does not match the "
                  "upstream ETag - deleting")
            try:
                dest.unlink()
            except OSError:
                pass
            if _had_partial and not _no_resume:
                print("    [INFO] re-downloading from scratch (resume came "
                      "from a different upstream generation)")
                return download_file(repo, f, dest_dir, auto_yes, _no_resume=True, dest_name=dest_name)
            return False
    else:
        print("    [INFO] checksum: no sha256 available - size check only")
    return True


def _spec_matches_file(spec: dict, gguf: Path) -> bool:
    """True if this local GGUF matches the spec (pinned file_dest or file_regex)."""
    if spec.get("file_dest"):
        return gguf.name.lower() == str(spec["file_dest"]).lower()
    return any(
        re.search(rx, gguf.name, re.IGNORECASE)
        for rx in spec.get("file_regex", [])
    )


def _download_extra_file(repo: str, ef: dict) -> bool:
    """Fetch a non-GGUF companion (chat template) to its repo-relative dest.

    ATOMIC-DEST (audit r4): the file used to be written straight to the
    destination - a failed download left a PARTIAL file there, and every
    later run then said "Extra file present - skipping": the model was
    permanently stuck with a broken template. Download to `.part`, then
    os.replace on success; a failure cleans up after itself.
    """
    dest = ROOT / ef["dest"]
    part = dest.with_name(dest.name + ".part")
    try:
        url = HF_RESOLVE.format(repo=repo, path=urllib.parse.quote(str(ef["path"]), safe="/"))
        print(f"    Downloading extra file: {ef['path']} -> {ef['dest']}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers=_hf_auth_headers())
        with urllib.request.urlopen(req, timeout=120) as r, open(part, "wb") as fh:
            fh.write(r.read())
        part.replace(dest)
        print(f"    [OK] extra file -> {ef['dest']}")
        return True
    except (OSError, http.client.HTTPException) as e:
        part.unlink(missing_ok=True)
        print(f"    [WARNING] Extra file download failed ({e})")
        return False


def _record_known_filename(gguf_name: str, tag: str) -> None:
    """Append filename→tag to the shipped manifest so the backend registers
    the file under the catalog tag even without models.json (manual
    downloads, fresh machines)."""
    map_file = ROOT / "model_configs" / "gguf_filename_tags.json"
    try:
        data = json.loads(map_file.read_text(encoding="utf-8")) if map_file.exists() else {}
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"    [WARNING] could not read gguf_filename_tags.json: {e}")
        return
    if gguf_name.lower() in data and data[gguf_name.lower()] == tag:
        return
    data[gguf_name.lower()] = tag
    try:
        # ATOMIC (audit r4): same reasoning as models.json - a torn write of
        # the manifest would make every later load fall back to {} (OSError
        # / JSONDecodeError), silently losing every recorded tag.
        _atomic_write_text(map_file, json.dumps(data, indent=2, ensure_ascii=False))
        print(f"    known-filename recorded: {gguf_name} -> {tag}")
    except OSError as e:
        print(f"    [WARNING] could not update gguf_filename_tags.json: {e}")


def _seed_spec_default_profile(spec: dict) -> Path | None:
    """Seed a catalog spec's bundled default_profile on download if missing."""
    profile = spec.get("default_profile")
    if not isinstance(profile, dict):
        return None
    tag = str(spec.get("tag") or spec.get("key") or "").strip()
    if not tag:
        return None
    try:
        from model_configs.models_registry import find_profile_file, save_profile  # noqa: E402

        if find_profile_file(tag) is not None:
            return None
        payload = copy.deepcopy(profile)
        payload.setdefault("model", tag)
        path = save_profile(tag, payload)
        print(f"    default-profile written: {path.name}")
        return path
    except (ImportError, OSError, ValueError) as e:
        print(f"    [WARNING] could not write default profile for {tag}: {e}")
        return None


def write_models_json(models_dir: Path, out: Path | None = None) -> int:
    """Populate models.json from all GGUFs in the folder."""
    from backend.llama_models import _parse_gguf_filename  # noqa: E402
    from backend.llama_models import _known_filename_tags  # noqa: E402

    ggufs = sorted(models_dir.rglob("*.gguf"))
    mapping: dict[str, str] = {}
    families: set[str] = set()

    # Normal models first
    from backend.llama_models import _SPLIT_SHARD_RE, is_non_model_gguf  # noqa: E402
    for g in ggufs:
        # SHARED RULE (2026-11-05 audit): one non-model check for every
        # writer — the private copy here missed 'projector'/'vision_encoder'.
        if is_non_model_gguf(g.name):
            continue
        _sm_shard = _SPLIT_SHARD_RE.search(g.stem)
        if _sm_shard and int(_sm_shard.group(1)) > 1:
            continue
        # TAG-PIN (2026-09-12): a spec tag is authoritative — the filename
        # parser over-normalizes (unsloth names their MTP quants WITHOUT
        # "mtp": Qwen3.5-4B-Q4_K_M.gguf parsed as qwen3.5:4b, so the
        # settings default qwen3.5:4b-mtp found nothing). Pin catalog tag →
        # downloaded file; the parsed alias (qwen3.5:4b) still exists.
        _spec_tag = next(
            (s["tag"] for s in SPECS if s.get("tag") and _spec_matches_file(s, g)),
            None,
        )
        if _spec_tag:
            mapping[_spec_tag] = str(g)
            if g.name.lower() not in _known_filename_tags():
                _record_known_filename(g.name, _spec_tag)
        names = _parse_gguf_filename(g.name)
        if names:
            mapping.setdefault(names[0], str(g))
            families.add(names[0].split(":")[0])

    # Assign mmproj entries to the matching family
    for g in ggufs:
        if "mmproj" not in g.name.lower():
            continue
        low = g.name.lower().replace(".", "")
        family = next((fam for fam in families if fam.replace(".", "") in low), None)
        if family:
            # SIZE-KEYED (2026-10-03): families with per-size projectors
            # (mmproj-Qwen3.5-{0.8B,2B,4B,9B}-F16) get a ":<size>b" key so
            # each model resolves ITS size; the bare family key stays as
            # fallback for single-projector families (gemma-4, hermes3.6).
            # Size token comes from the ORIGINAL filename (dots/dashes are
            # boundaries) - the dot-stripped `low` broke the boundary match.
            mapping[f"{family}_mmproj"] = str(g)
            _sm = re.search(r"(?:^|[-_.])(e?\d+(?:\.\d+)?)b(?:[-_.]|$)", g.name.lower())
            if _sm:
                _size = _sm.group(1).replace(".", "") + "b"
                mapping[f"{family}:{_size}_mmproj"] = str(g)

    out = out or (ROOT / "models.json")   # out= lets callers (tests, deploys) pick the target
    # MERGE (2026-11-05, P0 audit): this used to be a full rewrite — every
    # run silently DELETED manual registrations (add_model.py's "single
    # GGUF by path" flow), including under --scan-only. Merge instead:
    # existing entries whose file still exists are preserved, scanned/
    # spec entries win on key collision, and only entries whose file is
    # GONE are dropped (the old stale-pin cleanup, now honest).
    # SORTED + STABLE (2026-10-03): sorted keys keep diffs comparable.
    # ATOMIC (audit r4): temp file + os.replace.
    try:
        existing = json.loads(out.read_text(encoding="utf-8"))
        existing = existing if isinstance(existing, dict) else {}
    except (OSError, ValueError):
        existing = {}
    for _k, _v in existing.items():
        if not isinstance(_k, str) or not isinstance(_v, str):
            continue
        if _k.startswith("_"):
            continue
        if _k in mapping:
            continue
        try:
            if Path(_v.strip()).is_file():
                mapping[_k] = _v
            else:
                print(f"    dropped stale entry {_k} (file gone: {_v})")
        except OSError:
            mapping[_k] = _v
    _atomic_write_text(
        out,
        json.dumps(mapping, indent=2, ensure_ascii=False, sort_keys=True),
    )
    return len(mapping)


def _resolve_only(args_only: str) -> list[dict] | None:
    """--only: comma-separated selection of spec keys OR 1-based numbers
    (as shown by setup_models.bat). Returns the filtered specs."""
    sel = [s.strip() for s in args_only.split(",") if s.strip()]
    if not sel:
        return None
    by_idx = {str(i + 1): s["key"] for i, s in enumerate(SPECS)}
    keys = {s["key"] for s in SPECS}
    chosen = set()
    for item in sel:
        if item in keys:
            chosen.add(item)
        elif item in by_idx:
            chosen.add(by_idx[item])
        else:
            print(f"    [WARNING] Unknown selection skipped: {item}")
    if not chosen:
        print("[ERROR] --only: no valid selection (keys or numbers 1-"
              + str(len(SPECS)) + ").")
        return None
    return [s for s in SPECS if s["key"] in chosen]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models-dir", default="")
    ap.add_argument("--only-missing", action="store_true")
    ap.add_argument("--no-drafters", action="store_true",
                    help="skip the drafter sidecars (costs ~50%% decode for the "
                         "models that need one)")
    ap.add_argument("--print-essential", action="store_true",
                    help="print the essential-set models (key + size) and exit")
    ap.add_argument("--essential", action="store_true",
                    help="skip the optional omni vision worker - core models only")
    ap.add_argument("--list-only", action="store_true")
    ap.add_argument("--scan-only", action="store_true",
                    help="Only scan local GGUFs + write models.json, NO download, NO network")
    ap.add_argument("--print-catalog", action="store_true",
                    help="Print the numbered download catalog and exit (no network, "
                         "no folder touch) — setup_models.bat renders its menu from this")
    ap.add_argument("--only-keys", default="",
                    help="comma-separated spec KEYS to install (stable, unlike --only numbers)")
    ap.add_argument("--only", default="",
                    help="Only these models: keys or numbers, comma-separated (e.g. 1,4)")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    global SKIP_DRAFTERS
    SKIP_DRAFTERS = bool(getattr(args, "no_drafters", False))
    if SKIP_DRAFTERS:
        print("    [WARN] --no-drafters: speculative decoding stays off for "
              "every model that needs a drafter sidecar")
        print("           (Gemma-4 MTP and the DSpark models lose ~50 % decode).")

    if getattr(args, 'print_essential', False):
        # The menu shows the real list instead of a vague sentence.
        _ess = [sp for sp in SPECS if sp['key'] in ESSENTIAL_KEYS]
        print("  ESSENTIAL SET - the default route, nothing else is needed to start")
        print("  " + "-" * 68)
        print()
        for _sp in _ess:
            print(f"    {_sp['key']}")
            print(f"        {_sp['desc']}")
            print()
        print(f"  {len(_ess)} models, vision projectors and drafters included.")
        print()
        _rest = [sp['key'] for sp in SPECS if sp['key'] not in ESSENTIAL_KEYS]
        print("  ALSO AVAILABLE - choose S to pick single models")
        print("  " + "-" * 68)
        print()
        for _i in range(0, len(_rest), 2):
            print("    " + "".join(f"{k:<34}" for k in _rest[_i:_i + 2]).rstrip())
        print()
        return 0

    if args.print_catalog:
        # Wide, two-line layout: key on its own line, description underneath.
        # A single 120-character line wrapped in narrow terminals and cut the
        # description mid-word.
        for _i, _s in enumerate(SPECS):
            print(f"  [{_i + 1:>2}] {_s['key']}")
            print(f"       {_s['desc']}")
            print()
        return 0

    mdir = Path(args.models_dir).expanduser() if args.models_dir else \
        Path(os.environ.get("HIVEMIND_MODELS_DIR", "") or (ROOT / "models"))
    if not args.list_only:
        mdir.mkdir(parents=True, exist_ok=True)
        _persist_models_dir(mdir)
    local_files = {g.name.lower(): g for g in mdir.rglob("*.gguf")} if mdir.is_dir() else {}
    print(f"Models folder: {mdir}")
    if local_files:
        print(f"Existing GGUFs: {len(local_files)}")

    # SCAN-ONLY (2026-08-27, user request): auto-detect own models and register
    # them in models.json - no download, no HF API. Previously --list-only was
    # abused for this, which does NOT register (only "would load").
    if args.scan_only:
        ggufs = sorted(mdir.rglob("*.gguf"))
        if not ggufs:
            print("[WARNING] No .gguf files found in the folder.")
        else:
            for g in ggufs:
                print(f"    found: {g.name}")
        n = write_models_json(mdir)
        print(f"[OK] models.json written: {n} entries -> {ROOT / 'models.json'}")
        return 0

    specs = [s for s in SPECS
               if not getattr(args, 'essential', False)
               or s.get('key') in ESSENTIAL_KEYS]
    if getattr(args, 'only_keys', ''):
        _ok = [k.strip() for k in args.only_keys.split(',') if k.strip()]
        _found = set(sp.get('key') for sp in SPECS)
        _missing = [k for k in _ok if k not in _found]
        if _missing:
            print('[ERROR] unknown model keys: ' + ', '.join(_missing))
            raise SystemExit(2)
        specs = [sp for sp in specs if sp.get('key') in set(_ok)]
        if not specs:
            print('[ERROR] no models left after applying --essential + --only-keys')
            return 2

    if args.only:
        specs = _resolve_only(args.only)
        if specs is None:
            return 1
        print(f"Only {len(specs)} model(s): {', '.join(s['key'] for s in specs)}")

    # PREFLIGHT (2026-08-26): check disk space before starting — the complete
    # set is ~30 GB. Below 3 GB free the run makes no sense.
    if not args.list_only:
        free_gb = disk_space_free_gb(mdir)
        print(f"Free disk space: {free_gb:.1f} GB")
        if 0 <= free_gb < 3.0:
            print("[ERROR] Too little free disk space for the model download.")
            print("         Free up space or pick a different models folder.")
            return 1

    for spec in specs:
        print()
        print(f"== {spec['key']} — {spec['desc']}")

        # EXTRA FILES (2026-09-29): non-GGUF companions fetched alongside the
        # model into repo-relative destinations — e.g. the Sharp-MiniCPM5 chat
        # template landing in model_configs/. Checked BEFORE the GGUF
        # --only-missing skip so a missing template self-heals even when the
        # model itself is already on disk. Idempotent via existence check.
        for _ef in (spec.get("extra_files") or []):
            _ef_dest = ROOT / _ef["dest"]
            if _ef_dest.exists():
                print(f"    Extra file present: {_ef['dest']} — skipping")
                continue
            if args.list_only:
                print(f"    Would download (extra): {_ef['path']} -> {_ef['dest']}")
                continue
            _download_extra_file(spec["repo"], _ef)

        # SIDECARS before the "already present" skip (2026-10-19): a re-run with
        # --only-missing used to skip the whole spec as soon as the main GGUF
        # existed, so a drafter added later (DSpark) never reached existing
        # installs. Same self-healing rule as extra_files: idempotent, keyed on
        # the sidecar filename.
        for _sc in (spec.get("sidecar") or []):
            _sc_name = Path(_sc["path"]).name.lower()
            if SKIP_DRAFTERS:
                print(f"    Drafter skipped (--no-drafters): {Path(_sc['path']).name}"
                      f" [{_sc.get('note', '')}]")
                continue
            if args.only_missing and _sc_name in local_files:
                print(f"    Sidecar present: {Path(_sc['path']).name} — skipping (--only-missing)")
                continue
            if args.list_only:
                print(f"    Would download (sidecar): {_sc['path']} [{_sc.get('note', '')}]")
                continue
            try:
                if download_file(_sc["repo"], {"path": _sc["path"], "size": 0}, mdir, args.yes):
                    local_files[_sc_name] = mdir / Path(_sc["path"]).name
            except Exception as e:
                print(f"    [WARNING] Sidecar download failed ({e})")

        # Already present? Match across ALL spec file_regex patterns (not only [0]).
        if spec.get("file_dest"):
            have = [n for n in local_files if n == str(spec["file_dest"]).lower()]
        else:
            have = [n for n in local_files if any(re.search(rx, n) for rx in spec["file_regex"])]

        if spec.get("repo"):
            # PINNED REPO (2026-09-11): no HF search at all — the catalog pins
            # the exact repo, so third-party lookalikes can never be picked.
            repos = [spec["repo"]]
            print(f"    Repo (pinned): {spec['repo']}")
        else:
            print(f"    Searching Hugging Face: '{spec['search']}'...")
            repos = pick_repos(spec)
            if not repos:
                print("    [ERROR] No matching repo found — load manually from huggingface.co.")
                continue

        # The top repo can contain a GGUF that does not match file_regex
        # (e.g. google/gemma-4-E4B-it-qat-q4_0-gguf) — then try the next candidates.
        repo = None
        main_file = None
        files = []
        first_files = []
        for cand in repos:
            if not spec.get("repo"):
                print(f"    Repo candidate: {cand}")
            try:
                cand_files = list_repo_files(cand)
            except Exception as e:
                print(f"      [WARNING] Repo not fetchable ({e})")
                continue
            if not first_files:
                first_files = cand_files
            cand_file = pick_file(cand_files, spec["file_regex"], exclude_mmproj=True)
            if cand_file:
                repo, files, main_file = cand, cand_files, cand_file
                break
        if not main_file:
            if args.only_missing and have:
                print(f"    Already present: {have[0]} — skipping (--only-missing, offline fallback)")
                if not args.list_only:
                    _seed_spec_default_profile(spec)
                continue
            print("    [ERROR] No matching GGUF in the candidate repos:")
            for f in first_files[:15]:
                if f.get("type") == "file":
                    print(f"      - {f['path']}")
            continue
        # the candidate loop above assigns repo together with main_file;
        # normalize once so both download calls below take a plain str
        repo = str(repo or "")
        print(f"    Repo: {repo}")

        _main_dest = spec.get("file_dest") or Path(main_file["path"]).name
        if spec.get("file_dest"):
            main_file = {**main_file, "dest_name": _main_dest}
        _existing_complete = False
        if args.only_missing and have:
            _existing_path = local_files[have[0]]
            _expected_bytes = int(main_file.get("size") or 0)
            _on_disk = _existing_path.stat().st_size if _existing_path.exists() else 0
            if _on_disk > 0 and (not _expected_bytes or _on_disk == _expected_bytes):
                _existing_complete = True
                print(f"    Already present: {have[0]} — skipping (--only-missing)")
                if not args.list_only:
                    _seed_spec_default_profile(spec)
            elif _on_disk > 0:
                print(f"    Partial file found ({have[0]}: {_on_disk}/{_expected_bytes} bytes) — resuming...")
        if _existing_complete:
            _main_ok = True
        elif args.list_only:
            if spec.get("file_dest"):
                print(f"    Would download: {main_file['path']} -> {_main_dest}")
            else:
                print(f"    Would download: {main_file['path']}")
        else:
            # Declining a large GGUF must skip the WHOLE spec: the projector
            # and the models.json entry belong to a model that is not on disk.
            # The old path downloaded the projector anyway and registered it.
            # main file now skips the whole spec.
            # NET-ERROR (2026-11-04): a mid-body reset raised out of
            # download_file and killed the whole batch before models.json was
            # written. Treat it as a failed download and continue.
            try:
                _main_ok = download_file(repo, main_file, mdir, args.yes)
            except (OSError, http.client.HTTPException) as _dl_err:
                print(f"    [ERROR] download failed: {_dl_err}")
                _main_ok = False
            if not _main_ok:
                print("    Skipped - model not downloaded "
                      "(projector/drafter/registration skipped too)")
                continue
            local_files[main_file["path"].lower()] = mdir / _main_dest
            local_files[_main_dest.lower()] = mdir / _main_dest
            if spec.get("tag"):
                _record_known_filename(_main_dest, spec["tag"])
            _seed_spec_default_profile(spec)

        if spec["mmproj_regex"]:
            # RE-RUN GUARD (2026-10-03): an earlier install may already have
            # this model's projector under a different filename (manual
            # installs, pre-mmproj_dest naming). If models.json already
            # carries an exact tag-key or size-key entry pointing at an
            # existing file, skip the download instead of adding a duplicate
            # multi-hundred-MB copy under the new dest name. The bare family
            # key deliberately does NOT satisfy this guard: it may point at
            # another size's projector.
            _mm_skip = False
            try:
                _mj_path = ROOT / "models.json"
                if _mj_path.exists():
                    _mj = json.loads(_mj_path.read_text(encoding="utf-8"))
                    _tag = spec.get("tag") or spec["key"]
                    # family keeps its dots: models.json keys are written as
                    # "qwen3.5:4b_mmproj" (only the SIZE digits are stripped)
                    _fam = _tag.split(":")[0]
                    _okeys = [f"{_tag}_mmproj"]
                    if ":" in _tag:
                        _sm = re.match(r"(e?\d+(?:\.\d+)?)b", _tag.split(":")[-1].lower())
                        if _sm:
                            _okeys.insert(1, f"{_fam}:{_sm.group(1).replace('.', '')}b_mmproj")
                    for _ok_key in _okeys:
                        _ov = str(_mj.get(_ok_key, "") or "")
                        if _ov and Path(_ov).exists():
                            print(f"    Projector already present ({Path(_ov).name}) - skipping download")
                            _mm_skip = True
                            break
            except (json.JSONDecodeError, OSError) as _mm_guard_err:
                print(f"    [WARNING] mmproj re-run guard failed ({_mm_guard_err})")
            mm = None if _mm_skip else pick_file(files, spec["mmproj_regex"], exclude_mmproj=False)
            # MMPROJ DEST (2026-10-03): several repos ship their projector
            # under the SAME generic filename (mmproj-BF16.gguf in unsloth
            # gemma-4 e4b/e2b AND qwen3.6 repos). Downloading directly to _mm_dest
            # ensures models never collide or resume each other's generic projector.
            _mm_dest = spec.get("mmproj_dest") or (Path(mm["path"]).name if mm else None)
            _mm_expected = int(mm.get("size") or 0) if mm else 0
            _mm_have = (local_files[_mm_dest.lower()].stat().st_size
                        if (_mm_dest and _mm_dest.lower() in local_files and local_files[_mm_dest.lower()].exists())
                        else 0)
            _mm_done = bool(_mm_dest and _mm_have > 0 and (not _mm_expected or _mm_have == _mm_expected) and args.only_missing)
            if mm and _mm_dest and not _mm_done:
                if args.list_only:
                    print(f"    Would download (Vision): {mm['path']} -> {_mm_dest}")
                else:
                    _mm_ok = False
                    try:
                        _mm_ok = download_file(repo, mm, mdir, args.yes, dest_name=_mm_dest)
                    except (OSError, http.client.HTTPException) as _mm_err:
                        print(f"    [ERROR] projector download failed: {_mm_err}")
                    _dstp = mdir / _mm_dest
                    if _mm_ok and _dstp.exists():
                        local_files[_mm_dest.lower()] = _dstp
                    else:
                        print("    [WARNING] projector not downloaded - keeping the destination untouched")

    if args.list_only:
        return 0

    n = write_models_json(mdir)
    print()
    print(f"[OK] models.json written: {n} entries -> {ROOT / 'models.json'}")
    return 0


if __name__ == "__main__":
    # Ctrl+C during a download must not dump an ssl/socket traceback. A partial
    # file is kept on purpose - the next run resumes it with a Range request.
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        print("  [CANCELLED] Download interrupted. A partial file stays on disk and "
              "the next run resumes it.")
        sys.exit(130)
