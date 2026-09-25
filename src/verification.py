"""分场景核验规则。

充停位置、动火许可与人员资质、积水阈值、保险服务承诺分别维护各自的
规则版本与判定逻辑，互不共用阈值。所有规则的输出都只是**辅助立案**
的结论（见 ``models.ActionKind``），不直接产生工单。

每个规则类声明独立的 ``rule_version``；规则调整时版本递增，便于按
版本追溯当时给出的自动结论。
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .models import ActionKind, Scenario, SourceEvent, VerificationResult


RULE_PACK_VERSION = "rule-pack-2026.09"


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _latest_payload(events: Iterable[SourceEvent]) -> dict:
    ordered = sorted(events, key=lambda e: e.received_at)
    return ordered[-1].payload if ordered else {}


class VerificationRule:
    scenario: Scenario
    rule_version: str

    def verify(self, events: list[SourceEvent], checked_at: str) -> VerificationResult:
        raise NotImplementedError


class EVChargingRule(VerificationRule):
    """电动自行车充停核验：位置性质 + 电流特征。"""

    scenario = Scenario.EV_CHARGING
    rule_version = f"ev-charging/{RULE_PACK_VERSION}"

    # 允许充电的位置类型；其余（楼道、室内、安全出口）均为禁止位
    PERMITTED_LOCATIONS = frozenset({"outdoor_station", "designated_shed"})
    OVERCURRENT_MILLIAMP = 3000          # 超流阈值（mA）
    MAX_CONTINUOUS_MINUTES = 480         # 连续充电时长上限（分钟）

    def verify(self, events: list[SourceEvent], checked_at: str) -> VerificationResult:
        findings: list[str] = []
        location_forbidden: bool | None = None
        abnormal_current: bool | None = None

        for event in events:
            p = event.payload
            if "location_type" in p:
                allowed = p["location_type"] in self.PERMITTED_LOCATIONS
                if not allowed:
                    location_forbidden = True
                    findings.append(
                        f"充停位置 {p['location_type']} 不属于允许充电区域"
                    )
                else:
                    location_forbidden = False
            if "current_milliamp" in p:
                if p["current_milliamp"] >= self.OVERCURRENT_MILLIAMP:
                    abnormal_current = True
                    findings.append(
                        f"电流 {p['current_milliamp']}mA 达到超流阈值 "
                        f"{self.OVERCURRENT_MILLIAMP}mA"
                    )
                elif abnormal_current is None:
                    abnormal_current = False
            if p.get("continuous_minutes", 0) > self.MAX_CONTINUOUS_MINUTES:
                abnormal_current = True
                findings.append(
                    f"连续充电 {p['continuous_minutes']} 分钟超过 "
                    f"{self.MAX_CONTINUOUS_MINUTES} 分钟"
                )

        if location_forbidden and abnormal_current:
            action = ActionKind.SUPPORT_FILING
        elif location_forbidden is None and abnormal_current is None:
            action = ActionKind.MANUAL_REVIEW
            findings.append("缺少充停位置与电流证据")
        elif location_forbidden or abnormal_current:
            # 仅有位置或仅有电流异常时，建议人工复核而非直接立案
            action = ActionKind.MANUAL_REVIEW
            findings.append("仅单一维度异常，需人工结合现场判断")
        else:
            action = ActionKind.DO_NOT_FILE
            findings.append("充停位置合规且电流无异常")
        return VerificationResult(
            scenario=self.scenario,
            action=action,
            rule_version=self.rule_version,
            findings=tuple(findings),
            checked_at=checked_at,
        )


class HotWorkRule(VerificationRule):
    """动火作业核验：动火许可 + 人员资质，两者各自独立校验。"""

    scenario = Scenario.HOT_WORK
    rule_version = f"hot-work/{RULE_PACK_VERSION}"

    def verify(self, events: list[SourceEvent], checked_at: str) -> VerificationResult:
        payload = _latest_payload(events)
        findings: list[str] = []
        permit_ok = False
        qualification_ok = False

        permit = payload.get("hot_work_permit")
        observed = _parse(events[0].observed_at) if events else None
        if not permit:
            findings.append("未查询到有效的动火许可")
        else:
            start = _parse(permit["valid_from"])
            end = _parse(permit["valid_to"])
            covers = observed is not None and start <= observed <= end
            if permit.get("status") != "valid" or not covers:
                findings.append("动火许可状态无效或作业时刻不在许可时段内")
            else:
                permit_ok = True
                findings.append("动火许可在有效期内且覆盖作业时刻")

        qualification = payload.get("welder_qualification")
        if not qualification:
            findings.append("未查询到作业人员焊工资质")
        else:
            cert_until = _parse(qualification["valid_to"])
            if qualification.get("status") != "valid" or cert_until < observed:
                findings.append("焊工资质失效或已过期")
            else:
                qualification_ok = True
                findings.append("焊工资质在有效期内")

        if not permit_ok and not qualification_ok:
            action = ActionKind.SUPPORT_FILING
        elif permit_ok and qualification_ok:
            action = ActionKind.DO_NOT_FILE
        else:
            action = ActionKind.MANUAL_REVIEW
        return VerificationResult(
            scenario=self.scenario,
            action=action,
            rule_version=self.rule_version,
            findings=tuple(findings),
            checked_at=checked_at,
        )


class UrbanFloodingRule(VerificationRule):
    """城乡积涝核验：积水深度对照分档阈值。阈值与点位类型绑定。"""

    scenario = Scenario.URBAN_FLOODING
    rule_version = f"urban-flooding/{RULE_PACK_VERSION}"

    # 单位毫米；按点位类型各自设阈
    THRESHOLDS_MM = {
        "underpass": (150, 270),     # 下穿通道：关注 / 封控
        "urban_road": (100, 200),    # 城市道路
        "residential": (80, 150),    # 居民区
    }

    def verify(self, events: list[SourceEvent], checked_at: str) -> VerificationResult:
        findings: list[str] = []
        worst: tuple[str, int] | None = None
        for event in events:
            p = event.payload
            depth = p.get("water_depth_mm")
            site = p.get("site_type", "urban_road")
            if depth is None:
                continue
            if worst is None or depth > worst[1]:
                worst = (site, depth)

        if worst is None:
            findings.append("暂无积水深度实测数据，等待气象/水利数据")
            return VerificationResult(
                scenario=self.scenario,
                action=ActionKind.MANUAL_REVIEW,
                rule_version=self.rule_version,
                findings=tuple(findings),
                checked_at=checked_at,
            )

        site, depth = worst
        attention, control = self.THRESHOLDS_MM.get(
            site, self.THRESHOLDS_MM["urban_road"]
        )
        findings.append(f"点位类型 {site} 实测最大积水 {depth}mm")
        if depth >= control:
            action = ActionKind.SUPPORT_FILING
            findings.append(f"达到封控阈值 {control}mm")
        elif depth >= attention:
            action = ActionKind.MANUAL_REVIEW
            findings.append(f"达到关注阈值 {attention}mm，需人工研判")
        else:
            action = ActionKind.DO_NOT_FILE
            findings.append(f"低于关注阈值 {attention}mm")
        return VerificationResult(
            scenario=self.scenario,
            action=action,
            rule_version=self.rule_version,
            findings=tuple(findings),
            checked_at=checked_at,
        )


class LiabilityInsuranceRule(VerificationRule):
    """安责险报告核验：保险服务承诺（响应时限等）履行情况。"""

    scenario = Scenario.LIABILITY_INSURANCE
    rule_version = f"liability-insurance/{RULE_PACK_VERSION}"

    DEFAULT_RESPONSE_MINUTES = 60  # 缺省到场/响应承诺（分钟）

    def verify(self, events: list[SourceEvent], checked_at: str) -> VerificationResult:
        payload = _latest_payload(events)
        findings: list[str] = []

        commitment = payload.get("service_commitment", {})
        promised_minutes = commitment.get(
            "response_within_minutes", self.DEFAULT_RESPONSE_MINUTES
        )
        findings.append(f"保险服务承诺响应时限 {promised_minutes} 分钟")

        policy = payload.get("policy", {})
        if policy.get("status") != "active":
            findings.append("安责险保单状态非有效，服务承诺无法确认")
            action = ActionKind.MANUAL_REVIEW
        else:
            findings.append("安责险保单有效")
            responded_minutes = payload.get("insurer_response_minutes")
            if responded_minutes is None:
                action = ActionKind.MANUAL_REVIEW
                findings.append("保险机构尚未反馈响应，等待服务记录")
            elif responded_minutes > promised_minutes:
                action = ActionKind.SUPPORT_FILING
                findings.append(
                    f"实际响应 {responded_minutes} 分钟，超出承诺 "
                    f"{promised_minutes} 分钟"
                )
            else:
                action = ActionKind.DO_NOT_FILE
                findings.append(
                    f"实际响应 {responded_minutes} 分钟，符合服务承诺"
                )
        return VerificationResult(
            scenario=self.scenario,
            action=action,
            rule_version=self.rule_version,
            findings=tuple(findings),
            checked_at=checked_at,
        )


_RULES: dict[Scenario, VerificationRule] = {
    rule.scenario: rule
    for rule in (
        EVChargingRule(),
        HotWorkRule(),
        UrbanFloodingRule(),
        LiabilityInsuranceRule(),
    )
}


def verify_events(
    scenario: Scenario, events: list[SourceEvent], checked_at: str
) -> VerificationResult:
    """执行该场景自己的核验规则；场景之间不串用阈值。"""
    return _RULES[scenario].verify(events, checked_at)
