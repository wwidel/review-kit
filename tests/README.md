# Tests

```bash
python3 -m pytest tests -q
uv run --no-project --python 3.10 --with pytest python -m pytest tests -q   # the oldest supported
```

Every test runs against its own temporary git repository and its own temporary `HOME`, with
no global or system git config. Nothing here reads or writes your real `~/.review-kit` or
`~/.claude`. The fixtures are in `conftest.py`.

## What these tests cannot check

The access-log hook depends on what Claude Code sends it and on where Claude Code runs it.
The tests feed it hand-built payloads, so they pass even if Claude Code changes either. That
is what the live check below is for.

## Live check before a release

About five minutes, in a fresh Claude Code session:

1. Make a scratch git repository with a `src/` directory and a file in it, and install the kit
   for it: `python3 <review-kit>/bin/install.py --repo <scratch>`. If the session was already
   running when the hook was installed, open `/hooks` once or restart the session.
2. Start a session in the scratch repository's root and ask it to spawn one subagent that runs
   `cd src`, then reads the file there with the Read tool and with `cat`.
3. Run `python3 <review-kit>/bin/review.py audit --repo . --access-log "$(python3
   <review-kit>/bin/review.py store --repo .)/access.jsonl" --list-agents`. The subagent's id
   must be listed, with the file among the paths it read.
4. Write a findings file declaring that file in `scope.files_read`, and run `audit` with
   `--findings <file> --agent <id> --since <a time just before step 2>`. It must exit 0.

If step 3 lists no subagent id, or the file is missing, the hook is not seeing what the kit
assumes it sees, and `audit` cannot be trusted until that is understood.
