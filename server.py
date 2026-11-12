#!/usr/bin/env python3
"""组织搭建台的后端:提供网页文件和一个点评接口。只用标准库。

    python3 server.py                 # http://127.0.0.1:8000
    python3 server.py --port 9000
    python3 server.py --demo          # 演示模式:不调大模型,按固定规则给出点评

接口:
    GET  /api/health   → {"llm": 是否已接入大模型, "demo": 是否演示模式}
    POST /api/review   → {"doc": markdown, "findings": [{id, level, title, reason}]}
"""
import argparse
import json
import mimetypes
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from org_builder.demo import demo_review
from org_builder.llm import LLM, LLMBusy
from org_builder.review import ReviewError, review

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
MAX_BODY = 512 * 1024          # 请求体上限
NO_LLM = ("还没有接入大模型，所以暂时不能点评。在 .env 里填好 LLM_BASE_URL、LLM_API_KEY、LLM_MODEL 后重启服务；"
          "也可以用 python3 server.py --demo 先看演示。")


def load_env_file(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class App:
    """服务的全部状态。测试里可以直接换掉 llm。"""

    def __init__(self, llm=None, demo=False, max_reviews=4):
        self.llm, self.demo = llm, demo
        self.gate = threading.Semaphore(max_reviews)      # 同时最多处理几个点评请求,满了直接回「繁忙」

    def health(self):
        return {"llm": bool(self.llm), "demo": bool(self.demo)}

    def do_review(self, req):
        """返回 (HTTP 状态码, 响应体)。"""
        if not self.demo and not self.llm:
            return 503, {"error": NO_LLM, "code": "no_llm"}
        if not self.gate.acquire(blocking=False):
            return 429, {"error": "同时点评的人太多，请稍后再试。", "code": "busy"}
        try:
            return 200, (demo_review(req) if self.demo else review(req, self.llm))
        except ReviewError as e:
            return 400, {"error": str(e)}
        except LLMBusy as e:
            return 429, {"error": str(e), "code": "busy"}
        except Exception as e:
            log("点评失败", type(e).__name__, str(e)[:200])
            return 502, {"error": "大模型没有给出可用的结果，请重试。", "code": "llm_failed"}
        finally:
            self.gate.release()


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, file=sys.stderr, flush=True)


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "org-builder"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/health":
                return self._send(200, app.health())
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            f = (WEB / rel).resolve()
            if WEB not in f.parents or not f.is_file():      # 只提供 web/ 目录里的文件
                return self._send(404, {"error": "not found"})
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/json", "application/javascript"):
                ctype += "; charset=utf-8"
            self._send(200, f.read_bytes(), ctype)

        do_HEAD = do_GET

        def do_POST(self):
            if self.path.split("?", 1)[0] != "/api/review":
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n <= 0 or n > MAX_BODY:
                left = min(max(n, 0), 8 * MAX_BODY)      # 把多出来的内容读掉再回复,否则对方只会看到连接被重置
                while left > 0:
                    chunk = self.rfile.read(min(65536, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                self.close_connection = True
                return self._send(413 if n > MAX_BODY else 400, {"error": "请求体为空或过大"})
            try:
                req = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return self._send(400, {"error": "请求不是合法的 JSON"})
            t0 = time.time()
            code, body = app.do_review(req)
            log("POST /api/review", code, "%.1fs" % (time.time() - t0))
            self._send(code, body)

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description="组织搭建台")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--demo", action="store_true", help="演示模式:不调大模型,按固定规则给出点评")
    a = ap.parse_args(argv)
    load_env_file(str(ROOT / ".env"))
    demo = a.demo or os.environ.get("ORG_BUILDER_DEMO") == "1"
    app = App(llm=LLM.from_env(), demo=demo)
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(app))
    state = "演示模式" if demo else ("已接入大模型 %s" % app.llm.model if app.llm else "未接入大模型(点评不可用,其余功能正常)")
    log("组织搭建台 http://%s:%d  %s" % (a.host, a.port, state))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
