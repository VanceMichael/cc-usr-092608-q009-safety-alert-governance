"""移送其他部门时的证据最小披露。

原则：转给其他部门时只发送对方履责所需证据，住户与从业人员信息
不随工单任意扩散。原始报文（``raw``）永远不进移送包，改放事件的
结构化视图；视图字段按“移送目的 × 风险类型”白名单裁剪，
白名单之外以及命中敏感词的字段一律剥离并记入 ``redacted_fields``。
"""

from __future__ import annotations

from src.governance.model import EventRecord, RiskType

# 移送目的：决定接收单位履责所需的最小字段集
PURPOSE_FIRE_ENFORCEMENT = "fire_enforcement"  # 消防执法核查
PURPOSE_FLOOD_CONTROL = "flood_control"  # 排涝调度
PURPOSE_INSURANCE_SERVICE = "insurance_service"  # 保险事故预防服务
PURPOSE_COMMUNITY_GOVERNANCE = "community_governance"  # 社区劝导

# 任何目的下都允许出现的事件封套字段
_COMMON_FIELDS = frozenset(
    {
        "event_id",
        "risk_type",
        "source",
        "occurred_at",
        "location",
        "algorithm_version",
        "confidence",
    }
)

# 按移送目的 × 风险类型 的 payload 白名单
_PAYLOAD_WHITELIST: dict[str, dict[RiskType, frozenset[str]]] = {
    PURPOSE_FIRE_ENFORCEMENT: {
        RiskType.BIKE_CHARGING: frozenset(
            {"zone", "indoor_charging", "blocks_escape", "in_elevator_hall"}
        ),
        RiskType.HOT_WORK: frozenset(
            {
                "permit_present",
                "permit_valid",
                "welder_cert_present",
                "welder_cert_valid",
                "fire_watch_present",
            }
        ),
    },
    PURPOSE_FLOOD_CONTROL: {
        RiskType.WATERLOGGING: frozenset(
            {"depth_cm", "rate_cm_h", "gauge_id", "road_segment"}
        ),
    },
    PURPOSE_INSURANCE_SERVICE: {
        RiskType.LIABILITY_INSURANCE: frozenset(
            {
                "policy_active",
                "service_promised",
                "elapsed_hours",
                "service_record_exists",
                "service_item",
            }
        ),
    },
    PURPOSE_COMMUNITY_GOVERNANCE: {
        RiskType.BIKE_CHARGING: frozenset({"zone", "indoor_charging"}),
        RiskType.WATERLOGGING: frozenset({"depth_cm", "road_segment"}),
    },
}

# 即使误入白名单也必须剥离的敏感字段（住户/从业人员个人信息）
_SENSITIVE_KEY_TOKENS = (
    "name",  # 姓名/作业人员姓名/住户姓名
    "phone",
    "mobile",
    "id_card",
    "id_no",
    "cert_no",  # 证书编号可反查到人，只保留是否有效
    "household",
    "resident",
    "employee",
    "employer_contact",
    "address_detail",
    "license_plate",  # 车牌可关联到具体车主
)


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in _SENSITIVE_KEY_TOKENS)


def redacted_event_view(event: EventRecord, purpose: str) -> tuple[dict, list[str]]:
    """生成单条事件的移送视图，返回视图与被剥离字段路径列表。"""
    allowed_payload = _PAYLOAD_WHITELIST.get(purpose, {}).get(event.risk_type, frozenset())
    redacted: list[str] = []

    view = {
        "event_id": event.event_id,
        "risk_type": event.risk_type.value,
        "source": event.source.value,
        "occurred_at": event.occurred_at,
        "location": event.location,
        "algorithm_version": event.algorithm_version,
        "confidence": event.confidence,
    }

    payload_view: dict[str, object] = {}
    for key, value in event.payload.items():
        if is_sensitive_key(key):
            redacted.append(f"payload.{key}")
            continue
        if key not in allowed_payload:
            redacted.append(f"payload.{key}")
            continue
        payload_view[key] = value
    if payload_view:
        view["payload"] = payload_view

    # 原始报文永不随移送包扩散；含敏感信息时明确记录剥离动作
    if event.raw:
        redacted.append("raw")
    if event.sensitive:
        redacted.append("sensitive_raw")
    return view, redacted


def build_transfer_bundle(
    events: list[EventRecord], purpose: str
) -> tuple[tuple[dict, ...], tuple[str, ...]]:
    """为整批证据生成移送视图，汇总去重被剥离的字段。"""
    views: list[dict] = []
    redacted: set[str] = set()
    for event in events:
        view, removed = redacted_event_view(event, purpose)
        views.append(view)
        redacted.update(removed)
    return tuple(views), tuple(sorted(redacted))


def purpose_supports(purpose: str, risk_type: RiskType) -> bool:
    """该移送目的是否接收此类风险证据（不匹配则整类不予提供）。"""
    return risk_type in _PAYLOAD_WHITELIST.get(purpose, {})
