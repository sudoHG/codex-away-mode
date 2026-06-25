---
name: codex-away-mode
description: Keep a live Codex turn reachable while the user is away, with Feishu completion notifications, approval-request reminders, Away Mode checkpoint cards, and short-window Feishu replies that continue the current Codex turn. Use when the user asks for Codex Away Mode, Feishu completion notifications, Codex approval reminders, waiting for Feishu replies before continuing, or remote continuation while away from the computer.
---

# Codex Away Mode

## Overview

Use this skill to keep a live Codex turn reachable while the user is away. Route Codex completion notifications, approval reminders, and short continuation checkpoints through Feishu. Keep user-facing messages concise, Chinese by default, and explicit about what changed, what was verified, what remains unverified, and whether user action is needed.

## Operating Boundaries

- Use this skill only when Codex Away Mode, Feishu notification, or reply-based continuation is part of the task.
- Do not expose private Open IDs, chat IDs, tokens, project paths, or internal group details in user-visible or publishable output.
- Prefer configured local tools or scripts over hard-coded personal values.
- If Feishu tools are unavailable or not configured, report the missing dependency instead of inventing delivery.
- For Away Mode, keep waiting quiet: do not send heartbeat or waiting-status chat updates while `away wait` is polling. Only send user-visible updates when a routed card reply arrives, the wait ends, times out, errors, or requires user action.
- When a routed card reply arrives, write a concise Codex chat note that includes the received Feishu text and the action or answer you are about to give. After completing the reply work, write the result or answer in the Codex chat before resuming, so the desktop thread preserves an audit trail.
- Away Mode is for the current live Codex turn. Do not claim it can wake or inject a closed historical Codex thread.

## Setup

For installation, permissions, hook trust, and verification, read `references/install.md`. For user-facing failures, read `references/troubleshooting.md`. For local data boundaries, read `references/privacy.md`. Treat `install --yes` as a setup step, not completion. The installer manages a pinned private `@larksuite/cli` dependency; do not default to global or latest `lark-cli` during setup. Feishu binding must be verified with `setup feishu`. If Feishu app config or user OAuth is pending, tell the user to confirm in the browser and then rerun `setup feishu --json`; do not ask them to run `lark-cli` commands, copy device codes, or use `--device-code` on the main path. Use `setup feishu --restart-auth --json` only when authorization expired or the user explicitly wants a fresh authorization. `doctor --e2e-notify --json` verifies notification delivery only; it does not prove Codex Desktop trusted the Hook. Current Hook trust is checked by `doctor --json` against Codex Desktop's Hook state. Historical Hook execution records are diagnostic only and must not be treated as proof that the Hook is still trusted.

## Completion Notifications

If the local hook contract is installed, stage final-turn summaries through the CLI:

```bash
"${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode" notify stage-summary --cwd "$PWD" --session-id "${CODEX_THREAD_ID:-}" --json
```

Pass the summary markdown on stdin. Do not create `.codex-away-mode/`, `latest-summary.md`, prompt markers, state databases, or any other Codex Away Mode runtime files under the current workspace cwd. The Stop hook reads the staged summary from the central runtime store by session/thread route first, with `cwd_hash` only as a fallback when no session id is available. If the summary is missing, the Stop hook sends a no-summary fallback only when the session has a user prompt marker and the current transcript does not show an active goal. For in-progress goal-mode continuation turns, do not stage a completion summary until the goal is complete, blocked, or needs human attention; active goal turns must not create completion or fallback noise.

## Approval Reminders

The installer manages a `PermissionRequest` hook. When Codex asks the user to approve an operation, the hook may send a Feishu reminder card. This is only a reminder: the user must still approve or reject in Codex Desktop. Do not tell users they can approve Codex operations in Feishu.

## Away Mode Workflow

For CLI examples and operational behavior, read `references/usage.md`.

Use Away Mode only for a live Codex turn where the user explicitly asks for it. Do not infer Away Mode from ordinary work.

When the user asks to start Away Mode or says they are leaving the computer, use the quick-start path. Do not run doctor, route probes, or status-driven resume first. Start a new Away Session for the current Codex turn.

Never resume an Away Session discovered from `away status`. Resume only after this same Codex turn receives an Away Mode reply result with `keep_waiting=true` and `resume_token`.

If the routed reply is a natural-language request to extend the current Away Mode wait, convert the requested duration to minutes and pass `--extend-minutes <minutes>` on the next `away resume` call. Do not inspect or modify Away Mode SQLite/StateStore directly.

Route probes are installation/diagnostic tools only. Run them only when the user explicitly asks to verify routing or during setup. Do not consume ordinary private-chat text as a Codex prompt.

## Implementation Notes

- Keep deterministic Feishu transport logic in scripts or a CLI package.
- Keep installation and usage details in `references/`.
- Keep `SKILL.md` focused on when and how to use the capability.
- Validate the skill structure after changing metadata or bundled resources.
