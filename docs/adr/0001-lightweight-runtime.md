# ADR 0001：轻量单进程运行时

采用 FastAPI + Jinja2 + SQLite。理由：部署端无需 Docker、Node 或外部数据库，适合 macOS/Ubuntu 的单机盘后分析。SQLite 保留原始日线、板块快照和信号，后续可迁移 PostgreSQL 而不改变 API 边界。
