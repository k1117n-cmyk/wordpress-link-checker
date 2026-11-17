#!/usr/bin/env python3
"""Read-only public WordPress link checker. Python 3.11+, standard library only."""

from __future__ import annotations

import argparse
from collections import Counter, deque
import contextlib
import csv
from datetime import datetime, timezone, timedelta
from email.message import Message
from email.utils import parsedate_to_datetime
import fcntl
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import math
from pathlib import Path
import queue
import re
import socket
import ssl
import sys
import threading
import time
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
import uuid
import xml.etree.ElementTree as ET
import zlib


ROOT = Path(__file__).resolve().parents[1]
JST = timezone(timedelta(hours=9))
VERSION = "0.2.0"
UA = f"WordPressLinkCheck/{VERSION}"
REDIRECTS = {301, 302, 303, 307, 308}
AMAZON_DOMAINS = frozenset({
    "amazon.co.jp", "amazon.jp", "amazon.com", "amazon.ca", "amazon.com.mx",
    "amazon.com.br", "amazon.co.uk", "amazon.de", "amazon.fr", "amazon.it",
    "amazon.es", "amazon.nl", "amazon.com.be", "amazon.se", "amazon.pl",
    "amazon.ie", "amazon.in", "amazon.com.au", "amazon.sg", "amazon.ae",
    "amazon.sa", "amazon.com.tr", "amazon.eg", "amazon.co.za",
    "amzn.to", "amzn.asia", "amzn.eu", "amzn.in", "amzn.com", "a.co", "link.amazon",
})
DEFAULTS = {
    "collection": "sitemap", "sitemap_urls": [], "additional_urls": [],
    "allowed_origins": [], "allowed_path_prefixes": ["/"],
    "exclude_patterns": [], "check_images": False, "crawl_internal": False,
    "connect_timeout": 5, "request_timeout": 20, "request_interval": 1,
    "max_redirects": 5, "max_pages": 5000, "max_links": 50000,
    "html_limit": 5 * 1024 * 1024, "link_limit": 256 * 1024,
    "sitemap_limit": 10 * 1024 * 1024, "max_sitemaps": 100,
    "max_sitemap_depth": 5, "max_runtime": 7200,
    "confirm_delay": 60, "retry_delays": [10, 60],
    "output_dir": "reports/linkcheck", "soft_404_patterns": [],
}


def now():
    return datetime.now(JST).isoformat(timespec="seconds")


class StopCheck(Exception):
    pass


class UnsafeURL(ValueError):
    pass


def normalize(url):
    if not isinstance(url, str):
        raise UnsafeURL("URLは文字列で指定してください")
    if re.search(r"[\x00-\x20\x7f]", url) or "\\" in url:
        raise UnsafeURL("URLに空白・制御文字・バックスラッシュがあります")
    p = urlsplit(url)
    if p.scheme.lower() not in {"http", "https"} or not p.hostname:
        raise UnsafeURL("HTTP/HTTPSの絶対URLが必要です")
    if p.username is not None or p.password is not None:
        raise UnsafeURL("認証情報を含むURLは使用できません")
    host = p.hostname.encode("idna").decode("ascii").lower()
    port = p.port
    if port not in {None, 80, 443}:
        raise UnsafeURL("許可ポートは80・443のみです")
    authority = f"[{host}]" if ":" in host else host
    if port and port != (443 if p.scheme.lower() == "https" else 80):
        authority += f":{port}"
    return urlunsplit((p.scheme.lower(), authority,
                      quote(p.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                      quote(p.query, safe="%/?@:!$&'()*+,;=-._~"), ""))


def origin(url):
    p = urlsplit(normalize(url))
    return f"{p.scheme}://{p.netloc}"


def load_config(path):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("設定はJSONオブジェクトで指定してください")
    unknown = set(raw) - set(DEFAULTS) - {"site_id", "site_url"}
    if unknown:
        raise ValueError(f"不明な設定項目: {sorted(unknown)}")
    c = {**DEFAULTS, **raw}
    if not isinstance(c.get("site_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", c["site_id"]):
        raise ValueError("site_idは英数字・ハイフン・アンダースコアで指定してください")
    c["site_url"] = normalize(c.get("site_url", ""))
    if not isinstance(c["collection"], str) or c["collection"] not in {"sitemap", "rest", "urls"}:
        raise ValueError("collectionはsitemap・rest・urlsのいずれかです")
    for key in ("sitemap_urls", "additional_urls", "allowed_origins", "allowed_path_prefixes",
                "exclude_patterns", "soft_404_patterns"):
        if not isinstance(c[key], list) or not all(isinstance(x, str) for x in c[key]):
            raise ValueError(f"{key}は文字列の配列で指定してください")
    for key in ("check_images", "crawl_internal"):
        if not isinstance(c[key], bool):
            raise ValueError(f"{key}は真偽値で指定してください")
    for key in ("connect_timeout", "request_timeout", "request_interval", "max_runtime", "confirm_delay"):
        if type(c[key]) not in (int, float) or not math.isfinite(c[key]) or c[key] <= 0:
            raise ValueError(f"{key}は正の有限数で指定してください")
    for key in ("max_redirects", "max_pages", "max_links", "html_limit", "link_limit",
                "sitemap_limit", "max_sitemaps", "max_sitemap_depth"):
        if type(c[key]) is not int or c[key] < 1:
            raise ValueError(f"{key}は正の整数で指定してください")
    if (not isinstance(c["retry_delays"], list) or len(c["retry_delays"]) != 2
            or any(type(x) not in (int, float) or not math.isfinite(x) or x <= 0
                   for x in c["retry_delays"])):
        raise ValueError("retry_delaysは正の数2個の配列で指定してください")
    if c["request_interval"] < 1 or c["confirm_delay"] < 60 or c["retry_delays"][0] < 10 or c["retry_delays"][1] < 60:
        raise ValueError("要求間隔1秒・再確認60秒・再試行10/60秒の下限を守ってください")
    c["allowed_origins"] = list(dict.fromkeys([origin(c["site_url"])] + [origin(x) for x in c["allowed_origins"]]))
    if not c["allowed_path_prefixes"] or any(not x.startswith("/") for x in c["allowed_path_prefixes"]):
        raise ValueError("allowed_path_prefixesは/で始まるパスを指定してください")
    for key in ("sitemap_urls", "additional_urls"):
        c[key] = [normalize(urljoin(c["site_url"], x)) for x in c[key]]
        if any(origin(x) not in c["allowed_origins"] for x in c[key]):
            raise ValueError(f"{key}に未許可の掲載元オリジンがあります")
    for pattern in c["exclude_patterns"] + c["soft_404_patterns"]:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"正規表現が不正です: {pattern}: {exc}") from exc
    if not isinstance(c["output_dir"], str) or not c["output_dir"]:
        raise ValueError("output_dirは空でないパスで指定してください")
    out = Path(c["output_dir"])
    c["output_dir"] = str(out if out.is_absolute() else ROOT / out)
    return c


def retry_after(value, default=60):
    try:
        if value.strip().isdigit():
            return max(0, int(value))
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, AttributeError, OverflowError):
        return default


def public_addresses(host, port, timeout):
    """Bound DNS wait and return socket addresses; never resolve again at connect."""
    q = queue.Queue(maxsize=1)

    def resolve():
        try:
            q.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError as exc:
            q.put(exc)

    threading.Thread(target=resolve, daemon=True).start()
    try:
        entries = q.get(timeout=timeout)
    except queue.Empty as exc:
        raise TimeoutError("DNS解決タイムアウト") from exc
    if isinstance(entries, Exception):
        raise entries
    if not entries:
        raise socket.gaierror("DNSの応答が空です")
    for _, _, _, _, address in entries:
        ip = ipaddress.ip_address(address[0].split("%")[0])
        if not ip.is_global or (ip.version == 6 and ip.ipv4_mapped and not ip.ipv4_mapped.is_global):
            raise UnsafeURL("DNS解決先に非公開IPが含まれます")
    return entries


def wire_get(url, limit, connect_timeout, total_timeout):
    """Single GET, pinned IP, verified TLS, absolute deadline, bounded decoding."""
    p = urlsplit(url)
    port = p.port or (443 if p.scheme == "https" else 80)
    deadline = time.monotonic() + total_timeout
    entries = public_addresses(p.hostname, port, min(connect_timeout, total_timeout))
    conn = http.client.HTTPConnection(p.hostname, port)
    active = [None]

    def abort():
        if active[0] is not None:
            with contextlib.suppress(OSError):
                active[0].shutdown(socket.SHUT_RDWR)

    timer = threading.Timer(max(.001, deadline - time.monotonic()), abort)
    timer.daemon = True
    timer.start()
    try:
        last = None
        for family, kind, proto, _, address in entries:
            if time.monotonic() >= deadline:
                raise TimeoutError("要求の総時間上限")
            sock = socket.socket(family, kind, proto)
            active[0] = sock
            sock.settimeout(max(.001, min(connect_timeout, deadline - time.monotonic())))
            try:
                sock.connect(address)
                if p.scheme == "https":
                    ctx = ssl.create_default_context()
                    ctx.set_alpn_protocols(["http/1.1"])
                    sock = ctx.wrap_socket(sock, server_hostname=p.hostname, do_handshake_on_connect=False)
                    active[0] = sock
                    sock.do_handshake()
                conn.sock = sock
                break
            except ssl.SSLError:
                sock.close()
                raise
            except OSError as exc:
                sock.close()
                last = exc
        else:
            raise last or OSError("接続できません")
        conn.sock.settimeout(max(.001, deadline - time.monotonic()))
        target = urlunsplit(("", "", p.path or "/", p.query, ""))
        conn.request("GET", target, headers={"User-Agent": UA, "Accept-Encoding": "identity", "Connection": "close"})
        response = conn.getresponse()
        headers = {k.lower(): v for k, v in response.getheaders()}
        encoding = headers.get("content-encoding", "identity").lower()
        if encoding not in {"identity", "gzip", "deflate"}:
            raise ValueError(f"未対応のContent-Encoding: {encoding}")
        decoder = zlib.decompressobj(31 if encoding == "gzip" else zlib.MAX_WBITS) if encoding != "identity" else None
        body = bytearray()
        transferred = 0
        truncated = False
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("要求の総時間上限")
            raw = response.read1(min(16384, limit + 1 - transferred))
            if not raw:
                if response.length:
                    raise http.client.IncompleteRead(bytes(body))
                if decoder and not decoder.eof:
                    raise ValueError("圧縮本文が途中で終了しました")
                break
            transferred += len(raw)
            body.extend(decoder.decompress(raw, limit + 1 - len(body)) if decoder else raw)
            if len(body) > limit or transferred > limit:
                truncated = True
                break
        if time.monotonic() >= deadline:
            raise TimeoutError("要求の総時間上限")
        return {"status": response.status, "headers": headers, "body": bytes(body[:limit]),
                "truncated": truncated}
    finally:
        timer.cancel()
        conn.close()
        if active[0] is not None:
            active[0].close()


def decode_body(result):
    msg = Message()
    msg["content-type"] = result.get("headers", {}).get("content-type", "")
    encoding = msg.get_content_charset()
    body = result.get("body", b"")
    if not encoding:
        match = re.search(br'<meta\b[^>]*charset\s*=\s*[\x22\x27]?\s*([a-zA-Z0-9_-]+)', body[:8192], re.I)
        encoding = match[1].decode("ascii") if match else "utf-8-sig"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


class PageParser(HTMLParser):
    def __init__(self, images=False):
        super().__init__(convert_charrefs=True)
        self.images = images
        self.links = []
        self.base = None
        self.title = ""
        self.in_title = False
        self.anchor = None
        self.counts = Counter()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.counts[tag] += 1
        if tag == "base" and self.base is None and a.get("href"):
            self.base = a["href"].strip()
        if tag == "title":
            self.in_title = True
        if tag == "a":
            self.anchor = None
            if "href" in a:
                self.anchor = {"element": "a", "position": self.counts[tag], "raw_url": a["href"] or "", "text": ""}
                self.links.append(self.anchor)
        if tag == "img":
            alt = a.get("alt") or ""
            if self.anchor:
                self.anchor["text"] += alt
            if self.images:
                if "src" in a:
                    self.links.append({"element": "img[src]", "position": self.counts[tag], "raw_url": a["src"] or "", "text": alt})
                # srcset URLs may contain commas (notably data: URLs).
                for value in parse_srcset(a.get("srcset") or ""):
                    self.links.append({"element": "img[srcset]", "position": self.counts[tag], "raw_url": value, "text": alt})

    def handle_endtag(self, tag):
        if tag == "a":
            self.anchor = None
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.anchor is not None:
            self.anchor["text"] += data


def parse_srcset(value):
    results = []
    while value:
        value = value.lstrip(" \t\r\n\f,")
        if not value:
            break
        match = re.match(r"[^\s]+", value)
        token = match[0]
        value = value[len(token):]
        if token.endswith(","):
            results.append(token.rstrip(","))
        else:
            results.append(token)
            # Skip density/width descriptors, not URL commas.
            _, sep, value = value.partition(",")
            if not sep:
                break
    return results


class RobotsRules(RobotFileParser):
    """RFC 9309 wildcard/longest-match rules, with stdlib delay extensions."""

    def parse(self, lines):
        super().parse(lines)
        self.groups = []
        agents, rules, records = [], [], False
        for line in lines:
            key, sep, value = line.split("#", 1)[0].partition(":")
            if not sep:
                continue
            key, value = key.strip().lower().lstrip("\ufeff"), value.strip()
            if key == "user-agent":
                if records:
                    self.groups.append((agents, rules))
                    agents, rules, records = [], [], False
                agents.append(value.lower())
            elif agents:
                records = True
                if key in {"allow", "disallow"} and value.startswith("/"):
                    rules.append((key == "allow", self.octets(value)))
        self.groups.append((agents, rules))

    @staticmethod
    def octets(value):
        value = quote(value, safe="/%?@!$&'()*+,;=:-._~")
        def replace(match):
            byte = int(match[0][1:], 16)
            char = chr(byte)
            return char if char.isascii() and (char.isalnum() or char in "-._~") else match[0].upper()
        return re.sub(r"%[0-9a-fA-F]{2}", replace, value)

    def can_fetch(self, useragent, url):
        token = useragent.split("/", 1)[0].lower()
        specific = [rules for agents, rules in self.groups if token in agents]
        groups = specific or [rules for agents, rules in self.groups if "*" in agents]
        p = urlsplit(url)
        path = self.octets((p.path or "/") + ("?" + p.query if p.query else ""))
        if p.path == "/robots.txt":
            return True
        matches = []
        for rules in groups:
            for allow, pattern in rules:
                anchored = pattern.endswith("$")
                literal = pattern[:-1] if anchored else pattern
                expression = "^" + ".*".join(re.escape(part) for part in literal.split("*")) + ("$" if anchored else "")
                if re.search(expression, path):
                    length = len(re.sub(r"%[0-9A-F]{2}", "x", literal.replace("*", "")))
                    matches.append((length, allow))
        return max(matches, default=(0, True))[1]


class Client:
    def __init__(self, config, log, deadline, transport=wire_get):
        self.c = config
        self.log = log
        self.deadline = deadline
        self.transport = transport
        self.next_request = {}
        self.robots = {}
        self.delays = {}

    def wait_until(self, target):
        if target >= self.deadline:
            raise StopCheck("待機時間が実行期限を超えます")
        while time.monotonic() < target:
            if time.monotonic() >= self.deadline:
                raise StopCheck("実行時間上限")
            time.sleep(min(.2, target - time.monotonic()))

    def excluded(self, url):
        # Check the actual hostname before fetching even robots.txt. Use domain
        # boundaries so unrelated hosts such as amazon.com.example.org are safe.
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        if any(host == domain or host.endswith("." + domain) for domain in AMAZON_DOMAINS):
            return "Amazon関連リンク（クリック集計への影響防止）"
        decoded = unquote(url)
        p = urlsplit(decoded)
        if re.search(r"/(?:wp-admin|wp-login\.php|logout|log-out|cart|checkout)(?:/|$)", p.path, re.I):
            return "管理・認証・購入操作パス"
        if re.search(r"(?:^|&)(?:add-to-cart|remove_item|logout|_wpnonce|action)=", p.query, re.I):
            return "操作系クエリ"
        if any(re.search(x, decoded) for x in self.c["exclude_patterns"]):
            return "設定による除外"
        return ""

    def one(self, url, limit, purpose):
        host = origin(url)
        self.wait_until(max(time.monotonic(), self.next_request.get(host, 0)))
        started = now()
        self.next_request[host] = time.monotonic() + max(self.c["request_interval"], self.delays.get(host, 0))
        try:
            result = self.transport(url, limit, self.c["connect_timeout"],
                                    min(self.c["request_timeout"], self.deadline - time.monotonic()))
            result.setdefault("error", "")
        except UnsafeURL as exc:
            result = {"error": "unsafe_url", "reason": str(exc)}
        except ssl.SSLError as exc:
            result = {"error": "tls", "reason": str(exc)}
        except socket.gaierror as exc:
            result = {"error": "dns", "reason": str(exc)}
        except (TimeoutError, socket.timeout) as exc:
            result = {"error": "timeout", "reason": str(exc)}
        except (OSError, http.client.HTTPException, ValueError, zlib.error) as exc:
            result = {"error": "connection", "reason": str(exc)}
        result.update(url=url, first_checked=started, last_checked=now())
        result.setdefault("status", "")
        result.setdefault("headers", {})
        result.setdefault("body", b"")
        result.setdefault("truncated", False)
        if result["status"] == 429:
            self.next_request[host] = max(self.next_request[host], time.monotonic() + retry_after(result["headers"].get("retry-after")))
        record = {k: v for k, v in result.items() if k != "body"}
        record.update(method="GET", purpose=purpose)
        self.log.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.log.flush()
        return result

    def robots_allowed(self, url):
        host = origin(url)
        if host not in self.robots:
            result = self.fetch(host + "/robots.txt", self.c["link_limit"], "robots", robots=False)
            rp = RobotsRules()
            if result["status"] == 404:
                rp.parse([])
            elif result.get("error") or not 200 <= (result["status"] or 0) < 300 or result["truncated"]:
                self.robots[host] = None
                return "robots_unknown"
            else:
                rp.parse(decode_body(result).splitlines())
                self.delays[host] = rp.crawl_delay(UA) or 0
                rate = rp.request_rate(UA)
                if rate and rate.requests > 0:
                    self.delays[host] = max(self.delays[host], rate.seconds / rate.requests)
                self.next_request[host] = max(self.next_request.get(host, 0), time.monotonic() + self.delays[host])
            self.robots[host] = rp
        rp = self.robots[host]
        return "robots_unknown" if rp is None else ("" if rp.can_fetch(UA, url) else "robots_excluded")

    def fetch(self, url, limit, purpose="link", robots=True, source=False):
        seen, history = set(), []
        result = {"status": "", "headers": {}, "body": b"", "truncated": False}
        for hop in range(self.c["max_redirects"] + 1):
            try:
                url = normalize(url)
                if source and origin(url) not in self.c["allowed_origins"]:
                    raise UnsafeURL("掲載元の転送先が許可オリジン外です")
                if purpose == "source" and not any(urlsplit(url).path.startswith(x) for x in self.c["allowed_path_prefixes"]):
                    raise UnsafeURL("掲載元の転送先が許可パス範囲外です")
                if url in seen:
                    result.update(error="redirect", reason="転送ループ")
                    break
                seen.add(url)
                reason = self.excluded(url)
                if reason:
                    result.update(error="excluded", reason=reason)
                    break
                if robots:
                    rule = self.robots_allowed(url)
                    if rule:
                        result.update(error=rule, reason=rule)
                        break
                result = self.one(url, limit, purpose)
            except (ValueError, UnicodeError) as exc:
                result.update(error="unsafe_url", reason=str(exc))
                break
            history.append({k: v for k, v in result.items() if k != "body"})
            if result["status"] not in REDIRECTS or result.get("error"):
                break
            location = result["headers"].get("location")
            if not location or hop == self.c["max_redirects"]:
                result.update(error="redirect", reason="転送先欠落または転送回数超過")
                break
            url = urljoin(url, location)
        result.update(final_url=url, history=history)
        return result


def classify(r):
    err, status = r.get("error"), r.get("status") or 0
    if err:
        return {"excluded": "excluded", "robots_excluded": "excluded",
                "robots_unknown": "unprocessed", "tls": "tls_error",
                "unsafe_url": "invalid_url", "redirect": "redirect_error"}.get(err, "unreachable")
    if 200 <= status < 300:
        return "redirected" if len(r.get("history", [])) > 1 else "ok"
    if status in {404, 410}:
        return "candidate"
    if status in {401, 403, 429}:
        return "pending"
    return "unreachable" if status == 408 or status >= 500 else "review"


def inspect_link(client, url, config):
    attempts = []
    try:
        r = client.fetch(url, config["link_limit"])
        attempts.append(r)
        first = classify(r)
        kind = first
        waits = [config["confirm_delay"]] if first == "candidate" else (
            config["retry_delays"] if first == "unreachable" or r["status"] == 429 else [])
        for delay in waits:
            if r["status"] == 429:
                delay = retry_after(r["headers"].get("retry-after"))
            client.wait_until(time.monotonic() + delay)
            r = client.fetch(url, config["link_limit"])
            attempts.append(r)
            current = classify(r)
            if current in {"ok", "redirected"}:
                kind = "recovered"
                break
            if first == "candidate":
                kind = "broken" if current == "candidate" else "review"
                break
            kind = "review" if current == "candidate" else current
            if current != "unreachable" and r["status"] != 429:
                break
    except StopCheck as exc:
        r = attempts[-1] if attempts else {"status": "", "final_url": url}
        kind = "candidate" if attempts and classify(r) == "candidate" else "unprocessed"
        r = {**r, "reason": str(exc), "incomplete": True}
    if kind in {"ok", "redirected", "recovered"} and any(
            re.search(p, decode_body(r)) for p in config["soft_404_patterns"]):
        kind = "soft_404_suspected"
    return {"classification": kind, "status": r["status"], "final_url": r.get("final_url", url),
            "reason": r.get("reason", ""), "attempts": len(attempts),
            "first_checked": attempts[0]["history"][0]["first_checked"] if attempts and attempts[0].get("history") else "",
            "last_checked": now(), "incomplete": r.get("incomplete", False) or kind == "unprocessed"}


REPORT_LABELS = {
    "broken": "再確認済みリンク切れ", "candidate": "リンク切れ候補（再確認待ち）",
    "unreachable": "到達失敗", "pending": "確認保留（認証・アクセス拒否・頻度制限）",
    "tls_error": "TLSエラー", "redirect_error": "転送エラー", "invalid_url": "不正なURL",
    "review": "要確認", "soft_404_suspected": "エラーページの疑い", "unprocessed": "未処理",
}

REPORT_DESCRIPTIONS = {
    "broken": "再確認でも404（見つからない）・410（削除済み）。リンク切れ候補としてブラウザで確認してください。",
    "candidate": "初回404・410。時間上限などで再確認が終わっていないため、リンク切れは未確定です。",
    "unreachable": "DNS・接続・タイムアウト・408・5xxなどで到達を確認できません。一時的な障害の可能性もあります。",
    "pending": "401（認証が必要）・403（アクセス拒否）・429（頻度制限）のため確認保留。リンク切れとは限りません。",
    "tls_error": "証明書など、HTTPSの安全な接続を確認する処理でエラーになりました。",
    "redirect_error": "転送ループ、転送先の欠落、転送回数の上限超過などで確認できませんでした。",
    "invalid_url": "URLの形式が不正、または非公開IP・許可外ポートなどアクセスできない接続先です。",
    "review": "通常の到達成功・リンク切れ候補などに分類できない応答、または再確認結果の変化があり、個別確認が必要です。",
    "soft_404_suspected": "成功応答でも、本文が設定したエラーページの規則に一致しました。リンク切れは未確定です。",
    "redirected": "転送先でHTTP上の到達を確認しました。",
    "recovered": "初回に問題がありましたが、再確認・再試行でHTTP上の到達を確認しました。",
    "unprocessed": "時間・件数の上限、robots.txtの取得失敗などで未検査です。理由欄と未完了の理由を確認してください。",
    "excluded": "Amazon関連リンク、robots.txtの禁止、除外設定、未対応形式などで対象外です。正常・リンク切れの判定はしていません。",
    "ok": "HTTPの成功応答（2xx）で到達を確認しました。ページ内容や商品の販売継続まで保証するものではありません。",
}


def report_classification_guide():
    return ["## 分類の意味", "", "| 分類 | 意味・確認の仕方 |", "|---|---|"] + [
        f"| {kind} | {description} |" for kind, description in REPORT_DESCRIPTIONS.items()
    ]


def report_overview(config, run_id, finished, links, source_counts, incomplete, code):
    def total(rows):
        return f"{len({r.get('request_url') for r in rows if r.get('request_url')})} URL / {len(rows)} 掲載箇所"

    broken = [r for r in links if r["classification"] == "broken"]
    attention = [r for kind in REPORT_LABELS for r in links if r["classification"] == kind]
    other = [r for r in attention if r["classification"] != "broken"]
    amazon = [r for r in links if r["classification"] == "excluded" and
              r.get("reason") == "Amazon関連リンク（クリック集計への影響防止）"]
    lines = ["# WordPressリンク検査結果", "", f"- バージョン: {VERSION}", f"- 対象: {config['site_url']}",
             f"- 実行ID: {run_id}", f"- 終了日時: {finished}",
             f"- 状態: {'検査未完了' if incomplete else '設定した範囲の検査完了'}",
             f"- 終了コード: {code}", f"- 再確認済みリンク切れ: {total(broken)}",
             f"- その他の要確認・未処理: {total(other)}", f"- Amazonリンク（通信せず除外）: {total(amazon)}",
             f"- 掲載元: {sum(source_counts.values())} / 処理状態: {dict(source_counts)}", ""]
    if not broken:
        lines += ["再確認済みのリンク切れはありません。確認保留・未処理がある場合は、以下の内容を確認してください。", ""]
    if amazon:
        lines += ["Amazon関連リンクにはアクセスしておらず、リンク切れかどうかは判定していません。", ""]
    lines += ["## 確認が必要なリンク", ""]
    if not attention:
        lines += ["該当するリンクはありません。", ""]
    for i, row in enumerate(attention[:100], 1):
        lines += [f"### {i}. {REPORT_LABELS[row['classification']]}", ""]
        fields = [("掲載元", row.get("source_url")), ("記事タイトル", row.get("source_title")),
                  ("リンク文字列", row.get("text") or "（文字列なし・画像リンクなど）"),
                  ("検査URL", row.get("request_url") or row.get("absolute_url") or row.get("raw_url")),
                  ("最終URL", row.get("final_url") or "（なし）"),
                  ("HTTP", row.get("status") or "（応答なし・未取得）"),
                  ("判定", row["classification"]), ("理由", row.get("reason") or "（なし）")]
        for label, value in fields:
            lines.append(f"    {label}: {' '.join(str(value or '').splitlines())}")
        lines.append("")
    if len(attention) > 100:
        lines += [f"省略掲載箇所: {len(attention) - 100}。全件は links.tsv に記録しています。", ""]
    lines += ["HTTP応答に基づく結果です。リンク先の削除を断定せず、必要に応じてブラウザで確認してください。", ""]
    return lines


class Checker:
    def __init__(self, c, client, run_id):
        self.c, self.client, self.run_id = c, client, run_id
        self.sources = {}
        self.links = []
        self.results = {}
        self.issues = []
        self.queue = deque()
        self.collected = []

    def in_scope(self, url):
        return origin(url) in self.c["allowed_origins"] and any(
            urlsplit(url).path.startswith(x) for x in self.c["allowed_path_prefixes"])

    def add_source(self, url, discovered):
        try:
            url = normalize(url)
            if url in self.sources:
                return
            entry = {"url": url, "discovered_by": discovered, "state": "unprocessed", "reason": "", "link_count": 0}
            self.sources[url] = entry
            if not self.in_scope(url):
                entry.update(state="excluded", reason="掲載元の許可範囲外")
            elif len(self.queue) + sum(x["state"] == "ok" for x in self.sources.values()) >= self.c["max_pages"]:
                entry["reason"] = "掲載元ページ数上限"
                self.issues.append("掲載元ページ数上限")
            else:
                self.queue.append(url)
        except (ValueError, UnicodeError) as exc:
            self.issues.append(f"不正な掲載元URL: {url}: {exc}")

    def collect(self):
        self.add_source(self.c["site_url"], "top")
        for url in self.c["additional_urls"]:
            self.add_source(url, "additional")
        if self.c["collection"] == "sitemap":
            self.collect_sitemaps()
        elif self.c["collection"] == "rest":
            self.collect_rest()

    def collect_sitemaps(self):
        pending = deque((url, 0) for url in (self.c["sitemap_urls"] or [urljoin(self.c["site_url"], "/wp-sitemap.xml")]))
        seen = set()
        while pending:
            url, depth = pending.popleft()
            if url in seen:
                continue
            if len(seen) >= self.c["max_sitemaps"] or depth > self.c["max_sitemap_depth"]:
                self.issues.append(f"サイトマップ上限: {url}")
                continue
            seen.add(url)
            try:
                r = self.client.fetch(url, self.c["sitemap_limit"], "sitemap", source=True)
                if r.get("error") or r["truncated"] or not 200 <= (r["status"] or 0) < 300:
                    raise ValueError(f"取得失敗: {r.get('error') or r['status']}")
                body = r["body"]
                if body.startswith(b"\x1f\x8b"):
                    decoder = zlib.decompressobj(31)
                    body = decoder.decompress(body, self.c["sitemap_limit"] + 1)
                    if len(body) > self.c["sitemap_limit"] or not decoder.eof:
                        raise ValueError("圧縮サイトマップの受信上限または破損")
                # Reject DTD/entity declarations even for UTF-16/32 XML.
                if re.search(br"<!\s*(DOCTYPE|ENTITY)\b", body.replace(b"\x00", b""), re.I):
                    raise ValueError("DTD・実体宣言は使用できません")
                root = ET.fromstring(body)
                tag = root.tag.rsplit("}", 1)[-1]
                if tag not in {"sitemapindex", "urlset"}:
                    raise ValueError("サイトマップ形式ではありません")
                count = 0
                for item in root:
                    loc = next((x.text for x in item if x.tag.rsplit("}", 1)[-1] == "loc"), None)
                    if not loc:
                        raise ValueError("loc欠落")
                    target = normalize(urljoin(r["final_url"], loc.strip()))
                    count += 1
                    if tag == "sitemapindex":
                        pending.append((target, depth + 1))
                    else:
                        self.add_source(target, url)
                self.collected.append({"url": url, "count": count, "kind": tag})
            except (ValueError, UnicodeError, ET.ParseError, zlib.error) as exc:
                self.issues.append(f"サイトマップ {url}: {exc}")

    def collect_rest(self):
        for kind in ("posts", "pages"):
            page, pages, total, received, ids = 1, 1, None, 0, set()
            while page <= pages:
                url = urljoin(self.c["site_url"].rstrip("/") + "/", f"wp-json/wp/v2/{kind}")
                url += f"?status=publish&per_page=100&page={page}&orderby=id&order=asc&_fields=id,link"
                try:
                    r = self.client.fetch(url, self.c["html_limit"], "rest", source=True)
                    if r.get("error") or r["truncated"] or r["status"] != 200:
                        raise ValueError("API取得失敗")
                    new_pages = int(r["headers"]["x-wp-totalpages"])
                    new_total = int(r["headers"]["x-wp-total"])
                    if new_pages < 0 or new_total < 0 or new_pages != (new_total + 99) // 100:
                        raise ValueError("API件数ヘッダーが不正です")
                    if total is not None and (total != new_total or pages != new_pages):
                        raise ValueError("取得中にAPI総件数が変化しました")
                    pages, total = new_pages, new_total
                    items = json.loads(r["body"])
                    if not isinstance(items, list):
                        raise ValueError("API応答が配列ではありません")
                    for item in items:
                        if not isinstance(item, dict) or type(item.get("id")) is not int or not isinstance(item.get("link"), str):
                            raise ValueError("API項目にid/linkがありません")
                        if item["id"] in ids:
                            raise ValueError("APIのページ間でIDが重複しています")
                        ids.add(item["id"])
                        self.add_source(item["link"], f"rest:{kind}")
                    received += len(items)
                    self.collected.append({"url": url, "count": len(items), "total": total, "pages": pages, "kind": kind})
                    if len(self.sources) > self.c["max_pages"]:
                        raise ValueError("掲載元ページ数上限")
                    page += 1
                    if page > pages and received != total:
                        raise ValueError("API取得件数と総件数の不一致")
                except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                    self.issues.append(f"REST {kind} page={page}: {exc}")
                    break

    def extract(self):
        processed = 0
        while self.queue:
            if processed >= self.c["max_pages"]:
                self.issues.append("掲載元ページ数上限")
                return
            url = self.queue.popleft()
            entry = self.sources[url]
            processed += 1
            r = self.client.fetch(url, self.c["html_limit"], "source", source=True)
            if r.get("error") in {"excluded", "robots_excluded"}:
                entry.update(state="excluded", reason=r.get("reason", ""))
                continue
            if r.get("error") or r["truncated"] or not 200 <= (r["status"] or 0) < 300:
                entry.update(state="failed", reason=r.get("reason") or f"HTTP {r['status']} / truncated={r['truncated']}")
                self.issues.append(f"掲載元取得失敗: {url}")
                continue
            content_type = r["headers"].get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                entry.update(state="excluded", reason="HTMLではありません")
                continue
            parser = PageParser(self.c["check_images"])
            parser.feed(decode_body(r))
            parser.close()
            base = r["final_url"]
            if parser.base:
                try:
                    normalize(urljoin(base, parser.base))
                    base = urljoin(base, parser.base)
                except (ValueError, UnicodeError):
                    pass
            for link in parser.links:
                row = {**link, "run_id": self.run_id, "site_id": self.c["site_id"],
                       "source_url": url, "source_title": " ".join(parser.title.split()),
                       "absolute_url": "", "request_url": "", "scope": "", "classification": ""}
                raw = link["raw_url"].strip()
                try:
                    if not raw or raw.startswith("#"):
                        row.update(classification="excluded", reason="空URLまたはページ内アンカー")
                    else:
                        absolute = urljoin(base, raw)
                        row["absolute_url"] = absolute
                        if urlsplit(absolute).scheme.lower() not in {"http", "https"}:
                            row.update(classification="excluded", reason="HTTP/HTTPS以外")
                        else:
                            target = normalize(absolute)
                            row.update(request_url=target, scope="internal" if origin(target) in self.c["allowed_origins"] else "external")
                            if self.c["crawl_internal"] and self.in_scope(target):
                                self.add_source(target, url)
                except (ValueError, UnicodeError) as exc:
                    row.update(classification="invalid_url", reason=str(exc))
                self.links.append(row)
            entry.update(state="ok", link_count=len(parser.links))

    def check_links(self):
        for row in self.links:
            if row["classification"]:
                continue
            url = row["request_url"]
            if url not in self.results:
                if len(self.results) >= self.c["max_links"]:
                    row.update(classification="unprocessed", reason="リンク先URL数上限")
                    self.issues.append("リンク先URL数上限")
                    continue
                self.results[url] = inspect_link(self.client, url, self.c)
            row.update(self.results[url])

    def finish_unprocessed(self):
        for row in self.links:
            if not row["classification"]:
                row.update(classification="unprocessed", reason="実行が終了しました")

    def write_reports(self, out):
        self.finish_unprocessed()
        counts = Counter(x["classification"] for x in self.links)
        source_counts = Counter(x["state"] for x in self.sources.values())
        incomplete = bool(self.issues or source_counts["unprocessed"] or source_counts["failed"]
                          or counts["unprocessed"] or any(x.get("incomplete") for x in self.links))
        abnormal = any(k not in {"ok", "redirected", "recovered", "excluded"} and v for k, v in counts.items())
        code = 2 if incomplete else (1 if abnormal else 0)
        link_fields = ["run_id", "site_id", "source_url", "source_title", "element", "position", "text",
                       "raw_url", "absolute_url", "request_url", "final_url", "scope", "status", "classification",
                       "reason", "attempts", "first_checked", "last_checked"]
        write_tsv(out / "links.tsv", self.links, link_fields)
        write_tsv(out / "sources.tsv", self.sources.values(), ["url", "discovered_by", "state", "reason", "link_count"])
        order = ["broken", "candidate", "unreachable", "pending", "tls_error", "redirect_error", "invalid_url",
                 "review", "soft_404_suspected", "redirected", "recovered", "unprocessed", "excluded", "ok"]
        lines = report_overview(self.c, self.run_id, now(), self.links, source_counts, incomplete, code)
        lines += ["## 検査範囲", "", f"- 収集方法: {self.c['collection']}", f"- 掲載元発見数: {len(self.sources)}",
                 f"- 掲載元内訳: {dict(source_counts)}", f"- 掲載箇所数: {len(self.links)}",
                 f"- 確認対象URL数（除外を含む）: {len(self.results)}", "", "## 判定件数", "",
                 "| 分類 | 掲載箇所 | URL数 |", "|---|---:|---:|"]
        for kind in order:
            urls = {x["request_url"] for x in self.links if x["classification"] == kind and x["request_url"]}
            lines.append(f"| {kind} | {counts[kind]} | {len(urls)} |")
        lines += [""] + report_classification_guide()
        lines += ["", "除外した範囲・非HTML・未対応形式を含め、サイト全体の完全性を保証する結果ではありません。",
                  "問題の掲載元・位置はlinks.tsv、要求の原記録はrequests.jsonlに記録しています。", "", "## 未完了の理由", ""]
        lines += ["- " + x.replace("\n", " ") for x in dict.fromkeys(self.issues)] or ["なし"]
        lines += ["", "## 収集記録", "", "```json", json.dumps(self.collected, ensure_ascii=False, indent=2), "```",
                  "", "## 適用設定", "", "```json", json.dumps(self.c, ensure_ascii=False, indent=2), "```", ""]
        (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
        return code


def write_tsv(path, rows, fields):
    def escape(value):
        return str(value).replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(fields)
        for row in rows:
            writer.writerow([escape(row.get(key, "")) for key in fields])


def run(c, transport=wire_get):
    parent = Path(c["output_dir"]) / c["site_id"]
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (parent / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("同じサイトの検査がすでに実行中です") from exc
        run_id = datetime.now(JST).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        out = parent / run_id
        out.mkdir(mode=0o700)
        with (out / "requests.jsonl").open("w", encoding="utf-8") as log:
            client = Client(c, log, time.monotonic() + c["max_runtime"], transport)
            checker = Checker(c, client, run_id)
            try:
                checker.collect()
                checker.extract()
                checker.check_links()
            except (StopCheck, KeyboardInterrupt) as exc:
                checker.issues.append(str(exc) or "利用者による中断")
            except Exception as exc:
                checker.issues.append(f"実行エラー: {type(exc).__name__}: {exc}")
            code = checker.write_reports(out)
        return code, out


class ActivityIndicator:
    """Print a heartbeat even while DNS, HTTP or retry waits are blocking."""

    def __enter__(self):
        self.stopped = threading.Event()
        self.stream = sys.stderr
        self.stream.write("検査中（待機時間を含む）: #")
        self.stream.flush()
        self.thread = threading.Thread(target=self._display, daemon=True)
        self.thread.start()
        return self

    def _display(self):
        count = 1
        while not self.stopped.wait(1):
            self.stream.write("#")
            count += 1
            if count % 60 == 0:
                self.stream.write("\n")
            self.stream.flush()

    def __exit__(self, exc_type, exc_value, traceback):
        self.stopped.set()
        self.thread.join()
        self.stream.write("\n")
        self.stream.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"wordpress-link-checker {VERSION}")
    parser.add_argument("--config", type=Path, required=True, help="専用JSON設定ファイル")
    parser.add_argument("--validate-config", action="store_true", help="設定検証のみ。HTTP要求やファイル作成を行わない")
    args = parser.parse_args(argv)
    try:
        c = load_config(args.config)
        if args.validate_config:
            print("設定は有効です（通信・検査は未実施）")
            return 0
        with ActivityIndicator():
            code, out = run(c)
        print(f"終了コード: {code}\n結果: {out / 'summary.md'}")
        return code
    except (ValueError, OSError, RecursionError) as exc:
        print(f"起動・設定エラー: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
