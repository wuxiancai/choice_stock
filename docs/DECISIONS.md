# 当前决策

- 调度使用应用内 APScheduler；生产由 systemd 保持进程存活。
- 21:00 任务按上海时区执行，周末/节假日由 Tushare 交易日历跳过。
- 指标需要至少 30 个交易日历史；历史不足的证券不产生技术评分。
- Tushare 账户按 3000 积分权限运行：仅可调用 `trade_cal`、`daily`、`daily_basic`、`moneyflow`、`stock_basic`、`index_member_all` 等不高于该门槛的接口；禁止调用 `moneyflow_ind_ths` 和 `moneyflow_ind_dc`。
- 盘后当日行业资金流优先使用 Tushare `moneyflow`（2000 分权限）按申万一级行业 `index_member_all` 聚合；失败时仅允许使用同样按申万一级聚合的腾讯公开行情。至少 30 行且同时具备涨跌幅、主力净流入才视为成功，并记录实际来源与前置降级原因。
- 历史行业快照仅使用 Tushare `moneyflow` 按申万一级行业聚合；禁止混入东方财富、同花顺或腾讯的非一级行业板块数据。AKShare 同花顺与腾讯只支持当日快照，状态中必须明确标注“未用于历史”。
