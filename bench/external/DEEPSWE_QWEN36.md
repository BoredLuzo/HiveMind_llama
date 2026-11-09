# DeepSWE: Qwen3.6-35B-A3B + HiveMind sharp template

DeepSWE is the Datacurve long-horizon agent benchmark: 113 tasks across
TypeScript, Go, Python, JavaScript and Rust, run through Pier/Harbor with
isolated task containers. This directory contains two different things:

1. **`deepswe_qwen36.sh` / `deepswe_qwen36_colab*.ipynb`** — a model/template
   baseline using DeepSWE's own `mini-swe-agent`. It does **not** install or
   run HiveMind.
2. **`deepswe_hivemind.py`** — the actual HiveMind harness runner: HiveMind
   `/stream` edits a workspace, then the official DeepSWE verifier grades the
   generated patch in the task's verifier container.

Use the second one when the claim is about **HiveMind**, not about the model.

## Actual HiveMind run

Prerequisites:

- HiveMind engine running (`http://127.0.0.1:8001`) with the
  `qwen3.6:35b-a3b-ud` profile and the sharp template,
- Docker (for the official DeepSWE verifier image) — or a Modal account with
  `pip install modal` when using `--verifier modal` (no local Docker needed,
  e.g. on Colab),
- git and Python 3.11+,
- the DeepSWE tasks checkout (`git clone https://github.com/datacurve-ai/deep-swe`).

```bash
python bench/external/deepswe_hivemind.py \
  --tasks /path/to/deep-swe/tasks \
  --hivemind http://127.0.0.1:8001 \
  --model qwen3.6:35b-a3b-ud \
  --n-tasks 5 --sample-seed 0 \
  --timeout 10800 \
  --out bench/results/deepswe-hivemind-5
```

The runner:

1. clones the task repo at `base_commit_hash`,
2. sends `instruction.md` to HiveMind in `code_duo` / agentic mode,
3. stages the workspace and writes `model.patch`,
4. builds `tests/Dockerfile` and runs `/tests/test.sh` in the pristine
   verifier image (locally via Docker, or on Modal with `--verifier modal`),
5. records `reward.json`, patch, stop reason, tokens and time.

`--dry-run` only lists the selected tasks/repos/images. `--no-verify` skips
verification entirely and only checks that HiveMind produced a patch.

This is a HiveMind harness benchmark, but the agent runs on the host
workspace, not inside the task container. The hidden verifier is isolated;
document that limitation in any published result.

### Verifier on Modal (Colab A100, no local Docker)

Where local Docker is unreliable (Colab, some cloud notebooks), offload only
the grading step to Modal with `--verifier modal`:

```bash
pip install modal   # plus a token: `modal token new`, or the
                    # MODAL_TOKEN_ID / MODAL_TOKEN_SECRET Colab secrets
python bench/external/deepswe_hivemind.py \
  --tasks /path/to/deep-swe/tasks \
  --hivemind http://127.0.0.1:8001 \
  --model qwen3.6:35b-a3b-ud \
  --n-tasks 5 --sample-seed 0 \
  --verifier modal \
  --timeout 10800 \
  --out bench/results/deepswe-hivemind-5-modal
```

With `--verifier modal`:

- a Modal image is built from the task's `tests/Dockerfile`
  (`modal.Image.from_dockerfile(..., context_dir=tests_dir)`),
- an ephemeral Modal volume mounted at `/logs` is seeded with
  `artifacts/model.patch`, `/tests/test.sh` runs inside that image, and
  `/logs/verifier/reward.json` / `reward.txt` are read back into the local
  logs directory (same `{ok, reward, exit_code, output}` shape as Docker),
- **HiveMind itself still runs on the notebook/host process** and edits the
  workspace over `/stream`; only the hidden verifier is remote, so the
  engine must stay reachable at `--hivemind` from where you run the script.

Limitations: the first Modal image build per task takes minutes and consumes
Modal credits (the free plan includes $30/month); verifier grading runs on
Modal's infra instead of the exact Pier runner environment. Results are
otherwise recorded identically to the Docker path.


## Model/template baseline (not HiveMind)

- Model: official `unsloth/Qwen3.6-35B-A3B-GGUF`, `UD-Q4_K_XL` (~20 GB).
- Template: HiveMind's bundled
  `model_configs/chat_template_sharp_v22.5.0.jinja`, pinned from commit
  `16fdd71`.
- Sampling: temperature 1.0, top_p 0.95, top_k 20 (the model card default).
- Harness: `datacurve-pier` + `mini-swe-agent`, 5 deterministic tasks
  (`--n-tasks 5 --sample-seed 0`). This does **not** run the HiveMind engine.

HiveMind's `qwen3.6:35b-a3b-ud` profile now also points at the sharp template,
so the installer and the baseline use the same chat format.

## Host requirements

| Host | Feasibility |
|---|---|
| A100 40 GB + Docker | recommended |
| L4 24 GB | only with a smaller context; 35B is tight |
| T4 16 GB | not viable |
| Colab | A100 runtime + Modal (`--env modal` baseline, `--verifier modal` HiveMind harness; Docker-in-Colab is unreliable) |

DeepSWE needs Docker for the local Pier runner. On Colab use
`--env modal`; the model endpoint must be publicly reachable, which the
notebook does with a temporary cloudflared tunnel. The HiveMind harness
(`deepswe_hivemind.py`) has its own Modal path for the same reason:
`--verifier modal` (see above) runs the verifier on Modal, so no local
Docker is needed on the notebook.

## Linux GPU host

```bash
bash bench/external/deepswe_qwen36.sh --all --env docker --tasks 5
```

The script:

1. downloads the UD-Q4_K_XL GGUF into `bench/.external/deepswe_qwen36/models`,
2. downloads the sharp template from the pinned HiveMind commit,
3. builds `llama-server` from llama.cpp `b12001` with CUDA,
4. starts it on `:8080` with the sharp template,
5. clones DeepSWE and installs Pier,
6. runs 5 tasks with `mini-swe-agent`.

For Docker on Linux, the model endpoint defaults to
`http://host.docker.internal:8080/v1`; if your Docker does not resolve that
name, pass `--model-base-url http://172.17.0.1:8080/v1` (or the host's LAN IP).

Stop the model server afterwards:

```bash
bash bench/external/deepswe_qwen36.sh --stop
```

## Colab

Open `deepswe_qwen36_colab.ipynb` in Colab:

1. Runtime → Change runtime type → **A100 GPU**, High-RAM.
2. Create a Modal token:
   - **Colab Secrets (recommended):** click the key icon in the left sidebar,
     add `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`, enable "Notebook access"
     for both. Get them from <https://modal.com/settings/tokens>.
   - **CLI:** run `!modal token new` once and follow the printed browser link.
     It writes `~/.modal.toml`; no Colab secret is needed then.
3. Run the cells. The notebook downloads/builds the model, starts
   `llama-server`, opens a cloudflared tunnel, and runs 5 DeepSWE tasks with
   `--env modal`.

Never paste the token into a cell or commit it. The notebook prints only the
first 8 characters of the token ID. `MODAL_TOKEN_*` is for the sandbox
provider; the model download is public and needs no HF token.

If the A100 run OOMs, lower `--ctx-size` from 16384 to 8192; do not use the T4
runtime for this model.

### HiveMind harness on Colab (Modal verifier)

The same token setup works for the actual HiveMind runner. In a notebook
cell: start the HiveMind engine with the `qwen3.6:35b-a3b-ud` profile (or
point `--hivemind` at a reachable engine), then run
`deepswe_hivemind.py` with `--verifier modal` (command above). HiveMind
still runs on the Colab host via `/stream`; only the verifier containers are
offloaded to Modal, so the notebook needs no Docker daemon. Use the
`MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` Colab secrets described above — never
paste the token into a cell.


## Honest reporting

- Report **5-task subset** results, model name, quant, template hash, Pier
  version, and `--sample-seed 0`.
- Do not compare the absolute score with the DeepSWE leaderboard: those runs
  use frontier API models and a different harness configuration.
- For a real comparison, run the same subset with Aider/Claude Code/Codex and
  report pass@1, wall time, token/cost, and the task IDs.
