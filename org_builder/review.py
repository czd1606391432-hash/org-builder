"""组织架构点评:把一棵组织树交给大模型,拿回「总体意见文档 + 逐节点评判」。

做法:两件事拆开、并发去做——
  · 总体意见:整棵树一次看完,写一篇文档(带对标时多写一节对比)
  · 逐节点评判:把节点每 40 个切一批,几批同时跑,每批只挑有话可说的节点
不用一个超长 prompt 硬扛,模型不容易顾此失彼,也不容易超时。
"""
from concurrent.futures import ThreadPoolExecutor

from .llm import LLMBusy

MAX_NODES = 150        # 一次最多评多少个节点,超出的截掉
MAX_BENCH = 80         # 对标组织最多带多少个节点
CHUNK = 40             # 逐节点评判每批多少个
WORKERS = 3            # 逐节点评判同时跑几批
MAX_FINDINGS = 16
LEVELS = ("good", "bad", "risk")

DIMS = ("评估维度:1管理幅度(一个上级直管几个下级,1-2 过窄、10+ 过宽)2层级深度(过深链条长,过平失控)"
        "3关键职能完备性(财务/人力/法务/审计/IT/战略等是否缺失)4制衡与内控(审计独立性、财务与业务是否分离)"
        "5职能重复或冲突6前中后台与共享服务设置是否合理7业务单元划分逻辑是否一致(按产品/区域/客户混用要指出)。")


class ReviewError(Exception):
    """请求本身有问题,文案可以直接给使用者看。"""


def clean_nodes(raw, cap):
    """只留认识的字段、夹住长度和层级,避免把任意内容塞进 prompt。"""
    out = []
    for n in (raw if isinstance(raw, list) else [])[:cap]:
        if not isinstance(n, dict):
            continue
        try:
            depth = max(0, min(12, int(n.get("depth") or 0)))
        except (TypeError, ValueError):
            depth = 0
        out.append({"id": str(n.get("id") or "")[:40], "name": str(n.get("name") or "")[:30],
                    "type": str(n.get("type") or "部门")[:24], "depth": depth})
    return out


def tree_text(nodes, with_id=True):
    return "\n".join("%s- %s【%s】(%s)" % ("  " * n["depth"], ("id=%s " % n["id"]) if with_id else "", n["name"], n["type"])
                     for n in nodes)


def overall_prompt(org_name, tree, truncated, bench_name, bench_tree):
    system = ("你是一位资深组织发展(OD)顾问。用户搭了一个虚拟组织架构,请你专业点评其整体设计。\n" + DIMS +
              "\n只评组织设计本身,不臆测业务细节;不要提任何人名。\n"
              "严格输出 JSON,不要 JSON 之外的任何文字:\n"
              '{"doc":"完整意见文档,markdown,含:## 总体评价 / ## 亮点 / ## 主要问题 / ## 风险提示 / ## 改进建议,400~700字"}')
    user = "组织名称:%s\n组织架构树(缩进=层级):\n%s" % (org_name, tree)
    if truncated:
        user += "\n(注:节点过多已截断,仅评前 %d 个)" % MAX_NODES
    if bench_tree:
        system += ("\n本次带对标组织「%s」(树附在用户消息里)。doc 中必须增加一节『## 与%s对比』,"
                   "从三个维度具体比较:①层级深度 ②管理幅度 ③职能配置差异(对方有而用户没有的关键职能、反之亦然),"
                   "并给 1-2 条借鉴建议。对比要具体到组织名称,不要泛泛而谈。") % (bench_name, bench_name)
        user += "\n\n对标组织「%s」的组织架构树:\n%s" % (bench_name, bench_tree)
    return system, user


def findings_prompt(chunk):
    system = ("你是资深组织发展(OD)顾问。下面是某虚拟组织架构中的一部分节点,请逐节点评判。\n" + DIMS +
              "\n严格输出 JSON,不要 JSON 之外的任何文字:\n"
              '{"findings":[{"id":"节点id","level":"good|bad|risk","title":"不超过12字的结论","reason":"40~80字的为什么"}]}\n'
              "规则:只评这批节点里确实有话可说的(2~6 条即可,不要硬凑);"
              "good=设计得好的点,bad=明显不合理,risk=当下合理但高危需警惕;"
              "id 必须严格取自输入里的节点 id;不要提人名。")
    return system, "本批节点(缩进=层级):\n%s" % tree_text(chunk)


def merge_findings(batches, valid_ids):
    """合并各批结果:只认输入里有的节点 id 和三种级别,同一节点只留一条;各批轮流取,免得靠前的批次占满名额。"""
    cleaned = []
    seen = set()
    for batch in batches:
        rows = []
        for f in ((batch or {}).get("findings") or []) if isinstance(batch, dict) else []:
            if not isinstance(f, dict):
                continue
            fid = str(f.get("id") or "")
            if fid in valid_ids and f.get("level") in LEVELS and fid not in seen:
                seen.add(fid)
                rows.append({"id": fid, "level": f["level"], "title": str(f.get("title") or "")[:24],
                             "reason": str(f.get("reason") or "")[:220]})
        cleaned.append(rows)
    out, i = [], 0
    while len(out) < MAX_FINDINGS and any(i < len(r) for r in cleaned):
        out += [r[i] for r in cleaned if i < len(r)]
        i += 1
    return out[:MAX_FINDINGS]


def parse(req):
    if not isinstance(req, dict):
        raise ReviewError("请求格式不对")
    org = req.get("org") if isinstance(req.get("org"), dict) else {}
    name = str(org.get("name") or "未命名组织").strip()[:60] or "未命名组织"
    raw = req.get("nodes") if isinstance(req.get("nodes"), list) else []
    nodes = [n for n in clean_nodes(raw, MAX_NODES) if n["id"]]
    if len(nodes) < 3:
        raise ReviewError("组织太小:至少搭 3 个节点再点评。")
    bench = req.get("benchmark") if isinstance(req.get("benchmark"), dict) else {}
    bnodes = clean_nodes(bench.get("nodes"), MAX_BENCH)
    bname = (str(bench.get("company") or "对标组织").strip()[:40] or "对标组织") if bnodes else ""
    return {"name": name, "nodes": nodes, "truncated": len(raw) > MAX_NODES, "bench_name": bname, "bench_nodes": bnodes}


def review(req, llm):
    """返回 {"doc": markdown, "findings": [{id, level, title, reason}]}。"""
    p = parse(req)
    nodes = p["nodes"]
    bench_tree = tree_text(p["bench_nodes"], with_id=False) if p["bench_nodes"] else ""
    sys_o, user_o = overall_prompt(p["name"], tree_text(nodes), p["truncated"], p["bench_name"], bench_tree)
    chunks = [nodes[i:i + CHUNK] for i in range(0, len(nodes), CHUNK)]

    def one_chunk(chunk):
        s, u = findings_prompt(chunk)
        return llm.call_json(s, u, require=["findings"], temperature=0.35, max_tokens=1800, timeout=120)

    with ThreadPoolExecutor(max_workers=WORKERS + 1) as pool:
        f_overall = pool.submit(llm.call_json, sys_o, user_o, require=["doc"], temperature=0.4, max_tokens=2600, timeout=150)
        f_chunks = [pool.submit(one_chunk, c) for c in chunks]
        batches, busy = [], None
        for f in f_chunks:      # 某一批失败不影响整体,少几条评判而已
            try:
                batches.append(f.result())
            except LLMBusy as e:
                busy = e
            except Exception:
                pass
        overall = f_overall.result()      # 总体意见失败则整个请求失败
    doc = str(overall.get("doc") or "").strip()
    findings = merge_findings(batches, {n["id"] for n in nodes})
    if not doc and not findings:
        raise busy or ValueError("模型没有给出点评")
    return {"doc": doc, "findings": findings}
