#!/usr/bin/env python3
"""Build the vault analysis site with a public/private split.

Public  : 群聊 topics + categories (rows referencing 私聊 filtered out).
Private : 私聊 topics + full categories, encrypted with AES-GCM under a
          password; only ciphertext lands in the repo. raw/ never enters.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import posixpath
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import PBKDF2

from fetch_articles import clean_article

DEFAULT_DATA_ROOT = Path("~/Documents/wechat-local-vault").expanduser()
KDF_ITERATIONS = 100_000

STYLE = """\
:root { --fg:#1f2328; --muted:#656d76; --bg:#ffffff; --card:#f6f8fa; --accent:#0969da; --border:#d0d7de; }
* { box-sizing:border-box; }
body { margin:0; color:var(--fg); background:var(--bg);
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  line-height:1.75; font-size:16px; }
.container { max-width:860px; margin:0 auto; padding:32px 20px 80px; }
a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
header.site { border-bottom:1px solid var(--border); padding:20px 0; margin-bottom:32px; }
header.site .title { font-size:20px; font-weight:700; }
header.site .sub { color:var(--muted); font-size:13px; margin-top:4px; }
.crumbs { color:var(--muted); font-size:13px; margin-bottom:20px; }
h1 { font-size:26px; line-height:1.35; margin:8px 0 20px; }
h2 { font-size:20px; margin:32px 0 12px; padding-bottom:8px; border-bottom:1px solid var(--border); }
h3 { font-size:17px; margin:24px 0 8px; }
blockquote { margin:12px 0; padding:10px 16px; background:var(--card); border-left:3px solid var(--accent); border-radius:6px; color:#3b424b; }
code { background:var(--card); border:1px solid var(--border); border-radius:4px; padding:1px 6px; font-size:.88em; font-family:ui-monospace,Menlo,monospace; }
pre { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:14px; overflow-x:auto; }
pre code { border:0; padding:0; background:none; }
table { border-collapse:collapse; width:100%; margin:14px 0; font-size:14.5px; }
th,td { border:1px solid var(--border); padding:7px 12px; text-align:left; vertical-align:top; }
th { background:var(--card); }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(250px,1fr)); gap:14px; margin:16px 0 8px; }
.card { display:block; background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px 16px; }
.card .name { font-weight:700; }
.card .hint { color:var(--muted); font-size:13px; margin-top:6px; display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; }
ul.meta { list-style:none; padding:0; margin:0; }
ul.meta li { padding:6px 0; border-bottom:1px dashed var(--border); font-size:15px; }
footer { color:var(--muted); font-size:12px; margin-top:56px; border-top:1px solid var(--border); padding-top:16px; }
img { max-width:100%; height:auto; border-radius:8px; }
figure { margin:18px 0; text-align:center; }
figcaption { color:var(--muted); font-size:13px; margin-top:8px; line-height:1.6; }
.article { font-size:16.5px; }
.article p { margin:12px 0; }
.article h2 { margin-top:36px; }
.article h3 { margin-top:26px; }
@media (max-width:640px){ .container{padding:20px 14px 60px;} body{font-size:15.5px;} h1{font-size:22px;} h2{font-size:18px;} table{display:block; overflow-x:auto; -webkit-overflow-scrolling:touch;} }
"""

PRIVATE_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>私聊区 · 加密</title><style>__STYLE__
.lock { max-width:420px; margin:18vh auto 0; text-align:center; }
.lock input { width:100%%; padding:12px 14px; font-size:16px; margin:14px 0; border:1px solid var(--border); border-radius:8px; }
.lock button { width:100%%; padding:12px; font-size:16px; border:0; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; }
.err { color:#cf222e; font-size:14px; min-height:20px; }
.wrap { display:none; max-width:980px; margin:0 auto; padding:24px 16px 80px; display:none; }
.layout { display:grid; grid-template-columns:280px 1fr; gap:24px; }
.nav { border-right:1px solid var(--border); padding-right:12px; }
.nav a { display:block; padding:8px 10px; border-radius:6px; font-size:14px; color:var(--fg); }
.nav a.on { background:var(--card); font-weight:700; }
@media (max-width:760px){ .layout{ grid-template-columns:1fr; } .nav{ border-right:0; border-bottom:1px solid var(--border); padding-bottom:10px; } }
</style></head><body>
<div class="lock" id="lock">
  <h2>🔒 私聊加密区</h2>
  <p style="color:var(--muted);font-size:14px">内容以 AES-256-GCM 加密存储,输入密码解锁。</p>
  <input type="password" id="pw" placeholder="访问密码" autocomplete="off">
  <button onclick="unlock()">解锁</button>
  <div class="err" id="err"></div>
</div>
<div class="wrap" id="wrap">
  <div class="layout">
    <nav class="nav" id="nav"></nav>
    <div id="content"></div>
  </div>
</div>
<script>
const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
let PAGES = [], CATS = [];
async function unlock() {
  const pw = document.getElementById('pw').value;
  const err = document.getElementById('err');
  err.textContent = '';
  try {
    const bundle = await (await fetch('bundle.json')).json();
    const km = await crypto.subtle.importKey('raw', new TextEncoder().encode(pw), 'PBKDF2', false, ['deriveKey']);
    const key = await crypto.subtle.deriveKey({name:'PBKDF2', salt:b64(bundle.salt), iterations:__ITER__, hash:'SHA-256'},
      km, {name:'AES-GCM', length:256}, false, ['decrypt']);
    const pt = await crypto.subtle.decrypt({name:'AES-GCM', iv:b64(bundle.iv)}, key, b64(bundle.ct));
    const data = JSON.parse(new TextDecoder().decode(pt));
    PAGES = data.pages || []; CATS = data.cats || [];
    document.getElementById('lock').style.display = 'none';
    document.getElementById('wrap').style.display = 'block';
    renderNav(); show(0);
  } catch (e) { err.textContent = '密码错误或数据损坏'; }
}
function renderNav() {
  const nav = document.getElementById('nav');
  const items = [];
  PAGES.forEach((p, i) => items.push(`<a href="#" data-i="${i}" onclick="show(${i},'p');return false">${p.title}</a>`));
  CATS.forEach((c, j) => items.push(`<a href="#" style="color:var(--muted)" onclick="show(${j},'c');return false">分类·${c.name}</a>`));
  nav.innerHTML = items.join('');
}
function show(i, kind) {
  const list = kind === 'c' ? CATS : PAGES;
  const item = list[i];
  document.getElementById('content').innerHTML =
    `<div class="crumbs">私聊加密区 / ${item.title}</div>` + item.html;
  document.querySelectorAll('.nav a').forEach(a => a.classList.remove('on'));
  const el = document.querySelector(`.nav a[data-i="${i}"]`);
  if (el) el.classList.add('on');
}
document.getElementById('pw').addEventListener('keydown', e => { if (e.key === 'Enter') unlock(); });
</script></body></html>
"""


def md_inline(text: str, src_dir: Path, data_root: Path, url_of) -> str:
    text = html.escape(text)

    def link(m: re.Match) -> str:
        label, target = m.group(1), m.group(2)
        if target.startswith(("http://", "https://", "#", "mailto:")):
            return f'<a href="{target}">{label}</a>'
        md_path = (src_dir / target).resolve()
        try:
            rel = md_path.relative_to(data_root)
        except ValueError:
            return f"<u>{label}</u>"
        url = url_of(rel) if md_path.exists() and md_path.suffix == ".md" else None
        return f'<a href="{url}">{label}</a>' if url else f"<u>{label}</u>"

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)


def render_markdown(md: str, src_dir: Path, data_root: Path, url_of) -> str:
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            buf = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>" + html.escape("\n".join(buf)) + "</code></pre>")
        elif re.match(r"^\s*>\s?", line):
            buf = []
            while i < len(lines) and re.match(r"^\s*>\s?", lines[i]):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + md_inline(" ".join(buf), src_dir, data_root, url_of) + "</blockquote>")
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                t = ("<table><thead><tr>" + "".join(f"<th>{md_inline(c, src_dir, data_root, url_of)}</th>" for c in rows[0])
                     + "</tr></thead><tbody>")
                for r in rows[1:]:
                    t += "<tr>" + "".join(f"<td>{md_inline(c, src_dir, data_root, url_of)}</td>" for c in r) + "</tr>"
                out.append(t + "</tbody></table>")
        elif re.match(r"^\s*[-*]\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*]\s+", "", lines[i]))
                i += 1
            out.append("<ul>" + "".join(f"<li>{md_inline(x, src_dir, data_root, url_of)}</li>" for x in items) + "</ul>")
        elif re.match(r"^\s*\d+\.\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                items.append(re.sub(r"^\s*\d+\.\s+", "", lines[i]))
                i += 1
            out.append("<ol>" + "".join(f"<li>{md_inline(x, src_dir, data_root, url_of)}</li>" for x in items) + "</ol>")
        elif re.match(r"^#{1,4}\s", line):
            level = min(len(line) - len(line.lstrip("#")), 4)
            content = md_inline(line.lstrip("#").strip(), src_dir, data_root, url_of)
            out.append((f"<h1>{content}</h1>" if not out else f"<h{level}>{content}</h{level}>"))
            i += 1
        elif re.fullmatch(r"\s*(-{3,}|\*{3,})\s*", line):
            out.append("<hr>")
            i += 1
        elif line.lstrip().startswith("!["):
            m = re.match(r"\s*!\[([^\]]*)\]\(([^)\s]+)\)", line)
            if m:
                alt, src = m.group(1).strip(), m.group(2).strip()
                cap = f"<figcaption>{html.escape(alt)}</figcaption>" if alt else ""
                out.append(f'<figure><img src="{html.escape(src)}" alt="{html.escape(alt)}" '
                           f'loading="lazy" referrerpolicy="no-referrer">{cap}</figure>')
            else:
                out.append("<p>" + md_inline(line.strip(), src_dir, data_root, url_of) + "</p>")
            i += 1
        elif line.strip():
            buf = [line.strip()]
            i += 1
            while i < len(lines) and lines[i].strip() and not re.match(
                    r"^\s*(#{1,4}\s|[-*]\s|\d+\.\s|\||>|```|!\[)", lines[i]):
                buf.append(lines[i].strip())
                i += 1
            text = ""
            for seg in buf:
                if not text:
                    text = seg
                elif re.search(r"[\u4e00-\u9fff]$", text) or re.match(r"^[\u4e00-\u9fff]", seg):
                    text += seg
                else:
                    text += " " + seg
            out.append("<p>" + md_inline(text, src_dir, data_root, url_of) + "</p>")
        else:
            i += 1
    return "\n".join(out)


def page(title: str, body: str, crumbs: str = "", prefix: str = "") -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{html.escape(title)}</title><link rel="stylesheet" href="{prefix}assets/style.css">
</head><body><div class="container">
<header class="site"><div class="title"><a href="{prefix}index.html">微信数字资产库 · 分析站</a> · <a href="{prefix}articles/index.html">📄 公众号</a> · <a href="{prefix}private/index.html">🔒 私聊区</a></div>
<div class="sub">由本地 vault 生成 · raw 层永不上云</div></header>
<div class="crumbs">{crumbs}</div>
{body}
<footer>生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')} · build_site.py</footer>
</div></body></html>"""


def first_blockquote(md: str) -> str:
    m = re.search(r"^\s*>\s?(.+)$", md, re.M)
    return m.group(1).strip() if m else ""


def split_rows(md: str) -> tuple[list[str], list[str]]:
    """Return (public_rows, private_rows) table rows split by 私聊 link target."""
    pub, priv = [], []
    for ln in md.splitlines():
        if not ln.startswith("|") or re.fullmatch(r"\|[\s:|-]+\|", ln):
            continue
        (priv if "](../../topics/私聊/" in ln or "](../../topics/%E7%A7%81" in ln else pub).append(ln)
    return pub, priv


def topic_titles(md_path: Path, limit: int = 6) -> list[str]:
    if not md_path.exists():
        return []
    titles = []
    for ln in md_path.read_text(encoding="utf-8").splitlines():
        if ln.startswith("### "):
            titles.append(re.sub(r"^\d+\.\s*", "", ln[4:].strip()))
        if len(titles) >= limit:
            break
    return titles


def enrich_rows(rows: list[str], cat_dir: Path) -> list[str]:
    """Replace the 话题 cell with the target digest's actual section titles."""
    out = []
    for ln in rows:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) >= 3:
            m = re.search(r"\]\(([^)]+)\)", cells[0])
            if m:
                titles = topic_titles((cat_dir / m.group(1)).resolve())
                if titles:
                    cells[2] = " · ".join(titles)
        out.append("| " + " | ".join(cells) + " |")
    return out


def encrypt_bundle(payload: dict, password: str) -> dict:
    salt, iv = __import__("os").urandom(16), __import__("os").urandom(12)
    key = PBKDF2(password.encode(), salt, dkLen=32, count=KDF_ITERATIONS, hmac_hash_module=SHA256)
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
    ct, tag = cipher.encrypt_and_digest(json.dumps(payload, ensure_ascii=False).encode())
    return {"salt": base64.b64encode(salt).decode(), "iv": base64.b64encode(iv).decode(),
            "ct": base64.b64encode(ct + tag).decode(), "iter": KDF_ITERATIONS}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    ap.add_argument("--output", help="默认 {data-root}/tmp/site")
    ap.add_argument("--password", help="私聊区加密密码,默认取配置 site_password 或环境变量 WECHAT_SITE_PASSWORD")
    args = ap.parse_args()
    data_root = Path(args.data_root).expanduser()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    out = Path(args.output).expanduser() if args.output else data_root / "tmp" / "site"
    for stale in ("topics", "categories", "private"):
        shutil.rmtree(out / stale, ignore_errors=True)
    (out / "assets").mkdir(parents=True, exist_ok=True)
    (out / "assets" / "style.css").write_text(STYLE, encoding="utf-8")
    (out / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")

    password = args.password
    if not password:
        cfg_path = Path("~/.config/wechat-local-vault.json").expanduser()
        if cfg_path.exists():
            password = json.loads(cfg_path.read_text()).get("site_password")
    if not password:
        password = __import__("os").environ.get("WECHAT_SITE_PASSWORD")

    rules_path = data_root / "config" / "rules.json"
    rules = json.loads(rules_path.read_text(encoding="utf-8")) if rules_path.exists() else {}
    global_block = list(rules.get("global_block", []))

    def is_published(md_path: Path) -> bool:
        try:
            rel = md_path.resolve().relative_to(data_root)
        except ValueError:
            return True
        if len(rel.parts) < 3 or rel.parts[0] != "topics":
            return True
        obj = rules.get("objects", {}).get(f"{rel.parts[1]}/{rel.parts[2]}", {})
        if obj.get("publish", True) is False:
            return False
        text = md_path.read_text(encoding="utf-8")
        return not any(b and b in text for b in list(obj.get("block", [])) + global_block)

    def row_published(row: str, cat_dir: Path) -> bool:
        m = re.search(r"\]\(([^)]+)\)", row)
        return is_published((cat_dir / m.group(1)).resolve()) if m else True

    topics_root, cats_root = data_root / "topics", data_root / "categories"
    public_topics = [p for p in sorted((topics_root / "群聊").rglob("*.md")) if is_published(p)] \
        if (topics_root / "群聊").exists() else []
    private_topics = [p for p in sorted((topics_root / "私聊").rglob("*.md")) if is_published(p)] \
        if (topics_root / "私聊").exists() else []
    cat_files = sorted(cats_root.glob("*/index.md")) if cats_root.exists() else []
    private_names = [d.name for d in (topics_root / "私聊").iterdir()] if (topics_root / "私聊").exists() else []

    def public_url(rel: Path) -> str | None:
        rel = rel.with_suffix(".html")
        if rel.parts and rel.parts[0] == "topics" and rel.parts[1] == "私聊":
            return None
        return quote(rel.as_posix(), safe="/")

    def make_url_fn(page_dir: Path, url_of):
        rel_dir = page_dir.relative_to(data_root).as_posix() if page_dir != data_root else "."
        def f(rel: Path):
            u = url_of(rel)
            return None if u is None else posixpath.relpath(u, rel_dir if rel_dir != "." else "")
        return f

    # --- public topic pages ---
    for tf in public_topics:
        rel = tf.relative_to(data_root)
        url_fn = make_url_fn(tf.parent, public_url)
        body = render_markdown(tf.read_text(encoding="utf-8"), tf.parent, data_root, url_fn)
        crumbs = f'<a href="../../index.html">首页</a> / 群聊 / {html.escape(tf.parent.name)}'
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.with_suffix(".html").write_text(page(f"{tf.parent.name} · {tf.stem}", body, crumbs, "../../.."), encoding="utf-8")

    # --- categories ---
    cards, private_cats_payload = [], []
    for cf in cat_files:
        name = cf.parent.name
        md = cf.read_text(encoding="utf-8")
        pub_rows, priv_rows = split_rows(md)
        pub_rows = [r for r in pub_rows if row_published(r, cf.parent)]
        priv_rows = [r for r in priv_rows if row_published(r, cf.parent)]
        review = first_blockquote(md)
        review_public = review if not any(n in review for n in private_names) \
            else "综述涉及私聊内容,完整版在 🔒 私聊加密区。"
        url_fn = make_url_fn(cf.parent, public_url)
        pub_table = ("| 对象 | 时间窗 | 话题 |\n|---|---|---|\n" + "\n".join(enrich_rows(pub_rows, cf.parent))) if pub_rows else ""
        pub_md = f"# {name}\n\n> {review_public}\n\n{pub_table}"
        hint = review_public[:120] or f"{len(pub_rows)} 条索引"
        cards.append(f'<a class="card" href="categories/{quote(name)}/index.html"><div class="name">{html.escape(name)}</div>'
                     f'<div class="hint">{html.escape(hint)}</div></a>')
        (out / "categories" / name).mkdir(parents=True, exist_ok=True)
        (out / "categories" / name / "index.html").write_text(
            page(name, render_markdown(pub_md, cf.parent, data_root, url_fn),
                 f'<a href="../../index.html">首页</a> / {html.escape(name)}', "../../"), encoding="utf-8")

        if private_topics:
            full_md = (f"# {name}(含私聊)\n\n> {review}\n\n| 对象 | 时间窗 | 话题 |\n|---|---|---|\n"
                       + "\n".join(enrich_rows(pub_rows + priv_rows, cf.parent)))
            private_cats_payload.append({"name": name, "html": render_markdown(full_md, cf.parent, data_root, lambda rel: None)})

    # --- public articles (公众号 full text) ---
    articles_dir = data_root / "raw" / "公众号"
    article_items = []
    if articles_dir.exists():
        for af in sorted(articles_dir.glob("*.md")):
            md = clean_article(af.read_text(encoding="utf-8"))
            title = next((ln.lstrip("# ").strip() for ln in md.splitlines() if ln.startswith("# ")), af.stem)
            date = af.stem[:8]
            date_fmt = f"{date[:4]}-{date[4:6]}-{date[6:]}" if date.isdigit() else ""
            body = f'<div class="article">{render_markdown(md, af.parent, data_root, lambda rel: None)}</div>'
            (out / "articles").mkdir(parents=True, exist_ok=True)
            (out / "articles" / f"{af.stem}.html").write_text(
                page(title, body, f'<a href="../index.html">首页</a> / 公众号 / {html.escape(date_fmt)}', "../"),
                encoding="utf-8")
            article_items.append((date_fmt, title, af.stem))
    if article_items:
        listing = "".join(
            f'<li><a href="{quote(stem)}.html">{html.escape(title)}</a>'
            f'<span style="color:var(--muted);font-size:13px"> · {date_fmt}</span></li>'
            for date_fmt, title, stem in sorted(article_items, reverse=True))
        (out / "articles" / "index.html").write_text(
            page("公众号文章", f"<h1>公众号文章</h1><ul class=\"meta\">{listing}</ul>",
                 '<a href="../index.html">首页</a> / 公众号', "../"), encoding="utf-8")
        article_home = "".join(
            f'<li><a href="articles/{quote(stem)}.html">{html.escape(title)}</a>'
            f'<span style="color:var(--muted);font-size:13px"> · {date_fmt}</span></li>'
            for date_fmt, title, stem in sorted(article_items, reverse=True)[:10])
    else:
        article_home = ""

    # --- public homepage ---
    def home_item(tf: Path) -> str:
        md = tf.read_text(encoding="utf-8")
        chips = " · ".join(html.escape(t) for t in topic_titles(tf, 5))
        intro = next((ln[2:].strip() for ln in md.splitlines()
                      if ln.startswith("- ") and not re.match(r"^- (数据源|群性质|核心人物|本人)", ln)), "")
        parts = [f'<li><a href="{public_url(tf.relative_to(data_root))}">'
                 f'{html.escape(tf.parent.name)} · {html.escape(tf.stem)}</a>']
        if chips:
            parts.append(f'<div class="hint">话题:{chips}</div>')
        if intro:
            parts.append(f'<div class="hint">{html.escape(intro[:120])}</div>')
        return "".join(parts) + "</li>"

    items = [home_item(tf) for tf in sorted(public_topics, key=lambda p: p.name, reverse=True)]
    home = (f"<h1>总览</h1><p>公开区:群聊分析 + 公众号全文。分类 {len(cat_files)} 个 · 群聊话题 {len(public_topics)} 篇 · "
            f"公众号文章 {len(article_items)} 篇;私聊内容共 {len(private_topics)} 篇存于<a href=\"private/index.html\">🔒 加密区</a>。</p>"
            f"<h2>分类</h2><div class=\"grid\">{''.join(cards)}</div>"
            f"<h2>群聊话题</h2><ul class=\"meta\">{''.join(items)}</ul>"
            + (f"<h2>公众号文章</h2><ul class=\"meta\">{article_home}</ul>" if article_home else ""))
    (out / "index.html").write_text(page("微信数字资产库 · 分析站", home), encoding="utf-8")

    # --- private encrypted bundle ---
    if private_topics:
        if not password:
            raise SystemExit("私聊内容存在但未提供加密密码(--password / 配置 site_password / 环境变量)")
        pages_payload = []
        for tf in private_topics:
            title = f"私聊 · {tf.parent.name} · {tf.stem}"
            body = render_markdown(tf.read_text(encoding="utf-8"), tf.parent, data_root, lambda rel: None)
            pages_payload.append({"title": title, "html": body})
        bundle = encrypt_bundle({"pages": pages_payload, "cats": private_cats_payload}, password)
        (out / "private").mkdir(parents=True, exist_ok=True)
        (out / "private" / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
        page_html = PRIVATE_PAGE.replace("__STYLE__", STYLE).replace("__ITER__", str(KDF_ITERATIONS))
        (out / "private" / "index.html").write_text(page_html, encoding="utf-8")

    print(f"OK site -> {out} (public topics {len(public_topics)}, private topics {len(private_topics)}, "
          f"categories {len(cat_files)}, encrypted={'yes' if private_topics else 'no'})")


if __name__ == "__main__":
    main()
