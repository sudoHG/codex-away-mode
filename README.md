# Codex Away Mode

中文别名暂定：Codex 离开模式。

`Codex Away Mode` lets Codex send Feishu completion notifications and keep a live Codex turn waiting for a short Feishu card reply while the user is away.

The installable Skill package is `codex-away-mode/`.

## Quick Start

Ask Codex to read this repository and install Codex Away Mode. The agent should run the bundled installer, explain global writes, guide Feishu authorization, ask the user to trust Hooks, verify notification delivery, then run `doctor --json` to confirm the current Codex Desktop Hook trust state.

```bash
./codex-away-mode/scripts/codex-away-mode install --dry-run --json
./codex-away-mode/scripts/codex-away-mode install --yes --json
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode setup feishu --json
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode doctor --e2e-notify --json
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode doctor --json
```

Read the Skill first:

- `codex-away-mode/SKILL.md`
- `codex-away-mode/references/install.md`
- `codex-away-mode/references/usage.md`

## What It Does

- Sends completion cards from summaries staged through `codex-away-mode notify stage-summary`.
- Sends approval reminder cards when Codex raises a `PermissionRequest`; the user still approves or rejects in Codex Desktop.
- Protects against cross-project notifications by storing staged summaries in the central runtime store keyed by cwd hash.
- Keeps Codex Away Mode runtime files out of project workspaces.
- Sends a no-summary fallback only for fresh user turns whose transcript does not show an active goal.
- Supports `all`, `off`, and `snooze` notification modes.
- Starts user-triggered Away Mode waits that accept only replies to the corresponding Feishu card.
- Handles `/延长等待`, `/状态`, `/结束等待`, timeout cards, ordinary-private-chat hints, and `Get` reaction acknowledgement.

## Public Package Layout

```text
codex-away-mode/                    Installable Skill package.
codex-away-mode/SKILL.md            Agent-facing entrypoint.
codex-away-mode/references/         Install and usage references.
codex-away-mode/scripts/            Bundled Python runtime and CLI.
tests/                              Local automated tests.
```

## Boundaries

This repository is the development workspace. Future open-source publishing should export only public files such as `README.md`, `codex-away-mode/`, tests, and runtime code. Private planning notes, local evidence, personal configuration, and sensitive identifiers must stay out of Git history.

Live Feishu authorization, Codex Hook trust, and route probes can require user confirmation. Automated tests use fake transports and must not be treated as proof of live Feishu delivery. `doctor --e2e-notify` is a CLI delivery probe; current Hook trust is reported by `doctor --json` from Codex Desktop's Hook state. Historical Stop hook invocation records are diagnostic only. PermissionRequest notifications are reminders only; this project does not approve or reject Codex operations from Feishu.
