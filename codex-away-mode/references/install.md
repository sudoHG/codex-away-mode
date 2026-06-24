# Install Codex Away Mode

Use the bundled CLI bootstrap from the Skill package:

```bash
./codex-away-mode/scripts/codex-away-mode install --dry-run --json
```

Review the planned writes before applying them. The installer manages:

- `${CODEX_AWAY_HOME:-~/.codex-away-mode}/config.toml`
- `${CODEX_AWAY_HOME:-~/.codex-away-mode}/install-state.sqlite`
- `${CODEX_AWAY_HOME:-~/.codex-away-mode}/bin/codex-away-mode`
- `${CODEX_AWAY_HOME:-~/.codex-away-mode}/scripts/`
- `${CODEX_AWAY_HOME:-~/.codex-away-mode}/skill/`
- `${CODEX_AWAY_HOME:-~/.codex-away-mode}/npm/` for the pinned private `lark-cli`
- A managed block in `${CODEX_HOME:-~/.codex}/AGENTS.md`
- Managed commands in `${CODEX_HOME:-~/.codex}/hooks.json`
- A minimal Skill discovery entry in `${CODEX_HOME:-~/.codex}/skills/codex-away-mode`

Runtime data is intentionally not stored in the workspace. Away sessions, prompt markers, staged summaries, locks, message idempotency state, and diagnostics live in a central per-user temporary runtime store. The default location is `${TMPDIR}/codex-away-mode/state.sqlite`; `CODEX_AWAY_RUNTIME_DIR` may override it. Existing workspace `.codex-away-mode/` artifacts are legacy leftovers and should be treated as warnings, not as active state.

Apply only after the user confirms:

```bash
./codex-away-mode/scripts/codex-away-mode install --yes --json
```

After that, use the managed wrapper:

```bash
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode install status --json
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode setup feishu --json
```

The installer uses a verified private `@larksuite/cli@1.0.57` install under `${CODEX_AWAY_HOME:-~/.codex-away-mode}/npm/`. Do not use `lark-cli install`, `lark-cli update`, `npx @larksuite/cli@latest`, or a global `lark-cli` as the default path during setup. This keeps the Skill on a known command surface while still allowing advanced users to override `lark_cli_path` explicitly.

`setup feishu` runs a command preflight first. It checks Feishu app config with the pinned CLI. If app config is missing, `setup feishu` starts the official browser-confirmed app setup flow and returns `status=lark_app_config_browser_pending` with a `verification_url` and `browser_opened` flag. Tell the user:

```text
我已经打开飞书配置页面。请在浏览器里确认，完成后告诉我继续。
```

If `browser_opened=false`, show the returned `verification_url` and ask the user to click it. After the user confirms in the browser, rerun `setup feishu --json`. Do not make non-technical users run `lark-cli` commands manually; any returned `debug_command` is for advanced troubleshooting only.

If user OAuth is not complete, `setup feishu` opens or returns one Feishu authorization URL and stores the pending authorization state locally. Tell the user:

```text
我已经打开飞书授权页面。请在浏览器里确认授权，完成后告诉我继续。
```

When the user says they have confirmed, rerun `setup feishu --json`. Do not ask the user to copy a device code, do not run `setup feishu --device-code` on the main path, and do not restart authorization unless setup reports expiry or the user explicitly asks to restart. If restart is needed, use:

```bash
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode setup feishu --restart-auth --json
```

If OAuth completes but no user open_id is available, do not guess IDs; use the documented user-message binding fallback only after live preflight.

After install, tell the user to trust the new hooks:

- Codex Desktop 中文界面：设置 -> 钩子
- Codex Desktop English UI: Settings -> Hooks
- Codex CLI fallback: `/hooks`

Then run diagnostics:

```bash
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode doctor --json
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode doctor --e2e-notify --json
```

`notify test` only proves a basic Feishu send. `doctor --e2e-notify --json` proves the notification-delivery path, but it runs from the CLI and does not pass through Codex Desktop's Hook trust gate. Installation is complete only after `doctor --json` sees both notification delivery and the current Codex Desktop Hook trust state for the managed Stop, UserPromptSubmit, and PermissionRequest hooks. If `doctor --json` reports `hook_trust_disabled` or `hook_trust_missing`, ask the user to open Codex Desktop 设置 -> 钩子 (English UI: Settings -> Hooks), trust the Codex Away Mode hooks, then run `doctor --json` again.

The PermissionRequest hook sends approval reminder cards when Codex is waiting for the user to approve an operation. It does not approve or reject operations from Feishu. Tell users to return to Codex Desktop to handle the approval.

Run the route probe only when the user is ready to reply to a real Feishu test card:

```bash
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode doctor --route-probe --json
```

Probe outcomes:

- `route_probe` passed: multiple active Away Mode windows can be enabled.
- `route_probe_already_verified`: routing is already verified; no probe card is sent.
- `route_probe_inconclusive`: the user did not reply before timeout; keep existing routing config unchanged and rerun later.
- `route_probe_failed`: a user reply was observed without exact `reply_to`; use single-window fallback.

Live Feishu authorization, hook trust, and route probe replies may require user confirmation. Do not mark them verified unless they actually ran.
