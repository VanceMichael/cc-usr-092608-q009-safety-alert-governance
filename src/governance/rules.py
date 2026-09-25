"""场景核验规则。

四类风险各用各的规则与版本，互不混用：

- 充停位置：电动自行车是否在合规区域充停，是否占用疏散通道
- 动火资质：动火许可与焊工资质是否齐备、在有效期内、监护是否到位
- 积水阈值：积水深度/上涨速率对照分级阈值
- 保险承诺：安责险服务承诺是否在约定时限内兑现

任何规则的自动结论都只“辅助立案”：``assists_filing`` 为真也不会
自动生成工单，立案必须由人工确认。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from src.governance.model import (
    EventRecord,
    RuleLevel,
    VerificationResult,
    RiskType,
)


def _result(
    event: EventRecord,
    rule_version: str,
    level: RuleLevel,
    findings: list[str],
) -> VerificationResult:
    return VerificationResult(
        risk_type=event.risk_type,
        rule_version=rule_version,
        level=level,
        passed=level is RuleLevel.PASS,
        findings=tuple(findings),
        assists_filing=level in (RuleLevel.BREACH, RuleLevel.ATTENTION),
    )


# ---------------------------------------------------------------- 充停位置

BIKE_RULE_VERSION = "bike-location-v3"

# 允许判定的位置标签
_DESIGNATED_ZONES = frozenset({"指定充电桩", "室外集中充电棚"})


def verify_bike_charging(event: EventRecord) -> VerificationResult:
    """充停位置核验。室内充电、堵占疏散通道/电梯厅为明确违规。"""
    p = event.payload
    findings: list[str] = []

    zone = str(p.get("zone", ""))
    indoor = bool(p.get("indoor_charging", False))
    blocks_exit = bool(p.get("blocks_escape", False))
    in_elevator_hall = bool(p.get("in_elevator_hall", False))

    if indoor:
        findings.append("电动自行车在建筑物内充电")
    if blocks_exit:
        findings.append("车辆/充电设施堵塞疏散通道或安全出口")
    if in_elevator_hall:
        findings.append("车辆进入电梯厅或客梯")
    if findings:
        return _result(event, BIKE_RULE_VERSION, RuleLevel.BREACH, findings)

    if zone in _DESIGNATED_ZONES:
        findings.append(f"位于{zone}，充停位置合规")
        return _result(event, BIKE_RULE_VERSION, RuleLevel.PASS, findings)

    # 无法确认是否在指定区域（摄像头角度、定位漂移等）
    findings.append("充停区域无法从现有证据确认")
    return _result(event, BIKE_RULE_VERSION, RuleLevel.ATTENTION, findings)


# ---------------------------------------------------------------- 动火资质

HOT_WORK_RULE_VERSION = "hotwork-permit-v2"


def verify_hot_work(event: EventRecord) -> VerificationResult:
    """动火许可与人员资质核验，两者各自独立检查。"""
    p = event.payload
    findings: list[str] = []
    level = RuleLevel.PASS

    permit_present = bool(p.get("permit_present", False))
    permit_valid = bool(p.get("permit_valid", False))
    cert_present = bool(p.get("welder_cert_present", False))
    cert_valid = bool(p.get("welder_cert_valid", False))
    fire_watch = bool(p.get("fire_watch_present", False))

    if not permit_present:
        findings.append("未见动火作业许可")
        level = RuleLevel.BREACH
    elif not permit_valid:
        findings.append("动火许可已失效或超出许可时段、范围")
        level = RuleLevel.BREACH

    if not cert_present:
        findings.append("作业人员未提供焊工作业资质")
        level = RuleLevel.BREACH
    elif not cert_valid:
        findings.append("焊工资质证书已过期或未按期复审")
        level = RuleLevel.BREACH

    if level is not RuleLevel.BREACH and not fire_watch:
        findings.append("动火现场未安排监护人员")
        level = RuleLevel.ATTENTION

    if level is RuleLevel.PASS:
        findings.append("动火许可、人员资质与现场监护均齐备")
    return _result(event, HOT_WORK_RULE_VERSION, level, findings)


# ---------------------------------------------------------------- 积水阈值

WATER_RULE_VERSION = "water-depth-threshold-v4"

# 分级阈值（厘米），可随城市防指调标而更换规则版本
WATER_WARN_CM = 15.0
WATER_DANGER_CM = 27.0
WATER_RATE_WARN_CM_H = 5.0


def verify_waterlogging(
    event: EventRecord,
    *,
    warn_cm: float = WATER_WARN_CM,
    danger_cm: float = WATER_DANGER_CM,
) -> VerificationResult:
    """积水深度与上涨速率核验；缺测时只能给存疑结论。"""
    p = event.payload
    findings: list[str] = []

    depth = p.get("depth_cm")
    rate = p.get("rate_cm_h")
    if depth is None:
        findings.append("积水深度缺测，无法对照阈值")
        return _result(event, WATER_RULE_VERSION, RuleLevel.ATTENTION, findings)

    depth = float(depth)
    findings.append(f"积水深度{depth:g}厘米（预警{warn_cm:g}、危险{danger_cm:g}）")

    level = RuleLevel.PASS
    if depth >= danger_cm:
        findings.append("达到危险阈值，可能威胁通行与配电设施")
        level = RuleLevel.BREACH
    elif depth >= warn_cm:
        findings.append("达到预警阈值")
        level = RuleLevel.ATTENTION

    if rate is not None and float(rate) >= WATER_RATE_WARN_CM_H:
        findings.append(f"上涨速率{float(rate):g}厘米/小时，需防范快速积水")
        if level is RuleLevel.PASS:
            level = RuleLevel.ATTENTION

    if level is RuleLevel.PASS:
        findings.append("低于预警阈值")
    return _result(event, WATER_RULE_VERSION, level, findings)


def verify_waterlogging_with_payload(event: EventRecord, extra_payload: dict) -> VerificationResult:
    """用迟到数据合并后的报文重算积水结论（生成新结论，不改旧结论）。"""
    merged = {**event.payload, **extra_payload}
    return verify_waterlogging(replace(event, payload=merged))


# ---------------------------------------------------------------- 保险承诺

INSURANCE_RULE_VERSION = "insurance-commitment-v1"

# 安责险事故预防服务承诺的反馈时限（小时）
SERVICE_SLA_HOURS = 24.0


def verify_liability_insurance(event: EventRecord) -> VerificationResult:
    """安责险服务承诺核验：保单有效、承诺事项按时限兑现。"""
    p = event.payload
    findings: list[str] = []

    if not bool(p.get("policy_active", False)):
        findings.append("安责险保单失效或未查询到有效保单")
        return _result(event, INSURANCE_RULE_VERSION, RuleLevel.BREACH, findings)

    promised = bool(p.get("service_promised", False))
    elapsed = p.get("elapsed_hours")
    service_done = bool(p.get("service_record_exists", False))

    if service_done:
        findings.append("保险事故预防服务已留存履约记录")
        return _result(event, INSURANCE_RULE_VERSION, RuleLevel.PASS, findings)

    if not promised:
        findings.append("保单有效但未见服务承诺事项")
        return _result(event, INSURANCE_RULE_VERSION, RuleLevel.ATTENTION, findings)

    if elapsed is None:
        findings.append("已承诺服务但未填报响应时长，需人工核实")
        return _result(event, INSURANCE_RULE_VERSION, RuleLevel.ATTENTION, findings)

    if float(elapsed) > SERVICE_SLA_HOURS:
        findings.append(f"承诺服务超过{SERVICE_SLA_HOURS:g}小时未兑现")
        return _result(event, INSURANCE_RULE_VERSION, RuleLevel.BREACH, findings)

    findings.append(f"承诺服务在途（{float(elapsed):g}小时），未超时限")
    return _result(event, INSURANCE_RULE_VERSION, RuleLevel.ATTENTION, findings)


# ---------------------------------------------------------------- 注册表

RuleFn = Callable[[EventRecord], VerificationResult]

RULE_REGISTRY: dict[RiskType, RuleFn] = {
    RiskType.BIKE_CHARGING: verify_bike_charging,
    RiskType.HOT_WORK: verify_hot_work,
    RiskType.WATERLOGGING: verify_waterlogging,
    RiskType.LIABILITY_INSURANCE: verify_liability_insurance,
}

RULE_VERSIONS: dict[RiskType, str] = {
    RiskType.BIKE_CHARGING: BIKE_RULE_VERSION,
    RiskType.HOT_WORK: HOT_WORK_RULE_VERSION,
    RiskType.WATERLOGGING: WATER_RULE_VERSION,
    RiskType.LIABILITY_INSURANCE: INSURANCE_RULE_VERSION,
}


def verify(event: EventRecord) -> VerificationResult:
    """按风险类型分派到各自的核验规则。"""
    try:
        return RULE_REGISTRY[event.risk_type](event)
    except KeyError:
        raise ValueError(f"未知风险类型：{event.risk_type}") from None
