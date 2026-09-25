# HostDare CN2 GIA GitHub Actions Monitor v1.1.0

这是针对 HostDare Cloudflare 403 的修正版。

## 监控逻辑

### 主通道：HostDare 官方 RSS

```text
https://bill.hostdare.com/announcements/rss
```

监控 CN2 / CN2 GIA 官方活动公告。

发现：

```text
>= 30%
```

并且属于 CN2 / China Optimized 优惠时，触发 GitHub Actions 原生失败通知邮件。

会尽量提取：

- 折扣百分比
- recurring
- coupon / promo code
- 官方活动链接

### 辅通道：商品页库存

监控：

- Premium China Optimized NVMe KVM
- Premium China Optimized AMD KVM
- Premium China Optimized HDD KVM

重点关注：

- CSSD1
- CAMD1

如果页面可以正常读取，检测补货。

如果 HostDare 返回：

```text
403
Just a moment...
Cloudflare
```

程序将标记：

```text
blocked=true
```

**不会把 403 当成“无货”。**

因此不会因为 Cloudflare 阻断而错误重置库存状态。

---

## 升级方法

用本包里的两个文件覆盖 GitHub 仓库中原文件：

```text
monitor.py
.github/workflows/hostdare-monitor.yml
```

另外建议同步覆盖：

```text
README.md
requirements.txt
```

`state.json` 可以保留原来的，也可以使用新版初始文件。

提交后：

```text
Actions
→ HostDare CN2 GIA Deal Monitor
→ Run workflow
```

正常情况下，在 Summary 的 JSON 里应该看到：

```json
"rss": {
  "ok": true
}
```

商品页即使还是：

```json
"status": 403,
"blocked": true
```

也没有关系，因为优惠活动已经由 RSS 主通道负责。

---

## 通知

保持 GitHub 个人设置：

```text
Settings
→ Notifications
→ System
→ Actions
→ GitHub, Email
→ Failed workflows only
```

无优惠时：绿色 Success，不发邮件。

新优惠/补货时：工作流故意 Failure，由 GitHub 发邮件。

---

## 默认频率

每 30 分钟：

```yaml
cron: "11,41 * * * *"
```

---

## v1.1.0 变化

- 新增官方 RSS 作为主监控源
- 修复 Cloudflare 403 被误认为正常页面的问题
- 403 不再清空库存状态
- RSS 与商品库存状态分开维护
- 增加 health 字段
- 继续支持 GitHub 原生邮箱通知
