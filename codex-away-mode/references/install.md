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

The official `lark-cli` is the preferred Feishu dependency. `setup feishu` runs an auth command preflight first. If user OAuth is not complete, it returns a Feishu authorization URL and device code instead of pretending setup is finished. If OAuth completes but no user open_id is available, do not guess IDs; use the documented user-message binding fallback only after live preflight.

After install, tell the user to trust the new hooks:

- Codex Desktop: Settings -> Hooks
- Codex CLI fallback: `/hooks`

Then run diagnostics:

```bash
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode doctor --json
${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode doctor --e2e-notify --json
```

`notify test` only proves a basic Feishu send. `doctor --e2e-notify --json` proves the notification-delivery path, but it runs from the CLI and does not pass through Codex Desktop's Hook trust gate. Installation is complete only after `doctor --json` sees both notification delivery and the current Codex Desktop Hook trust state for the managed Stop and UserPromptSubmit hooks. If `doctor --json` reports `hook_trust_disabled` or `hook_trust_missing`, open Codex Desktop Settings -> Hooks, trust the Codex Away Mode hooks, then run `doctor --json` again.

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
