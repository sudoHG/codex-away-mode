from types import SimpleNamespace

from codex_away_mode import setup
from codex_away_mode.config import AppConfig, load_config, save_config
from codex_away_mode.state import StateStore


class FakePaths:
    def __init__(self, root):
        self.codex_home = root
        self.data_dir = root / "codex-away-mode"
        self.config_path = self.data_dir / "config.toml"
        self.install_state_path = self.data_dir / "install-state.sqlite"


class FakeLarkSetup:
    def __init__(
        self,
        *,
        open_id="ou_test_user",
        chat_id="oc_test_chat",
        auth_complete_open_id=None,
        auth_complete_payload=None,
        auth_browser_opened=True,
    ):
        self.open_id = open_id
        self.chat_id = chat_id
        self.auth_complete_open_id = auth_complete_open_id
        self.auth_complete_payload = auth_complete_payload
        self.auth_browser_opened = auth_browser_opened
        self.calls = []
        self.app_config_response = {"ok": True, "configured": True}
        self.app_config_init_response = {
            "ok": False,
            "status": "lark_app_config_browser_pending",
            "verification_url": "https://example.com/config",
            "browser_opened": True,
            "debug_command": ["lark-cli", "config", "init", "--new"],
            "developer_detail": {
                "command": ["lark-cli", "config", "init", "--new"],
            },
        }

    def preflight_auth_commands(self):
        self.calls.append("preflight")
        return {"ok": True}

    def app_config_status(self):
        self.calls.append("app_config_status")
        return self.app_config_response

    def start_app_config_init(self):
        self.calls.append("start_app_config_init")
        return self.app_config_init_response

    def auth_status(self):
        self.calls.append("auth_status")
        if self.open_id is None:
            return {"ok": True, "data": {}}
        return {"ok": True, "data": {"user_open_id": self.open_id}}

    def auth_login_start(self):
        self.calls.append("auth_login_start")
        return {
            "ok": True,
            "data": {
                "verification_url": "https://example.com/login",
                "device_code": "device-code-1",
                "user_code": "ABCD-EFGH",
                "expires_in": 600,
            },
            "browser_opened": self.auth_browser_opened,
        }

    def auth_login_complete(self, device_code):
        self.calls.append(("auth_login_complete", device_code))
        if self.auth_complete_payload is not None:
            return self.auth_complete_payload
        if self.auth_complete_open_id is not None:
            return {"ok": True, "data": {"user_open_id": self.auth_complete_open_id}}
        if self.open_id is None:
            return {"ok": True, "data": {}}
        return {"ok": True, "data": {"user_open_id": self.open_id}}

    def send_test_notification(self):
        self.calls.append("send_test_notification")
        return SimpleNamespace(chat_id=self.chat_id, message_id="om_test")


class FailingPreflight(FakeLarkSetup):
    def preflight_auth_commands(self):
        self.calls.append("preflight")
        return {"ok": False, "failed_code": "lark_auth_command_unverified"}


def test_setup_feishu_uses_existing_user_identity_and_persists_binding(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())
    lark = FakeLarkSetup(open_id="ou_user", chat_id="oc_chat")

    result = setup.run_setup_feishu(paths, lark=lark)

    config = load_config(paths.config_path)
    assert result["ok"] is True
    assert result["status"] == "feishu_binding_verified"
    assert config.feishu_user_id == "ou_user"
    assert config.feishu_chat_id == "oc_chat"
    assert lark.calls == [
        "preflight",
        "app_config_status",
        "auth_status",
        "send_test_notification",
    ]
    assert StateStore(paths.install_state_path).install_status()["status"] == "feishu_binding_verified"


def test_setup_feishu_preserves_route_key_when_chat_binding_unchanged(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(
        paths.config_path,
        AppConfig(
            feishu_user_id="ou_user",
            feishu_chat_id="oc_chat",
            route_key_verified=True,
            multi_window_enabled=True,
        ),
    )
    StateStore(paths.install_state_path).set_install_state(
        "route_key",
        {"status": "verified", "source": "doctor_route_probe", "verified_at": "2026-06-18T09:00:00Z"},
    )
    lark = FakeLarkSetup(open_id="ou_user", chat_id="oc_chat")

    result = setup.run_setup_feishu(paths, lark=lark)

    config = load_config(paths.config_path)
    assert result["ok"] is True
    assert config.route_key_verified is True
    assert config.multi_window_enabled is True
    assert StateStore(paths.install_state_path).get_install_state("route_key")["status"] == "verified"


def test_setup_feishu_marks_route_key_unknown_when_chat_binding_changes(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(
        paths.config_path,
        AppConfig(
            feishu_user_id="ou_user",
            feishu_chat_id="oc_old_chat",
            route_key_verified=True,
            multi_window_enabled=True,
        ),
    )
    StateStore(paths.install_state_path).set_install_state(
        "route_key",
        {"status": "verified", "source": "doctor_route_probe", "verified_at": "2026-06-18T09:00:00Z"},
    )
    lark = FakeLarkSetup(open_id="ou_user", chat_id="oc_new_chat")

    result = setup.run_setup_feishu(paths, lark=lark)

    config = load_config(paths.config_path)
    assert result["ok"] is True
    assert config.feishu_chat_id == "oc_new_chat"
    assert config.route_key_verified is False
    assert StateStore(paths.install_state_path).get_install_state("route_key")["status"] == "unknown"


def test_setup_feishu_starts_browser_authorization_when_user_identity_missing(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())
    lark = FakeLarkSetup(open_id=None)

    result = setup.run_setup_feishu(paths, lark=lark)

    assert result["ok"] is False
    assert result["status"] == "feishu_authorization_pending"
    assert result["verification_url"] == "https://example.com/login"
    assert result["user_code"] == "ABCD-EFGH"
    assert result["reused_pending"] is False
    assert "device_code" not in result
    pending = StateStore(paths.install_state_path).get_install_state("feishu_auth_pending")
    assert pending["device_code"] == "device-code-1"
    assert pending["verification_url"] == "https://example.com/login"
    assert pending["user_code"] == "ABCD-EFGH"
    assert StateStore(paths.install_state_path).install_status()["waiting_for"] == "feishu_authorization"


def test_setup_feishu_starts_browser_config_when_app_config_missing(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(lark_cli_path="/managed/lark-cli"))
    lark = FakeLarkSetup(open_id="ou_user")
    lark.app_config_response = {
        "ok": False,
        "status": "lark_app_config_pending",
        "failed_code": "lark_app_config_missing",
        "config_command": ["/managed/lark-cli", "config", "init", "--new"],
    }
    lark.app_config_init_response = {
        "ok": False,
        "status": "lark_app_config_browser_pending",
        "verification_url": "https://example.com/config",
        "browser_opened": True,
        "debug_command": ["/managed/lark-cli", "config", "init", "--new"],
        "developer_detail": {
            "command": ["/managed/lark-cli", "config", "init", "--new"],
        },
    }

    result = setup.run_setup_feishu(paths, lark=lark)

    assert result["ok"] is False
    assert result["status"] == "lark_app_config_browser_pending"
    assert result["verification_url"] == "https://example.com/config"
    assert result["browser_opened"] is True
    assert result["debug_command"] == ["/managed/lark-cli", "config", "init", "--new"]
    assert "浏览器" in result["user_message"]
    assert "lark-cli" not in result["user_message"]
    assert "config init" not in result["user_message"]
    assert "终端" not in result["user_message"]
    status = StateStore(paths.install_state_path).install_status()
    assert status["status"] == "lark_app_config_browser_pending"
    assert status["waiting_for"] == "feishu_browser_confirmation"
    assert lark.calls == ["preflight", "app_config_status", "start_app_config_init"]


def test_setup_feishu_continues_when_browser_config_finishes_immediately(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(lark_cli_path="/managed/lark-cli"))
    lark = FakeLarkSetup(open_id="ou_user")
    lark.app_config_response = {
        "ok": False,
        "status": "lark_app_config_pending",
        "failed_code": "lark_app_config_missing",
    }
    lark.app_config_init_response = {"ok": True, "configured": True}

    result = setup.run_setup_feishu(paths, lark=lark)

    assert result["ok"] is True
    assert result["status"] == "feishu_binding_verified"
    assert lark.calls == [
        "preflight",
        "app_config_status",
        "start_app_config_init",
        "auth_status",
        "send_test_notification",
    ]


def test_setup_feishu_device_code_without_open_id_returns_p2p_unverified(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())
    lark = FakeLarkSetup(open_id=None)

    result = setup.run_setup_feishu(paths, lark=lark, device_code="device-code-1")

    assert result["ok"] is False
    assert result["failed_code"] == "feishu_p2p_binding_unverified"
    assert "私聊" in result["user_message"]
    assert StateStore(paths.install_state_path).install_status()["failed_code"] == "feishu_p2p_binding_unverified"


def test_setup_feishu_preflight_failure_is_structured(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())
    lark = FailingPreflight()

    result = setup.run_setup_feishu(paths, lark=lark)

    assert result["ok"] is False
    assert result["failed_code"] == "lark_auth_command_unverified"
    assert "lark-cli" in result["user_message"]


def test_extract_open_id_prefers_user_open_id_from_lark_cli_auth_status():
    payload = {
        "identities": {
            "bot": {"status": "ready", "openId": "ou_bot"},
            "user": {"status": "ready", "openId": "ou_user"},
        }
    }

    assert setup.extract_open_id(payload) == "ou_user"


def test_setup_feishu_oauth_pending_reports_actual_browser_open_state(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())
    lark = FakeLarkSetup(open_id=None, auth_browser_opened=False)

    result = setup.run_setup_feishu(paths, lark=lark)

    assert result["status"] == "feishu_authorization_pending"
    assert result["browser_opened"] is False
    assert "浏览器没有自动打开" in result["user_message"]


def test_setup_feishu_reuses_existing_pending_authorization(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())
    lark = FakeLarkSetup(open_id=None)

    first = setup.run_setup_feishu(paths, lark=lark)
    second = setup.run_setup_feishu(paths, lark=lark)

    assert first["status"] == "feishu_authorization_pending"
    assert second["status"] == "feishu_authorization_pending"
    assert second["reused_pending"] is True
    assert second["verification_url"] == first["verification_url"]
    assert lark.calls.count("auth_login_start") == 1
    assert ("auth_login_complete", "device-code-1") in lark.calls
    assert "device_code" not in second


def test_setup_feishu_completes_binding_when_pending_auth_returns_open_id(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())
    lark = FakeLarkSetup(open_id=None, auth_complete_open_id="ou_user", chat_id="oc_chat")

    setup.run_setup_feishu(paths, lark=lark)
    result = setup.run_setup_feishu(paths, lark=lark)

    config = load_config(paths.config_path)
    assert result["ok"] is True
    assert result["status"] == "feishu_binding_verified"
    assert config.feishu_user_id == "ou_user"
    assert config.feishu_chat_id == "oc_chat"
    assert StateStore(paths.install_state_path).get_install_state("feishu_auth_pending") is None


def test_setup_feishu_completes_binding_when_auth_status_has_open_id_after_pending(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())
    lark = FakeLarkSetup(open_id=None)

    setup.run_setup_feishu(paths, lark=lark)
    lark.open_id = "ou_user"
    result = setup.run_setup_feishu(paths, lark=lark)

    config = load_config(paths.config_path)
    assert result["ok"] is True
    assert result["status"] == "feishu_binding_verified"
    assert config.feishu_user_id == "ou_user"
    assert StateStore(paths.install_state_path).get_install_state("feishu_auth_pending") is None


def test_setup_feishu_keeps_same_pending_when_auth_poll_still_pending(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())
    lark = FakeLarkSetup(
        open_id=None,
        auth_complete_payload={
            "ok": False,
            "status": "feishu_authorization_still_pending",
            "failed_code": "feishu_authorization_still_pending",
        },
    )

    first = setup.run_setup_feishu(paths, lark=lark)
    second = setup.run_setup_feishu(paths, lark=lark)

    assert second["status"] == "feishu_authorization_pending"
    assert second["reused_pending"] is True
    assert second["verification_url"] == first["verification_url"]
    assert lark.calls.count("auth_login_start") == 1


def test_setup_feishu_restart_auth_discards_existing_pending(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())
    lark = FakeLarkSetup(open_id=None)

    setup.run_setup_feishu(paths, lark=lark)
    lark.auth_login_start = lambda: {
        "ok": True,
        "data": {
            "verification_url": "https://example.com/login-2",
            "device_code": "device-code-2",
            "user_code": "IJKL-MNOP",
            "expires_in": 600,
        },
    }
    result = setup.run_setup_feishu(paths, lark=lark, restart_auth=True)

    assert result["status"] == "feishu_authorization_pending"
    assert result["verification_url"] == "https://example.com/login-2"
    pending = StateStore(paths.install_state_path).get_install_state("feishu_auth_pending")
    assert pending["device_code"] == "device-code-2"


def test_setup_feishu_restarts_authorization_after_pending_expires(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())
    store = StateStore(paths.install_state_path)
    store.set_feishu_auth_pending(
        {
            "status": "pending",
            "device_code": "expired-device-code",
            "verification_url": "https://example.com/expired",
            "user_code": "OLD-CODE",
            "started_at": "2026-06-24T05:00:00+00:00",
            "expires_at": "2026-06-24T05:10:00+00:00",
            "last_poll_at": None,
            "attempt_count": 0,
        }
    )
    lark = FakeLarkSetup(open_id=None)

    result = setup.run_setup_feishu(paths, lark=lark)

    assert result["status"] == "feishu_authorization_pending"
    assert result["restart_reason"] == "expired"
    assert result["verification_url"] == "https://example.com/login"
    assert ("auth_login_complete", "expired-device-code") not in lark.calls


def test_setup_feishu_user_message_hides_oauth_technical_details(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())
    lark = FakeLarkSetup(open_id=None)

    result = setup.run_setup_feishu(paths, lark=lark)

    for forbidden in ("device_code", "--device-code", "lark-cli", "auth login", "终端"):
        assert forbidden not in result["user_message"]
