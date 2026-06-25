import json

from codex_away_mode.thread_context import (
    CardTitleContext,
    format_card_title,
    resolve_card_title_context,
)


def _write_session_index(codex_home, *records):
    codex_home.mkdir(parents=True)
    path = codex_home / "session_index.jsonl"
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )


def test_resolve_title_context_uses_hook_session_id_before_env(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    _write_session_index(
        codex_home,
        {"id": "env_thread", "thread_name": "环境变量里的线程"},
        {"id": "hook_thread", "thread_name": "建立 Skill-Create 基线"},
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_THREAD_ID", "env_thread")

    context = resolve_card_title_context(
        cwd="/Users/example/Codex/Skill-Create",
        hook_stdin=json.dumps({"session_id": "hook_thread"}),
    )

    assert context.project_name == "Skill-Create"
    assert context.thread_id == "hook_thread"
    assert context.thread_title == "建立 Skill-Create 基线"
    assert context.thread_id_source == "hook_session_id"
    assert context.thread_title_source == "session_index"


def test_resolve_title_context_falls_back_to_thread_id_env_then_explicit(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    _write_session_index(
        codex_home,
        {"id": "thread_from_payload", "thread_name": "Hook thread_id 标题"},
        {"id": "thread_from_env", "thread_name": "环境标题"},
        {"id": "thread_from_arg", "thread_name": "参数标题"},
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_THREAD_ID", "thread_from_env")

    hook_context = resolve_card_title_context(
        cwd="/Users/example/Codex/Skill-Create",
        hook_stdin=json.dumps({"thread_id": "thread_from_payload"}),
        explicit_codex_session_id="thread_from_arg",
    )
    env_context = resolve_card_title_context(
        cwd="/Users/example/Codex/Skill-Create",
        explicit_codex_session_id="thread_from_arg",
    )
    explicit_context = resolve_card_title_context(
        cwd="/Users/example/Codex/Skill-Create",
        env={"CODEX_HOME": str(codex_home)},
        explicit_codex_session_id="thread_from_arg",
    )

    assert hook_context.thread_title == "Hook thread_id 标题"
    assert hook_context.thread_id_source == "hook_thread_id"
    assert env_context.thread_title == "环境标题"
    assert env_context.thread_id_source == "env_CODEX_THREAD_ID"
    assert explicit_context.thread_title == "参数标题"
    assert explicit_context.thread_id_source == "explicit_codex_session_id"


def test_resolve_title_context_does_not_guess_thread_from_cwd_recency(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    _write_session_index(
        codex_home,
        {"id": "unrelated_recent", "thread_name": "最近但不相关的标题"},
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)

    context = resolve_card_title_context(cwd="/Users/example/Codex/Skill-Create")

    assert context.project_name == "Skill-Create"
    assert context.thread_id is None
    assert context.thread_title is None


def test_session_index_last_matching_record_wins_and_bad_json_is_ignored(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(parents=True)
    (codex_home / "session_index.jsonl").write_text(
        "\n".join(
            [
                "{not json",
                json.dumps({"id": "thread_1", "thread_name": "旧标题"}, ensure_ascii=False),
                json.dumps({"id": "thread_1", "thread_name": "新标题"}, ensure_ascii=False),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    context = resolve_card_title_context(
        cwd="/Users/example/Codex/Skill-Create",
        hook_stdin=json.dumps({"session_id": "thread_1"}),
    )

    assert context.thread_title == "新标题"


def test_format_card_title_uses_project_and_thread_with_dedup_and_truncation():
    assert (
        format_card_title(
            "Codex 完成通知",
            CardTitleContext(project_name="Skill-Create", thread_title="建立 Skill-Create 基线"),
        )
        == "Codex 完成通知 - Skill-Create / 建立 Skill-Create 基线"
    )
    assert (
        format_card_title(
            "Codex 完成通知",
            CardTitleContext(project_name="Skill-Create", thread_title="Skill-Create"),
        )
        == "Codex 完成通知 - Skill-Create"
    )
    assert (
        format_card_title(
            "Codex 完成通知",
            CardTitleContext(project_name=None, thread_title="建立 Skill-Create 基线"),
        )
        == "Codex 完成通知 / 建立 Skill-Create 基线"
    )
    long_context = CardTitleContext(
        project_name="p" * 50,
        thread_title="t" * 50,
    )
    title = format_card_title("Away Mode 已超时", long_context)
    assert title == f"Away Mode 已超时 - {'p' * 40}… / {'t' * 40}…"
