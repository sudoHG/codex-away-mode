# Privacy and Local Data

Codex Away Mode is a local Skill. It does not provide a hosted service, and it does not proxy Codex approvals through Feishu.

## Local Files

The installer writes the minimum Codex integration files needed for global use:

- `${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}`: Skill runtime, wrapper, configuration, install state, and local dependency cache.
- `${CODEX_HOME:-$HOME/.codex}`: Codex-managed Hook and Skill discovery entries.
- `${TMPDIR}/codex-away-mode/state.sqlite` by default: short-lived runtime state such as Away Mode sessions, windows, processed message ids, prompt markers, staged summaries, waiter locks, and diagnostics.

Codex Away Mode must not create `.codex-away-mode/`, `latest-summary.md`, SQLite files, marker files, or other runtime files inside user project workspaces.

## Feishu Data

The local configuration may store the Feishu app binding and recipient identifiers needed to send messages, such as app id, app name, open id, and chat id. Secrets and OAuth material are handled by the local Feishu CLI configuration flow and should never be committed.

Runtime state may temporarily contain Feishu message ids, card ids, chat ids, hashes of message text, and staged completion summaries. It is stored locally and is used for routing, duplicate protection, and diagnostics.

## Completion Summaries

Completion summaries are staged through:

```bash
codex-away-mode notify stage-summary --cwd "$PWD" --session-id "${CODEX_THREAD_ID:-}" --json
```

The summary body is passed on stdin and stored in the central runtime store keyed first by a hash of the Codex session/thread id, with normalized cwd hash only as a fallback when no session id is available. Routine Feishu completion cards do not display the raw workspace cwd.

## Approval Reminders

PermissionRequest cards are reminders only. Feishu cannot approve or reject Codex operations in the MVP architecture. The user must return to Codex Desktop to approve or reject the operation. Approval urgent diagnostics must not store raw Feishu user ids; invalid urgent recipients are recorded only as counts and short hashes.

## What Not To Publish

Do not publish:

- Feishu app secrets, OAuth tokens, open ids, chat ids, tenant ids, or user ids.
- Personal project paths, private validation logs, or real card/message ids.
- Local install state, runtime SQLite databases, or generated diagnostics.
