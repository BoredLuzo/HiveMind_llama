# HiveMind 1.4.3

One real hang, found on a ThinkPad and fixed properly.

## write_file hung for eleven minutes

The coder called `write_file`, the UI said "Processing context...", and nothing
happened: `llama-server` idle, Python at 100 % CPU, the file never appeared.

Cause: the atomic write used `tempfile.mkstemp(dir=<target folder>)`. When the
process may not write into that folder, Windows still reports it as writable
(`os.access` checks the read-only attribute, not the ACL), so CPython treats
`PermissionError` as a name collision and retries - up to `TMP_MAX`, which is
2**31-1 on Windows. Not slow: endless.

Now:

- all seven atomic write paths create their temp file with a bounded
  `O_CREAT|O_EXCL` attempt (16 names max), and a real permission error is raised
  on the first try,
- `write_file` catches it and returns `WRITE_FILE_PERMISSION` to the model with
  the cause (sandbox/ACL) and what to do about it, instead of spinning,
- the STT temp WAV had the same trap and uses the same bounded creation.

Measured after the fix: denied folder -> `PermissionError` in **0.003 s**,
writable folder -> unchanged. Regression suite `write_hang_bounded` pins it.

## Why the folder was locked at all

The engine had been started from inside an agent terminal that runs in a write
sandbox covering only the HiveMind folder. Start HiveMind by double-clicking
`start_hivemind.bat` so it runs with your normal user rights.

## Upgrade

Run `update.bat`. It backs up the code first; `settings.json`, models, sessions and
logs stay untouched.
