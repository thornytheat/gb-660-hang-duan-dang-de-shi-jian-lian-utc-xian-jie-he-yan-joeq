# 深度登记 航班时刻运行台

- 规模：中型
- 核心规则：用机场 IANA 时区解析 DST 重叠与跳时，所有航链在 UTC 上校验先后和衔接。
- 使用者与工作流：运行数据员导入航段，确认机场时区，处理当地时刻歧义，构建 UTC 航链并核对转机。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 航段采信 | 导入航段并隔离重复冲突 | POST /api/legs/import | `app/modules/m1_legs/parser.py` `app/modules/m1_legs/keys.py` | `tests/test_m1_legs.py` |
| M2 机场时区 | 查询日期对应的时区版本 | POST /api/airports | `app/modules/m2_airports/registry.py` `app/modules/m2_airports/contracts.py` | `tests/test_m2_airports.py` |
| M3 当地时刻 | 解析偏移、重叠与跳时 | POST /api/local-time/resolve | `app/modules/m3_localtime/resolve.py` `app/modules/m3_localtime/evidence.py` | `tests/test_m3_localtime.py` |
| M4 UTC 航链 | 换算航段并校验先后顺序 | POST /api/itineraries/build | `app/modules/m4_itinerary/builder.py` `app/modules/m4_itinerary/validate.py` | `tests/test_m4_itinerary.py` |
| M4+ 航段账本 | 导入实际航段、逐段更正、发布与归档对照 | GET /itineraries · `/api/imports` · `/api/releases` | `app/modules/m4_itinerary/ledger.py` `app/modules/m4_itinerary/chain.py` `app/modules/m4_itinerary/interpret.py` `app/modules/m1_legs/ingest.py` | `tests/test_m7_revisions.py` |
| M5 衔接判定 | 核对航站楼换乘与等待 | POST /api/connections/check | `app/modules/m5_connections/rules.py` `app/modules/m5_connections/windows.py` | `tests/test_m5_connections.py` |
| M6 批次导出 | 核对航段守恒并导出时刻 | POST /api/reconcile | `app/modules/m6_batches/compare.py` `app/modules/m6_batches/exporter.py` | `tests/test_m6_batches.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-03 | 尾行记录不完整 | 半条航段进入换算 | 完整边界检查后隔离 | `app/modules/m1_legs/parser.py` | `tests/test_m1_legs.py::test_truncated_tail_detected` |
| TRAP-05 | 必填时刻缺失 | 空时间在深层报错 | 采信阶段给原因码和行号 | `app/modules/m1_legs/parser.py` | `tests/test_m1_legs.py::test_missing_time_quarantined` |
| TRAP-07 | 空当地时刻 | 被转换成午夜 | 空值不参与航链 | `app/modules/m1_legs/parser.py` | `tests/test_m1_legs.py::test_missing_time_quarantined` |
| TRAP-09 | 超大外部行程号 | JSON 数字往返丢精度 | 始终按字符串保形 | `app/modules/m1_legs/keys.py` | `tests/test_m1_legs.py::test_external_id_keeps_large_digits` |
| TRAP-11 | 偏移与 Z 混用 | 同一时刻先后次序错误 | 统一到 UTC 后输出 Z | `app/modules/m3_localtime/resolve.py` | `tests/test_m3_localtime.py::test_utc_serializes_with_z` |
| TRAP-12 | DST 重叠与跳时 | 重复小时覆盖或虚构时刻 | 往返验证并显式 fold | `app/modules/m3_localtime/resolve.py` | `tests/test_m3_localtime.py::test_dst_overlap_has_two_candidates` |
| TRAP-13 | 航段乱序 | 输入顺序被当成航程顺序 | 按 UTC 起飞时刻稳定排序 | `app/modules/m4_itinerary/builder.py` | `tests/test_m4_itinerary.py::test_order_by_utc_is_stable` |
| TRAP-14 | 同航段时刻冲突 | 后写覆盖原计划 | 冲突版本进入隔离区 | `app/modules/m1_legs/parser.py` | `tests/test_m1_legs.py::test_conflicting_duplicate_detected` |
| TRAP-23 | 衔接窗口端点 | 结束时刻被重复计入 | 使用半开区间 | `app/modules/m5_connections/windows.py` | `tests/test_m5_connections.py::test_half_open_window_excludes_end` |
| TRAP-25 | 时区版本重叠 | 随机选中一版偏移 | 多个生效版本立即拒绝 | `app/modules/m2_airports/registry.py` | `tests/test_m2_airports.py::test_overlapping_timezone_versions_rejected` |
| TRAP-26 | 更正并回写原航链 | 覆盖历史时刻，误机依据消失 | 原始行与历次解释只追加；归档快照冻结导入时时刻与 fold | `app/modules/m4_itinerary/ledger.py` | `tests/test_m7_revisions.py::test_archived_chain_keeps_original_times_and_verdict` |
| TRAP-27 | 更正只改本段 | 相邻转机沿用旧等待 | 端点版本或 UTC 变化的衔接对重算 | `app/modules/m4_itinerary/chain.py` | `tests/test_m7_revisions.py::test_correction_recomputes_only_adjacent_connections` |
| TRAP-28 | 并发编辑同一航段 | 后写静默覆盖先写 | expected_revision 不符返回 409 | `app/modules/m4_itinerary/ledger.py` | `tests/test_m7_revisions.py::test_stale_revision_is_blocked` |
| TRAP-29 | 发布流程中断后重试 | 已写入的更正被重复记一次 | 幂等键固定发布，逐项断点续跑 | `app/modules/m4_itinerary/ledger.py` | `tests/test_m7_revisions.py::test_release_resumes_after_interruption` `::test_release_replay_does_not_duplicate_revisions` |
| TRAP-30 | 按航班日期取时区 | 跨日到达用错时区版本 | 起降各按自身日期查机场时区 | `app/modules/m4_itinerary/interpret.py` | `tests/test_m7_revisions.py::test_airport_timezone_looked_up_per_timestamp_date` |

## 对账规则

- 输入航段 = 有效航段 + 冲突航段 + 隔离航段。
- 行程航段 = 可衔接航段 + 不可衔接航段。
- 到达 UTC 必须晚于起飞 UTC，飞行时长不得超过 18 小时。
- 账本行数 = 现行航段数（每个 leg_key 只取最高 revision）+ 隔离航段数；原始行数在导入时固定。

## 已知不覆盖

- 不计算天气和空管延误，输入只描述计划时刻。

