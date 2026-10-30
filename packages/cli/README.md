# designaround (CLI + local engine)

<!-- mcp-name: io.github.elidot007/designaround -->

```bash
uv tool install designaround        # or: pipx install designaround
designaround onboard                # trial key, stored in the OS keychain
designaround skill install claude-code   # or: cursor, codex, gemini-cli, opencode, openclaw, kiro, all
designaround config mode host       # inside an agent host; or platform / byok
```

Commands print one JSON envelope with `--json` (default when stdout is not a terminal). Exit codes: 0 ok, 2 needs a person, 3 budget or plan, 4 auth, 5 invalid input, 6 error.

See the repository README for the full workflow.
