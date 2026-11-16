REASONS = {
    "EMPTY_VALUE": "必填值为空",
    "BAD_FORMAT": "字段格式不正确",
    "DUPLICATE_KEY": "业务键重复",
    "VALUE_CONFLICT": "相同业务键值冲突",
    "TRUNCATED_INPUT": "输入尾部不完整",
    "UNKNOWN_REFERENCE": "引用对象不存在",
    "RECONCILE_DIFF": "对账不相等",
}

# Isolation evidence for the leg ledger.  Codes are stable English tokens; the
# accompanying messages are for the reviewer and never used as keys.
ISOLATION_REASONS = {
    "EMPTY_VALUE": "必填值为空，无法构成航段",
    "BAD_FORMAT": "字段格式不正确",
    "BAD_FLIGHT_NO": "航班号格式不正确",
    "BAD_AIRPORT": "机场代码格式不正确",
    "UNKNOWN_AIRPORT": "机场不在时区登记表中",
    "TIMEZONE_VERSION_OVERLAP": "机场时区版本重叠，拒绝猜测",
    "LOCAL_TIME_GAP": "当地时刻落在夏令时跳时空档",
    "OVERLAP_FOLD_REQUIRED": "重叠小时存在两个瞬时，尚未选择 fold",
    "BAD_FOLD": "fold 与当地时刻的候选瞬时不符",
    "INVALID_FLIGHT_DURATION": "到达不晚于起飞或飞行时长超过 18 小时",
    "DUPLICATE_KEY": "业务键与已有行完全重复",
    "VALUE_CONFLICT": "业务键相同但字段值冲突",
    "TRUNCATED_INPUT": "输入尾部不完整，航段不进入换算",
    "UNKNOWN_IMPORT": "导入批次不存在",
    "LEG_NOT_FOUND": "航段不存在",
    "STALE_REVISION": "基于旧版本的更正，已被其他编辑取代",
    "CONCURRENT_RELEASE": "发布期间出现并发更正，已阻止覆盖",
    "REASON_REQUIRED": "更正必须注明理由",
    "RELEASE_INCOMPLETE": "发布未完成，可继续运行",
}
