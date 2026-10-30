#!/usr/bin/env python3
"""Fetch WeChat public-account articles as Markdown via the remote MCP service.

Standalone:  fetch_articles.py <url> [url...]
Scan mode :  fetch_articles.py --from-raw <raw_export.md>   (extract mp.weixin links)

Articles are saved under {data_root}/raw/公众号/ with a source header, and a
URL index deduplicates repeat fetches. The service is a personal third-party
server (no SLA); failures are reported per URL and never crash the pipeline.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

MCP_URL = "https://changfengbox.top/api/mcp"
DEFAULT_DATA_ROOT = Path("~/Documents/wechat-local-vault").expanduser()
ARTICLE_RE = re.compile(r"https?://mp\.weixin\.qq\.com/s[^\s\"'<>)\]]+")
ALBUM_RE = re.compile(r"https?://mp\.weixin\.qq\.com/mp/appmsgalbum[^\s\"'<>)\]]+")
JUNK_LINE = (
    re.compile(r"^原创"),
    re.compile(r"^_?\d{4}年\d{1,2}月\d{1,2}日.*$"),
    re.compile(r"^在小说阅读器读本章$"),
    re.compile(r"^去阅读$"),
    re.compile(r"^[\s_]+$"),
)


def clean_article(md: str) -> str:
    """Strip WeChat article chrome: byline/date/reader nav, duplicated image captions."""
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    out: list[str] = []
    in_code = False
    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            in_code = not in_code
        if not in_code:
            stripped = line.strip()
            if any(p.match(stripped) for p in JUNK_LINE):
                continue
            line = re.sub(r"[ \t]{2,}", " ", line).replace("\u200b", "").replace("\xa0", " ")
        out.append(line)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    # hard-wrapped image syntax: join "![alt..." + "...](url)" back onto one line
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)",
                  lambda m: re.sub(r"\s*\n\s*", " ", m.group(0)), text)
    lines = text.splitlines()
    cleaned: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("!["):
            m = re.match(r"\s*!\[([^\]]*)\]", line)
            alt = re.sub(r"\s+", "", m.group(1)) if m else ""
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            follow: list[str] = []
            while j < len(lines) and lines[j].strip():
                follow.append(lines[j])
                j += 1
            if alt and follow and re.sub(r"\s+", "", "".join(follow)) == alt:
                cleaned.append(line)
                i = j
                continue
        cleaned.append(line)
        i += 1
    return "\n".join(cleaned)


def call_tool(name: str, arguments: dict, timeout: int) -> dict:
    payload = {"jsonrpc": "2.0", "method": "tools/call", "id": 1,
               "params": {"name": name, "arguments": arguments}}
    req = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    if "error" in data:
        raise RuntimeError(f"MCP error: {data['error']}")
    return json.loads(data["result"]["content"][0]["text"])


def download(url: str, timeout: int) -> str:
    url = urllib.parse.quote(url, safe=":/?&=#%")
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def save_article(root: Path, url: str, body: str) -> Path:
    title = next((ln.lstrip("# ").strip() for ln in body.splitlines() if ln.startswith("# ")), "")
    safe = re.sub(r'[\\/:*?"<>|\s]+', "-", title)[:60].strip("-") or "untitled"
    save_dir = root / "raw" / "公众号"
    save_dir.mkdir(parents=True, exist_ok=True)
    path = save_dir / f"{datetime.now().strftime('%Y%m%d')}-{safe}.md"
    header = (f"<!-- source: {url}\n     fetched: {datetime.now().isoformat(timespec='seconds')} -->\n\n")
    path.write_text(header + clean_article(body), encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="*", help="公众号文章或合集链接")
    ap.add_argument("--from-raw", help="从 raw 导出文件中提取公众号链接")
    ap.add_argument("--from-favorites", action="store_true", help="从收藏夹(favorite.db)提取文章链接")
    ap.add_argument("--decrypted-dir", help="解密库目录,默认取配置")
    ap.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    ap.add_argument("--max", type=int, default=10)
    ap.add_argument("--timeout", type=int, default=90)
    args = ap.parse_args()
    root = Path(args.data_root).expanduser()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    urls = list(args.urls)
    if args.from_raw:
        text = Path(args.from_raw).read_text(encoding="utf-8")
        urls += [u.rstrip(".,;，。；") for u in ARTICLE_RE.findall(text)]
        urls += [u.rstrip(".,;，。；") for u in ALBUM_RE.findall(text)]
    if args.from_favorites:
        cfg_file = Path("~/.config/wechat-local-vault.json").expanduser()
        decrypted = Path(args.decrypted_dir or
                         (json.loads(cfg_file.read_text()).get("decrypted_dir")
                          if cfg_file.exists() else None)
                         or "~/Library/Application Support/wechat-local-vault/decrypted/current").expanduser()
        fav_db = decrypted / "favorite/favorite.db"
        if not fav_db.exists():
            raise SystemExit(f"找不到 favorite.db: {fav_db}")
        con = sqlite3.connect(fav_db)
        for (content,) in con.execute("SELECT content FROM fav_db_item WHERE type=5 ORDER BY update_time DESC"):
            m = ARTICLE_RE.search((content or "").replace("&amp;", "&"))
            if m:
                urls.append(m.group(0))
    seen: set[str] = set()
    urls = [u for u in urls if not (u in seen or seen.add(u))]

    index_file = root / "raw" / "公众号" / "index.json"
    index = json.loads(index_file.read_text(encoding="utf-8")) if index_file.exists() else {}
    results = []
    for url in urls[: args.max]:
        if url in index:
            results.append({"url": url, "status": "已存在", "file": index[url]["file"]})
            continue
        try:
            if ALBUM_RE.match(url):
                data = call_tool("wechat_collection", {"url": url}, args.timeout)
                saved = []
                for md_url in data.get("urls", []):
                    body = download(md_url, args.timeout)
                    path = save_article(root, url, body)
                    index[f"{url}#{path.name}"] = {
                        "file": str(path.relative_to(root)), "collection": url,
                        "fetched_at": datetime.now().isoformat(timespec="seconds")}
                    saved.append(str(path.relative_to(root)))
                results.append({"url": url, "status": "collection", "files": saved})
                continue
            data = call_tool("wechat", {"url": url, "config": {"MD": True}}, args.timeout)
            md_urls = data.get("urls") or []
            if not md_urls:
                raise RuntimeError(f"未返回下载链接: {data}")
            body = download(md_urls[0], args.timeout)
            path = save_article(root, url, body)
            title = next((ln.lstrip("# ").strip() for ln in body.splitlines() if ln.startswith("# ")), "")
            index[url] = {"file": str(path.relative_to(root)), "title": title,
                          "fetched_at": datetime.now().isoformat(timespec="seconds")}
            results.append({"url": url, "status": "ok", "file": str(path.relative_to(root)), "title": title})
        except Exception as e:  # noqa: BLE001 - per-URL failure must not abort the batch
            results.append({"url": url, "status": "error", "error": str(e)})

    index_file.parent.mkdir(parents=True, exist_ok=True)
    index_file.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
