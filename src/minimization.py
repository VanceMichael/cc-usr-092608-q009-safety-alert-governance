"""转派证据的最小披露组装。

转给其他部门时**只发送对方履责所需证据**：客观风险事实可以共享，
住户与从业人员信息默认不随工单扩散；现场救援确需联络时，也只提供
脱敏后的姓名/电话/地址。

策略与转派目的绑定（不是与接收部门的"级别"绑定），同一条证据在
不同履责目的下披露范围不同。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import CaseView, EvidencePackage, SourceEvent


MINIMIZATION_VERSION = "minimization-2026.09"

# 身份类信息：任何目的下都剔除
IDENTITY_KEYS = frozenset(
    {
        "id_card",
        "id_card_no",
        "resident_id",
        "worker_id",
        "employee_no",
        "household_members",
        "household_size",
        "subject_ref",
    }
)

# 联络类信息：仅在履责目的确有必要时脱敏保留
CONTACT_KEYS = frozenset(
    {
        "person_name",
        "resident_name",
        "worker_name",
        "welder_name",
        "contact_phone",
        "phone",
        "detailed_address",
    }
)


@dataclass(frozen=True)
class DisclosurePolicy:
    """某一转派目的下的披露策略。"""

    purpose: str
    allow_masked_contact: bool
    allowed_keys: frozenset[str] = frozenset()  # 额外放行的字段名


# 目的注册表：没有登记的目的按最严格策略处理
POLICIES: dict[str, DisclosurePolicy] = {
    "积水排导": DisclosurePolicy(purpose="积水排导", allow_masked_contact=False),
    "保险服务": DisclosurePolicy(
        purpose="保险服务",
        allow_masked_contact=False,
        allowed_keys=frozenset(
            {"policy", "service_commitment", "insurer_response_minutes"}
        ),
    ),
    "联合执法": DisclosurePolicy(purpose="联合执法", allow_masked_contact=False),
    "现场救援": DisclosurePolicy(purpose="现场救援", allow_masked_contact=True),
}

DEFAULT_POLICY = DisclosurePolicy(purpose="默认", allow_masked_contact=False)


class _Drop:
    """标记递归脱敏中被剔除的节点。"""


_DROP = _Drop()


def _mask_name(value: str) -> str:
    if not value:
        return "**"
    return value[0] + "**"


def _mask_phone(value: str) -> str:
    digits = str(value)
    if len(digits) >= 7:
        return digits[:3] + "****" + digits[-4:]
    return "****"


def _mask_address(value: str) -> str:
    return str(value)[:6] + "****（已脱敏）"


def _mask_scalar(key: str, value: Any) -> Any:
    if "name" in key:
        return _mask_name(str(value))
    if "phone" in key:
        return _mask_phone(value)
    if "address" in key:
        return _mask_address(value)
    return "****（已脱敏）"


def _redact(
    key: str,
    value: Any,
    policy: DisclosurePolicy,
    path: str,
    redacted: list[str],
) -> Any:
    """递归脱敏；被剔除节点返回 _DROP。"""
    full_path = f"{path}.{key}" if path else key

    if key in IDENTITY_KEYS and key not in policy.allowed_keys:
        redacted.append(full_path)
        return _DROP

    if key in CONTACT_KEYS and key not in policy.allowed_keys:
        if policy.allow_masked_contact:
            redacted.append(f"{full_path}（已脱敏）")
            return _mask_scalar(key, value)
        redacted.append(full_path)
        return _DROP

    if isinstance(value, dict):
        result = {}
        for sub_key, sub_value in value.items():
            kept = _redact(sub_key, sub_value, policy, full_path, redacted)
            if kept is not _DROP:
                result[sub_key] = kept
        return result

    if isinstance(value, list):
        result = []
        for index, item in enumerate(value):
            kept = _redact(str(index), item, policy, full_path, redacted)
            if kept is not _DROP:
                result.append(kept)
        return result

    return value


def build_evidence_package(
    case: CaseView,
    events: list[SourceEvent],
    target_unit: str,
    purpose: str,
    assembled_at: str,
) -> EvidencePackage:
    """按履责目的组装最小证据包。

    原始事件不受影响：脱敏只发生在对外证据包上，库内证据原样留存。
    未登记的目的按最严格策略处理（仅默认策略允许这种回退）。
    """
    policy = POLICIES.get(purpose, DEFAULT_POLICY)

    package_evidence: list[dict[str, Any]] = []
    redacted_overall: list[str] = []
    for event in events:
        excerpt = {}
        for key, value in event.payload.items():
            kept = _redact(key, value, policy, "", redacted_overall)
            if kept is not _DROP:
                excerpt[key] = kept
        package_evidence.append(
            {
                "event_id": event.event_id,
                "source": event.source.value,
                "scenario": event.scenario.value,
                "observed_at": event.observed_at,
                "payload_excerpt": excerpt,
            }
        )

    # 去重并保持稳定排序，避免同一路径重复出现
    redacted_fields = tuple(sorted(set(redacted_overall)))
    return EvidencePackage(
        target_unit=target_unit,
        purpose=policy.purpose,
        case_id=case.case_id,
        scenario=case.scenario,
        evidence=tuple(package_evidence),
        redacted_fields=redacted_fields,
        assembled_at=assembled_at,
        rule_version=MINIMIZATION_VERSION,
    )
