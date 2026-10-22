"""Decider unload on off (2026-10-22, user request: off soll RAM freigeben).

routing_decider=off must free the loaded encoder model immediately (not
at the next engine restart): react_to_routing_settings unloads the
backend singleton; EncoderBackend.unload clears the model under the
lock. Also: a model CHANGE swaps the backend (the old model is GC'd).
No model download, no GPU - the encoder is faked on the instance."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import routing.decider as D
from routing.decider import EncoderBackend, Decision

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def main():
    # Fake encoder: never touches gliner2/torch - decide returns a canned
    # Decision so "load" is simulated by _model being set.
    class _FakeCore:
        def decide(self, text, want_planner=True):
            return "direct", 0.9, {"inference_ms": 1.0}

    enc = EncoderBackend("fake/test-model")
    enc._model = _FakeCore()
    D._BACKEND = enc

    # react with decider ON: nothing unloads
    D.react_to_routing_settings({"routing_decider": "on"})
    check("react with decider on keeps the model", enc._model is not None)

    # react with decider OFF: model released, backend reset
    D.react_to_routing_settings({"routing_decider": "off"})
    check("react with decider off unloads the model", enc._model is None)
    check("backend singleton reset", D._BACKEND is None)

    # next decide with decider off: skipped entirely (no model needed)
    d = D.decide_from_signals({"routing_decider": "off",
                               "routing_decider_threshold": 0.55},
                              "what to do tomorrow", encoder_decision=None)
    check("decider off: rules decide without encoder", d.source == "rule")

    # model change: backend() swaps the instance (old model freed by GC)
    b1 = D.backend("model-a")
    D._BACKEND = b1
    b2 = D.backend("model-b")
    check("model change swaps the backend instance", b1 is not b2)

    # restore: the real live model name for other suites
    D._BACKEND = None

    print()
    print(f"SUMMARY decider_unload: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


main()
