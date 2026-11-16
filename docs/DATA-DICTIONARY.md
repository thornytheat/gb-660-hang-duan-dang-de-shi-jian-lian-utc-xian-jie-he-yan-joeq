# 数据字典

- run_id：规则版本与输入 SHA-256 前 12 位组成的稳定批次标识。
- line_no：从 1 开始的物理来源行号，不因隔离或排序改变。
- business_key：本项目声明的业务唯一键，始终按字符串存储。
- reason_code：隔离原因的稳定英文编码。
- accepted：进入业务计算的记录数。
- quarantined：保留证据但不参与业务计算的记录数。
- 数值：金额、税额、用量和权重使用 Decimal；对外序列化为字符串。
- 时间：存储为带偏移的 ISO 8601，分桶使用半开区间。
- 空值：缺失键、空串和业务哨兵分别记录。
- 幂等：完整输入字节与规则版本相同则复用 run_id，不新增结果。

## 航段账本（M4 UTC 航链页）

- import_id：导入批次标识，由规则版本与输入 SHA-256 前 12 位组成；相同字节重复导入复用同一批次，不重复写原始行。
- line_no：从 1 开始的物理行号，包含表头行；隔离、更正与排序都不改变它。
- leg_key：账本内稳定的航段标识。正常行为 `航班号|航班日期|起飞机场|到达机场`；缺键行为 `LINE:<行号>`；与前面行业务键重复的行追加 `#L<行号>`，因此冲突行保有独立身份。
- raw_line：导入时该物理行的原始文本，写入后不再修改。`leg_raw.raw_json` 同时保存解析出的字段。
- revision：同一 leg_key 的解释版本，从 1 开始。导入是 r1，每次更正追加 r+1。历史版本永不覆盖或删除。
- source：`import` 或 `correction`。相邻行的隔离说明、原始行与历次解释分区保存，互不覆盖。
- reason：更正理由，必填；空理由返回 `REASON_REQUIRED`。导入行的 reason 记录采信或隔离说明。
- release_id：更正发布标识，形如 `REL-<哈希>`；同一批次内由 idempotency_key 唯一确定。
- archived_chain：导入当刻的航链快照（含被隔离航段）。它不随后续更正重算，用于复查原时刻、原重叠小时选择与原误机判定。

## 航段隔离原因码

| 码 | 含义 |
|---|---|
| `EMPTY_VALUE` | 航班日期或起降当地时刻缺失 |
| `BAD_FLIGHT_NO` / `BAD_AIRPORT` / `BAD_FORMAT` | 字段格式不合法 |
| `UNKNOWN_AIRPORT` | 机场不在时区登记表 |
| `TIMEZONE_VERSION_OVERLAP` | 机场时区版本重叠，拒绝猜测 |
| `LOCAL_TIME_GAP` | 当地时刻落在夏令时跳时空档 |
| `OVERLAP_FOLD_REQUIRED` | 重叠小时有两个候选瞬时，未选择 fold |
| `BAD_FOLD` | fold 与当地时刻不匹配 |
| `INVALID_FLIGHT_DURATION` | 到达不晚于起飞，或飞行时长超过 18 小时 |
| `DUPLICATE_KEY` / `VALUE_CONFLICT` | 业务键完全重复 / 同键不同值 |
| `TRUNCATED_INPUT` | 尾部行不完整 |
| `STALE_REVISION` / `CONCURRENT_RELEASE` | 更正基于旧版本，已被拒绝 |

## 更正与发布规则

- 并发：更正必须携带 `expected_revision`；当前版本不同即返回 409 `STALE_REVISION`，不写入。更正后的业务键若与另一现行航段冲突，返回 409 `VALUE_CONFLICT`。
- 发布：`POST /api/imports/{import_id}/releases` 按 idempotency_key 只创建一次；每条更正独立提交，中断后已完成的更正保留，`POST /api/releases/{release_id}/resume` 从剩余项继续。
- 幂等重试：同一 idempotency_key 重复提交返回同一发布记录，不新增 revision。
- 失败可见：发布记录保存 failed/partial/published 状态和逐项错误；原归档航链始终可按 import_id 读取。
- 相邻衔接：航段更正后，凡端点版本或 UTC 时刻发生变化的衔接对都会重算等待分钟与判定；未受影响的衔接保留原结果。
- 时区按各自日期查询：起飞端用 `departure_local` 的日期查起飞机场时区，到达端用 `arrival_local` 的日期查到达机场时区。
- 隔离航段：跳时空档、未选重叠小时、未知机场等航段不进入航链，在隔离依据区单列，不猜测补位。

