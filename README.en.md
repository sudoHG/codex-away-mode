# Codex Away Mode

Chinese version: [README.md](README.md)

`Codex Away Mode` is a Skill for the Codex desktop app. It does not move Codex into Feishu or turn Feishu into a new remote Codex client. Instead, it extends your desktop Codex session while you are away from your computer: it sends Feishu completion notifications and approval reminders, and it can keep a short reply window open so your Feishu card reply is sent back to the current Codex session.

This project currently supports Codex only. It does not support Claude Code, Cursor, OpenCode, or other agent runtimes.

## What Problem It Solves

Codex tasks can run for a long time. When you are away from your computer, common problems include:

- A task finishes, but you do not know.
- Codex is waiting for a permission approval, but you do not see it.
- You want to keep directing Codex while away from your computer, but using ChatGPT mobile to connect to Codex can be slow and unstable.

Codex Away Mode focuses on reachability after you leave your computer.

It is not a remote-control service, and it is not a Codex client inside Feishu. It only works while the current Codex session is still alive.

## How It Differs From Similar Projects

Many similar projects treat IM as the new primary entry point: they start a command-line task through `codex exec`, or expose Codex as a remotely callable session through something like `codex-app-server`. That works well for command-like, automated, one-shot tasks, but it is less natural for creative and iterative work. The work can move away from the Codex desktop app, and when you return to your computer, it may be harder to continue the same Codex session with the original context, approvals, file changes, visual results, and discussion history.

The core idea of Codex Away Mode is: **the Codex desktop app remains the main workspace, and Feishu is only an extension layer for the time when you are away**.

- **The desktop app remains the main workspace**: tasks start, run, and finish in the Codex desktop app. Feishu tells you what happened, whether approval is needed, and whether you want to add one more instruction. When you return to your computer, you continue in the same Codex session inside the Codex desktop app.
- **It does not start a separate headless Codex task**: Away Mode does not turn Feishu messages into new `codex exec` runs. While the current Codex session is alive, it sends your reply to the corresponding Feishu card back into that same Codex session.
- **It is better suited to creative and long-running iterative work**: design, writing, product discussion, code review, and debugging often require context, approvals, file inspection, visual comparison, and follow-up discussion. Codex Away Mode is designed to keep the connection alive while you are away and keep the desktop workflow intact when you return.
- **It does not require a long-running remote service**: this tool does not require deploying a permanently online bot server, and it does not turn Feishu into a full remote control console. It mainly relies on Codex Hooks, a local CLI, and a reply window inside the current Codex session.
- **Its boundary is narrower, but steadier**: it currently supports only Codex and Feishu. Through Codex Hooks, the local CLI, and Feishu message APIs, it polls card replies during the Away Mode wait window and tries to route notifications and replies back to the corresponding Codex session.

If you want to operate a full remote Codex from IM, connect every agent to one chat entry point, or host Codex sessions on a long-running server, this project is not trying to be that. It is closer to an away mode for the Codex desktop app: you temporarily leave the desktop, but not the current work context.

## Screenshots

### Away Mode: continue the current session while away

<img src="assets/screenshots/away-mode-waiting.png" alt="Away Mode waiting card" width="720">

After you reply directly to the Feishu card, the reply goes back to the current Codex session. Codex handles it and sends a new progress card.

<img src="assets/screenshots/away-mode-reply.png" alt="Away Mode reply continuation" width="720">

### Approval reminders and completion notifications

When Codex is waiting for desktop approval, Feishu sends a reminder. You still approve or reject the request in the Codex desktop app.

<img src="assets/screenshots/approval-reminder.png" alt="Codex approval reminder card" width="720">

When a work round finishes, Codex sends a structured completion summary to Feishu.

<img src="assets/screenshots/completion-notification.png" alt="Codex completion notification card" width="720">

## Features

- **Completion notifications**: sends a summary to Feishu when a Codex work round finishes.
- **Away Mode**: when you leave your computer, Codex sends a Feishu waiting card. Your reply to that card enters the current Codex session.
- **Multi-window routing**: when multiple Codex windows or sessions are in Away Mode at the same time, replies are routed to the matching window as much as possible through Feishu cards.
- **Approval reminders**: when Codex needs you to approve an operation in the desktop app, Feishu sends a reminder.
- **Notification modes**: supports default notifications, turning notifications off, and temporary snooze.

## What It Does Not Support

- It does not let you approve Codex permission requests inside Feishu. You still need to approve or reject them in the Codex desktop app.
- It does not wake historical Codex sessions that have already ended.
- It does not support non-Codex agents.
- The installer tries to automate as much as possible, but Feishu authorization, app configuration, and Codex Hook trust may still require confirmation in the browser or in the Codex desktop app.

## Installation

The recommended installation flow is to give this repository link to Codex and ask Codex to read the repository and install Codex Away Mode for you.

If you use the `skills` CLI, you can install the Skill into Codex first:

```bash
npx skills add sudoHG/codex-away-mode --skill codex-away-mode -a codex -g
```

This only installs the Skill. Feishu authorization, Codex Hook trust, and end-to-end notification verification should still be completed by Codex through the flow below.

You can say:

```text
Please read this repository and install Codex Away Mode.
Before installing, explain which global files will be written.
When I need to confirm Feishu authorization or Codex Hook trust, tell me what to do.
After installation, run doctor to verify it.
```

The agent will usually run commands like:

```bash
./codex-away-mode/scripts/codex-away-mode install --dry-run --json
./codex-away-mode/scripts/codex-away-mode install --yes --json
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode setup feishu --json
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode doctor --e2e-notify --json
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode doctor --json
```

The installer uses the verified pinned version `@larksuite/cli@1.0.57`, installed by default under:

```text
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/npm
```

It does not depend on an existing global `lark-cli`, and it does not use the moving `latest` version by default.

## Where Installation Writes Files

The installer writes to these locations:

```text
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}
  Program files, config, install state, local dependency cache

${CODEX_HOME:-$HOME/.codex}
  Codex Skill discovery, managed AGENTS block, Hooks config

${TMPDIR}/codex-away-mode/state.sqlite
  Temporary runtime state, such as Away Mode sessions, card routing, dedupe records, and diagnostic events
```

## Important Post-Install Check

After installation, you need to trust the Hook in the Codex desktop app:

```text
Settings -> Hooks
```

If the Hook is not trusted, completion notifications, approval reminders, and Away Mode automation may not work.

You can check the current state with:

```bash
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode doctor --json
```

`doctor --e2e-notify --json` only proves that Feishu delivery works. The actual Hook trust state should be checked with `doctor --json`.

## Usage

### Completion Notifications

After installation, before a user-visible work round ends, Codex stages a completion summary. The Stop hook sends that summary to Feishu.

Agents submit completion summaries through `codex-away-mode notify stage-summary`.

Notifications are enabled by default. You can tell Codex:

```text
Snooze Feishu notifications for 2 hours
```

or:

```text
Turn off Feishu completion notifications
```

### Away Mode

When you are leaving your computer, you can tell Codex:

```text
Start Codex Away Mode for 30 minutes
I will be away from my computer for 3 hours
```

Codex sends a Feishu card. Reply directly to that card instead of sending a normal direct message.

Available commands:

```text
/延长等待
/状态
/结束等待
```

Natural language also works, for example:

```text
Extend the wait by 1 hour
```

### Approval Reminders

When Codex triggers a permission approval request, Feishu sends a reminder. The reminder only tells you that the desktop app is waiting for confirmation. It cannot approve the request for you.

You still need to return to the Codex desktop app to handle the approval.

## Agent Entry Points

For installation or troubleshooting, agents should read:

- `codex-away-mode/SKILL.md`
- `codex-away-mode/references/install.md`
- `codex-away-mode/references/usage.md`
- `codex-away-mode/references/troubleshooting.md`
- `codex-away-mode/references/privacy.md`

## Project Structure

```text
codex-away-mode/
  SKILL.md              Skill entry point
  agents/openai.yaml    Skill display metadata
  references/           Installation, usage, troubleshooting, and privacy docs
  scripts/              CLI and runtime code

tests/                  Automated tests
```

## Verification

Developers can run:

```bash
pytest -q
python3 -m compileall -q codex-away-mode/scripts/codex_away_mode tests
python3 /path/to/quick_validate.py codex-away-mode
```

Installer dry-run:

```bash
./codex-away-mode/scripts/codex-away-mode install --dry-run --json
```

Real Feishu authorization, Codex Hook trust, and Feishu card reply routing require manual confirmation. Unit tests alone cannot prove those paths.

## Platform Support and Feedback

This tool has only been tested on macOS so far. The primary verified environment is the Codex desktop app, Feishu, and the local `lark-cli`.

The author does not currently have a Windows machine, so installation reliability, Hook trust, Feishu authorization, Away Mode waiting, and reply routing on Windows have not been verified on real hardware.

If you run into a problem, please open an issue or email `by331works@gmail.com`. Helpful feedback should include:

- Operating system and Codex version.
- Feishu / Lark environment.
- The step that failed.
- Output from `codex-away-mode doctor --json`.
- Whether the Hook has been trusted in the Codex desktop app.

## Privacy and Security

- Feishu app bindings, open ids, chat ids, and related values should only be stored in local config.
- OAuth tokens and app secrets are handled by the local Feishu CLI authorization flow and should not be committed to Git.
- Runtime state is stored by default at `${TMPDIR}/codex-away-mode/state.sqlite`.
- Regular Feishu notification cards do not show the original working directory.
- PermissionRequest cards are reminders only. They do not move Codex approval capability into Feishu.

See also:

- `codex-away-mode/references/privacy.md`
- `codex-away-mode/references/troubleshooting.md`

## License

MIT License.
