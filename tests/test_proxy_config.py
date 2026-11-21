"""Unit tests for the [proxy] config section: validation, persistence, masking."""

from __future__ import annotations

from pathlib import Path

import pytest

from surplus import config as cfgmod
from surplus.config import (
    AppConfig,
    ProxyConfig,
    load_config,
    mask_secret_url,
    normalize_proxy_mode,
    normalize_proxy_url,
    public_view,
    save_config,
)


def test_proxy_defaults(tmp_path: Path) -> None:
    proxy = load_config(tmp_path / "missing.ini").proxy
    assert proxy.enabled is False
    assert proxy.mode == "external"
    assert proxy.proxy_url == ""
    assert proxy.subscription_url == ""
    assert proxy.subscription_links == ""
    assert proxy.xray_binary == ""
    assert proxy.health_check_interval_seconds == 30
    assert proxy.failure_threshold == 2
    assert proxy.subscription_refresh_hours == 6


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        (None, ""),
        ("http://127.0.0.1:10809", "http://127.0.0.1:10809"),
        ("https://proxy.internal:3128", "https://proxy.internal:3128"),
        ("socks5://127.0.0.1:10808", "socks5://127.0.0.1:10808"),
        ("socks5h://user:pw@10.0.0.5:1080", "socks5h://user:pw@10.0.0.5:1080"),
        ("ftp://127.0.0.1:21", ""),
        ("not a url at all", ""),
        ("http://", ""),
    ],
)
def test_normalize_proxy_url(raw: str | None, expected: str) -> None:
    assert normalize_proxy_url(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("managed", "managed"),
        ("external", "external"),
        ("MANAGED", "managed"),
        ("weird", "external"),
        ("", "external"),
        (None, "external"),
    ],
)
def test_normalize_proxy_mode(raw: str | None, expected: str) -> None:
    assert normalize_proxy_mode(raw) == expected


def test_proxy_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.ini"
    proxy = ProxyConfig(
        enabled=True,
        mode="managed",
        proxy_url="http://127.0.0.1:10809",
        subscription_url="https://panel.example:2096/sub/token123",
        subscription_links="vless://a@h:80\nvless://b@h2:80",
        xray_binary="C:/tools/xray.exe",
        health_check_interval_seconds=45,
        failure_threshold=3,
        subscription_refresh_hours=12,
    )
    save_config(AppConfig(proxy=proxy), path)
    assert load_config(path).proxy == proxy


def test_proxy_invalid_values_fall_back(tmp_path: Path) -> None:
    path = tmp_path / "config.ini"
    path.write_text(
        "[proxy]\n"
        "enabled = maybe\n"
        "mode = weird\n"
        "proxy_url = junk\n"
        "health_check_interval_seconds = 1\n"
        "failure_threshold = 0\n"
        "subscription_refresh_hours = 0\n",
        encoding="utf-8",
    )
    proxy = load_config(path).proxy
    assert proxy.enabled is False
    assert proxy.mode == "external"
    assert proxy.proxy_url == ""
    assert proxy.health_check_interval_seconds == 10  # clamped minimum
    assert proxy.failure_threshold == 1
    assert proxy.subscription_refresh_hours == 1


def test_public_view_masks_subscription_url() -> None:
    cfg = AppConfig(
        proxy=ProxyConfig(
            subscription_url="https://panel.example:2096/cfcheck/abcdefgh12345",
            subscription_links="vless://uuid@host:80",
        )
    )
    view = public_view(cfg)["proxy"]
    assert view["subscription_url_set"] is True
    assert view["subscription_links_set"] is True
    assert view["subscription_links_count"] == 1
    dumped = str(view)
    assert "abcdefgh12345" not in dumped
    assert "uuid@host" not in dumped
    assert view["subscription_url_masked"].startswith("https://panel.example:2096/")


def test_mask_secret_url_shapes() -> None:
    assert mask_secret_url("") == ""
    assert mask_secret_url(None) == ""
    short = mask_secret_url("https://p.example/abc")
    assert short.startswith("https://p.example/")
    long = mask_secret_url("https://p.example/cfcheck/abcdefgh12345")
    assert long.startswith("https://p.example/cfch")
    assert long.endswith("12345")
    assert "…" in long


def test_proxy_view_covers_every_setting() -> None:
    """Guards against drift between DEFAULTS, ProxyConfig and public_view."""
    fields = set(ProxyConfig().__dict__)
    assert set(cfgmod.DEFAULTS["proxy"]) == fields
    view_keys = set(public_view(AppConfig())["proxy"])
    mapped: set[str] = set()
    for name in fields:
        if name == "subscription_url":
            mapped |= {"subscription_url_masked", "subscription_url_set"}
        elif name == "subscription_links":
            mapped |= {"subscription_links_set", "subscription_links_count"}
        else:
            mapped.add(name)
    assert mapped <= view_keys


def test_example_config_contains_empty_proxy_section(tmp_path: Path) -> None:
    from surplus.config import write_example_config

    text = write_example_config(tmp_path / "config.example.ini").read_text(
        encoding="utf-8"
    )
    assert "[proxy]" in text
    assert "subscription_url = " in text
    # no real secret ever lands in the committed example
    assert "surplusintelligence" in text  # upstream base_url still documented
    assert text.count("api_key =") == 1
