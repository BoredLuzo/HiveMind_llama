"""Fetch what the Hugging Face model cards say for every catalog model."""
import json
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, r"C:\Users\NtheP\Desktop\HiveMind\repo_gateway")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from deploy.fetch_models import SPECS  # noqa: E402

UA = {"User-Agent": "HiveMind-config-audit/1.0"}


def get(url: str, limit: int | None = None) -> str | None:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=40) as r:
            data = r.read()
        txt = data.decode("utf-8", "replace")
        return txt[:limit] if limit else txt
    except (urllib.error.HTTPError, OSError):
        return None


def card_facts(txt: str) -> dict:
    t = txt.lower()
    facts = {}
    # context length mentions
    ctx = set()
    for m in re.finditer(r"(\d{2,3}(?:[.,]\d+)?)\s*k\b", t):
        ctx.add(m.group(0))
    for m in re.finditer(r"context (?:window|length)[^.\n]{0,40}?(\d[\d,\.]*)", t):
        ctx.add(m.group(1))
    facts["ctx_mentions"] = sorted(ctx)[:6]
    for key, pat in (("temperature", r"temperature[^.\n]{0,40}?([01](?:\.\d+)?)"),
                     ("top_p", r"top[ _-]?p[^.\n]{0,30}?([01](?:\.\d+)?)"),
                     ("top_k", r"top[ _-]?k[^.\n]{0,30}?(\d{1,3})"),
                     ("min_p", r"min[ _-]?p[^.\n]{0,30}?([01](?:\.\d+)?)")):
        vals = re.findall(pat, t)
        if vals:
            facts[key] = sorted(set(vals))[:4]
    facts["jinja"] = "jinja" in t
    facts["mtp"] = bool(re.search(r"\bmtp\b|multi[- ]token", t))
    facts["tool"] = "tool" in t
    facts["thinking"] = "think" in t
    facts["vision"] = "vision" in t or "mmproj" in t or "image" in t
    return facts


for spec in SPECS:
    repo = spec["repo"]
    print("=" * 78)
    print(f"{spec['key']}   ({repo})")
    card = get(f"https://huggingface.co/{repo}/raw/main/README.md", 200_000)
    cfg = get(f"https://huggingface.co/{repo}/raw/main/config.json")
    if cfg:
        try:
            c = json.loads(cfg)
            keys = ("max_position_embeddings", "sliding_window", "moe_cpu_experts",
                    "num_experts", "num_experts_per_tok", "torch_dtype")
            got = {k: c.get(k) for k in keys if c.get(k) is not None}
            print("  config.json:", got)
        except ValueError:
            print("  config.json: unparsable")
    else:
        print("  config.json: absent (pure GGUF repo)")
    if card:
        f = card_facts(card)
        print("  card ctx mentions :", f["ctx_mentions"])
        print("  card sampling     :", {k: f[k] for k in
                                        ("temperature", "top_p", "top_k", "min_p")
                                        if k in f})
        print("  flags             :", {k: f[k] for k in
                                        ("jinja", "mtp", "tool", "thinking", "vision")})
        # first YAML/frontmatter description lines
        head = "\n".join(card.splitlines()[:30])
        snips = [ln.strip() for ln in head.splitlines()
                 if ln.strip() and not ln.strip().startswith(("---", "license", "tags",
                                                              "language", "base_model"))]
        print("  card head         :", " | ".join(snips[:5])[:300])
    else:
        print("  card: unavailable")
