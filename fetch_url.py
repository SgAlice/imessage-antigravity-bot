#!/usr/bin/env python3
"""Safe read-only webpage fetcher strictly confined to public http/https URLs.

Only allows public http/https URLs (blocks local/private/link-local IPs to prevent SSRF),
extracts title + clean readable text, and caps output length to protect context budget.
"""

import html
import ipaddress
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import List

MAX_BYTES = 2 * 1024 * 1024
MAX_CHARS = 12000
TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


BLOCKED_NETS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)
FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")


def _check_public_url(url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"仅支持 http/https 链接，收到: {parsed.scheme or '空'}")
    host = parsed.hostname
    if not host:
        raise ValueError("URL 缺少主机名")
    lower = host.lower()
    if lower in ("localhost", "localhost.localdomain") or lower.endswith(".local"):
        raise PermissionError(f"禁止访问本地主机: {host}")

    # Reject raw IP literals inside blocked or Fake-IP ranges directly
    host_is_ip = False
    try:
        literal_ip = ipaddress.ip_address(host)
        host_is_ip = True
        if literal_ip in FAKE_IP_NET or any(literal_ip in net for net in BLOCKED_NETS):
            raise PermissionError(f"禁止直接访问内网或保留 IP: {host}")
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise RuntimeError(f"域名解析失败 ({host}): {exc}") from exc

    for _, _, _, _, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        # Allow RFC 2544 198.18.0.0/15 when resolved from a real domain (TUN/Fake-IP proxy)
        if not host_is_ip and ip in FAKE_IP_NET:
            continue
        if any(ip in net for net in BLOCKED_NETS) or ip.is_multicast:
            raise PermissionError(f"禁止访问内网或保留地址: {host} ({ip})")
    return parsed


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ReadableHTMLParser(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "template"}
    BLOCK_TAGS = {
        "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
        "article", "section", "header", "footer", "blockquote", "pre",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.in_title = False
        self.title_parts: List[str] = []
        self.text_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        t = tag.lower()
        if t in self.SKIP_TAGS:
            self.skip_depth += 1
        elif t == "title":
            self.in_title = True
        elif t in self.BLOCK_TAGS and self.skip_depth == 0:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self.SKIP_TAGS and self.skip_depth > 0:
            self.skip_depth -= 1
        elif t == "title":
            self.in_title = False
        elif t in self.BLOCK_TAGS and self.skip_depth == 0:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.skip_depth == 0:
            self.text_parts.append(data)


def fetch(url: str) -> str:
    _check_public_url(url)
    opener = urllib.request.build_opener(SafeRedirectHandler())
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with opener.open(req, timeout=TIMEOUT) as resp:
        final_url = resp.geturl()
        ctype = (resp.headers.get("Content-Type") or "").lower()
        raw = resp.read(MAX_BYTES)
        charset = resp.headers.get_content_charset() or "utf-8"

    try:
        decoded = raw.decode(charset, errors="replace")
    except LookupError:
        decoded = raw.decode("utf-8", errors="replace")

    if "html" in ctype or "<html" in decoded[:500].lower():
        parser = ReadableHTMLParser()
        parser.feed(decoded)
        title = re.sub(r"\s+", " ", html.unescape("".join(parser.title_parts))).strip()
        body = "".join(parser.text_parts)
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in body.splitlines()]
        body = "\n".join(ln for ln in lines if ln)
    else:
        title = ""
        body = decoded.strip()

    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS] + "\n...(正文过长，已截断)"

    header = [f"URL: {final_url}"]
    if title:
        header.append(f"Title: {title}")
    return "\n".join(header) + "\n\n" + body


def main() -> None:
    if len(sys.argv) != 2:
        print("用法: ./fetch_url.py <https://...>", file=sys.stderr)
        sys.exit(2)
    try:
        print(fetch(sys.argv[1]))
    except Exception as exc:
        print(f"抓取失败: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
