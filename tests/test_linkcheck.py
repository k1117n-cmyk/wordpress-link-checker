import copy
import csv
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "bin/check_wordpress_links.py"
spec = importlib.util.spec_from_file_location("linkcheck", SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
BASE = "https://example.test"


def config(**values):
    return {**copy.deepcopy(m.DEFAULTS), "site_id": "test", "site_url": BASE + "/",
            "allowed_origins": [BASE], **values}


def reply(status=200, body=b"", headers=None, truncated=False):
    if isinstance(body, str):
        body = body.encode()
    return {"status": status, "body": body, "headers": headers or {}, "truncated": truncated}


class Clock:
    def __init__(self):
        self.value = 1000.

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Scenario:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def __call__(self, url, limit, connect_timeout, total_timeout):
        self.calls.append((url, m.time.monotonic(), limit))
        if url.endswith("/robots.txt") and url not in self.routes:
            return reply(404)
        item = self.routes.get(url, reply())
        if isinstance(item, list):
            value = item.pop(0) if len(item) > 1 else item[0]
        else:
            value = item
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


class LogicTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.patches = [patch.object(m.time, "monotonic", self.clock.monotonic),
                        patch.object(m.time, "sleep", self.clock.sleep)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def client(self, routes=None, **settings):
        c = config(**settings)
        scenario = Scenario(routes)
        log = io.StringIO()
        return c, m.Client(c, log, self.clock.value + c["max_runtime"], scenario), scenario, log

    def test_duplicate_run_is_locked(self):
        with tempfile.TemporaryDirectory() as directory:
            c = config(collection="urls", output_dir=directory)
            parent = Path(directory) / c["site_id"]
            parent.mkdir()
            with (parent / ".lock").open("a") as lock:
                m.fcntl.flock(lock, m.fcntl.LOCK_EX | m.fcntl.LOCK_NB)
                scenario = Scenario()
                with self.assertRaises(ValueError):
                    m.run(c, scenario)
                self.assertEqual(scenario.calls, [])

    def test_source_redirect_path_restriction(self):
        _, client, scenario, _ = self.client({BASE + "/blog/a": reply(302, headers={"location": "/outside"})},
                                               allowed_path_prefixes=["/blog/"])
        r = client.fetch(BASE + "/blog/a", 100, "source", source=True)
        self.assertEqual(r["error"], "unsafe_url")
        self.assertFalse(any(x[0].endswith("/outside") for x in scenario.calls))

    def test_status_and_retries(self):
        cases = [([200], "ok", 1), ([404, 404], "broken", 2), ([410, 404], "broken", 2),
                 ([404, 200], "recovered", 2), ([404, 403], "review", 2),
                 ([401], "pending", 1), ([403], "pending", 1),
                 ([503, 503, 503], "unreachable", 3), ([503, 200], "recovered", 2),
                 ([503, 404], "review", 2),
                 ([304], "review", 1), ([429, 200], "recovered", 2)]
        for statuses, expected, attempts in cases:
            with self.subTest(statuses=statuses):
                c, client, scenario, _ = self.client({BASE + "/x": [reply(x) for x in statuses]})
                result = m.inspect_link(client, BASE + "/x", c)
                self.assertEqual(result["classification"], expected)
                self.assertEqual(result["attempts"], attempts)
                calls = [x[1] for x in scenario.calls if x[0].endswith("/x")]
                if statuses[0] == 404:
                    self.assertGreaterEqual(calls[1] - calls[0], 60)

    def test_transport_error_categories(self):
        for error, expected, calls in [(ssl.SSLError("cert"), "tls_error", 1),
                                       (socket.gaierror("dns"), "unreachable", 3),
                                       (TimeoutError("timeout"), "unreachable", 3),
                                       (m.UnsafeURL("private"), "invalid_url", 1)]:
            with self.subTest(expected=expected):
                c, client, _, log = self.client({BASE + "/x": error})
                r = m.inspect_link(client, BASE + "/x", c)
                self.assertEqual(r["classification"], expected)
                self.assertEqual(r["attempts"], calls)
                self.assertNotIn('"body"', log.getvalue())

    def test_redirects_and_limits(self):
        routes = {BASE + "/r": reply(301, headers={"location": "/target"}),
                  BASE + "/target": reply(200), BASE + "/loop": reply(302, headers={"location": "/loop"})}
        c, client, _, _ = self.client(routes)
        self.assertEqual(m.inspect_link(client, BASE + "/r", c)["classification"], "redirected")
        self.assertEqual(m.inspect_link(client, BASE + "/loop", c)["classification"], "redirect_error")
        routes[BASE + "/target"] = reply(404)
        c, client, _, _ = self.client(routes)
        self.assertEqual(m.inspect_link(client, BASE + "/r", c)["classification"], "broken")
        c, client, _, _ = self.client(routes, max_redirects=0)
        self.assertEqual(client.fetch(BASE + "/r", 100)["error"], "redirect")

    def test_external_source_redirect_is_not_fetched(self):
        _, client, scenario, _ = self.client({BASE + "/": reply(302, headers={"location": "https://other.test/"})})
        r = client.fetch(BASE + "/", 100, "source", source=True)
        self.assertEqual(r["error"], "unsafe_url")
        self.assertFalse(any("other.test" in x[0] for x in scenario.calls))

    def test_redirect_into_excluded_action(self):
        _, client, scenario, _ = self.client({BASE + "/r": reply(302, headers={"location": "/?add-to-cart=1"})})
        self.assertEqual(client.fetch(BASE + "/r", 100)["error"], "excluded")
        self.assertFalse(any("add-to-cart" in x[0] for x in scenario.calls))

    def test_amazon_links_are_excluded_without_any_requests(self):
        urls = ["https://" + domain + "/product?tag=example-22" for domain in m.AMAZON_DOMAINS]
        urls += ["https://www.amazon.co.jp/dp/EXAMPLE?tag=example-22",
                 "https://WWW.AMAZON.COM./dp/EXAMPLE", "https://amzn.to/short",
                 "https://link.amazon/B0iOioCT6",
                 "https://www.amazon.co.jp/dp/B0G4NYSWS1?th=1&linkCode=ll2&tag=golden-rocks-22"
                 "&linkId=8a203b4f303a84a7a77c51e17042567a&ref_=as_li_ss_tl"]
        for url in urls:
            with self.subTest(url=url):
                c, client, scenario, log = self.client()
                result = m.inspect_link(client, url, c)
                self.assertEqual(result["classification"], "excluded")
                self.assertIn("Amazon", result["reason"])
                self.assertEqual(scenario.calls, [])
                self.assertEqual(log.getvalue(), "")

    def test_redirect_to_amazon_is_stopped_before_robots_or_product_request(self):
        for target in ["https://www.amazon.co.jp/dp/EXAMPLE?tag=example-22", "https://amzn.to/short",
                       "https://link.amazon/B0iOioCT6"]:
            with self.subTest(target=target):
                c, client, scenario, _ = self.client({BASE + "/go": reply(302, headers={"location": target})})
                result = m.inspect_link(client, BASE + "/go", c)
                self.assertEqual(result["classification"], "excluded")
                self.assertEqual([call[0] for call in scenario.calls], [BASE + "/robots.txt", BASE + "/go"])

    def test_amazon_domain_lookalikes_are_still_checked(self):
        for url in ["https://amazon.com.example.test/product", "https://notamazon.co.jp/product",
                    BASE + "/amazon.co.jp?next=https%3A%2F%2Famzn.to%2Fshort"]:
            with self.subTest(url=url):
                c, client, scenario, _ = self.client()
                self.assertEqual(m.inspect_link(client, url, c)["classification"], "ok")
                self.assertIn(url, [call[0] for call in scenario.calls])

    def test_robots_and_unknown(self):
        for result, error in [(reply(200, "User-agent: *\nDisallow: /private\n"), "robots_excluded"),
                              (reply(503), "robots_unknown"), (reply(200, "x", truncated=True), "robots_unknown")]:
            _, client, scenario, _ = self.client({BASE + "/robots.txt": result})
            self.assertEqual(client.fetch(BASE + "/private", 100)["error"], error)
            self.assertEqual(len(scenario.calls), 1)

    def test_robots_delay_and_request_spacing(self):
        _, client, scenario, _ = self.client({BASE + "/robots.txt": reply(200, "User-agent: *\nCrawl-delay: 3\n")})
        client.fetch(BASE + "/a", 100)
        client.fetch(BASE + "/b", 100)
        self.assertGreaterEqual(scenario.calls[2][1] - scenario.calls[1][1], 3)

    def test_retry_after_blocks_origin(self):
        _, client, scenario, _ = self.client({BASE + "/limited": reply(429, headers={"retry-after": "120"})})
        client.fetch(BASE + "/limited", 100)
        client.fetch(BASE + "/other", 100)
        self.assertGreaterEqual(scenario.calls[-1][1] - scenario.calls[-2][1], 120)

    def test_retry_deadline_preserves_candidate(self):
        c, client, _, _ = self.client({BASE + "/x": reply(404)}, max_runtime=30)
        r = m.inspect_link(client, BASE + "/x", c)
        self.assertEqual(r["classification"], "candidate")
        self.assertTrue(r["incomplete"])

    def test_sitemap_split_cycle_and_compressed(self):
        index = '<sitemapindex><sitemap><loc>/a.xml.gz</loc></sitemap><sitemap><loc>/wp-sitemap.xml</loc></sitemap></sitemapindex>'
        urls = '<urlset><url><loc>https://example.test/page</loc></url></urlset>'
        c, client, scenario, _ = self.client({BASE + "/wp-sitemap.xml": reply(body=index),
                                             BASE + "/a.xml.gz": reply(body=gzip.compress(urls.encode()))})
        checker = m.Checker(c, client, "test")
        checker.collect()
        self.assertIn(BASE + "/page", checker.sources)
        self.assertEqual(checker.issues, [])
        self.assertEqual(sum(x[0].endswith("/wp-sitemap.xml") for x in scenario.calls), 1)

    def test_sitemap_failure_and_dtd(self):
        for body in ["<html>error</html>", '<!DOCTYPE x [<!ENTITY a "expanded">]><urlset/>']:
            c, client, _, _ = self.client({BASE + "/wp-sitemap.xml": reply(body=body)})
            checker = m.Checker(c, client, "test")
            checker.collect()
            self.assertTrue(checker.issues)

    def test_sitemap_file_limit(self):
        index = '<sitemapindex><sitemap><loc>/a.xml</loc></sitemap></sitemapindex>'
        c, client, scenario, _ = self.client({BASE + "/wp-sitemap.xml": reply(body=index)}, max_sitemaps=1)
        checker = m.Checker(c, client, "test")
        checker.collect()
        self.assertTrue(checker.issues)
        self.assertFalse(any(x[0].endswith("/a.xml") for x in scenario.calls))

    def rest_routes(self, duplicate=False, fail=False):
        routes = {}
        for kind, total in [("posts", 101), ("pages", 0)]:
            for page in range(1, max(1, (total + 99) // 100) + 1):
                url = BASE + f"/wp-json/wp/v2/{kind}?status=publish&per_page=100&page={page}&orderby=id&order=asc&_fields=id,link"
                items = [{"id": i, "link": BASE + f"/post/{i}"} for i in range(1 + (page - 1) * 100, min(total, page * 100) + 1)]
                if duplicate and page == 2:
                    items[0]["id"] = 1
                routes[url] = reply(503 if fail and page == 2 else 200, json.dumps(items),
                                    {"x-wp-total": str(total), "x-wp-totalpages": str((total + 99) // 100)})
        return routes

    def test_rest_over_100(self):
        c, client, _, _ = self.client(self.rest_routes(), collection="rest")
        checker = m.Checker(c, client, "test")
        checker.collect()
        self.assertEqual(len(checker.sources), 102)
        self.assertEqual(checker.issues, [])

    def test_rest_duplicate_and_partial_failure(self):
        for routes in [self.rest_routes(duplicate=True), self.rest_routes(fail=True)]:
            c, client, _, _ = self.client(routes, collection="rest")
            checker = m.Checker(c, client, "test")
            checker.collect()
            self.assertTrue(checker.issues)

    def test_extract_dedup_and_all_positions(self):
        html = '<title>Title</title><base href="/dir/"><a href="x?q=1&amp;b=2#f">One</a><a href="x?q=1&amp;b=2">Two</a><a href="#id">skip</a>'
        c, client, scenario, _ = self.client({BASE + "/": reply(body=html, headers={"content-type": "text/html"})}, collection="urls")
        checker = m.Checker(c, client, "test")
        checker.collect()
        checker.extract()
        checker.check_links()
        self.assertEqual(len(checker.links), 3)
        self.assertEqual([x["text"] for x in checker.links], ["One", "Two", "skip"])
        self.assertEqual(sum(x[0] == BASE + "/dir/x?q=1&b=2" for x in scenario.calls), 1)
        self.assertEqual(checker.links[0]["source_title"], "Title")
        self.assertEqual(checker.links[2]["classification"], "excluded")

    def test_source_truncation_and_failure_are_incomplete(self):
        for r in [reply(body="<a href='/x'>", truncated=True), reply(503)]:
            r["headers"]["content-type"] = "text/html"
            c, client, _, _ = self.client({BASE + "/": r}, collection="urls")
            checker = m.Checker(c, client, "test")
            checker.collect()
            checker.extract()
            with tempfile.TemporaryDirectory() as directory:
                self.assertEqual(checker.write_reports(Path(directory)), 2)
            self.assertEqual(checker.links, [])

    def test_link_limit_and_reports(self):
        c, client, _, _ = self.client({BASE + "/": reply(body='<a href="/a">A</a><a href="/b">B</a>', headers={"content-type": "text/html"})}, collection="urls", max_links=1)
        checker = m.Checker(c, client, "test")
        checker.collect()
        checker.extract()
        checker.check_links()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.assertEqual(checker.write_reports(out), 2)
            with (out / "links.tsv").open() as f:
                rows = list(csv.DictReader(f, delimiter="\t"))
            self.assertEqual([r["classification"] for r in rows], ["ok", "unprocessed"])
            self.assertIn("検査未完了", (out / "summary.md").read_text())

    def test_page_limit(self):
        c, client, _, _ = self.client(collection="urls", max_pages=1, additional_urls=[BASE + "/second"])
        checker = m.Checker(c, client, "test")
        checker.collect()
        self.assertEqual(len(checker.queue), 1)
        self.assertEqual(checker.sources[BASE + "/second"]["state"], "unprocessed")
        self.assertTrue(checker.issues)

    def test_summary_shows_pending_occurrences_and_amazon_exclusion(self):
        html = ('<title>確認用の記事</title><a href="/blocked">参考資料</a>'
                '<a href="/blocked"><img alt=""></a><a href="https://link.amazon/test">商品</a>')
        c, client, scenario, _ = self.client({BASE + "/": reply(body=html, headers={"content-type": "text/html"}),
                                             BASE + "/blocked": reply(403)}, collection="urls")
        checker = m.Checker(c, client, "test")
        checker.collect()
        checker.extract()
        checker.check_links()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.assertEqual(checker.write_reports(out), 1)
            summary = (out / "summary.md").read_text()
        for expected in ["再確認済みリンク切れ: 0 URL / 0 掲載箇所", "その他の要確認・未処理: 1 URL / 2 掲載箇所",
                         "Amazonリンク（通信せず除外）: 1 URL / 1 掲載箇所", "掲載元: " + BASE + "/",
                         "記事タイトル: 確認用の記事", "リンク文字列: 参考資料", "文字列なし・画像リンクなど",
                         "検査URL: " + BASE + "/blocked", "HTTP: 403", "判定: pending"]:
            self.assertIn(expected, summary)
        self.assertLess(summary.index("判定: pending"), summary.index("## 判定件数"))
        self.assertIn("## 分類の意味", summary)
        self.assertIn("403（アクセス拒否）", summary)
        self.assertIn("正常・リンク切れの判定はしていません", summary)
        self.assertNotIn("分類の意味はREADME.mdを参照", summary)
        for kind in m.REPORT_DESCRIPTIONS:
            self.assertIn(f"| {kind} |", summary.split("## 分類の意味", 1)[1])
        self.assertFalse(any("link.amazon" in call[0] for call in scenario.calls))

    def test_summary_prioritizes_broken_and_caps_details_without_dropping_tsv(self):
        c, client, _, _ = self.client(collection="urls")
        checker = m.Checker(c, client, "test")
        checker.links = [dict(classification="pending", request_url=BASE + "/blocked") for _ in range(101)]
        checker.links.append(dict(classification="broken", request_url=BASE + "/gone", status=404))
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.assertEqual(checker.write_reports(out), 1)
            summary = (out / "summary.md").read_text()
            with (out / "links.tsv").open() as f:
                self.assertEqual(len(list(csv.DictReader(f, delimiter="\t"))), 102)
        self.assertIn("### 1. 再確認済みリンク切れ", summary)
        self.assertEqual(summary.count("### "), 100)
        self.assertIn("省略掲載箇所: 2", summary)

    def test_soft404_is_not_confirmed_broken(self):
        c, client, _, _ = self.client({BASE + "/x": reply(body="Page missing")}, soft_404_patterns=["Page missing"])
        self.assertEqual(m.inspect_link(client, BASE + "/x", c)["classification"], "soft_404_suspected")

    def test_run_outputs_are_unique_and_code_one(self):
        scenario = Scenario({BASE + "/": reply(body='<a href="/gone">Gone</a>', headers={"content-type": "text/html"}), BASE + "/gone": reply(410)})
        with tempfile.TemporaryDirectory() as directory:
            c = config(collection="urls", output_dir=directory)
            code, out = m.run(c, scenario)
            self.assertEqual(code, 1)
            self.assertEqual({x.name for x in out.iterdir()}, {"summary.md", "links.tsv", "sources.tsv", "requests.jsonl"})
            records = [json.loads(x) for x in (out / "requests.jsonl").read_text().splitlines()]
            self.assertEqual(len(records), len(scenario.calls))
            self.assertEqual(sum(x["status"] == 410 for x in records), 2)
            _, out2 = m.run(c, scenario)
            self.assertNotEqual(out, out2)


class URLTests(unittest.TestCase):
    def test_robots_wildcard_longest_rule_and_group_merge(self):
        rp = m.RobotsRules()
        rp.parse(["User-agent: *", "Disallow: /all", "User-agent: WordPressLinkCheck",
                  "Disallow: /private/*", "Allow: /private/public/", "Disallow: /*.pdf$",
                  "User-agent: WordPressLinkCheck", "Disallow: /second", "Disallow: /日本語"])
        for path, allowed in [("/private/a", False), ("/private/public/a", True),
                              ("/a.pdf", False), ("/a.pdf?x=1", True), ("/second", False),
                              ("/all", True), ("/%E6%97%A5%E6%9C%AC%E8%AA%9E", False)]:
            with self.subTest(path=path):
                self.assertEqual(rp.can_fetch(m.UA, BASE + path), allowed)

    def test_dns_deadline(self):
        def slow_dns(*args, **kwargs):
            time.sleep(.05)
            return []
        with patch.object(m.socket, "getaddrinfo", side_effect=slow_dns):
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                m.public_addresses("example.test", 80, .01)
            self.assertLess(time.monotonic() - started, .04)

    def test_normalization_preserves_semantics(self):
        self.assertEqual(m.normalize("HTTPS://EXAMPLE.COM:443/A/?b=2&a=1#x"), "https://example.com/A/?b=2&a=1")
        self.assertIn("%E6%97%A5", m.normalize("https://example.com/日本語"))
        self.assertEqual(m.normalize("https://[2606:4700::1111]/"), "https://[2606:4700::1111]/")

    def test_unsafe_urls(self):
        for url in ["https://user:pass@example.com/", "http://example.com:22/", "file:///etc/passwd", "http://example.com/\nx", "https://example.com\\bad"]:
            with self.subTest(url=url), self.assertRaises((ValueError, m.UnsafeURL)):
                m.normalize(url)

    def test_private_dns_and_mixed_answer_rejected(self):
        for address in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"]:
            with self.subTest(address=address), patch.object(m.socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))]):
                with self.assertRaises(m.UnsafeURL):
                    m.public_addresses("example.test", 80, 1)
        entries = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (x, 80)) for x in ["8.8.8.8", "127.0.0.1"]]
        with patch.object(m.socket, "getaddrinfo", return_value=entries), self.assertRaises(m.UnsafeURL):
            m.public_addresses("example.test", 80, 1)

    def test_image_srcset_with_data_comma(self):
        parser = m.PageParser(True)
        parser.feed('<img alt="A" src="one.jpg" srcset="data:image/png;base64,AAAA 1x, two.jpg 2x"><a href="/x"><img alt="Link"></a>')
        self.assertEqual([x["raw_url"] for x in parser.links[:3]], ["one.jpg", "data:image/png;base64,AAAA", "two.jpg"])
        self.assertEqual(parser.links[-1]["text"], "Link")

    def test_config_validation_and_no_side_effects(self):
        example = SCRIPT.parents[1] / "conf/linkcheck_example.json"
        self.assertEqual(m.main(["--config", str(example), "--validate-config"]), 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "c.json"
            for update in [{"unexpected": 1}, {"site_id": "../escape"}, {"site_url": None},
                           {"request_interval": .1}, {"exclude_patterns": ["["]}, {"max_pages": True}]:
                path.write_text(json.dumps({"site_id": "x", "site_url": "https://example.com/", **update}))
                self.assertEqual(m.main(["--config", str(path), "--validate-config"]), 3)


class WireTests(unittest.TestCase):
    """Real loopback HTTP; only the test injects a verified-address resolver."""

    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                try:
                    if self.path == "/slow":
                        time.sleep(.3)
                    payload = b"hello world"
                    encoding = "identity"
                    if self.path == "/gzip":
                        payload, encoding = gzip.compress(b"a" * 10000), "gzip"
                    self.send_response(200)
                    self.send_header("Content-Encoding", encoding)
                    self.send_header("Content-Length", str(len(payload) + (10 if self.path == "/short" else 0)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def get(self, path, limit=1000, timeout=1):
        address = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", self.server.server_address)]
        with patch.object(m, "public_addresses", return_value=address), patch.object(m.socket, "getaddrinfo", side_effect=AssertionError("second DNS lookup")):
            return m.wire_get("http://public.test" + path, limit, 1, timeout)

    def test_pinned_connection(self):
        self.assertEqual(self.get("/")["body"], b"hello world")

    def test_body_limit(self):
        r = self.get("/", limit=5)
        self.assertTrue(r["truncated"])
        self.assertEqual(r["body"], b"hello")

    def test_decompressed_limit(self):
        r = self.get("/gzip", limit=100)
        self.assertTrue(r["truncated"])
        self.assertEqual(len(r["body"]), 100)

    def test_total_timeout(self):
        started = time.monotonic()
        with self.assertRaises((OSError, m.http.client.HTTPException, TimeoutError)):
            self.get("/slow", timeout=.05)
        self.assertLess(time.monotonic() - started, .25)

    def test_incomplete_content_length(self):
        with self.assertRaises(m.http.client.IncompleteRead):
            self.get("/short")


if __name__ == "__main__":
    unittest.main()
