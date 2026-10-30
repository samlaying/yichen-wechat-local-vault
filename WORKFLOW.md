# 微信数据分析工作流(raw → topics → categories)

本仓库(skill)只放代码和文档;所有数据产物落在工作区 `~/Documents/wechat-local-vault/`,
明文库留在私有 vault(`~/Library/Application Support/wechat-local-vault/`)。任何聊天明文
不进 git 仓库。

## 目录结构

```
~/Documents/wechat-local-vault/
├── raw/                          # ① 原始层:只写不改,append-only
│   ├── 群聊/{群名}/
│   │   ├── 2026-10-19~23-全量.md  #   文件名 = 时间窗
│   │   └── 2026-10-23~25-增量.md  #   增量导出,按日期分片
│   ├── 私聊/{备注名}/
│   │   └── 2026-10-25.md
│   ├── 收藏/
│   │   └── 2026-10-25-全量.md     #   收藏体量小,每次全量快照
│   ├── 公众号/                     #   聊天中出现的公众号文章(Markdown + index.json 去重)
│   │   └── 20261025-标题.md
│   ├── 朋友圈/{联系人}/
│   │   └── 2026-10-25.md
│   └── _digest/{群ID}-{群名}/     #   大群专用 digest 素材包(机器原料,含画像)
│
├── topics/                       # ② 分析层:对象 × 时间窗,写完固定不动
│   ├── 群聊/{群名}/
│   │   ├── 2026-10-22~25-话题梳理.md
│   │   └── 2026-10-25-单话题深挖.md
│   └── 私聊/{备注名}/
│       └── 2026-10-25-关系复盘.md
│
└── categories/                   # ③ 分类层:主题 × 全对象,唯一持续演化的活文档
    ├── AI与编程/index.md
    ├── 求职准备/index.md
    └── ...

tmp/                              # 临时区:处理过程中的中间产物与半成品
└── (可随时清空;正式产物确认后才移入三层,避免污染)
```

三层的职责:`raw` 记录"发生过什么"(不可变);`topics` 记录"当时的分析"(按窗固定);
`categories` 记录"当前的判断"(持续重写)。

## 固定流程

```
1. 更新      decrypt_all_dbs.py --mode incremental
2. 落 raw    按对象导出:群/私聊增量窗,收藏全量快照,朋友圈按联系人
3. 话题处理   读 raw 新窗 → 写 topics/{对象}/{日期区间}-话题梳理.md
4. 分类挂载   新话题挂进 categories 索引,重写受影响分类的综述
5. 发布站点   build_site.py 生成 → push 到站点仓库(私有,GitHub Pages 自动部署)
```

站点发布:

```bash
python3 scripts/build_site.py                        # 生成到 {data-root}/tmp/site
cd ~/Documents/wechat-local-vault/tmp/site && git add -A && git commit -m "site: update" && git push
```

线上地址:<你的站点域名>(站点仓库为私有,不随代码公开)。
**raw 层永不进站点和仓库**;站点只包含 topics/categories 衍生内容。

要点:

- 增量窗的起点 = 该对象上一个 raw 文件的窗终点,避免重复或遗漏。
- `tmp/` 放中间产物(导出缓冲、待归档素材、半成品分析);三层里只出现确认过的正式产物,
  tmp 可整目录清空不心疼。
- 大群(消息量大到不适合直接进对话)先用 `digest-source` 在 `raw/_digest/` 生成
  素材包再分析;普通导出直接用 `vault_cli export`。
- 朋友圈本地库只含在微信里浏览过的部分;要完整数据需先在微信中翻阅再增量刷新。

## categories 更新原则

1. 只追加链接,永不复制内容:分类文件只存索引(指向 topics/raw 的链接)、一句话摘要
   和综述段,原文永远只在 raw/topics。
2. 一条话题可挂多个分类(多对多)。
3. 综述可重写,但必须带更新日期;它是"当前判断",不是历史档案。
4. 新分类有门槛:同一主题第 2~3 次出现才建目录,首次先挂已有分类或`杂项/`。
5. 分类目录可增删改名:索引层重组不伤底层文件。

触发时机:每轮话题处理后自动挂载(主路径);用户点名专题时反向扫描 topics 建分类;
周/月可选做一次全量整理。

## 公众号文章抓取

聊天里出现的公众号链接会自动转成 Markdown 存到 `raw/公众号/`,摘要时把正文节选喂给模型
(链接不再是"内容不可见")。实现走第三方远程 MCP(`https://changfengbox.top/api/mcp`,
免费无鉴权,个人服务器无 SLA;失败只告警不影响流水线)。

```bash
python3 scripts/fetch_articles.py <文章或合集链接> [更多链接...]
python3 scripts/fetch_articles.py --from-raw raw/群聊/摸鱼/2026-10-25-0556-增量.md --max 10
```

- 配置:`~/.config/wechat-local-vault.json` 的 `article_fetch`(默认 true)、`article_max_per_window`(默认 10)
- 去重:`raw/公众号/index.json` 按源 URL 记录,重复链接不再抓
- 返回的下载链接是临时地址,脚本立即下载落盘;服务不可用时仅跳过本轮

## 发布规则(本地维护)

规则文件:`~/Documents/wechat-local-vault/config/rules.json`,改完即生效,每轮流水线自动读取。

- `objects."群聊/xx".publish`:false = 只存本地、不上传站点
- `objects."群聊/xx".focus`:注入模型提示词,决定摘要写什么(如"只保留具体的产品问题")
- `objects."群聊/xx".block`:摘要正文命中关键词则整篇不上传(如教务通知)
- `global_block`:对所有对象生效的禁用词(默认空,避免误杀正常话题)
- 窗口内没有符合规则的内容时,模型输出 `SKIP`,不生成摘要文件

## 跨平台支持

| 能力 | macOS | Windows |
|---|---|---|
| 密钥提取 / 全量与增量解密 | ✅ | ❌(仅 macOS) |
| snapshot 离线快照分析 | ✅ | ✅ |
| vault_cli 查询/统计/导出 | ✅ | ✅(需已解密的 vault 或 snapshot) |
| fetch_articles 公众号抓取 | ✅ | ✅ |
| build_site 站点构建(公开/加密双区) | ✅ | ✅ |
| auto_update 定时流水线 | ✅ 全流程 | ⚠️ 自动跳过解密步骤,只重建站点/抓文章 |

脚本已做平台处理:非 macOS 上 `auto_update.py` 打印警告并跳过增量解密(Windows 请用
`vault_cli snapshot` 离线分析);三处入口在 Windows 控制台自动切 UTF-8 输出;站点 URL 统一用
`as_posix()` 生成,避免反斜杠。

## 隐私边界

- 明文库(含全部聊天隐私)只在私有 vault,不同步、不拷贝、不进仓库。
- `~/Documents` 若开启 iCloud「桌面与文稿」同步会整体上云,工作区含聊天原文,
  使用前确认该开关状态,必要时整体迁移到非同步目录。
- 回复和报告中少量引用必要原文即可,完整原文留在文件里。
