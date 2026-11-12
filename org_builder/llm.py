"""大模型调用:OpenAI 兼容的 chat/completions 接口,只用标准库。

做了几件让结构化输出稳定下来的事:
  · 重试 + 指数退避;模型返回空内容也算失败,会重试
  · 并发闸门:同时最多 N 个请求打到上游,排队超过一定时间就报「繁忙」而不是无限等
  · 一次调用(含全部重试)有总时限,超了就不再重试
  · JSON 修复:去掉推理段和代码块围栏、截出最外层 {...}、去尾逗号、补右括号,再校验必填字段
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request


class LLMError(RuntimeError):
    pass


class LLMBusy(LLMError):
    """上游正忙(并发闸满)。"""


def _endpoint(base):
    base = (base or "").rstrip("/")
    if base.endswith("/chat/completions") or "chatcompletion" in base:      # 已经是完整地址就原样用
        return base
    return base + "/chat/completions"


def repair_json(text):
    """尽力把模型输出解析成 dict。解析不了就抛异常。"""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)          # 有的模型把推理过程也放在正文里
    t = re.sub(r"^```(?:json)?|```$", "", t.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", t, re.S)
    cand = m.group(0) if m else t
    loads = lambda x: json.loads(x, strict=False)                          # strict=False:容忍字符串里直接换行(长文档常见)
    try:
        return loads(cand)
    except Exception:
        pass
    try:
        return loads(re.sub(r",\s*([}\]])", r"\1", cand))                  # 去尾逗号
    except Exception:
        pass
    return loads(_close_brackets(t[t.index("{"):] if "{" in t else t))


def _close_brackets(s):
    """输出被截断时:把没写完的字符串和括号按嵌套顺序补上。"""
    stack, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    s = s + ('"' if in_str else "")
    s = re.sub(r"[,:]\s*$", "", s.rstrip())                                # 断在逗号或冒号后面
    return re.sub(r",\s*([}\]])", r"\1", s + "".join(reversed(stack)))


class LLM:
    def __init__(self, base_url, api_key, model, concurrency=6, sem_wait_s=25, call_budget_s=100, extra_tokens=0):
        self.url, self.api_key, self.model = _endpoint(base_url), api_key, model
        self.sem = threading.Semaphore(max(1, int(concurrency)))
        self.sem_wait_s, self.call_budget_s = float(sem_wait_s), float(call_budget_s)
        self.extra_tokens = int(extra_tokens)      # 会先推理再作答的模型,推理也占 max_tokens,要额外留余量

    @classmethod
    def from_env(cls, env=None):
        """从环境变量建;没配齐就返回 None。"""
        env = os.environ if env is None else env
        base, key, model = env.get("LLM_BASE_URL"), env.get("LLM_API_KEY"), env.get("LLM_MODEL")
        if not (base and key and model):
            return None
        return cls(base, key, model,
                   concurrency=env.get("LLM_CONCURRENCY") or 6,
                   extra_tokens=env.get("LLM_EXTRA_TOKENS") or 0)

    def _post(self, messages, temperature, max_tokens, timeout):
        body = json.dumps({"model": self.model, "messages": messages, "temperature": temperature,
                           "max_tokens": int(max_tokens) + self.extra_tokens}).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers={
            "Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise LLMError("上游返回 HTTP %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:200]))
        br = data.get("base_resp") or {}                                   # 个别厂商把错误放在 200 响应的这个字段里
        if br.get("status_code", 0) != 0:
            raise LLMError("上游报错: %s" % br.get("status_msg"))
        if data.get("error"):
            raise LLMError("上游报错: %s" % str(data["error"])[:200])
        try:
            return (data["choices"][0]["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("上游返回的格式不对")

    def call(self, messages, temperature=0.4, max_tokens=2000, timeout=120, retries=2, deadline=None):
        """调一次模型,返回正文。"""
        deadline = deadline or (time.time() + self.call_budget_s)
        last = None
        for attempt in range(int(retries) + 1):
            if attempt > 0 and time.time() > deadline:
                break
            if not self.sem.acquire(timeout=self.sem_wait_s):
                raise LLMBusy("AI 服务当前繁忙，请稍后再试")
            try:
                txt = self._post(messages, temperature, max_tokens, timeout)
                if txt:
                    return txt
                last = LLMError("模型返回空")
            except LLMBusy:
                raise
            except Exception as e:
                last = e
            finally:
                self.sem.release()
            if attempt < int(retries):
                time.sleep(min(4.0, 0.6 * (2 ** attempt)))                 # 指数退避
        raise LLMError("调用失败: %s" % last)

    def call_json(self, system, user, require=None, tries=3, **kw):
        """要结构化输出时用:多次尝试 + JSON 修复 + 必填字段校验。返回 dict。"""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        t_end = time.time() + self.call_budget_s                           # 结构化重试与 call 内部重试共用一个总时限
        kw.setdefault("deadline", t_end)
        last = None
        for i in range(max(1, tries)):
            if i > 0 and time.time() > t_end:
                break
            try:
                obj = repair_json(self.call(messages, **kw))
                if not isinstance(obj, dict) or not obj:
                    raise ValueError("不是 JSON 对象")
                miss = [k for k in (require or []) if k not in obj]
                if miss:
                    raise ValueError("缺字段 %s" % miss)
                return obj
            except LLMBusy:
                raise
            except Exception as e:
                last = e
        raise LLMError("结构化输出失败: %s" % last)
