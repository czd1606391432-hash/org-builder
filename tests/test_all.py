"""离线测试:大模型用假的替身,不联网。运行: python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                                    # noqa: E402
from org_builder import demo, review as rv                        # noqa: E402
from org_builder.llm import LLM, LLMBusy, LLMError, repair_json   # noqa: E402


def nodes(n, prefix="n"):
    """一条根 + n-1 个下级。"""
    return [{"id": "%s%d" % (prefix, i), "name": "部门%d" % i, "type": "部门", "depth": 0 if i == 0 else 1, "parent": None}
            for i in range(n)]


class FakeLLM:
    """按 prompt 内容给出预设回答,并记录每次调用。"""

    def __init__(self, doc="## 总体评价\n不错", fail_overall=None, fail_chunks=(), delay=0, extra=None):
        self.calls, self.lock = [], threading.Lock()
        self.doc, self.fail_overall, self.fail_chunks, self.delay, self.extra = doc, fail_overall, set(fail_chunks), delay, extra or []
        self.running = self.peak = 0

    def call_json(self, system, user, require=None, **kw):
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            time.sleep(self.delay)
            kind = "overall" if '"doc"' in system else "findings"
            ids = [ln.split("id=")[1].split(" ")[0] for ln in user.splitlines() if "id=" in ln]
            with self.lock:
                self.calls.append({"kind": kind, "system": system, "user": user, "ids": ids, "require": require, "kw": kw})
                idx = sum(1 for c in self.calls if c["kind"] == "findings") - 1
            if kind == "overall":
                if self.fail_overall:
                    raise self.fail_overall
                return {"doc": self.doc}
            if ids and ids[0] in self.fail_chunks:
                raise LLMError("这一批挂了")
            return {"findings": [{"id": ids[0], "level": "good", "title": "好", "reason": "因为好"},
                                 {"id": ids[-1], "level": "risk", "title": "险", "reason": "因为险"}] + self.extra}
        finally:
            with self.lock:
                self.running -= 1


class ReviewTest(unittest.TestCase):
    def test_basic(self):
        llm = FakeLLM()
        out = rv.review({"org": {"name": "测试集团"}, "nodes": nodes(5)}, llm)
        self.assertEqual(out["doc"], "## 总体评价\n不错")
        self.assertEqual([(f["id"], f["level"]) for f in out["findings"]], [("n0", "good"), ("n4", "risk")])
        o = [c for c in llm.calls if c["kind"] == "overall"][0]
        self.assertIn("组织名称:测试集团", o["user"])
        self.assertIn("  - id=n3 部门3【部门】" .replace("部门3【部门】", "【部门3】(部门)"), o["user"])
        self.assertEqual(o["require"], ["doc"])
        self.assertNotIn("对比", o["system"])

    def test_too_small_and_bad_input(self):
        for bad in ({"nodes": nodes(2)}, {}, {"nodes": "x"}, [], {"nodes": [{"name": "没有id"}] * 5}):
            with self.assertRaises(rv.ReviewError):
                rv.review(bad, FakeLLM())

    def test_truncate_and_chunking(self):
        llm = FakeLLM(delay=0.05)
        out = rv.review({"nodes": nodes(200)}, llm)
        chunks = [c for c in llm.calls if c["kind"] == "findings"]
        self.assertEqual(sorted(len(c["ids"]) for c in chunks), [30, 40, 40, 40])      # 只评前 150 个,每批 40
        allids = {i for c in chunks for i in c["ids"]}
        self.assertNotIn("n150", allids)
        o = [c for c in llm.calls if c["kind"] == "overall"][0]
        self.assertIn("节点过多已截断", o["user"])
        self.assertNotIn("id=n150 ", o["user"])
        self.assertGreater(llm.peak, 1)                               # 确实是并发跑的
        self.assertLessEqual(llm.peak, rv.WORKERS + 1)
        self.assertEqual(len(out["findings"]), 8)                     # 4 批 × 2 条
        self.assertEqual({f["id"] for f in out["findings"]}, {"n0", "n39", "n40", "n79", "n80", "n119", "n120", "n149"})

    def test_sanitize_fields(self):
        raw = [{"id": "a" * 99, "name": "名" * 99, "type": "型" * 99, "depth": 999},
               {"id": "b", "name": "x", "depth": "-5"}, {"id": "c", "name": "y", "depth": "abc"}, "不是字典"]
        got = rv.clean_nodes(raw, 10)
        self.assertEqual((len(got[0]["id"]), len(got[0]["name"]), len(got[0]["type"]), got[0]["depth"]), (40, 30, 24, 12))
        self.assertEqual([g["depth"] for g in got[1:]], [0, 0])
        self.assertEqual(got[1]["type"], "部门")
        self.assertEqual(len(got), 3)

    def test_findings_whitelist_and_dedupe(self):
        extra = [{"id": "ghost", "level": "bad", "title": "编造的节点", "reason": "x"},
                 {"id": "n1", "level": "terrible", "title": "不认识的级别", "reason": "x"},
                 {"id": "n0", "level": "bad", "title": "重复的节点", "reason": "x"},
                 "不是字典", {"id": "n2", "level": "bad", "title": "长" * 99, "reason": "理" * 999}]
        out = rv.review({"nodes": nodes(5)}, FakeLLM(extra=extra))
        self.assertEqual([f["id"] for f in out["findings"]], ["n0", "n4", "n2"])
        self.assertEqual((len(out["findings"][2]["title"]), len(out["findings"][2]["reason"])), (24, 220))

    def test_merge_round_robin_and_cap(self):
        batches = [{"findings": [{"id": "a%d" % i, "level": "good"} for i in range(20)]},
                   {"findings": [{"id": "b%d" % i, "level": "bad"} for i in range(3)]}, None, {"findings": "坏的"}]
        valid = {"a%d" % i for i in range(20)} | {"b%d" % i for i in range(3)}
        got = [f["id"] for f in rv.merge_findings(batches, valid)]
        self.assertEqual(len(got), rv.MAX_FINDINGS)
        self.assertEqual(got[:6], ["a0", "b0", "a1", "b1", "a2", "b2"])      # 各批轮流取,后面的批次不会被挤掉

    def test_chunk_failure_tolerated_overall_failure_fatal(self):
        out = rv.review({"nodes": nodes(90)}, FakeLLM(fail_chunks={"n40"}))
        self.assertEqual({f["id"] for f in out["findings"]}, {"n0", "n39", "n80", "n89"})
        self.assertTrue(out["doc"])
        with self.assertRaises(LLMError):
            rv.review({"nodes": nodes(5)}, FakeLLM(fail_overall=LLMError("挂了")))
        with self.assertRaises(LLMBusy):
            rv.review({"nodes": nodes(5)}, FakeLLM(fail_overall=LLMBusy("忙")))

    def test_benchmark(self):
        llm = FakeLLM()
        bench = {"company": "示例：星河装备集团", "nodes": [{"name": "总部", "type": "集团总部", "depth": 0}] +
                 [{"name": "对标部门%d" % i, "type": "部门", "depth": 1} for i in range(120)]}
        rv.review({"nodes": nodes(5), "benchmark": bench}, llm)
        o = [c for c in llm.calls if c["kind"] == "overall"][0]
        self.assertIn("## 与示例：星河装备集团对比", o["system"])
        self.assertIn("对标组织「示例：星河装备集团」的组织架构树", o["user"])
        self.assertIn("【对标部门78】", o["user"])
        self.assertNotIn("【对标部门79】", o["user"])                  # 对标树最多带 80 个节点
        self.assertNotIn("id=", o["user"].split("对标组织「")[1])      # 对标树不带 id,免得模型把评判挂到对方节点上
        for c in llm.calls:
            if c["kind"] == "findings":
                self.assertNotIn("对标", c["user"])
        llm2 = FakeLLM()
        rv.review({"nodes": nodes(5), "benchmark": {"company": "空的", "nodes": []}}, llm2)
        self.assertNotIn("对比", [c for c in llm2.calls if c["kind"] == "overall"][0]["system"])


class _Upstream(BaseHTTPRequestHandler):
    """假的大模型接口:按队列里的预设依次回答。"""
    queue, seen = [], []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Upstream.seen.append((self.path, self.headers.get("Authorization"), body))
        item = _Upstream.queue.pop(0) if _Upstream.queue else "{}"
        code, payload = item if isinstance(item, tuple) else (200, {"choices": [{"message": {"content": item}}]})
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class LLMTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Upstream)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d/v1" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        _Upstream.queue, _Upstream.seen = [], []
        self._sleep, time.sleep = time.sleep, lambda s: None           # 不真的等退避

    def tearDown(self):
        time.sleep = self._sleep

    def llm(self, **kw):
        return LLM(self.base, "sk-test", "some-model", **kw)

    def test_request_shape(self):
        _Upstream.queue = ['{"doc": "ok"}']
        self.assertEqual(self.llm(extra_tokens=500).call_json("系统", "用户", require=["doc"], max_tokens=100), {"doc": "ok"})
        path, auth, body = _Upstream.seen[0]
        self.assertEqual((path, auth, body["model"], body["max_tokens"]), ("/v1/chat/completions", "Bearer sk-test", "some-model", 600))
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])

    def test_bad_json_is_repaired_or_retried(self):
        _Upstream.queue = ['<think>先想想 {不是答案}</think>\n```json\n{"doc": "带围栏", "x": [1, 2,],}\n```']
        self.assertEqual(self.llm().call_json("s", "u", require=["doc"])["doc"], "带围栏")
        _Upstream.queue = ['{"findings": [{"id": "n1", "level": "good"']                     # 被截断:补括号
        self.assertEqual(self.llm().call_json("s", "u", require=["findings"])["findings"][0]["id"], "n1")
        _Upstream.queue = ['{"doc": "## 总体评价\n直接换行的长文档\n- 一条"}']                  # 字符串里有真的换行符
        self.assertIn("直接换行", self.llm().call_json("s", "u", require=["doc"])["doc"])
        _Upstream.queue = ["这不是 JSON", '{"other": 1}', '{"doc": "第三次才对"}']             # 解析不了、缺字段 → 重试
        self.assertEqual(self.llm().call_json("s", "u", require=["doc"])["doc"], "第三次才对")
        self.assertEqual(len(_Upstream.seen), 6)
        _Upstream.queue = ["坏", "坏", "坏"]
        with self.assertRaises(LLMError):
            self.llm().call_json("s", "u", require=["doc"])

    def test_upstream_errors_and_empty(self):
        _Upstream.queue = [(500, {"error": "炸了"}), "", '{"doc": "ok"}']                    # HTTP 500、空内容都会重试
        self.assertEqual(self.llm().call_json("s", "u", require=["doc"], tries=1)["doc"], "ok")
        _Upstream.queue = [(200, {"base_resp": {"status_code": 1004, "status_msg": "鉴权失败"}})] * 3
        with self.assertRaises(LLMError) as c:
            self.llm().call([{"role": "user", "content": "x"}])
        self.assertIn("鉴权失败", str(c.exception))

    def test_busy_when_gate_full(self):
        llm = self.llm(concurrency=1, sem_wait_s=0.05)
        llm.sem.acquire()
        with self.assertRaises(LLMBusy):
            llm.call_json("s", "u")
        self.assertEqual(_Upstream.seen, [])

    def test_from_env_and_endpoint(self):
        self.assertIsNone(LLM.from_env({}))
        self.assertIsNone(LLM.from_env({"LLM_BASE_URL": "x", "LLM_MODEL": "m"}))
        a = LLM.from_env({"LLM_BASE_URL": "https://example.com/v1/", "LLM_API_KEY": "k", "LLM_MODEL": "m", "LLM_EXTRA_TOKENS": "2048"})
        self.assertEqual((a.url, a.extra_tokens), ("https://example.com/v1/chat/completions", 2048))
        b = LLM("https://example.com/v1/text/chatcompletion_v2", "k", "m")
        self.assertEqual(b.url, "https://example.com/v1/text/chatcompletion_v2")
        self.assertEqual(repair_json('前言 {"a": 1} 后记'), {"a": 1})


class DemoTest(unittest.TestCase):
    def test_rules(self):
        ns = [{"id": "r", "name": "总部", "type": "集团总部", "depth": 0},
              {"id": "f", "name": "财务部", "type": "部门", "depth": 1},
              {"id": "a", "name": "审计部", "type": "部门", "depth": 2},
              {"id": "s", "name": "财务共享中心", "type": "部门", "depth": 1},
              {"id": "w", "name": "事业群", "type": "部门", "depth": 1}] + \
             [{"id": "k%d" % i, "name": "事业部%d" % i, "type": "部门", "depth": 2} for i in range(11)]
        out = demo.demo_review({"org": {"name": "演示"}, "nodes": ns,
                                "benchmark": {"company": "对方", "nodes": [{"name": "总部", "depth": 0}, {"name": "法务部", "depth": 1}]}})
        got = {f["id"]: f["title"] for f in out["findings"]}
        self.assertEqual(got["a"], "审计独立性不足")
        self.assertEqual(got["w"], "管理幅度过宽")
        self.assertEqual(got["s"], "设置了共享服务")
        self.assertEqual(got["f"], "只有一个下级")
        self.assertTrue(out["demo"])
        self.assertIn("共 16 个组织节点、3 层", out["doc"])
        self.assertIn("## 与对方对比", out["doc"])
        self.assertIn("对方有而你没有的关键职能：法务", out["doc"])
        with self.assertRaises(rv.ReviewError):
            demo.demo_review({"nodes": ns[:2]})


class ServerTest(unittest.TestCase):
    def start(self, app):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return "http://127.0.0.1:%d" % srv.server_address[1]

    def req(self, url, data=None, raw=None):
        body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
        r = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET",
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.headers.get("Content-Type"), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()

    def test_static_and_health(self):
        base = self.start(server.App())
        code, ctype, body = self.req(base + "/")
        self.assertEqual((code, ctype), (200, "text/html; charset=utf-8"))
        self.assertIn("组织搭建台".encode(), body)
        code, _, body = self.req(base + "/benchmarks.json")
        self.assertEqual(code, 200)
        self.assertEqual(len(json.loads(body)["companies"]), 3)
        self.assertEqual(json.loads(self.req(base + "/api/health")[2]), {"llm": False, "demo": False})
        for p in ("/../server.py", "/..%2fserver.py", "/%2e%2e/README.md", "/nope.txt", "//etc/passwd"):
            self.assertEqual(self.req(base + p)[0], 404, p)             # 只给 web/ 里的文件

    def test_review_without_llm(self):
        base = self.start(server.App())
        code, _, body = self.req(base + "/api/review", {"nodes": nodes(5)})
        j = json.loads(body)
        self.assertEqual((code, j["code"]), (503, "no_llm"))
        self.assertIn("LLM_API_KEY", j["error"])

    def test_review_paths(self):
        llm = FakeLLM()
        base = self.start(server.App(llm=llm))
        code, _, body = self.req(base + "/api/review", {"org": {"name": "甲"}, "nodes": nodes(5)})
        self.assertEqual(code, 200)
        self.assertEqual(len(json.loads(body)["findings"]), 2)
        self.assertEqual(self.req(base + "/api/review", {"nodes": nodes(1)})[0], 400)
        self.assertEqual(self.req(base + "/api/review", raw=b"{oops")[0], 400)
        self.assertEqual(self.req(base + "/api/review", raw=b"x" * (server.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.req(base + "/api/other", {"a": 1})[0], 404)
        llm.fail_overall = LLMError("上游挂了 sk-secret")
        code, _, body = self.req(base + "/api/review", {"nodes": nodes(5)})
        self.assertEqual(code, 502)
        self.assertNotIn("sk-secret", body.decode())                    # 上游的报错细节不外露
        llm.fail_overall = LLMBusy("忙")
        self.assertEqual(self.req(base + "/api/review", {"nodes": nodes(5)})[0], 429)

    def test_gate_and_demo(self):
        app = server.App(llm=FakeLLM(), max_reviews=1)
        app.gate.acquire()
        self.assertEqual(app.do_review({"nodes": nodes(5)})[0], 429)
        app.gate.release()
        self.assertEqual(app.do_review({"nodes": nodes(5)})[0], 200)
        code, body = server.App(demo=True).do_review({"nodes": nodes(5)})
        self.assertEqual((code, body["demo"]), (200, True))


if __name__ == "__main__":
    unittest.main()
