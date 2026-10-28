import pytest

from chatwoot_mcp.config import ConfigError, Settings


def env(**overrides):
    base = {"CHATWOOT_BASE_URL": "https://chat.example.com", "CHATWOOT_ACCOUNT_ID": "1"}
    base.update(overrides)
    return base


def test_minimal_settings_use_safe_defaults():
    s = Settings.from_env(env())
    assert s.base_url == "https://chat.example.com"
    assert s.account_id == 1
    assert s.enable_writes is False
    assert s.max_items == 25
    assert s.max_text_chars == 2000
    assert s.timeout_seconds == 30.0


def test_trailing_slash_is_removed_from_the_base_url():
    assert Settings.from_env(env(CHATWOOT_BASE_URL="https://chat.example.com/")).base_url == "https://chat.example.com"


def test_base_url_must_be_http_or_https():
    with pytest.raises(ConfigError, match="CHATWOOT_BASE_URL"):
        Settings.from_env(env(CHATWOOT_BASE_URL="chat.example.com"))
    with pytest.raises(ConfigError, match="CHATWOOT_BASE_URL"):
        Settings.from_env(env(CHATWOOT_BASE_URL="ftp://chat.example.com"))


def test_base_url_is_required():
    with pytest.raises(ConfigError, match="CHATWOOT_BASE_URL"):
        Settings.from_env({"CHATWOOT_ACCOUNT_ID": "1"})


@pytest.mark.parametrize("value", ["", "abc", "0", "-3", "1.5", " 1"])
def test_account_id_must_be_a_positive_integer(value):
    with pytest.raises(ConfigError, match="CHATWOOT_ACCOUNT_ID"):
        Settings.from_env(env(CHATWOOT_ACCOUNT_ID=value))


def test_account_id_is_required():
    with pytest.raises(ConfigError, match="CHATWOOT_ACCOUNT_ID"):
        Settings.from_env({"CHATWOOT_BASE_URL": "https://chat.example.com"})


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("TRUE", True), ("1", True), ("false", False), ("0", False), ("", False)],
)
def test_enable_writes_flag(value, expected):
    assert Settings.from_env(env(CHATWOOT_MCP_ENABLE_WRITES=value)).enable_writes is expected


def test_enable_writes_rejects_unknown_words():
    with pytest.raises(ConfigError, match="CHATWOOT_MCP_ENABLE_WRITES"):
        Settings.from_env(env(CHATWOOT_MCP_ENABLE_WRITES="yes please"))


@pytest.mark.parametrize("name", ["CHATWOOT_MCP_MAX_ITEMS", "CHATWOOT_MCP_MAX_TEXT_CHARS"])
@pytest.mark.parametrize("value", ["0", "-1", "ten", "5.5", "100001"])
def test_limits_must_be_sensible_integers(name, value):
    with pytest.raises(ConfigError, match=name):
        Settings.from_env(env(**{name: value}))


def test_limits_can_be_changed():
    s = Settings.from_env(env(CHATWOOT_MCP_MAX_ITEMS="10", CHATWOOT_MCP_MAX_TEXT_CHARS="500"))
    assert (s.max_items, s.max_text_chars) == (10, 500)


@pytest.mark.parametrize("value", ["0", "-2", "soon", "601"])
def test_timeout_must_be_between_zero_and_ten_minutes(value):
    with pytest.raises(ConfigError, match="CHATWOOT_MCP_TIMEOUT_SECONDS"):
        Settings.from_env(env(CHATWOOT_MCP_TIMEOUT_SECONDS=value))


def test_actions_are_off_by_default_and_the_confirm_window_is_five_minutes():
    s = Settings.from_env(env())
    assert s.enable_actions is False and s.confirm_ttl_seconds == 300


def test_actions_can_be_switched_on():
    assert Settings.from_env(env(CHATWOOT_MCP_ENABLE_ACTIONS="true")).enable_actions is True
    with pytest.raises(ConfigError, match="CHATWOOT_MCP_ENABLE_ACTIONS"):
        Settings.from_env(env(CHATWOOT_MCP_ENABLE_ACTIONS="maybe"))


@pytest.mark.parametrize("value", ["29", "3601", "soon", "0", "-5"])
def test_the_confirm_window_must_be_between_thirty_seconds_and_an_hour(value):
    with pytest.raises(ConfigError, match="CHATWOOT_MCP_CONFIRM_TTL_SECONDS"):
        Settings.from_env(env(CHATWOOT_MCP_CONFIRM_TTL_SECONDS=value))
    assert Settings.from_env(env(CHATWOOT_MCP_CONFIRM_TTL_SECONDS="60")).confirm_ttl_seconds == 60
