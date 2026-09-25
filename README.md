# CN2 GIA Multi-Provider Deal Monitor v2.1.0

监控：

- HostDare
- DMIT
- BandwagonHost / 搬瓦工

硬门槛：

```text
CN2 GIA / CTGNet
RAM >= 1GB
独立 IPv4
年付 <= $50
```

## v2.1.0：DMIT 补强

v2.0.0 中 GitHub Actions 访问 DMIT pricing 页面可能遇到：

```text
403
Cloudflare / anti-bot challenge
```

v2.1.0 不再只依赖一个 DMIT URL，而是同时检查多个 **DMIT 官方页面**：

```text
https://www.dmit.io/pages/pricing
https://www.dmit.io/pages/pricing?language=english
https://www.dmit.io/pages/location/los-angeles
https://www.dmit.io/pages/location/los-angeles?language=english
https://www.dmit.io/index.php?rp=/announcements
```

任一官方源能读，就可以继续工作。

如果全部被 Cloudflare 挡住：

```text
不判无货
不清空 DMIT 状态
不制造“恢复访问 = 假补货”的误报
```

## 特别防止 DMIT Tier 1 误报

DMIT 官方目前同时存在：

```text
Premium Network -> CN2 GIA
Tier 1 Network -> 非中国优化
Eyeball Network -> 不是 CN2 GIA Premium
```

例如：

```text
LAX.AS3.T1 WEE
$36.90/year
1GB
```

价格虽然符合 <= $50，但它是 **Tier 1**，不是 CN2 GIA。

v2.1.0 会先按网络区块拆分，只允许 **Premium Network** 区块进入 CN2 GIA 候选。

## GitHub Actions 更新

使用：

```text
ubuntu-24.04
actions/checkout@v5
actions/setup-python@v6
```

避免旧 Node.js 20 Action 的弃用提示，并固定 runner，不跟随 `ubuntu-latest` 自动迁移。

## 升级

覆盖：

```text
monitor.py
requirements.txt
README.md
.github/workflows/hostdare-monitor.yml
```

### state.json

如果你的 v2.0.0 已经跑通，可以保留旧 `state.json`；v2.1.0 会自动补充新字段。

如果想重新建立三商家基线，可以覆盖本包的 `state.json`。

第一次：

```text
Success
baseline_mode: true
baseline_created: true
```

第二次：

```text
Success
baseline_mode: false
new_hits: []
```

真正发现符合条件的新货：

```text
exit 42
Workflow Failure
GitHub Actions 原生邮件
```
