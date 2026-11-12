"""演示模式:不调大模型,按几条固定规则给出点评。

用途:没有模型密钥时也能把整个流程走一遍(节点标记、意见文档、对标一节、导出)。
这些规则很粗,只是示意,不能代替大模型的判断,也不构成组织设计建议。
"""
import re

from .review import MAX_FINDINGS, parse

KEY_FUNCS = [("财务", r"财务|资金|结算"), ("人力", r"人力|人资|HR"), ("法务", r"法务|合规"), ("审计", r"审计"),
             ("信息化", r"数字|信息|IT|数据"), ("战略", r"战略|投资")]


def _stats(nodes):
    """按「前序遍历 + 层级」还原父子关系,返回 (每个节点的下级数, 父节点下标, 最大层数)。"""
    kids, parent, stack = [0] * len(nodes), [None] * len(nodes), []
    for i, n in enumerate(nodes):
        while stack and nodes[stack[-1]]["depth"] >= n["depth"]:
            stack.pop()
        if stack:
            parent[i] = stack[-1]
            kids[stack[-1]] += 1
        stack.append(i)
    return kids, parent, (max(n["depth"] for n in nodes) + 1 if nodes else 0)


def _avg(kids):
    spans = [k for k in kids if k]
    return round(sum(spans) / float(len(spans)), 1) if spans else 0


def demo_review(req):
    p = parse(req)
    nodes = p["nodes"]
    kids, parent, depth = _stats(nodes)
    findings, used = [], set()

    def add(i, level, title, reason):
        if i not in used and len(findings) < MAX_FINDINGS:
            used.add(i)
            findings.append({"id": nodes[i]["id"], "level": level, "title": title, "reason": reason})

    for i, n in enumerate(nodes):
        up = nodes[parent[i]] if parent[i] is not None else None
        if "审计" in n["name"] and up and re.search(r"财务|资金", up["name"]):
            add(i, "bad", "审计独立性不足", "审计挂在财务口下面，等于自己查自己。审计通常直接向最高管理层或审计委员会汇报，才能保持独立。")
        elif "审计" in n["name"] and n["depth"] <= 1:
            add(i, "good", "审计直属总部", "审计直接挂在总部下面，汇报线短，不受被审计部门的管辖，独立性有保障。")
        if kids[i] >= 10:
            add(i, "bad", "管理幅度过宽", "直接管 %d 个下级，负责人很难逐一顾到。可以按业务或职能归并，增设一层中间组织。" % kids[i])
        elif kids[i] == 1:
            add(i, "risk", "只有一个下级", "这一层只管一个下级，多了一级汇报却没有分工。可以考虑合并这两层。")
        if n["depth"] >= 5 and kids[i] == 0:
            add(i, "risk", "层级偏深", "从总部到这里已经有 %d 层，指令和信息要过很多手。可以看看中间有没有可以压缩的层级。" % (n["depth"] + 1))
        if re.search(r"共享", n["name"]):
            add(i, "good", "设置了共享服务", "把重复的事务性工作集中到共享中心，各业务单元不用各建一套，有利于降成本和统一标准。")

    text = "|".join(n["name"] + n["type"] for n in nodes)
    missing = [k for k, pat in KEY_FUNCS if not re.search(pat, text)]
    good = [f for f in findings if f["level"] == "good"]
    bad = [f for f in findings if f["level"] != "good"]
    name_of = {n["id"]: n["name"] for n in nodes}
    lines = ["## 总体评价",
             "「%s」共 %d 个组织节点、%d 层，平均每个上级直管 %s 个下级。" % (p["name"], len(nodes), depth, _avg(kids) or "–"),
             "## 亮点"]
    lines += ["- %s：%s" % (name_of[f["id"]], f["title"]) for f in good] or ["- 按固定规则没有识别出明显的亮点。"]
    lines.append("## 主要问题")
    lines += ["- %s：%s" % (name_of[f["id"]], f["title"]) for f in bad] or ["- 按固定规则没有识别出明显的问题。"]
    lines.append("## 风险提示")
    lines.append("- 没有找到这些关键职能：%s。" % "、".join(missing) if missing else "- 财务、人力、法务、审计、信息化、战略这几项关键职能都能找到。")
    lines += ["## 改进建议", "- 逐条看一下图上标红和标黄的节点，决定是调整还是保留。"]
    if missing:
        lines.append("- 评估是否需要补上缺少的职能，或者说明它由哪个现有部门承担。")
    if p["bench_nodes"]:
        bk, _, bdepth = _stats(p["bench_nodes"])
        btext = "|".join(n["name"] + n["type"] for n in p["bench_nodes"])
        only_b = [k for k, pat in KEY_FUNCS if re.search(pat, btext) and not re.search(pat, text)]
        lines += ["## 与%s对比" % p["bench_name"],
                  "- 层级深度：你 %d 层，对方 %d 层。" % (depth, bdepth),
                  "- 管理幅度：你平均 %s，对方平均 %s。" % (_avg(kids) or "–", _avg(bk) or "–"),
                  "- 职能配置：%s" % ("对方有而你没有的关键职能：%s。" % "、".join(only_b) if only_b else "关键职能上没有发现对方有而你没有的。")]
    return {"doc": "\n".join(lines), "findings": findings, "demo": True}
