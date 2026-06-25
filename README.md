# Codex Away Mode

中文别名暂定：Codex 离开模式。

`Codex Away Mode` lets Codex send Feishu completion notifications and keep a live Codex turn waiting for a short Feishu card reply while the user is away.

The installable Skill package is `codex-away-mode/`.

## Quick Start

Ask Codex to read this repository and install Codex Away Mode. The agent should run the bundled installer, explain global writes, guide Feishu authorization, ask the user to trust Hooks in Codex Desktop 设置 -> 钩子 (English UI: Settings -> Hooks), verify notification delivery, then run `doctor --json` to confirm the current Codex Desktop Hook trust state.

```bash
./codex-away-mode/scripts/codex-away-mode install --dry-run --json
./codex-away-mode/scripts/codex-away-mode install --yes --json
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode setup feishu --json
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode doctor --e2e-notify --json
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode doctor --json
```

The installer pins the Feishu CLI dependency to a verified private npm install under
`${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/npm`; it does not rely on a global
`lark-cli` or a moving `latest` package. If Feishu app config has not been initialized,
`setup feishu` starts the official browser-confirmed app setup flow for the user. The
agent should ask the user to confirm in the browser, then rerun `setup feishu --json`;
do not make non-technical users run `lark-cli` commands manually.

If Feishu user OAuth is needed, `setup feishu` opens or returns one authorization
URL and stores the pending authorization state locally. After the user confirms in
the browser, rerun `setup feishu --json`; do not ask the user to copy a device code
or run `lark-cli` manually. Only use `setup feishu --restart-auth --json` when the
current authorization has expired or the user explicitly wants to restart it.

Read the Skill first:

- `codex-away-mode/SKILL.md`
- `codex-away-mode/references/install.md`
- `codex-away-mode/references/usage.md`
- `codex-away-mode/references/troubleshooting.md`
- `codex-away-mode/references/privacy.md`

## What It Does

- Sends completion cards from summaries staged through `codex-away-mode notify stage-summary`.
- Sends approval reminder cards when Codex raises a `PermissionRequest`; the user still approves or rejects in Codex Desktop.
- Protects against cross-project notifications by routing staged summaries through the Codex session/thread id first, with cwd hash only as a fallback.
- Keeps Codex Away Mode runtime files out of project workspaces.
- Sends a no-summary fallback only for user-triggered sessions whose transcript does not show an active goal.
- Supports `all`, `off`, and `snooze` notification modes.
- Starts user-triggered Away Mode waits that accept only replies to the corresponding Feishu card.
- Handles `/延长等待`, `/状态`, `/结束等待`, timeout cards, ordinary-private-chat hints, and `Get` reaction acknowledgement.
- Provides local troubleshooting guidance for Hook trust, Feishu authorization, transport errors, and stale Away Mode sessions.

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
