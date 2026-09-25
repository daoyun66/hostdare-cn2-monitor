# HostDare CN2 GIA GitHub Actions 监控

无需自己的服务器，也不需要 SMTP。

## 触发条件
- HostDare 官方页面出现 **CN2/CN2 GIA >=30% 优惠**
- **CSSD1 / CAMD1** 入门 CN2 GIA 套餐补货

默认每 30 分钟检查一次。

## 部署
新建一个 GitHub Public 仓库，例如 `hostdare-cn2-monitor`，把压缩包内文件原样上传。

必须保持：
```text
.github/workflows/hostdare-monitor.yml
monitor.py
requirements.txt
state.json
.heartbeat
.gitignore
README.md
```

## 邮件通知
GitHub：
`Settings → Notifications → System → Actions → Email → Only notify for failed workflows`

发现新活动/补货时，本次 Actions 会故意失败，从而由 GitHub 给你的通知邮箱发邮件。无需把邮箱地址或密码写入仓库。

## 官方监控页
- https://bill.hostdare.com/announcements
- https://bill.hostdare.com/store/premium-china-optimized-nvme-kvm
- https://bill.hostdare.com/store/premium-china-optimized-amd-kvm-vps-usa
- https://bill.hostdare.com/store/premium-china-optimized-kvm-vps

同一活动持续存在不会重复提醒；活动消失后，将来再次出现会重新提醒。
