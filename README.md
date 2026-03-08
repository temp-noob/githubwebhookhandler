## Github Webhook server

The webhook expects GitHub JSON payloads.

CI is triggered when someone comments `runci` on a pull request (`issue_comment` event, case-insensitive, surrounding spaces allowed).
The server checks out that PR HEAD, reads `ci.json` from the repo, then runs:
- steps (top-level keys) sequentially
- commands within each step in parallel

```json
{
  "test": ["cmd1", "cmd2"],
  "lint": ["cmd3", "cmd4"]
}
```
