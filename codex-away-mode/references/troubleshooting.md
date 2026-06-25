# Troubleshooting

## Doctor Says Hook Trust Is Pending

Run:

```bash
codex-away-mode doctor --json
```

`doctor --json` checks the current Codex Desktop Hook state. If it reports Hook trust as pending, open Codex Desktop settings and trust the managed hooks:

- Chinese UI: 设置 -> 钩子
- English UI: Settings -> Hooks

`doctor --e2e-notify --json` only proves that the CLI can send a Feishu test card. It does not prove Codex Desktop will run the Hook.

## `notify_delivery_unverified`

This means the package and local binding are present, but notification delivery has not been verified in this install state. Run:

```bash
codex-away-mode doctor --e2e-notify --json
codex-away-mode doctor --json
```

If the test card arrives but `doctor --json` still reports Hook trust pending, trust the Hooks in Codex Desktop.

## Feishu Authorization Loops

Use the guided setup path:

```bash
codex-away-mode setup feishu --json
```

If the command returns an authorization URL, open it in the browser and confirm authorization, then rerun the same setup command. Use `--restart-auth` only when the existing authorization has expired or the user explicitly wants to start over.

## HTTP 429 Or Feishu Transport Errors During Away Mode

Feishu may rate-limit the CLI temporarily. If `away resume` returns `feishu_transport_error`, retry the same `away resume` command with the same `resume_token` if the token is still available in the current Codex turn.

If a failed resume leaves an old Away Mode card visible and `away status --json` shows an active session with an expired waiter lease, inspect first:

```bash
codex-away-mode away cleanup --orphan-active --dry-run --json
```

If the listed session is an expected leftover from a failed resume, close it:

```bash
codex-away-mode away cleanup --orphan-active --json
```

Do not edit the runtime SQLite database directly.

## Ordinary Feishu Messages Do Not Reach Codex

Away Mode only accepts replies to the current Away Mode card. Ordinary private-chat messages receive a visible hint and are not treated as Codex prompts.

If multiple Codex turns are waiting at once, reply to the corresponding card for the turn you want to continue.

## Approval Reminders Cannot Approve Operations

PermissionRequest cards tell the user that Codex Desktop is waiting for approval. The user still must approve or reject inside Codex Desktop. This is intentional; the MVP does not run a persistent Feishu callback service.

