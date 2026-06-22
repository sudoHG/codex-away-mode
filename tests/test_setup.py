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
    def __init__(self, *, open_id="ou_test_user", chat_id="oc_test_chat"):
        self.open_id = open_id
        self.chat_id = chat_id
        self.calls = []

    def preflight_auth_commands(self):
        self.calls.append("preflight")
        return {"ok": True}

    def config_init_new(self):
        self.calls.append("config_init_new")
        return {"ok": True}

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
            },
        }

    def auth_login_complete(self, device_code):
        self.calls.append(("auth_login_complete", device_code))
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
        "config_init_new",
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
    assert result["device_code"] == "device-code-1"
    assert StateStore(paths.install_state_path).install_status()["waiting_for"] == "feishu_authorization"


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
