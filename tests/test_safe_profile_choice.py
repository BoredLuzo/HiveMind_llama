"""
test_safe_profile_choice.py
===========================
Safe-profile policy must not stomp an explicit user model choice
(agents_user_model_choice marker), even when the chosen model equals the
shipped DEFAULT (CARDS-WIN blind spot, 2026-09-10: duo_coder
qwen3.5:4b-mtp was silently reverted to lfm2.5:2.6b by default_8gb_v1).

Standalone suite: sys.exit(0|1), registered in run_regressions.py.
"""
import copy
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hive_functions.safe_profile_policy import apply_safe_profile_policy  # noqa: E402
from settings import DEFAULT_AGENT_CFG  # noqa: E402

failed = 0


def check(name, cond):
    global failed
    print(f"  {'PASS' if cond else 'FAIL'} {name}")
    if not cond:
        failed += 1


def _write_matrix(tmp, coder_model):
    path = os.path.join(tmp, "matrix.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "default_8gb_v1": {
                "duo_coder_ctx_agentic": 16384,
                "duo_coder_ctx_normal": 8192,
                "model_overrides": {
                    "duo_coder": {"model": coder_model,
                                  "thinking": True,
                                  "thinking_budget": 4000,
                                  "temperature": 0.11},
                },
            },
        }, f)
    return path


DEFAULT_CODER = DEFAULT_AGENT_CFG["duo_coder"]["model"]
POLICY_CODER = "lfm2.5:2.6b" if DEFAULT_CODER != "lfm2.5:2.6b" else "qwen3.5:2b"

with tempfile.TemporaryDirectory() as tmp:
    matrix = _write_matrix(tmp, POLICY_CODER)

    # 1. Untouched card at default → policy model applies (legacy behaviour).
    s = {"safe_profile_policy": "default_8gb_v1",
         "safe_profile_matrix_file": matrix,
         "agents": {"duo_coder": {"model": DEFAULT_CODER}}}
    apply_safe_profile_policy(s, None)
    check("1a untouched default -> policy model",
          s["agents"]["duo_coder"]["model"] == POLICY_CODER)

    # 2. Explicit user choice == shipped default -> user choice survives.
    s = {"safe_profile_policy": "default_8gb_v1",
         "safe_profile_matrix_file": matrix,
         "agents_user_model_choice": {"duo_coder": DEFAULT_CODER},
         "agents": {"duo_coder": {"model": DEFAULT_CODER}}}
    apply_safe_profile_policy(s, None)
    check("2a explicit default choice survives", s["agents"]["duo_coder"]["model"] == DEFAULT_CODER)

    # 3. Legacy install: non-default card model without marker is backfilled
    #    as a user choice and survives (old CARDS-WIN path, now via marker).
    other = "qwen3.5:9b-ud" if DEFAULT_CODER != "qwen3.5:9b-ud" else "qwen3.5:2b"
    s = {"safe_profile_policy": "default_8gb_v1",
         "safe_profile_matrix_file": matrix,
         "agents": {"duo_coder": {"model": other}}}
    apply_safe_profile_policy(s, None)
    check("3a non-default choice survives", s["agents"]["duo_coder"]["model"] == other)
    check("3b choice backfilled into marker", s.get("agents_user_model_choice", {}).get("duo_coder") == other)

    # 4. Marker for another role does not protect duo_coder.
    s = {"safe_profile_policy": "default_8gb_v1",
         "safe_profile_matrix_file": matrix,
         "agents_user_model_choice": {"duo_critic": DEFAULT_CODER},
         "agents": {"duo_coder": {"model": DEFAULT_CODER}}}
    apply_safe_profile_policy(s, None)
    check("4a foreign marker ignored",
          s["agents"]["duo_coder"]["model"] == POLICY_CODER)

    # 5. Malformed marker must not crash (an exception here fails the suite).
    s = copy.deepcopy({"safe_profile_policy": "default_8gb_v1",
                       "safe_profile_matrix_file": matrix,
                       "agents": {"duo_coder": {"model": DEFAULT_CODER}}})
    s["agents_user_model_choice"] = "garbage"
    apply_safe_profile_policy(s, None)
    check("5a malformed marker tolerated",
          s["agents"]["duo_coder"]["model"] == POLICY_CODER)

    # 6. Policy never pushes ctx defaults (USER-OVER-POLICY, 2026-09-10).
    s = {"safe_profile_policy": "default_8gb_v1",
         "safe_profile_matrix_file": matrix,
         "agents": {"duo_coder": {"model": DEFAULT_CODER}}}
    apply_safe_profile_policy(s, None)
    check("6a no ctx default pushed (agentic)", "duo_coder_ctx_agentic" not in s)
    check("6b no ctx default pushed (normal)", "duo_coder_ctx_normal" not in s)

    # 7. Policy never pushes thinking flags onto cards.
    s = {"safe_profile_policy": "default_8gb_v1",
         "safe_profile_matrix_file": matrix,
         "agents": {"duo_coder": {"model": DEFAULT_CODER}}}
    apply_safe_profile_policy(s, None)
    check("7a no thinking pushed", "thinking" not in s["agents"]["duo_coder"])
    check("7b no thinking_budget pushed", "thinking_budget" not in s["agents"]["duo_coder"])

    # 8. Matrix temperature applies unless the user set the card temperature.
    s = {"safe_profile_policy": "default_8gb_v1",
         "safe_profile_matrix_file": matrix,
         "agents": {"duo_coder": {"model": DEFAULT_CODER}}}
    apply_safe_profile_policy(s, None)
    check("8a matrix temperature applies by default",
          s["agents"]["duo_coder"].get("temperature") == 0.11)
    s = {"safe_profile_policy": "default_8gb_v1",
         "safe_profile_matrix_file": matrix,
         "agents_user_sampling_choice": {"duo_coder": ["temperature"]},
         "agents": {"duo_coder": {"model": DEFAULT_CODER, "temperature": 0.77}}}
    apply_safe_profile_policy(s, None)
    check("8b user card temperature wins",
          s["agents"]["duo_coder"].get("temperature") == 0.77)

print("=" * 50)
print(f"{failed} check(s) failed" if failed else "all checks passed")
sys.exit(0 if failed == 0 else 1)
