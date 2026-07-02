# Use Codex Away Mode

## Completion Notifications

A Codex agent should stage a short final-turn summary through the CLI:

```bash
CODEX_AWAY_CLI="${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode"
"$CODEX_AWAY_CLI" notify stage-summary --cwd "$PWD" --session-id "${CODEX_THREAD_ID:-}" --json <<'EOF'
**项目**
Demo

**完成**
本轮完成事项。
EOF
```

Pass the summary markdown on stdin. Do not create `.codex-away-mode/`, `latest-summary.md`, prompt marker files, SQLite files, or any other Codex Away Mode runtime files under the current workspace cwd. The CLI stores prompt markers and staged summaries in the central per-user runtime store, keyed first by the Codex session/thread id and only falling back to `sha256(normalized_cwd)` when no session id is available.

The Stop hook selects the staged summary by the same session/thread route, so an agent may work inside a nested worktree while the Stop hook still resolves the outer Codex thread correctly. The cwd hash fallback still isolates unrelated projects when no session id is available. The agent may include workspace context inside the summary when it is useful for review, but routine Feishu completion cards do not display the raw cwd.

If the summary is missing, the hook sends a no-summary fallback only when all of these are true: the cwd is a user workspace, the session has a user prompt marker, and the transcript goal status is not `active`. If the cwd is `/`, `${CODEX_HOME:-~/.codex}`, `/tmp`, another internal temporary location, or the transcript shows an active goal, the hook skips notification. For in-progress goal-mode continuation turns, omit the summary until the goal is complete, blocked, or needs human attention.

Hook commands:

```bash
CODEX_AWAY_CLI="${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode"
"$CODEX_AWAY_CLI" notify mark-prompt --json
"$CODEX_AWAY_CLI" notify stage-summary --cwd "$PWD" --session-id "${CODEX_THREAD_ID:-}" --json
"$CODEX_AWAY_CLI" notify stop --json
"$CODEX_AWAY_CLI" notify permission-request --hook-json
```

Notification mode:

```bash
CODEX_AWAY_CLI="${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode"
"$CODEX_AWAY_CLI" notify mode all
"$CODEX_AWAY_CLI" notify mode off
"$CODEX_AWAY_CLI" notify snooze 2h
```

Default mode is `all`. Completion card titles should include the project name when available. Footer text should keep operational metadata out of the body in one compact note with no blank lines. Show time as local `HH:MM` only. Routine notification cards should not display the raw workspace cwd. User-facing cards should say that the user can tell Codex in natural language, using wording like `修改通知模式：告诉Codex「暂停飞书通知 2 小时」`; do not show `config.toml`, `CODEX_HOME`, UTC offsets, dates, raw cwd, or raw CLI commands in routine notification cards. The CLI commands above are for Codex/installer execution, not the main user instruction text.

## Approval Reminders

The managed `PermissionRequest` hook calls:

```bash
CODEX_AWAY_CLI="${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode"
"$CODEX_AWAY_CLI" notify permission-request --hook-json
```

This command reads the hook payload from stdin and sends a Feishu reminder card when Codex is waiting for user approval. If `approval_notifications_urgent_app_enabled` is enabled, it tries to add Feishu in-app urgent delivery to the same card. In hook mode it prints `{}` and exits 0 so the notification path cannot approve, deny, or block the original Codex approval flow. Feishu replies to this card are not prompts and do not approve the operation. Tell the user to return to Codex Desktop to approve or reject.

## Away Mode

Start Away Mode only when the user explicitly asks for it. Do not infer it from ordinary work.

If the user says "开启 Away Mode", "开启 codex-away-mode", "我要离开电脑", or similar, start a new Away Session immediately with `"${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode" away start ... --json`.

Do not run `doctor --json`, `doctor --route-probe`, or `away status` as a prerequisite for normal startup. Let `away start` return a structured error if local config or Feishu binding is missing.

Do not resume a session found in `away status`. Resume only when this same turn just received `status=reply`, `keep_waiting=true`, and `resume_token` from `away start`, `away wait`, or `away resume`.

While `away start` or `away resume` is polling, keep the Codex chat quiet. Do not send heartbeat or "still waiting" updates in the chat. Only produce user-visible output when a routed card reply arrives, the wait ends, times out, errors, or requires user action. The CLI itself should remain a blocking, final-result command.

When a routed card reply arrives, the desktop Codex thread must keep a concise trace. First write a short note that includes the received Feishu text and what you are going to do. After completing that `reply_text` work, write the result or answer in the Codex chat before calling `away resume`. Heartbeat or waiting-status text is still forbidden; user messages and agent answers are not.

Example:

```bash
CODEX_AWAY_CLI="${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode"
"$CODEX_AWAY_CLI" away start \
  --project Demo \
  --cwd /workspace/demo \
  --task "实现功能" \
  --completed "已完成当前检查点" \
  --changed "无" \
  --verification "pytest -q" \
  --unverified "真实飞书 route probe" \
  --need-user "请确认下一步" \
  --wait-minutes 30 \
  --poll-interval 5 \
  --json
```

The CLI blocks until one wait-cycle result is available:

- `reply`: a valid Feishu card reply should be treated as the next Codex prompt. When the JSON includes `keep_waiting: true`, the Away Session remains active. After handling `reply_text`, call `away resume "$away_session_id" --resume-token "$resume_token"` with updated progress fields to continue waiting.
- `ended`: the user sent `/结束等待`; close the whole Away Mode session and continue local wrap-up.
- `timeout`: the reply window closed; later Feishu messages cannot reach this Codex turn.
- `error`: report the error and continue without pretending Away Mode is active.

Resume after handling a Feishu reply:

```bash
CODEX_AWAY_CLI="${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode"
"$CODEX_AWAY_CLI" away resume sess_... \
  --resume-token rt_... \
  --completed "上一条飞书回复已处理" \
  --changed "无" \
  --verification "pytest -q" \
  --unverified "真实飞书 route probe" \
  --need-user "请继续回复这张最新卡片" \
  --poll-interval 5 \
  --json
```

Resume first drains queued card replies that arrived while Codex was processing. Internal commands such as `/延长等待` and `/状态` are handled with visible feedback and do not return to Codex; the first ordinary prompt is returned as `reply`. If there is no queued prompt, resume sends a new progress card and waits again. The original deadline is shared across resumes unless the user sends `/延长等待`.

If the routed reply is a natural-language request such as "把等待时间延长 3 个小时", the agent should parse the duration, complete any short confirmation work, then resume with an explicit extension:

```bash
CODEX_AWAY_CLI="${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode"
"$CODEX_AWAY_CLI" away resume sess_... \
  --resume-token rt_... \
  --extend-minutes 180 \
  --completed "已按用户要求延长等待时间" \
  --changed "无" \
  --verification "未运行" \
  --unverified "无" \
  --need-user "请继续回复这张最新卡片" \
  --poll-interval 5 \
  --json
```

Do not inspect or modify Away Mode SQLite, StateStore, or runtime files directly to change the deadline. The CLI is responsible for updating the session, window, lock expiry, visible feedback, and the next progress card.

Supported card-reply commands:

- `/延长等待`: extend by 30 minutes.
- `/状态`: send current wait status.
- `/结束等待`: close the whole Away Mode session.

Ordinary Feishu private-chat messages are not prompts. The user must reply to the corresponding Away Mode card. When exact `reply_to` routing is not verified, use single-window fallback rather than consuming newest private-chat text.

`away status` is a diagnostic command, not a startup command. The default output intentionally hides internal session/window ids. Use `--debug` or `--include-internal-ids` only for troubleshooting, and never use those ids to resume without a current `resume_token`.

## Runtime Cleanup

Use cleanup only for diagnostics or stale runtime repair:

```bash
CODEX_AWAY_CLI="${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode"
"$CODEX_AWAY_CLI" away cleanup --dry-run --json
"$CODEX_AWAY_CLI" away cleanup --json
"$CODEX_AWAY_CLI" away cleanup --orphan-active --dry-run --json
```

By default, the command only closes Away Sessions that are already past their deadline and have no live waiter lease. It does not send Feishu cards and does not replace the normal timeout path. Run `away cleanup --dry-run --json` first, then run the non-dry-run command only when the stale sessions are expected leftovers from tests, crashes, or interrupted validation.

Use `--orphan-active` only when a resume attempt failed after Codex already left the normal wait loop, the waiter lease is expired, and `away status --json` shows an active session that cannot be resumed by the current turn. This mode may close a future-deadline session if no live waiter lease exists. It is intended for crash or transport-error recovery, not normal timeout handling.
