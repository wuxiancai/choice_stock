# Handoff

## 当前状态

第一版已实现：可本地启动、可由 systemd 托管的 A 股盘后选股与行业资金流看板。尚未用目标环境的真实 Token 完成首次同步。`deploy.sh` 会自动检测 Python 3.12+；macOS 缺少时通过 Homebrew 安装，Ubuntu 缺少时通过 apt（必要时 deadsnakes PPA）安装，再创建 `.venv` 与安装全部项目依赖。

## 下一步

1. 在目标 macOS/Ubuntu 上执行 `bash deploy.sh`。
2. 将 Token 写入仅本地存在的 `.env`，执行 `./start.sh`。
3. 打开 `http://127.0.0.1:8012`，点击“立即同步”或等待 21:00 任务，并检查数据健康状态。

个股主力资金走 Tushare `moneyflow`；需以首次真实同步确认当前积分权限。龙虎榜仍是下一切片，不能声称已接入。
