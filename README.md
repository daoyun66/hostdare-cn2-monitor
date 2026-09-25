# CN2 GIA Multi-Provider Deal Monitor v2.0.0

统一监控：

- HostDare
- DMIT
- BandwagonHost（搬瓦工）

硬门槛：

```text
CN2 GIA / CTGNet
RAM >= 1GB
独立 IPv4
年付 <= $50 USD
```

符合条件的新活动或补货出现时：

```text
monitor.py -> exit code 42
GitHub Actions -> 故意 Failure
GitHub -> 原生 Actions 失败邮件通知
```

## 重要：首次运行

第一次健康运行只建立基线，不报警：

```text
baseline_mode: true
baseline_created: true
new_hits: []
```

第二次开始才真正监控新货。

## 数据源策略

### HostDare

主源：

```text
https://bill.hostdare.com/announcements/rss
```

只分析最近 45 天的官方公告，避免把旧的 2025/2026 历史促销重新当成新活动。

产品页只用于健康/库存辅助。GitHub Actions 被 Cloudflare 403 时不会判成无货。

### DMIT

只读取官方：

```text
https://www.dmit.io/pages/pricing
```

特别做了防误报：

**LAX.AS3.T1 / Tier 1 这种普通线路不会因为价格低于 $50 而报警。**

只有同一个本地价格区块明确确认：

```text
CN2 GIA / CTGNet
+ dedicated IPv4
+ >=1GB RAM
+ <=$50/year
```

才进入候选。

### BandwagonHost

只读取官方购物车：

```text
https://bandwagonhost.com/cart.php
```

只有官方页面同时确认：

```text
CN2 GIA
dedicated IPv4
RAM >= 1GB
annual <= $50
orderable
```

才报警。

这可以抓未来突然出现的 Limited Edition / SPECIAL 低价货，同时避免拿第三方“库存站”作为最终证据。

## 升级你现有 hostdare-cn2-monitor 仓库

覆盖：

```text
monitor.py
requirements.txt
README.md
.github/workflows/hostdare-monitor.yml
state.json
```

建议这次 **state.json 也覆盖**，因为 v2.0.0 是新的三商家统一状态格式。

覆盖后：

1. GitHub -> Actions
2. 打开 `CN2 GIA Multi-Provider Deal Monitor`
3. `Run workflow`
4. 第一次应为绿色 Success（建立基线）
5. 再运行一次，正常仍为绿色 Success

真正出现新货时才会红色 Failure + 邮件。

## GitHub 邮件

确保：

```text
GitHub -> Settings -> Notifications -> System -> Actions
Email
Failed workflows only
```

已开启。

## 监控频率

每小时两次：

```text
07 分
37 分
```

即约每 30 分钟检查一次。
