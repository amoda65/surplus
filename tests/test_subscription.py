"""Unit tests for subscription parsing and Xray client-config generation.

All links below are synthetic (fake UUIDs/passwords) but shaped like real
panel output, including the post-quantum VLESS ``encryption`` parameter.
"""

from __future__ import annotations

import base64
import json

from surplus.subscription import (
    build_client_config,
    decode_subscription,
    extract_links,
    parse_subscription,
)

UUID = "f79b1111-2222-3333-4444-555566667777"
PQ_ENCRYPTION = "mlkem768x25519plus.native.0rtt.oogUGm5ydl3s-7Kle4FYzX4CZg"
HOST_FRONT = "turbop1.wpsolv.example"


# ------------------------------------------------------------------ decoding


def test_decode_base64_blob() -> None:
    links = "vless://a@b:1\nvless://c@d:2"
    blob = base64.b64encode(links.encode()).decode()
    assert decode_subscription(blob) == links


def test_decode_plain_text_is_passthrough() -> None:
    assert decode_subscription("vless://a@b:1") == "vless://a@b:1"


def test_decode_garbage_returns_input() -> None:
    assert decode_subscription("???") == "???"


def test_extract_links_keeps_order_and_ignores_noise() -> None:
    text = "# a comment\nvless://a@b:1\n\nhttp://panel/config\nvmess://zzzz\n"
    links = extract_links(text)
    assert links == ["vless://a@b:1", "vmess://zzzz"]


# --------------------------------------------------------------------- vless


def test_parse_vless_ws_host_header_and_pq_encryption() -> None:
    """The dominant real-world shape: ws on :80, no TLS, CDN front + Host."""
    link = (
        f"vless://{UUID}@snapp.example:80"
        f"?encryption={PQ_ENCRYPTION}&host={HOST_FRONT}&path=/&security=none&type=ws"
        "#TurboPlus"
    )
    servers, skipped = parse_subscription(link)
    assert skipped == []
    assert len(servers) == 1
    server = servers[0]
    assert server.protocol == "vless"
    assert server.address == "snapp.example"
    assert server.port == 80
    assert server.name == "TurboPlus"

    outbound = server.outbound
    assert outbound["protocol"] == "vless"
    user = outbound["settings"]["vnext"][0]["users"][0]
    assert user["id"] == UUID
    assert user["encryption"] == PQ_ENCRYPTION  # verbatim passthrough

    stream = outbound["streamSettings"]
    assert stream["network"] == "ws"
    assert stream["security"] == "none"
    assert stream["wsSettings"]["path"] == "/"
    assert stream["wsSettings"]["headers"]["Host"] == HOST_FRONT
    assert "tlsSettings" not in stream


def test_parse_vless_reality() -> None:
    link = (
        f"vless://{UUID}@re.example:443"
        "?type=tcp&security=reality&pbk=PUBKEY&sid=abcd&sni=re.example"
        "&fp=chrome&flow=xtls-rprx-vision"
        "#reality"
    )
    servers, skipped = parse_subscription(link)
    assert skipped == []
    server = servers[0]
    stream = server.outbound["streamSettings"]
    assert stream["security"] == "reality"
    reality = stream["realitySettings"]
    assert reality["publicKey"] == "PUBKEY"
    assert reality["shortId"] == "abcd"
    assert reality["serverName"] == "re.example"
    user = server.outbound["settings"]["vnext"][0]["users"][0]
    assert user["flow"] == "xtls-rprx-vision"
    assert user["encryption"] == "none"


def test_parse_vless_tls_grpc() -> None:
    link = (
        f"vless://{UUID}@grpc.example:443"
        "?type=grpc&security=tls&sni=grpc.example&serviceName=svc&mode=multi&alpn=h2,http/1.1"
        "#g"
    )
    servers, _ = parse_subscription(link)
    stream = servers[0].outbound["streamSettings"]
    assert stream["network"] == "grpc"
    assert stream["security"] == "tls"
    assert stream["grpcSettings"]["serviceName"] == "svc"
    assert stream["grpcSettings"]["multiMode"] is True
    assert stream["tlsSettings"]["serverName"] == "grpc.example"
    assert stream["tlsSettings"]["alpn"] == ["h2", "http/1.1"]


def test_vless_without_id_is_skipped() -> None:
    servers, skipped = parse_subscription("vless://@host.example:80?type=ws")
    assert servers == []
    assert len(skipped) == 1
    assert "vless" in skipped[0]


# --------------------------------------------------------------------- vmess


def test_parse_vmess() -> None:
    payload = {
        "v": "2",
        "ps": "vm-test",
        "add": "vm.example",
        "port": "443",
        "id": UUID,
        "aid": "0",
        "scy": "auto",
        "net": "ws",
        "type": "none",
        "host": HOST_FRONT,
        "path": "/ws",
        "tls": "tls",
        "sni": "vm.example",
    }
    link = "vmess://" + base64.b64encode(json.dumps(payload).encode()).decode()
    servers, skipped = parse_subscription(link)
    assert skipped == []
    server = servers[0]
    assert server.name == "vm-test"
    assert server.address == "vm.example"
    assert server.port == 443
    outbound = server.outbound
    assert outbound["protocol"] == "vmess"
    user = outbound["settings"]["vnext"][0]["users"][0]
    assert user["id"] == UUID
    assert user["alterId"] == 0
    stream = outbound["streamSettings"]
    assert stream["security"] == "tls"
    assert stream["wsSettings"]["headers"]["Host"] == HOST_FRONT
    assert stream["tlsSettings"]["serverName"] == "vm.example"


# -------------------------------------------------------------------- trojan


def test_parse_trojan_decodes_percent_password() -> None:
    link = "trojan://pass%40word@tro.example:443?type=tcp&security=tls&sni=tro.example#tro"
    servers, skipped = parse_subscription(link)
    assert skipped == []
    server = servers[0]
    entry = server.outbound["settings"]["servers"][0]
    assert entry["address"] == "tro.example"
    assert entry["port"] == 443
    assert entry["password"] == "pass@word"
    assert server.outbound["streamSettings"]["security"] == "tls"


# ------------------------------------------------------------------------ ss


def test_parse_ss_modern_b64_userinfo() -> None:
    userinfo = base64.urlsafe_b64encode(b"aes-256-gcm:passw0rd").decode().rstrip("=")
    link = f"ss://{userinfo}@ss.example:8388#ss1"
    servers, skipped = parse_subscription(link)
    assert skipped == []
    entry = servers[0].outbound["settings"]["servers"][0]
    assert entry["method"] == "aes-256-gcm"
    assert entry["password"] == "passw0rd"
    assert entry["address"] == "ss.example"
    assert entry["port"] == 8388


def test_parse_ss_legacy_whole_b64() -> None:
    blob = base64.urlsafe_b64encode(b"aes-128-gcm:pw@ss.example:8388").decode().rstrip("=")
    link = f"ss://{blob}#ss2"
    servers, skipped = parse_subscription(link)
    assert skipped == []
    entry = servers[0].outbound["settings"]["servers"][0]
    assert entry["method"] == "aes-128-gcm"
    assert entry["password"] == "pw"
    assert entry["address"] == "ss.example"
    assert entry["port"] == 8388


def test_ss_with_plugin_is_skipped() -> None:
    servers, skipped = parse_subscription(
        "ss://YWVzLTI1Ni1nY206cGFzc3dvcmQ@ss.example:8388?plugin=obfs-local#p"
    )
    assert servers == []
    assert any("plugin" in reason for reason in skipped)


# -------------------------------------------------------------- unsupported


def test_unsupported_protocol_is_reported() -> None:
    servers, skipped = parse_subscription("hysteria2://abc@h:443?sni=x#h")
    assert servers == []
    assert any("unsupported protocol" in reason for reason in skipped)


def test_bad_link_does_not_kill_the_list() -> None:
    text = "vless://broken\nvless://ok@example:80?type=ws#ok"
    servers, skipped = parse_subscription(text)
    assert len(servers) == 1
    assert servers[0].address == "example"
    assert len(skipped) == 1


# ------------------------------------------------------------ client config


def test_build_client_config_shape() -> None:
    link = f"vless://{UUID}@snapp.example:80?type=ws&host={HOST_FRONT}&security=none#x"
    server = parse_subscription(link)[0][0]
    config = build_client_config(server, 15432)
    inbound = config["inbounds"][0]
    assert inbound["listen"] == "127.0.0.1"
    assert inbound["port"] == 15432
    assert inbound["protocol"] == "http"
    assert config["outbounds"][0] == server.outbound
    assert config["outbounds"][1]["protocol"] == "blackhole"
    # the generated config is JSON-serializable end to end
    json.dumps(config)


def test_server_summary_hides_credentials() -> None:
    link = f"vless://{UUID}@snapp.example:80?type=ws&security=none#n"
    server = parse_subscription(link)[0][0]
    summary = server.summary()
    assert summary == {
        "name": "n",
        "protocol": "vless",
        "address": "snapp.example",
        "port": 80,
    }
    assert UUID not in str(summary)