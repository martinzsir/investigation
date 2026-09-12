"""
core/llm/endpoint_guard.py
ADR-V-8 端点闸：local/cloud 两档部署的出网端点边界校验（调用前硬检查）。

local 档（本机/内网模型，数据不出机的纯离线立场）：
  - scheme http/https 均可（HTTP 不出网卡）；
  - host 必须 DNS 解析成功，且**全部**解析后 IP 落在 loopback
    （127.0.0.0/8、::1）或 allow_private_cidr 声明的内网段（缺省 RFC1918）；
  - 域名先解析、按解析后 IP 逐一复核——防"内网域名"解析到公网 IP 绕过；
  - DNS 解析失败 = 不可达 = 拒（不发起请求）。

cloud 档（公网 API，唯一出网面）：
  - https_only（缺省 true，fail-closed）：明文 http 硬失败；
  - host 必须精确命中 host_whitelist；未声明/空白名单 = 全拒。

校验失败抛 EndpointBlocked（LLMBlockedError 子类）；调用方落
llm_call_log(blocked_reason) 后**不得发起任何请求**。
DNS 解析函数可注入（测试假解析，不依赖真实网络）。
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Callable
from urllib.parse import urlparse

from core.access import LLMBlockedError

#: local 档缺省内网段（ADR-V-8：仅 loopback + RFC1918）
DEFAULT_LOCAL_CIDRS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")


class EndpointBlocked(LLMBlockedError):
    """端点闸拒绝：base_url 越出部署档边界（请求根本不发起）。"""


def _resolve_host(host: str) -> list[str]:
    """真实 DNS 解析：返回去重排序的 IP 字符串列表。"""
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None)})


def assert_endpoint_allowed(mode: str, base_url: str,
                            endpoints: dict | None = None, *,
                            resolve: Callable[[str], list[str]]
                            | None = None) -> None:
    """校验 base_url 是否落在 mode 档端点边界内；越界抛 EndpointBlocked。

    endpoints 取案件包 llm_policy.json 对应部署档的 endpoints 段
    （缺省 None/非 dict 按该档最严缺省处理）。
    resolve 可注入假解析（测试）；缺省真实 getaddrinfo。
    """
    resolve = resolve or _resolve_host
    eps = endpoints if isinstance(endpoints, dict) else {}
    u = urlparse(str(base_url or ""))
    host = (u.hostname or "").strip().rstrip(".")
    if not host:
        raise EndpointBlocked(f"base_url 无可解析主机：{base_url!r}")

    if mode == "cloud":
        if eps.get("https_only", True) and u.scheme != "https":
            raise EndpointBlocked(
                f"cloud 档仅允许 HTTPS（https_only=true），收到 {u.scheme!r}://")
        wl = eps.get("host_whitelist")
        if not isinstance(wl, list) or not wl:
            raise EndpointBlocked(
                "cloud 档未声明 host_whitelist（fail-closed：不白名单即全拒）")
        allowed = {str(h).strip().lower().rstrip(".") for h in wl}
        if host.lower() not in allowed:
            raise EndpointBlocked(
                f"host {host!r} 不在 cloud host_whitelist {sorted(allowed)}")
        return

    if mode == "local":
        try:
            ips = resolve(host)
        except (socket.gaierror, OSError) as e:
            raise EndpointBlocked(
                f"local 档端点 DNS 解析失败：{host!r}（{e}）；不发起请求")
        if not ips:
            raise EndpointBlocked(
                f"local 档端点 DNS 解析无地址：{host!r}；不发起请求")
        cidrs = [ipaddress.ip_network(c)
                 for c in eps.get("allow_private_cidr", DEFAULT_LOCAL_CIDRS)]
        allow_loopback = eps.get("allow_loopback", True)
        for ip_s in ips:
            ip = ipaddress.ip_address(ip_s)
            if allow_loopback and ip.is_loopback:
                continue
            if any(ip in net for net in cidrs):
                continue
            raise EndpointBlocked(
                f"local 档端点越界：{host!r} 解析到公网/非内网地址 {ip}"
                f"（仅允许 loopback/RFC1918；防内网域名解析到公网 IP 绕过）")
        return

    raise EndpointBlocked(f"未知部署档：{mode!r}（仅 local/cloud）")
