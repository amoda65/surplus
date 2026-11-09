"""Configuration load/save.

Plain ``config.ini`` on disk, atomic writes, and an environment override for the
API key. No secrets are ever logged by this module.
"""

from __future__ import annotations

import configparser
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILENAME = "config.ini"
EXAMPLE_FILENAME = "config.example.ini"
APP_DIR_NAME = "surplus-router"
CONFIG_ENV_VAR = "SURPLUS_CONFIG"


def user_config_dir() -> Path:
    """Per-user config dir: %APPDATA%\\surplus-router on Windows, XDG elsewhere."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_DIR_NAME


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    """Where config.ini lives. Precedence:

    1. ``--config`` argument
    2. ``SURPLUS_CONFIG`` environment variable
    3. ``./config.ini`` in the current directory, when it exists (project-local
       override, so running inside the repo uses the repo's config)
    4. the per-user config dir — this is what makes ``surplus`` work from any
       directory after a global install
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    from_env = os.environ.get(CONFIG_ENV_VAR, "").strip()
    if from_env:
        return Path(from_env).expanduser().resolve()
    local = Path.cwd() / CONFIG_FILENAME
    if local.exists():
        return local.resolve()
    return user_config_dir() / CONFIG_FILENAME

# Canonical defaults; also the source of truth for config.example.ini content.
DEFAULTS: dict[str, dict[str, str]] = {
    "server": {
        "host": "127.0.0.1",
        "port": "8404",
        "open_browser": "true",
    },
    "upstream": {
        "base_url": "https://api.surplusintelligence.ai",
        "catalog_ttl_seconds": "300",
        "request_timeout_seconds": "600",
    },
    "auth": {
        "api_key_env": "SURPLUS_API_KEY",
        "api_key": "",
    },
    "filter": {
        "enable_price_input": "true",
        "max_input_per_1m": "0.1",
        "enable_price_output": "true",
        "max_output_per_1m": "0.5",
        "enable_discount": "false",
        "min_discount_pct": "30",
        "enable_models_allow": "false",
        "allow_models": "",
        "enable_models_deny": "false",
        "deny_models": "",
        "enable_providers_allow": "false",
        "allow_providers": "",
        "enable_context_min": "false",
        "min_context_length": "32000",
        "allow_unknown_models": "false",
    },
}


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8404
    open_browser: bool = True


@dataclass
class UpstreamConfig:
    base_url: str = "https://api.surplusintelligence.ai"
    catalog_ttl_seconds: int = 300
    request_timeout_seconds: float = 600.0


@dataclass
class FilterConfig:
    enable_price_input: bool = True
    max_input_per_1m: float = 0.1
    enable_price_output: bool = True
    max_output_per_1m: float = 0.5
    enable_discount: bool = False
    min_discount_pct: int = 30
    enable_models_allow: bool = False
    allow_models: list[str] = field(default_factory=list)
    enable_models_deny: bool = False
    deny_models: list[str] = field(default_factory=list)
    enable_providers_allow: bool = False
    allow_providers: list[str] = field(default_factory=list)
    enable_context_min: bool = False
    min_context_length: int = 32000
    allow_unknown_models: bool = False


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    api_key_env: str = "SURPLUS_API_KEY"
    file_api_key: str = ""
    config_path: Path | None = None


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _to_bool(value: str, fallback: bool) -> bool:
    if value is None:
        return fallback
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return fallback


def _to_float(value: str, fallback: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _to_int(value: str, fallback: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return fallback


def resolve_api_key(cfg: AppConfig) -> tuple[str | None, str]:
    """Return (key, source) where source is 'env', 'file' or 'missing'.

    The environment variable wins over the file value so a key never has to be
    written to disk when the owner prefers env-only.
    """
    env_value = os.environ.get(cfg.api_key_env, "").strip()
    if env_value:
        return env_value, "env"
    file_value = cfg.file_api_key.strip()
    if file_value:
        return file_value, "file"
    return None, "missing"


def mask_key(key: str | None) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * 6}{key[-4:]}"


def load_config(path: Path | str | None = None) -> AppConfig:
    """Load config.ini, filling missing keys from DEFAULTS. Never raises on bad values."""
    config_path = Path(path) if path else Path(CONFIG_FILENAME)
    parser = configparser.ConfigParser()
    if config_path.exists():
        parser.read(config_path, encoding="utf-8")

    server_raw = {**DEFAULTS["server"], **dict(parser["server"])} if "server" in parser else dict(
        DEFAULTS["server"]
    )
    upstream_raw = (
        {**DEFAULTS["upstream"], **dict(parser["upstream"])}
        if "upstream" in parser
        else dict(DEFAULTS["upstream"])
    )
    auth_raw = (
        {**DEFAULTS["auth"], **dict(parser["auth"])} if "auth" in parser else dict(DEFAULTS["auth"])
    )
    filter_raw = (
        {**DEFAULTS["filter"], **dict(parser["filter"])}
        if "filter" in parser
        else dict(DEFAULTS["filter"])
    )

    server = ServerConfig(
        host=server_raw.get("host", "127.0.0.1"),
        port=_to_int(server_raw.get("port", "8404"), 8404),
        open_browser=_to_bool(server_raw.get("open_browser"), True),
    )
    upstream = UpstreamConfig(
        base_url=upstream_raw.get("base_url", DEFAULTS["upstream"]["base_url"]).rstrip("/"),
        catalog_ttl_seconds=max(5, _to_int(upstream_raw.get("catalog_ttl_seconds", "300"), 300)),
        request_timeout_seconds=max(
            5.0, _to_float(upstream_raw.get("request_timeout_seconds", "600"), 600.0)
        ),
    )
    filters = FilterConfig(
        enable_price_input=_to_bool(filter_raw.get("enable_price_input"), True),
        max_input_per_1m=_to_float(filter_raw.get("max_input_per_1m"), 0.1),
        enable_price_output=_to_bool(filter_raw.get("enable_price_output"), True),
        max_output_per_1m=_to_float(filter_raw.get("max_output_per_1m"), 0.5),
        enable_discount=_to_bool(filter_raw.get("enable_discount"), False),
        min_discount_pct=_to_int(filter_raw.get("min_discount_pct"), 30),
        enable_models_allow=_to_bool(filter_raw.get("enable_models_allow"), False),
        allow_models=_split_csv(filter_raw.get("allow_models", "")),
        enable_models_deny=_to_bool(filter_raw.get("enable_models_deny"), False),
        deny_models=_split_csv(filter_raw.get("deny_models", "")),
        enable_providers_allow=_to_bool(filter_raw.get("enable_providers_allow"), False),
        allow_providers=_split_csv(filter_raw.get("allow_providers", "")),
        enable_context_min=_to_bool(filter_raw.get("enable_context_min"), False),
        min_context_length=_to_int(filter_raw.get("min_context_length"), 32000),
        allow_unknown_models=_to_bool(filter_raw.get("allow_unknown_models"), False),
    )

    return AppConfig(
        server=server,
        upstream=upstream,
        filters=filters,
        api_key_env=auth_raw.get("api_key_env", "SURPLUS_API_KEY"),
        file_api_key=auth_raw.get("api_key", ""),
        config_path=config_path,
    )


def _to_parser(cfg: AppConfig) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser["server"] = {
        "host": cfg.server.host,
        "port": str(cfg.server.port),
        "open_browser": str(cfg.server.open_browser).lower(),
    }
    parser["upstream"] = {
        "base_url": cfg.upstream.base_url,
        "catalog_ttl_seconds": str(cfg.upstream.catalog_ttl_seconds),
        "request_timeout_seconds": str(cfg.upstream.request_timeout_seconds),
    }
    parser["auth"] = {"api_key_env": cfg.api_key_env, "api_key": cfg.file_api_key}
    f = cfg.filters
    parser["filter"] = {
        "enable_price_input": str(f.enable_price_input).lower(),
        "max_input_per_1m": str(f.max_input_per_1m),
        "enable_price_output": str(f.enable_price_output).lower(),
        "max_output_per_1m": str(f.max_output_per_1m),
        "enable_discount": str(f.enable_discount).lower(),
        "min_discount_pct": str(f.min_discount_pct),
        "enable_models_allow": str(f.enable_models_allow).lower(),
        "allow_models": ", ".join(f.allow_models),
        "enable_models_deny": str(f.enable_models_deny).lower(),
        "deny_models": ", ".join(f.deny_models),
        "enable_providers_allow": str(f.enable_providers_allow).lower(),
        "allow_providers": ", ".join(f.allow_providers),
        "enable_context_min": str(f.enable_context_min).lower(),
        "min_context_length": str(f.min_context_length),
        "allow_unknown_models": str(f.allow_unknown_models).lower(),
    }
    return parser


def save_config(cfg: AppConfig, path: Path | str | None = None) -> Path:
    """Atomically write config.ini (temp file in the same dir, then replace)."""
    target = Path(path) if path else (cfg.config_path or Path(CONFIG_FILENAME))
    parser = _to_parser(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".config-", suffix=".ini.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            parser.write(handle)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return target


def write_example_config(path: Path | str | None = None) -> Path:
    """Write config.example.ini from DEFAULTS (committed; contains no secrets)."""
    target = Path(path) if path else Path(EXAMPLE_FILENAME)
    parser = configparser.ConfigParser()
    for section, values in DEFAULTS.items():
        parser[section] = dict(values)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("# Example configuration. Copy to config.ini and edit.\n")
        handle.write("# config.ini is gitignored; never commit a real API key.\n\n")
        parser.write(handle)
    return target


def public_view(cfg: AppConfig) -> dict:
    """Config as safe-to-expose JSON for the admin UI: key is masked, never full."""
    key, source = resolve_api_key(cfg)
    return {
        "server": {
            "host": cfg.server.host,
            "port": cfg.server.port,
            "open_browser": cfg.server.open_browser,
        },
        "upstream": {
            "base_url": cfg.upstream.base_url,
            "catalog_ttl_seconds": cfg.upstream.catalog_ttl_seconds,
            "request_timeout_seconds": cfg.upstream.request_timeout_seconds,
        },
        "auth": {
            "api_key_env": cfg.api_key_env,
            "api_key_source": source,
            "api_key_masked": mask_key(key),
        },
        "filter": {
            "enable_price_input": cfg.filters.enable_price_input,
            "max_input_per_1m": cfg.filters.max_input_per_1m,
            "enable_price_output": cfg.filters.enable_price_output,
            "max_output_per_1m": cfg.filters.max_output_per_1m,
            "enable_discount": cfg.filters.enable_discount,
            "min_discount_pct": cfg.filters.min_discount_pct,
            "enable_models_allow": cfg.filters.enable_models_allow,
            "allow_models": cfg.filters.allow_models,
            "enable_models_deny": cfg.filters.enable_models_deny,
            "deny_models": cfg.filters.deny_models,
            "enable_providers_allow": cfg.filters.enable_providers_allow,
            "allow_providers": cfg.filters.allow_providers,
            "enable_context_min": cfg.filters.enable_context_min,
            "min_context_length": cfg.filters.min_context_length,
            "allow_unknown_models": cfg.filters.allow_unknown_models,
        },
        "config_path": str(cfg.config_path) if cfg.config_path else None,
    }
