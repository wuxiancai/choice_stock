# A股轻量选股系统

## Agent skills

### Issue tracker

任务以仓库内 Markdown 记录。见 `docs/agents/issue-tracker.md`。

### Triage labels

使用默认五态标签词汇。见 `docs/agents/triage-labels.md`。

### Domain docs

单一上下文：根目录 `CONTEXT.md` 与 `docs/adr/`。见 `docs/agents/domain.md`。

## 开发约束

- Python 3.12+，依赖必须安装于 `.venv`。
- 不提交密钥或数据库；Token 仅存 `.env`。
- 数据优先级：Tushare（盘后）→ AKShare → 东方财富公开行情；数据缺失必须在界面显式呈现。
- 每次变更同步 `docs/HANDOFF.md` 并使用 Git 提交。
