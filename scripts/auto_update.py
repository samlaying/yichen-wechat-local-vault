#!/usr/bin/env python3
"""Unattended vault pipeline.

incremental decrypt → per-object raw windows → LLM digests → category
indexes → encrypted public/private site → git push. Designed to run from
cron; every step degrades with a clear warning instead of crashing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

REPO_SCRIPTS = Path(__file__).resolve().parent
DEFAULT_DATA_ROOT = Path("~/Documents/wechat-local-vault").expanduser()
CONFIG_FILE = Path("~/.config/wechat-local-vault.json").expanduser()
MYAGENTS_CONFIG = Path("~/.myagents/config.json").expanduser()
MAX_RAW_CHARS = 80_000


def load_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 600), **kw)


class LLM:
    def __init__(self, cfg: dict):
        self.base = cfg.get("llm_base_url", "").rstrip("/")
        self.model = cfg.get("llm_model", "cn:glm-5.3-flash")
        keys = load_json(MYAGENTS_CONFIG).get("providerApiKeys", {})
        self.key = keys.get("local-proxy") or os.environ.get("WECHAT_LLM_KEY")
        self.enabled = bool(self.base and self.key)
        if not self.enabled:
            print("WARN: LLM 未启用(缺 ~/.myagents/config.json 的 providerApiKeys['local-proxy']"
                  " 或 llm_base_url),摘要步骤跳过,其余照常。")

    def chat(self, system: str, user: str) -> str:
        body = json.dumps({"model": self.model, "temperature": 0.4,
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": user}]}).encode()
        req = urllib.request.Request(
            f"{self.base}/chat/completions", data=body,
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as resp:
            data = json.load(resp)
        return data["choices"][0]["message"]["content"].strip()


GROUP_SYS = """你是微信群聊分析师。根据给出的聊天记录时间窗写一份「话题梳理」Markdown:
# {群名} 话题梳理({窗口})
开头一个要点列表:数据源相对路径、群性质一句话、核心人物、本人(萌绳/ᵐᵉ)参与度(无记录则写"本窗未参与")。
然后 "## 话题清单",每个话题一个 "### 序号. 标题(日期区间)" 小节,2~5 句:谁说了什么、关键结论/价格/工具、分歧点。
最后 "## 可跟进动作" 2~4 条。要求:只依据记录,不编造;语音仅写时长;图片内容不可见时明说。
正文结束后,最后一行输出分类结果(JSON 代码块):
```json
{{"categories": ["从给定分类中选 1~3 个;都不同意可提议新名"]}}
```"""

PRIVATE_SYS = """你是微信私聊关系分析师。写一份「话题梳理」Markdown:
# {联系人} 话题梳理(截至 {日期})
"## 近期互动主线" 按话题分小节;"## 观察与判断";"## 跟进建议" 2~3 条。
语气客观克制;涉及健康/家庭只描述事实不做推断;隐私细节保留但概括表达。
正文结束后,最后一行输出分类结果(JSON 代码块):
```json
{{"categories": ["从给定分类中选 1~3 个;都不同意可提议新名"]}}
```"""


def window_file_name(kind: str, start: str, end: str) -> str:
    s, e = start[:10], end[:10]
    if s == e:
        label = f"{s}-{start[11:16].replace(':', '')}"
    elif s[:7] == e[:7]:
        label = f"{s}~{e[5:]}"
    else:
        label = f"{s}~{e}"
    return f"{label}-增量.md" if kind == "群聊" else f"{label}.md"


def split_digest(raw_md: str) -> tuple[str, list[str]]:
    m = re.search(r"```json\s*(\{.*?\})\s*```?\s*$", raw_md, re.S)
    cats: list[str] = []
    body = raw_md
    if m:
        body = raw_md[: m.start()].rstrip()
        try:
            cats = json.loads(m.group(1)).get("categories", [])
        except (json.JSONDecodeError, AttributeError):
            cats = []
    return body, cats


def update_category(cats_root: Path, name: str, row_link: str, row_label: str, topic: str, llm: LLM) -> str:
    index = cats_root / name / "index.md"
    if not index.parent.exists():
        return f"分类 {name} 不存在(新分类需人工确认),未挂载"
    text = index.read_text(encoding="utf-8")
    if row_link in text:
        return f"{name}: 已有该条,跳过"
    lines = [ln for ln in text.splitlines() if ln.startswith("|")]
    table = [ln for ln in lines if not re.fullmatch(r"\|[\s:|-]+\|", ln)]
    new_row = f"| [{row_label}]({row_link}) | {topic} |"
    if table:
        text = text.rstrip() + "\n" + new_row + "\n"
    else:
        text = text.rstrip() + f"\n\n| 对象 | 话题 |\n|---|---|\n{new_row}\n"
    if llm.enabled:
        try:
            rewritten = llm.chat(
                "你维护一个分类索引页。保持既有格式(# 标题 / > 综述(更新于 日期): … / Markdown 表格),"
                "根据新增行重写综述段(带今天日期,可推翻旧判断),表格原样保留全部行,只输出文件全文。",
                f"现有文件:\n{text}")
            if rewritten.startswith("#") and "|" in rewritten:
                text = rewritten
        except Exception as e:  # noqa: BLE001
            print(f"WARN: 分类综述重写失败({name}): {e}")
    index.write_text(text, encoding="utf-8")
    return f"{name}: 已挂载并更新综述"


def fetch_article_context(root: Path, raw_file: Path, max_n: int) -> str:
    """Fetch public-account articles linked in the window; return prompt context."""
    try:
        r = run([sys.executable, str(REPO_SCRIPTS / "fetch_articles.py"),
                 "--from-raw", str(raw_file), "--max", str(max_n), "--data-root", str(root)], timeout=300)
        if r.returncode != 0:
            print(f"WARN: 公众号抓取失败: {(r.stderr or '')[-200:]}")
            return ""
        items = json.loads(r.stdout or "[]")
    except Exception as e:  # noqa: BLE001
        print(f"WARN: 公众号抓取异常: {e}")
        return ""
    chunks = []
    for it in items:
        for f in (it.get("files") or ([it["file"]] if it.get("file") else [])):
            p = root / f
            if p.exists():
                text = p.read_text(encoding="utf-8")
                title = it.get("title") or p.stem
                chunks.append(f"### {title}\n{text[:2500]}")
    if not chunks:
        return ""
    return "\n\n## 本窗引用的公众号文章(已抓取正文,节选)\n" + "\n\n".join(chunks)


def process_window(root: Path, kind: str, name: str, raw_file: Path, start_ts: str, end_ts: str,
                   cats: list[str], llm: LLM, obj_rules: dict, global_block: list[str],
                   article_max: int = 0) -> dict:
    rel = raw_file.relative_to(root)
    if not llm.enabled:
        return {"digest": "queued"}
    content = raw_file.read_text(encoding="utf-8")
    if len(content) > MAX_RAW_CHARS:
        content = content[:2000] + "\n…(中段截断)…\n" + content[-MAX_RAW_CHARS:]
    sys_prompt = (GROUP_SYS if kind == "群聊" else PRIVATE_SYS)
    focus = obj_rules.get("focus", "")
    block = list(obj_rules.get("block", [])) + list(global_block)
    rules_text = ""
    if focus:
        rules_text += f"\n内容规则(必须遵守):{focus}"
    if block:
        rules_text += f"\n以下内容一律不写:{'、'.join(block)}"
    rules_text += "\n如果本窗没有符合内容规则的信息,只输出一行:SKIP"
    article_ctx = fetch_article_context(root, raw_file, article_max) if article_max > 0 else ""
    raw_out = llm.chat(sys_prompt,
                       f"对象:{name}({kind})\n时间窗:{start_ts} ~ {end_ts}\n"
                       f"数据源相对路径:{rel}\n已有分类:{cats}{rules_text}\n\n聊天记录:\n{content}{article_ctx}")
    if raw_out.strip().startswith("SKIP") and "#" not in raw_out[:20]:
        return {"digest": "skipped(无符合规则的内容)"}
    body, chosen = split_digest(raw_out)
    topic_file = root / "topics" / kind / name / raw_file.name
    topic_file.parent.mkdir(parents=True, exist_ok=True)
    topic_file.write_text(body if body.startswith("#") else f"# {name} 话题梳理\n\n{body}", encoding="utf-8")
    link = f"../../topics/{kind}/{name}/{raw_file.name}"
    label = f"{name}·{raw_file.stem.split('~')[0]}"
    first_topic = next((ln.lstrip("# ").strip() for ln in body.splitlines() if ln.startswith("### ")), raw_file.stem)
    mounted = [update_category(root / "categories", c.strip(), link, label, first_topic, llm)
               for c in chosen[:3] if c.strip()]
    return {"digest": str(topic_file.relative_to(root)), "categories": mounted}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()
    root = Path(args.data_root).expanduser()
    cfg = load_json(CONFIG_FILE)
    state_file = root / "tmp" / "auto_state.json"
    state = load_json(state_file)
    objects_state: dict = state.setdefault("objects", {})
    llm = LLM(cfg)
    if args.no_llm:
        llm.enabled = False
    now = datetime.now()
    results: dict[str, dict] = {}
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if sys.platform == "darwin":
        r = run([sys.executable, str(REPO_SCRIPTS / "decrypt_all_dbs.py"), "--mode", "incremental"])
        if r.returncode != 0:
            print("FATAL: 增量解密失败:", r.stderr[-400:])
            sys.exit(1)
        print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "decrypt done")
    else:
        print(f"WARN: 当前平台 {sys.platform} 不支持增量解密(macOS 专属),跳过;"
              f"Windows 请用 vault_cli snapshot 模式离线分析,本流程只重建站点与抓取文章。")

    cats = sorted(d.name for d in (root / "categories").iterdir()) if (root / "categories").exists() else []
    rules = load_json(root / "config" / "rules.json")
    global_block = list(rules.get("global_block", []))
    article_max = int(cfg.get("article_max_per_window", 10)) if cfg.get("article_fetch", True) else 0

    def obj_rules(kind: str, name: str) -> dict:
        return rules.get("objects", {}).get(f"{kind}/{name}", {})

    pending: list = state.setdefault("pending", [])
    if llm.enabled and pending:
        keep = []
        for item in pending:
            raw_file = root / item["raw"]
            if raw_file.exists():
                try:
                    item["result"] = process_window(root, item["kind"], item["name"], raw_file,
                                                    item.get("start", ""), item.get("end", ""), cats, llm,
                                                    obj_rules(item["kind"], item["name"]), global_block,
                                                    article_max)
                    continue
                except Exception as e:  # noqa: BLE001
                    item.setdefault("errors", []).append(str(e))
            keep.append(item)
        state["pending"] = keep
        pending = keep
    for kind in ("群聊", "私聊"):
        base = root / "raw" / kind
        if not base.exists():
            continue
        for obj_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            name = obj_dir.name
            key = f"{kind}/{name}"
            res = results.setdefault(key, {})
            start_ts = objects_state.get(key)
            if not start_ts:
                last = sorted(obj_dir.glob("*.md"))[-1:] or []
                m = re.match(r"^(\d{4}-\d{2}-\d{2})", last[0].name) if last else None
                start_ts = f"{m.group(1)} 00:00:00" if m else f"{now.date()} 00:00:00"
            end_ts = now.strftime("%Y-%m-%d %H:%M:%S")
            if start_ts >= end_ts:
                res.update(exported=0, note="无新窗口")
                continue
            raw_file = obj_dir / window_file_name(kind, start_ts, end_ts)
            r = run([sys.executable, str(REPO_SCRIPTS / "vault_cli.py"), "export", name,
                     "--start-time", start_ts, "--end-time", end_ts, "--limit", "5000",
                     "--output", str(raw_file)], cwd=str(REPO_SCRIPTS))
            m = re.search(r"Exported (\d+) messages", r.stdout or "")
            count = int(m.group(1)) if m else 0
            res["exported"] = count
            objects_state[key] = end_ts
            if r.returncode != 0 or count == 0:
                raw_file.unlink(missing_ok=True)
                res["note"] = "无新增消息"
                continue

            rel = raw_file.relative_to(root)
            o_rules = obj_rules(kind, name)
            if o_rules.get("publish", True) is False:
                res["digest"] = "规则:不上传站点(仅本地留存 raw)"
                continue
            if not llm.enabled:
                pending.append({"raw": str(rel), "kind": kind, "name": name,
                                "start": start_ts, "end": end_ts})
                res["digest"] = "queued(等 LLM key)"
                continue
            try:
                res.update(process_window(root, kind, name, raw_file, start_ts, end_ts, cats, llm,
                                          o_rules, global_block, article_max))
            except Exception as e:  # noqa: BLE001
                res["digest"] = f"LLM 调用失败:{e}"
                pending.append({"raw": str(rel), "kind": kind, "name": name,
                                "start": start_ts, "end": end_ts})
                continue
    state["last_run"] = now.strftime("%Y-%m-%d %H:%M:%S")
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    if cfg.get("favorites_article_fetch", True):
        r = run([sys.executable, str(REPO_SCRIPTS / "fetch_articles.py"), "--from-favorites",
                 "--max", "20", "--data-root", str(root)], timeout=600)
        if r.returncode != 0:
            print(f"WARN: 收藏文章抓取失败: {(r.stderr or '')[-200:]}")

    r = run([sys.executable, str(REPO_SCRIPTS / "build_site.py"), "--data-root", str(root)])
    print(r.stdout.strip() or r.stderr[-400:])
    if r.returncode != 0:
        sys.exit(1)

    site_dir = Path(cfg.get("site_repo_dir", str(root / "tmp" / "site"))).expanduser()
    if not args.no_push:
        msg = f"site: auto update {now.strftime('%Y-%m-%d %H:%M')}"
        for cmd in (["git", "add", "-A"], ["git", "commit", "-m", msg], ["git", "push", "origin", "main"]):
            r = run(cmd, cwd=str(site_dir))
            if cmd[1] == "commit" and r.returncode != 0 and "nothing to commit" in (r.stdout + r.stderr):
                print("no changes to commit")
                break
            if r.returncode != 0:
                print(f"WARN: git {' '.join(cmd[1:])} 失败: {(r.stderr or r.stdout)[-300:]}")
        else:
            print(f"pushed: {msg}")
    print(json.dumps({"time": state["last_run"], "results": results}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
