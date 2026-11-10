# 需求挖掘系统

从公开社区抓真实需求 → LLM 结构化抽取 → 跨帖去重 → 六维打分 → 日报 / 推送 / 看板。

```
采集（Discourse / GitHub / WordPress / Reddit）
  → 关键词预筛（省 LLM 成本）
  → 正文复核
  → 竞品搜索（可选，需搜索 API key）
  → LLM 抽取 + quote 原文校验
  → LLM 语义去重 + 词面兜底
  → 六维打分 + 硬门槛
  → Markdown 日报 → 飞书 / Telegram
  → 本地看板（👍/👎 反馈调权）
```

## 目录结构

```
main.py                 入口：抓取 → 过滤 → 抽取 → 去重 → 打分 → 报告
dashboard.py            本地看板 + 反馈接口
config.yaml             数据源、provider、模型、成本上限
demand_miner/
  collect.py            Discourse / GitHub / WordPress / Reddit 采集
  prefilter.py          关键词预过滤
  websearch.py          竞品搜索（Brave / Tavily / Serper）
  extract.py            结构化抽取 + quote 校验 + 语义去重
  cluster.py            合并重复需求（LLM 分组 + 词面兜底）
  scoring.py            六维加权 + 硬门槛
  store.py              SQLite：已见帖子、需求、反馈
  notify.py             飞书 / Telegram 推送
  report.py             Markdown 日报
out/                    生成的日报
```

## 安装

```bash
uv pip install --python .venv/bin/python -r requirements.txt

cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY
```

## 运行

```bash
# 1. 先看抓取和过滤效果，不花钱
.venv/bin/python main.py --dry-run

# 2. 完整跑一轮（默认最多 40 条，约 $0.1）
.venv/bin/python main.py

# 3. 只跑某个来源
.venv/bin/python main.py --source shopify --limit 10

# 4. 跳过联网竞品核实
.venv/bin/python main.py --no-web

# 5. 打开看板
.venv/bin/python dashboard.py --port 8787
# http://127.0.0.1:8787
```

## 日报保存规则

| 文件 | 行为 |
| --- | --- |
| `out/YYYY-MM-DD.md` | 当天最新一次正式运行的日报（同一天再跑会覆盖） |
| `out/runs/YYYY-MM-DD_HHMMSS.md` | **每次正式运行都存档一份，不覆盖** |
| `out/latest.md` | 永远指向最近一次正式运行 |
| `out/dry-run-YYYY-MM-DD.md` | dry-run 结果，不会影响正式日报 |

数据库 `data/demand.db` 也会保留所有分析过的需求（按帖子 key 去重更新）。

## LLM Provider

默认 DeepSeek（`config.yaml` 里 `provider: deepseek`）。想换回 Claude：

```yaml
provider: anthropic
model: claude-opus-5-5
```

并在 `.env` 里填 `ANTHROPIC_API_KEY`。

## 数据源

| kind | 说明 | 配置 |
| --- | --- | --- |
| `discourse` | 任意 Discourse 论坛，走公开 JSON API | `base_url` + `pages` |
| `github` | GitHub Issue 搜索 | `queries`（支持 label / repo 语法） |
| `wordpress` | WordPress 插件支持论坛 RSS | `feeds` |
| `reddit` | Reddit 新帖 | `subs`，网络不通时需要 `proxy` |

GitHub 不填 `GITHUB_TOKEN` 时搜索接口限 10 次/分钟，当前配置 2 个查询够用。
Reddit 在部分网络环境不可达，配置 `proxy: http://127.0.0.1:7890` 后可启用。

## 竞品核实

填任意一个搜索 API key 即可启用，没填就自动跳过：

```bash
BRAVE_API_KEY=...      # 免费额度 2000 次/月
# TAVILY_API_KEY=...
# SERPER_API_KEY=...
```

搜索结果会注入抽取 prompt，模型据此调整 `competition_gap` 和 `existing_solutions`。

## 推送

```bash
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
# 或
TELEGRAM_BOT_TOKEN=123456:xxx
TELEGRAM_CHAT_ID=123456789
```

每轮结束后推送当天 Top 8 机会 + 报告路径。

## 打分与去重

六个维度 0-10，加权后由代码贴标签（不让模型直接给 high/medium）：

| 维度 | 权重 | 含义 |
| --- | --- | --- |
| severity | 20% | 痛点有多严重 |
| frequency | 20% | 频率与受影响人数 |
| pay | 20% | 付费意愿证据 |
| competition_gap | 20% | 竞争空白 |
| buildable | 10% | 一个人 4-8 周能否做出 MVP |
| channel | 10% | 有没有现成分发渠道 |

**硬门槛**：`pay >= 4` 且 `competition_gap >= 4` 且 `buildable >= 6` 且 `channel >= 3`。

**去重**：先让 LLM 判断哪些条目是同一个产品机会（跨平台合并），再用标题/引语的词面相似度兜底。
合并后保留分数最高的一条，来源数和频率信号会自动上调。

## 看板与反馈调权

`dashboard.py` 打开后可以给每条需求点 👍 / 👎，写入 `feedback` 表。
某个来源累计 5 票以上时：好评率 ≥70% → 下一轮该来源分数 ×1.15；≤30% → ×0.85。

## 定时运行

```bash
crontab -e
0 9 * * * cd /Users/apple/Desktop/code/需求挖掘系统 && .venv/bin/python main.py >> data/run.log 2>&1
```

## 已知限制

- Reddit 在部分网络不可达，需要代理
- 竞品核实依赖第三方搜索 API
- LLM 去重偶尔会漏掉表述差异很大的同义需求
- 没有对新帖做增量重评估（后续回复变多不会重新分析）
