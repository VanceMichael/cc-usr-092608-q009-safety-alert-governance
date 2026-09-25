"""领域记录：事件原样留存，归并、裁决、工单、责任链均不可变追加。

时间一律使用单调整数序号（``seq``）表示先后，禁止回填；
迟到数据只能生成新的重评记录，不得改写既有记录。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class GovernanceError(Exception):
    """领域规则被违反时抛出。"""


class AuthorizationError(GovernanceError):
    """操作人无权执行该动作。"""


class ConcurrencyConflict(GovernanceError):
    """并发操作导致责任链或工单状态分叉，调用方应重试。"""


class RiskType(str, enum.Enum):
    """四类风险场景，各有独立核验规则。"""

    BIKE_CHARGING = "bike_charging"  # 电动自行车充停
    HOT_WORK = "hot_work"  # 电焊等动火作业
    WATERLOGGING = "waterlogging"  # 城乡积涝
    LIABILITY_INSURANCE = "liability_insurance"  # 安责险报告


class SourceKind(str, enum.Enum):
    """预警来源类别；相同来源的幂等键重复到达不得新建工单。"""

    VIDEO = "video"
    CURRENT = "current"  # 电流监测
    LOCATION = "location"  # 定位
    WEATHER = "weather"  # 气象
    MANUAL = "manual"  # 人工举报
    INSURANCE = "insurance"  # 安责险报告


class RuleLevel(str, enum.Enum):
    """核验结论强度。自动结论只能辅助立案，不能代替确认。"""

    PASS = "pass"  # 规则通过，可辅助立案
    ATTENTION = "attention"  # 存疑，需人工核查
    BREACH = "breach"  # 明确违反规则，仍需人工确认才能立案


class ActorRole(str, enum.Enum):
    FRONTLINE = "frontline"  # 基层处置人员：领取、到场、措施
    MANAGER = "manager"  # 管理人员：升级督办
    ANALYST = "analyst"  # 分析人员：归并裁决、误报标记
    RULE_KEEPER = "rule_keeper"  # 规则维护人员：维护算法版本
    INSURANCE_STAFF = "insurance_staff"  # 保险服务人员
    ADMIN = "admin"  # 四眼审核人（与规则维护人不同自然人）


class GroupStatus(str, enum.Enum):
    OPEN = "open"  # 有待人工裁决的关联建议
    RESOLVED = "resolved"  # 组内事件均已裁决
    CLOSED_AS_FALSE = "closed_as_false"  # 整组被关闭为误报（须复核）


class DecisionKind(str, enum.Enum):
    SAME_RISK = "same_risk"  # 同一风险：归入既有工单
    INDEPENDENT = "independent"  # 相互独立：另案
    FALSE_ALARM = "false_alarm"  # 算法误报：不立案，进误报池待复核


class ProposalKind(str, enum.Enum):
    MERGE = "merge"
    SPLIT = "split"


class CaseStatus(str, enum.Enum):
    FILED = "filed"  # 已立案、等待处置
    IN_HAND = "in_hand"  # 已领取
    DISPOSING = "disposing"  # 处置中（措施追加）
    PENDING_REVIEW = "pending_review"  # 待复核
    RESOLVED = "resolved"  # 已解除
    TRANSFERRED = "transferred"  # 已移送其他部门（本方责任终止）


class ActionKind(str, enum.Enum):
    """处置动作只能按顺序追加：临时控制→整改→复核→解除。"""

    TEMP_CONTROL = "temp_control"
    RECTIFY = "rectify"
    REVIEW = "review"
    RELEASE = "release"


# 处置动作的先后次序；解除前必须先有复核
ACTION_ORDER: dict[ActionKind, int] = {
    ActionKind.TEMP_CONTROL: 0,
    ActionKind.RECTIFY: 1,
    ActionKind.REVIEW: 2,
    ActionKind.RELEASE: 3,
}


class ActionStatus(str, enum.Enum):
    PENDING = "pending"  # 已下令、待执行
    DONE = "done"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Unit:
    """责任单位。"""

    code: str
    name: str


@dataclass(frozen=True, slots=True)
class Actor:
    """操作人。``user_id`` 与 ``display_name`` 分离，移送包内只放角色性身份。"""

    user_id: str
    name: str
    role: ActorRole
    unit_code: str | None = None


@dataclass(frozen=True, slots=True)
class EventRecord:
    """来源事件，原样留存、不可修改。

    - ``source``/``source_event_id`` 构成天然幂等键；
    - ``occurred_at`` 为事件实际发生时刻（迟到事件该时刻可能早于接入时刻）；
    - ``ingested_seq`` 为系统接入序号，严格递增；
    - ``raw`` 为原始报文；``sensitive`` 标记其中含住户/从业人员信息，
      后续移送默认剥离。
    """

    event_id: str
    risk_type: RiskType
    source: SourceKind
    source_event_id: str
    occurred_at: int
    ingested_seq: int
    algorithm_version: str | None
    confidence: float
    location: str
    raw: dict[str, Any] = field(default_factory=dict)
    sensitive: bool = False
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MergeSuggestion:
    """系统给出的关联建议与理由，仅供人工裁决参考。"""

    suggestion_id: str
    left_event_id: str
    right_event_id: str
    proposal: ProposalKind
    score: float
    reasons: tuple[str, ...]
    rule_version: str
    created_seq: int


@dataclass(frozen=True, slots=True)
class CorrelationGroup:
    """关联组：人工对组内每条建议逐一裁决。"""

    group_id: str
    anchor_event_id: str  # 组内最早接入事件
    member_event_ids: frozenset[str]
    status: GroupStatus
    version: int  # 乐观锁版本


@dataclass(frozen=True, slots=True)
class CorrelationDecision:
    """人工裁决记录：同一风险 / 相互独立 / 算法误报。"""

    decision_id: str
    group_id: str
    event_id: str
    decision: DecisionKind
    target_case_id: str | None  # SAME_RISK 时归入的工单
    decided_by: str
    decided_seq: int
    note: str = ""


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """场景核验规则的自动结论。``level`` 永远只是辅助。"""

    risk_type: RiskType
    rule_version: str
    level: RuleLevel
    passed: bool
    findings: tuple[str, ...]
    assists_filing: bool  # 是否可辅助立案；任何结论都不自动立案


@dataclass(frozen=True, slots=True)
class Task:
    """督办工单。同一时刻唯一当前责任单位（``current_unit``）。"""

    case_id: str
    risk_type: RiskType
    location: str
    status: CaseStatus
    current_unit: Unit
    assignee: str | None  # 领取人 user_id
    on_site_deadline: int | None  # 到场期限
    filed_seq: int
    linked_event_ids: frozenset[str]
    algorithm_versions: frozenset[str]
    group_id: str | None
    version: int  # 乐观锁版本：领取/升级/措施/移送共用
    expected_next_action: ActionKind | None  # 下一允许追加的措施
    last_seq: int


@dataclass(frozen=True, slots=True)
class ResponsibilityLink:
    """责任链上的一环，只追加、不可删除。"""

    seq: int
    case_id: str
    unit: Unit
    actor_id: str
    action: str  # confirm/claim/onsite/transfer/escalate/release...
    detail: str
    predecessor_seq: int | None  # 指向前一环，构成单链
    evidence_event_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ResponsibilityAction:
    """处置措施记录（临时控制、整改、复核、解除依次追加）。"""

    action_id: str
    case_id: str
    kind: ActionKind
    status: ActionStatus
    ordered_by: str
    ordered_at: int  # 下令时刻（现场实际收到命令的时刻，永不改写）
    ordered_seq: int
    due_at: int | None = None
    completed_at: int | None = None
    content: str = ""
    # 重评后对本措施的调整以新动作表达；本字段保留原令，不允许篡改
    superseded_by: str | None = None


@dataclass(frozen=True, slots=True)
class Escalation:
    """管理人员升级督办。"""

    escalation_id: str
    case_id: str
    manager_id: str
    level: int
    reason: str
    seq: int


@dataclass(frozen=True, slots=True)
class DispositionAction:
    """误报处置标记（分析人员可标记，关闭须复核）。"""

    event_ids: frozenset[str]
    algorithm_version: str | None
    marked_by: str
    marked_seq: int
    reason: str


@dataclass(frozen=True, slots=True)
class FalseReviewRequest:
    """误报关闭申请：规则维护人提出，另一角色复核（四眼原则）。"""

    request_id: str
    event_ids: frozenset[str]
    algorithm_version: str
    reason: str
    requested_by: str  # 规则维护人
    requested_seq: int
    reviewed_by: str | None
    reviewed_seq: int | None
    approved: bool
    group_id: str | None


@dataclass(frozen=True, slots=True)
class TransferPacket:
    """移送其他部门时的证据包：只含对方履责所需，默认脱敏。"""

    case_id: str
    to_unit: Unit
    from_unit: Unit
    purpose: str
    event_ids: frozenset[str]
    evidence: tuple[dict[str, Any], ...]
    redacted_fields: tuple[str, ...]
    seq: int


@dataclass(frozen=True, slots=True)
class Reevaluation:
    """迟到数据触发的重新评估：只影响后续措施，不改写历史命令。"""

    reeval_id: str
    case_id: str
    trigger_event_id: str  # 迟到的气象/定位事件
    changes: tuple[str, ...]  # 对后续措施的调整说明
    created_seq: int
    kept_history: bool  # 必须为 True：历史命令原样保留


@dataclass(frozen=True, slots=True)
class VersionImpact:
    """按算法版本找出的可能受同类错误影响的历史事件。"""

    algorithm_version: str
    event_ids: frozenset[str]
    case_ids: frozenset[str]
    false_event_ids: frozenset[str]
    reason: str
