"""Subscription parsing and Xray client-config generation.

Input is a subscription body (base64-encoded or plain) or a raw list of share
links. Supported links: ``vless://``, ``vmess://``, ``trojan://``, ``ss://``.
Each link becomes one Xray outbound and the panel's ordering is preserved, so
the tunnel can rotate through the routes exactly as subscribed.

Secrets: links carry server credentials (UUIDs, passwords). This module never
logs them; the only place they surface is the generated outbound object and the
masked summaries used by the admin API.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

LINK_PREFIXES = ("vless://", "vmess://", "trojan://", "ss://")
ANY_LINK = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


@dataclass
class Server:
    """One subscribed route: display metadata plus a ready Xray outbound."""

    name: str
    protocol: str
    address: str
    port: int
    outbound: dict

    def summary(self) -> dict:
        """Safe-to-display view: no credentials."""
        return {
            "name": self.name,
            "protocol": self.protocol,
            "address": self.address,
            "port": self.port,
        }


def decode_subscription(text: str) -> str:
    """Return the link list inside a subscription body.

    Panels answer with either plain links or a base64 blob of them; anything
    that already contains ``://`` is treated as plain text.
    """
    body = (text or "").strip()
    if not body or "://" in body:
        return body
    compact = "".join(body.split())
    try:
        raw = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=False)
        decoded = raw.decode("utf-8", errors="strict")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return body
    return decoded if "://" in decoded else body


def extract_links(text: str) -> list[str]:
    """Every share-link line in a subscription body, in panel order.

    Recognizes any ``scheme://`` line (so unsupported protocols can be reported
    in the tunnel status instead of vanishing silently) except plain http(s),
    which panels use for metadata and messages rather than proxy links.
    """
    links = []
    for line in decode_subscription(text).splitlines():
        line = line.strip()
        if ANY_LINK.match(line) and not line.lower().startswith(("http://", "https://")):
            links.append(line)
    return links


def parse_subscription(text: str) -> tuple[list[Server], list[str]]:
    """Parse every usable link. Returns ``(servers, skipped_reasons)``."""
    servers: list[Server] = []
    skipped: list[str] = []
    for link in extract_links(text):
        scheme = link.split("://", 1)[0].lower()
        try:
            if scheme == "vless":
                servers.append(_parse_vless(link))
            elif scheme == "vmess":
                servers.append(_parse_vmess(link))
            elif scheme == "trojan":
                servers.append(_parse_trojan(link))
            elif scheme == "ss":
                servers.append(_parse_ss(link))
            else:
                skipped.append(f"{scheme}: unsupported protocol")
        except Exception as exc:  # noqa: BLE001 - one bad link must not kill the list
            skipped.append(f"{scheme}: {exc}")
    return servers, skipped


def build_client_config(server: Server, port: int) -> dict:
    """Xray client config: one local HTTP proxy inbound, one outbound.

    The router's httpx client speaks HTTP CONNECT to the loopback inbound, so
    no TUN device and no system proxy setting is involved anywhere.
    """
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "router-in",
                "listen": "127.0.0.1",
                "port": port,
                "protocol": "http",
                "settings": {},
            }
        ],
        "outbounds": [dict(server.outbound), {"tag": "blocked", "protocol": "blackhole"}],
    }


# --------------------------------------------------------------------- links


def _b64decode(data: str) -> bytes:
    compact = "".join(data.split())
    return base64.urlsafe_b64decode(compact + "=" * (-len(compact) % 4))


def _params(link: str) -> dict[str, str]:
    parsed = urlparse(link)
    return {key: values[0] for key, values in parse_qs(parsed.query).items()}


def _fragment_name(link: str) -> str:
    try:
        return unquote(urlparse(link).fragment).strip()
    except ValueError:
        return ""


def _split_hostport(value: str) -> tuple[str, int]:
    host, sep, port_text = value.rpartition(":")
    if not sep:
        raise ValueError("missing port")
    host = host.strip("[]").strip()
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("invalid port") from exc
    if not host or not 1 <= port <= 65535:
        raise ValueError("invalid address")
    return host, port


def _stream_settings(
    params: dict[str, str], network: str, security: str, address: str
) -> dict:
    """Map share-link parameters onto an Xray streamSettings object."""
    stream: dict = {"network": network, "security": security}
    if network == "ws":
        ws: dict = {"path": params.get("path") or "/"}
        host = params.get("host")
        if host:
            ws["headers"] = {"Host": host}
        stream["wsSettings"] = ws
    elif network == "grpc":
        grpc: dict = {
            "serviceName": params.get("serviceName") or params.get("path") or ""
        }
        if params.get("mode") == "multi":
            grpc["multiMode"] = True
        stream["grpcSettings"] = grpc
    elif network in {"h2", "http"}:
        h2: dict = {"path": params.get("path") or "/"}
        host = params.get("host")
        if host:
            h2["host"] = [part.strip() for part in host.split(",") if part.strip()]
        stream["httpSettings"] = h2
    elif network == "tcp" and params.get("headerType") == "http":
        request: dict = {"path": [params.get("path") or "/"]}
        host = params.get("host")
        if host:
            request["headers"] = {"Host": [host]}
        stream["tcpSettings"] = {"header": {"type": "http", "request": request}}

    if security == "tls":
        tls: dict = {"serverName": params.get("sni") or params.get("host") or address}
        if params.get("alpn"):
            tls["alpn"] = [
                part.strip() for part in params["alpn"].split(",") if part.strip()
            ]
        if params.get("fp"):
            tls["fingerprint"] = params["fp"]
        if str(params.get("allowInsecure", "")).lower() in {"1", "true", "yes"}:
            tls["allowInsecure"] = True
        stream["tlsSettings"] = tls
    elif security == "reality":
        stream["realitySettings"] = {
            "serverName": params.get("sni") or address,
            "publicKey": params.get("pbk") or "",
            "shortId": params.get("sid") or "",
            "fingerprint": params.get("fp") or "chrome",
            "spiderX": params.get("spx") or "/",
        }
    return stream


def _parse_vless(link: str) -> Server:
    parsed = urlparse(link)
    user_id = unquote(parsed.username or "")
    if not user_id:
        raise ValueError("missing id")
    if not parsed.hostname or not parsed.port:
        raise ValueError("missing address")
    params = _params(link)
    network = (params.get("type") or "tcp").lower()
    if network == "raw":
        network = "tcp"
    security = (params.get("security") or "none").lower()

    user: dict = {"id": user_id}
    # Post-quantum VLESS encryption values (e.g. mlkem768x25519plus...) must be
    # passed through verbatim; Xray decides how to negotiate them.
    user["encryption"] = params.get("encryption") or "none"
    if params.get("flow"):
        user["flow"] = params["flow"]

    outbound = {
        "tag": "proxy",
        "protocol": "vless",
        "settings": {
            "vnext": [
                {"address": parsed.hostname, "port": parsed.port, "users": [user]}
            ]
        },
        "streamSettings": _stream_settings(params, network, security, parsed.hostname),
    }
    return Server(
        name=_fragment_name(link),
        protocol="vless",
        address=parsed.hostname,
        port=parsed.port,
        outbound=outbound,
    )


def _parse_vmess(link: str) -> Server:
    try:
        data = json.loads(_b64decode(link.split("://", 1)[1]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid vmess payload") from exc
    address = str(data.get("add") or "").strip()
    user_id = str(data.get("id") or "").strip()
    try:
        port = int(data.get("port") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid port") from exc
    if not address or not user_id or not 1 <= port <= 65535:
        raise ValueError("missing add/port/id")

    network = str(data.get("net") or "tcp").lower()
    if network == "raw":
        network = "tcp"
    params = {
        "path": str(data.get("path") or ""),
        "host": str(data.get("host") or ""),
        "sni": str(data.get("sni") or ""),
        "serviceName": str(data.get("path") or ""),
        "alpn": str(data.get("alpn") or ""),
    }
    security = "tls" if str(data.get("tls") or "").lower() == "tls" else "none"
    outbound = {
        "tag": "proxy",
        "protocol": "vmess",
        "settings": {
            "vnext": [
                {
                    "address": address,
                    "port": port,
                    "users": [
                        {
                            "id": user_id,
                            "alterId": int(data.get("aid") or 0),
                            "security": str(data.get("scy") or "auto"),
                        }
                    ],
                }
            ]
        },
        "streamSettings": _stream_settings(params, network, security, address),
    }
    name = str(data.get("ps") or "").strip() or f"vmess-{address}"
    return Server(name=name, protocol="vmess", address=address, port=port, outbound=outbound)


def _parse_trojan(link: str) -> Server:
    parsed = urlparse(link)
    password = unquote(parsed.username or "")
    if not password:
        raise ValueError("missing password")
    if not parsed.hostname or not parsed.port:
        raise ValueError("missing address")
    params = _params(link)
    network = (params.get("type") or "tcp").lower()
    if network == "raw":
        network = "tcp"
    security = (params.get("security") or "tls").lower()
    outbound = {
        "tag": "proxy",
        "protocol": "trojan",
        "settings": {
            "servers": [
                {"address": parsed.hostname, "port": parsed.port, "password": password}
            ]
        },
        "streamSettings": _stream_settings(params, network, security, parsed.hostname),
    }
    return Server(
        name=_fragment_name(link),
        protocol="trojan",
        address=parsed.hostname,
        port=parsed.port,
        outbound=outbound,
    )


def _parse_ss(link: str) -> Server:
    body = link.split("://", 1)[1].split("#", 1)[0]
    if "?" in body:
        body, query = body.split("?", 1)
        if "plugin=" in query:
            raise ValueError("Shadowsocks plugins are not supported")
    if "@" in body:
        userinfo, hostport = body.rsplit("@", 1)
        if ":" in userinfo:
            method, _, password = userinfo.partition(":")
        else:
            # Base64 userinfo: "base64(method:password)@host:port".
            method, _, password = _b64decode(userinfo).decode("utf-8").partition(":")
    else:
        # Legacy form: "base64(method:password@host:port)".
        userinfo, _, hostport = (
            _b64decode(body).decode("utf-8").rpartition("@")
        )
        method, _, password = userinfo.partition(":")
    if not method or not password:
        raise ValueError("invalid Shadowsocks credentials")
    address, port = _split_hostport(hostport)
    outbound = {
        "tag": "proxy",
        "protocol": "shadowsocks",
        "settings": {
            "servers": [
                {
                    "address": address,
                    "port": port,
                    "method": method,
                    "password": password,
                }
            ]
        },
    }
    return Server(
        name=_fragment_name(link),
        protocol="ss",
        address=address,
        port=port,
        outbound=outbound,
    )