# CN2 GIA Multi-Provider Deal Monitor v2.2.0 Stable

统一监控：

- HostDare
- DMIT
- BandwagonHost（搬瓦工）

硬条件固定：

```text
CN2 GIA / CTGNet
RAM >= 1GB
独立 IPv4
年付 <= $50
```

## v2.2.0 重点：DMIT 做到底

GitHub Actions 的共享出口 IP 经常被 DMIT Cloudflare 403。

这一版采用三层结构：

### 第一层：DMIT 官方直读

同时检查多个官方页面：

```text
pricing
pricing?language=english
Los Angeles location
Los Angeles location?language=english
announcements
cloud-instance
christmas-2026
black-friday-2026
lax-eyeball
```

只要任意官方页面能读，就以官方直读为准。

### 第二层：官方活动页

对当前年份的典型官方促销页也主动检查。

如果未来 DMIT 把低价 Pro Special 放进活动页，只要页面同时确认：

```text
Pro / Premium
CN2 GIA / CTGNet
RAM >= 1GB
1 IPv4
annual <= $50
Order Now / Available
```

就成为 VERIFIED 命中。

### 第三层：搜索索引兜底

只有当所有 DMIT 官方页面都被 403/不可读时才启用。

使用 Bing RSS 做站内发现，但有严格约束：

- 搜索结果链接必须回到 `dmit.io`
- Tier 1 / `.T1.` 一律排除
- 必须在摘要里同时找到：
  - Pro / Premium / CN2 GIA
  - RAM >= 1GB
  - 1 IPv4
  - 年付 <= $50
- 明确写着 promotion ended / closed 的旧活动不提醒

这类命中标记：

```text
confidence: candidate
```

它会发邮件，但购买前必须打开官方 DMIT URL 再确认库存。

直接官方页面确认的命中标记：

```text
confidence: verified
```

## 为什么不把搜索结果直接当 VERIFIED

因为搜索索引可能有缓存延迟。

所以：

```text
VERIFIED  = 官方页面直接确认
CANDIDATE = GitHub 访问官方被 Cloudflare 挡住时的低漏报兜底
```

这样兼顾“不漏活动”和“不把搜索缓存冒充实时库存”。

## HostDare

继续使用官方 RSS + 产品页。

## BandwagonHost

继续使用官方购物车，只接受 CN2 GIA + 1GB+ + IPv4 + <= $50/year 的可购买产品。

## 防误报

- Cloudflare 403 永远不代表无货
- Provider 不可读时保留 last-known-good 状态
- Tier 1 不会当 CN2 GIA
- 第一次运行只建立基线
- 历史搜索结果第一次只登记、不报警

## GitHub Actions

固定：

```text
ubuntu-24.04
actions/checkout@v5
actions/setup-python@v6
Python 3.12
```

每小时：

```text
07 分
37 分
```

检查两次。

## 升级

覆盖：

```text
monitor.py
README.md
requirements.txt
.github/workflows/hostdare-monitor.yml
```

已有 v2.1.0 的 `state.json` 可以保留。

如果想从 v2.2.0 重新建立基线，也可以覆盖包内 `state.json`。

## 正常状态

```text
Success
new_hits: []
```

真正出现新货/候选：

```text
Failure
GitHub 原生 Email
```

Summary 里会明确告诉你：

```text
confidence = verified
```

还是：

```text
confidence = candidate
```
