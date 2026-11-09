"""Unit tests for config load/save, atomic writes, key resolution and masking."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from surplus import config as cfgmod
from surplus.config import (
    AppConfig,
    FilterConfig,
    ServerConfig,
    UpstreamConfig,
    load_config,
    mask_key,
    public_view,
    resolve_api_key,
    save_config,
    write_example_config,
)


def test_defaults_when_file_missing(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "config.ini")
    assert cfg.server.port == 8404
    assert cfg.server.host == "127.0.0.1"
    assert cfg.upstream.base_url == "https://api.surplusintelligence.ai"
    assert cfg.upstream.catalog_ttl_seconds == 300
    assert cfg.filters.enable_price_input is True
    assert cfg.filters.max_input_per_1m == pytest.approx(2.0)
    assert cfg.filters.max_output_per_1m == pytest.approx(10.0)
    assert cfg.filters.allow_unknown_models is False


def test_round_trip_preserves_values(tmp_path: Path) -> None:
    path = tmp_path / "config.ini"
    cfg = AppConfig(
        server=ServerConfig(host="127.0.0.1", port=9100),
        upstream=UpstreamConfig(
            base_url="https://api.surplusintelligence.ai",
            catalog_ttl_seconds=60,
            request_timeout_seconds=120.0,
        ),
        filters=FilterConfig(
            enable_price_input=True,
            max_input_per_1m=0.5,
            enable_price_output=True,
            max_output_per_1m=2.5,
            enable_discount=True,
            min_discount_pct=45,
            enable_models_deny=True,
            deny_models=["gpt-5.5-pro", "minimax-m2.1"],
            enable_providers_allow=True,
            allow_providers=["OpenAI", "MiniMax"],
            enable_context_min=True,
            min_context_length=64000,
            allow_unknown_models=True,
        ),
        file_api_key="si_test_1234567890",
    )
    save_config(cfg, path)
    loaded = load_config(path)

    assert loaded.server.port == 9100
    assert loaded.upstream.catalog_ttl_seconds == 60
    assert loaded.filters.max_input_per_1m == pytest.approx(0.5)
    assert loaded.filters.min_discount_pct == 45
    assert loaded.filters.deny_models == ["gpt-5.5-pro", "minimax-m2.1"]
    assert loaded.filters.allow_providers == ["OpenAI", "MiniMax"]
    assert loaded.filters.min_context_length == 64000
    assert loaded.filters.allow_unknown_models is True
    assert loaded.file_api_key == "si_test_1234567890"


def test_partial_file_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.ini"
    path.write_text("[server]\nport = 8123\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.server.port == 8123
    assert cfg.server.host == "127.0.0.1"  # default kept
    assert cfg.filters.max_output_per_1m == pytest.approx(10.0)


def test_invalid_values_are_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "config.ini"
    path.write_text(
        "[server]\nport = not-a-number\n"
        "[upstream]\ncatalog_ttl_seconds = 1\nrequest_timeout_seconds = 0\n"
        "[filter]\nmax_input_per_1m = abc\nenable_price_input = maybe\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.server.port == 8404  # fallback
    assert cfg.upstream.catalog_ttl_seconds == 5  # clamped to the minimum
    assert cfg.upstream.request_timeout_seconds == 5.0  # clamped to the minimum
    assert cfg.filters.max_input_per_1m == pytest.approx(2.0)  # fallback
    assert cfg.filters.enable_price_input is True  # unparseable bool -> default


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path: Path) -> None:
    path = tmp_path / "config.ini"
    save_config(AppConfig(), path)
    first = path.read_text(encoding="utf-8")
    save_config(AppConfig(server=ServerConfig(port=8500)), path)
    assert path.read_text(encoding="utf-8") != first
    assert "port = 8500" in path.read_text(encoding="utf-8")
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".config-")]
    assert leftovers == []


def test_env_key_wins_over_file_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SURPLUS_API_KEY", "si_from_env")
    cfg = AppConfig(file_api_key="si_from_file")
    key, source = resolve_api_key(cfg)
    assert key == "si_from_env"
    assert source == "env"


def test_file_key_used_when_env_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SURPLUS_API_KEY", raising=False)
    cfg = AppConfig(file_api_key="  si_from_file  ")
    key, source = resolve_api_key(cfg)
    assert key == "si_from_file"
    assert source == "file"


def test_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SURPLUS_API_KEY", raising=False)
    key, source = resolve_api_key(AppConfig())
    assert key is None
    assert source == "missing"


def test_custom_key_env_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_ROUTER_KEY", "si_custom")
    cfg = AppConfig(api_key_env="MY_ROUTER_KEY")
    key, source = resolve_api_key(cfg)
    assert (key, source) == ("si_custom", "env")


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (None, ""),
        ("", ""),
        ("short", "*****"),
        ("si_seller_abcdef123456", "si_s******3456"),
    ],
)
def test_mask_key(key: str | None, expected: str) -> None:
    assert mask_key(key) == expected


def test_public_view_never_exposes_full_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SURPLUS_API_KEY", raising=False)
    cfg = AppConfig(file_api_key="si_super_secret_value_1234")
    view = public_view(cfg)
    assert view["auth"]["api_key_source"] == "file"
    assert view["auth"]["api_key_masked"] == "si_s******1234"
    dumped = str(view)
    assert "si_super_secret_value_1234" not in dumped
    assert "api_key" not in view["auth"]


def test_example_config_has_no_secret_and_matches_defaults(tmp_path: Path) -> None:
    path = write_example_config(tmp_path / "config.example.ini")
    text = path.read_text(encoding="utf-8")
    assert "[filter]" in text
    assert "max_input_per_1m" in text
    assert text.count("api_key =") == 1  # the empty default only
    assert "api_key = \n" in text or text.rstrip().endswith("api_key =")


def test_defaults_cover_every_filter_key() -> None:
    """Guards against drift between DEFAULTS, FilterConfig and public_view."""
    defaults = set(cfgmod.DEFAULTS["filter"])
    view_filter = set(public_view(AppConfig())["filter"])
    dataclass_fields = set(FilterConfig().__dict__)
    assert defaults == view_filter == dataclass_fields


# ------------------------------------------------------- config path resolution


def test_explicit_path_wins_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cfgmod.CONFIG_ENV_VAR, str(tmp_path / "env.ini"))
    resolved = cfgmod.resolve_config_path(tmp_path / "explicit.ini")
    assert resolved == (tmp_path / "explicit.ini").resolve()


def test_env_var_used_when_no_explicit_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(cfgmod.CONFIG_ENV_VAR, str(tmp_path / "env.ini"))
    assert cfgmod.resolve_config_path() == (tmp_path / "env.ini").resolve()


def test_local_config_ini_preferred_over_user_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running inside a project dir uses that project's config."""
    monkeypatch.delenv(cfgmod.CONFIG_ENV_VAR, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.ini").write_text("[server]\nport = 8111\n", encoding="utf-8")

    resolved = cfgmod.resolve_config_path()
    assert resolved == (tmp_path / "config.ini").resolve()
    assert load_config(resolved).server.port == 8111


def test_falls_back_to_user_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No local config.ini -> the per-user dir, so `surplus` works from anywhere."""
    monkeypatch.delenv(cfgmod.CONFIG_ENV_VAR, raising=False)
    empty_dir = tmp_path / "somewhere-else"
    empty_dir.mkdir()
    monkeypatch.chdir(empty_dir)
    if os.name == "nt":
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        expected = tmp_path / "appdata" / "surplus-router" / "config.ini"
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        expected = tmp_path / "xdg" / "surplus-router" / "config.ini"
    assert cfgmod.resolve_config_path() == expected


def test_user_config_dir_honours_platform_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        monkeypatch.setenv("APPDATA", str(tmp_path))
        assert cfgmod.user_config_dir() == tmp_path / "surplus-router"
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert cfgmod.user_config_dir() == tmp_path / "surplus-router"


def test_public_view_reports_config_path(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "config.ini")
    assert public_view(cfg)["config_path"] == str(cfg.config_path)


# ------------------------------------------------------------------ ensure_config


def test_ensure_config_creates_defaults_on_first_run(tmp_path: Path) -> None:
    from surplus.main import ensure_config

    path = tmp_path / "nested" / "config.ini"
    cfg = ensure_config(path)
    assert path.exists(), "first run must create the config file"
    assert cfg.server.port == 8404
    assert cfg.server.open_browser is True
    assert (tmp_path / "nested" / "config.example.ini").exists()
    # reloading gives the same values
    assert load_config(path).server.port == 8404


def test_ensure_config_never_overwrites_existing(tmp_path: Path) -> None:
    from surplus.main import ensure_config

    path = tmp_path / "config.ini"
    path.write_text("[server]\nport = 8123\n[filter]\nmax_input_per_1m = 0.25\n", encoding="utf-8")
    cfg = ensure_config(path)
    assert cfg.server.port == 8123
    assert cfg.filters.max_input_per_1m == pytest.approx(0.25)
    assert path.read_text(encoding="utf-8").count("8123") == 1


def test_ensure_config_leaves_an_edited_example_alone(tmp_path: Path) -> None:
    from surplus.main import ensure_config

    example = tmp_path / "config.example.ini"
    example.write_text("# my own notes\n", encoding="utf-8")
    ensure_config(tmp_path / "config.ini")
    assert example.read_text(encoding="utf-8") == "# my own notes\n"
