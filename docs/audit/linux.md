# Audit — Linux (sandbox + installation)

The direct question first: **Windows job objects have no Linux twin in the current
code.** What exists on Linux, what is missing, and what changed in this audit.

---

## 1. Sandbox: what actually runs on Linux

| Mechanism | Windows | Linux (before) | Linux (now) |
|---|---|---|---|
| Own process group | — (job object) | `start_new_session=True` | same |
| Kill the whole tree | job object + `taskkill /T` | `killpg(SIGKILL)` | same |
| Memory cap per tool | job object `JOB_MEMORY` (default 4 GB) | **none** | opt-in `RLIMIT_AS` (`duo_tool_sandbox_max_mem_mb`) |
| Process-count cap per tool | job object `ACTIVE_PROCESS` (default 64) | **none** | opt-in `RLIMIT_NPROC` (`duo_tool_sandbox_max_procs`) |
| CPU cap per tool | — | **none** | opt-in `RLIMIT_CPU` (`duo_tool_sandbox_max_cpu_s`) |
| Kill on parent death | `KILL_ON_JOB_CLOSE` | systemd `KillMode=control-group` | same |
| Service-wide ceiling | — | `MemoryMax=12G`, `TasksMax=64` | same |

So before this audit a runaway `run_python` on Linux could eat memory until the
service-wide `MemoryMax` tripped - and systemd then kills the **whole cgroup**,
i.e. HiveMind itself, not just the tool child. On Windows the same tool only loses
its own job.

`tools/sandbox.py` now applies `RLIMIT_AS` / `RLIMIT_NPROC` / `RLIMIT_CPU` in a
`preexec_fn`, using **the same settings keys** as the Windows job object, so one
value configures both platforms. Every limit is **0 = unset**, so the default
behaviour is unchanged on every platform (verified on Windows: `spawn_kwargs()` is
still `{}`). The `preexec_fn` only calls `resource.setrlimit` and swallows
everything: worst case a limit is not applied, never a broken spawn.

Honest limitation: `preexec_fn` runs between fork and exec, which Python documents
as unsafe in threaded processes. Uvicorn is threaded. The function is deliberately
tiny and allocation-free for that reason; the alternative (`prlimit` wrapper,
cgroups v2, or `systemd-run --scope`) is more robust but adds a dependency or a
layer per spawn. Documented as a trade-off, not hidden.

## 2. Installation: what was wrong

1. **`cp -r . /opt/hivemind` copied user state.** Running the installer from a
   developer checkout dragged `settings.json` (with the local workspace path),
   `sessions/`, `logs/`, `stt/` and `__pycache__` into the install.
   Fixed: `rsync -a --delete` with explicit excludes (tar fallback if rsync is
   missing).
2. **The voice downloads sat under the `[5/8] llama.cpp` heading** and ran before
   the llama.cpp fetch, so the step numbering lied.
   Fixed: llama.cpp first, then its own `[5b/8] Voice (optional)` block.
3. **The TTS pre-warm cached PyTorch as root.** `uvx` ran as root during the
   install, i.e. into `/root/.cache/uv`, while the service runs as `hivemind` -
   the first real speech request would have downloaded the ~200 MB again.
   Fixed: the pre-warm runs with `HOME`/`UV_CACHE_DIR`/`HF_HOME` pointing at
   `/opt/hivemind` (and step [6/8] chowns the tree afterwards).
4. **No audio deps check.** With the `uv` path `uv sync --all-extras` installs the
   `voice` extra (`soundfile`, `numpy`); the legacy `pip -r requirements.txt` path
   does not (the comment there even says "install later"). If the import fails the
   installer now installs `requirements-voice.txt` before downloading models.
5. **The gated TTS model is unreachable for the service user.** `hivemind` is a
   nologin system user, so `hf auth login` cannot run for it.
   Fixed: the installer prints the exact fix (`Environment=HF_TOKEN=…` in the
   unit), and the unit carries a commented line for it.

## 3. systemd unit: what was missing

`deploy/hivemind.service` had no `HOME`. The TTS sidecar shells out to `uvx`,
which needs a writable cache directory; without `HOME` that is undefined for a
system user. Added:

```
Environment=HOME=/opt/hivemind
Environment=UV_CACHE_DIR=/opt/hivemind/.cache/uv
Environment=HF_HOME=/opt/hivemind/.cache/huggingface
#Environment=HF_TOKEN=hf_...        # only needed for text to speech
```

`TasksMax=64` happens to match the Windows `ACTIVE_PROCESS` default - good, worth
keeping in sync deliberately rather than by accident.

## 4. Verified / not verified

- `bash -n deploy/install_linux.sh` clean (no `shellcheck` on this machine).
- `tools/sandbox.py` imports and behaves identically on Windows (empty kwargs).
- **Not executed on a real Linux host** from this machine - the installer changes
  are syntax-checked and reviewed, not run. Treat the `[5b/8]` block as
  "reviewed, not yet exercised"; the first real Linux install should watch
  `journalctl -u hivemind` for the voice lines.

## 5. Open items

1. Per-tool rlimits are **opt-in and off**. Default-on would be safer, but a wrong
   `RLIMIT_AS` breaks node/chrome-style children; the honest default is 0, with the
   docs naming the keys.
2. TTS on Linux needs `HF_TOKEN` in the unit (no way around a gated model).
3. `MemoryMax=12G` is a guess for "model + torch + systemd". With a 30B model it is
   too small, with speech it is comfortable. Consider naming it in the docs as
   "raise me".
4. The installer has no `--help`; unknown flags are silently ignored.
