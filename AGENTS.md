# AGENTS.md

`devlease` is a Python package with no runtime dependencies (standard library only, Python 3.11+).

- `db.py` the SQLite schema and the machine-wide limits; `pool.py` the rules (matching, FIFO,
  limits, expiry, creation requests); `backends.py` the real `simctl`/`avdmanager`/`emulator`
  calls and a `Fake` backend; `hook.py` the PreToolUse check; `server.py` and `page.py` the web
  service; `system.py` memory and the cache whitelist; `cli.py` the commands.
- Tests run the real CLI, hook and web service against a fake device list
  (`DEVLEASE_FAKE_FILE`) in a temp directory: `PYTHONPATH=src python3 -m unittest discover -s tests`.
  A change to behaviour starts with a test there.
- Every write goes through `db.tx` (`BEGIN IMMEDIATE`), so two agents never interleave.
- The web page must never accept a path or a command from the browser: caches are a fixed
  whitelist in `system.CACHES`, and POSTs need the `X-Devlease` header and a local `Host`.
- The skill agents read is `skills/devlease/SKILL.md`; update it with any change to commands,
  exit codes or limits.
- Commit messages follow Conventional Commits. Text in this repository is English.
