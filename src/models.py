"""安全预警证据归并与闭环督办的领域模型。

模型一律为不可变值对象（``frozen=True``），状态演进由
``repository`` 中追加写入的事件承载，模型本身不含可变状态。

时序约定
--------
- ``observed_at``：现场事实实际发生/采集时刻，晚到数据不改变历史。
- ``received_at``：平台接收时刻，决定接入顺序，单调递增。
- ``issued_at``：命令（到场期限、临时控制等）对现场发出的时刻。
  命令一旦发出即历史事实，晚到的气象/定位数据只能追加再评估，
  不能改写已发命令。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------- 错误


class GovernanceError(Exception):
    """领域错误基类。"""


class DuplicateEventError(GovernanceError):
    """相同来源事件再次到达（幂等拒绝），不新建工单。"""


class ChainConflictError(GovernanceError):
    """责任链被并发修改（领取/升级/合并同时发生导致版本不匹配）。"""


class TimelineIntegrityError(GovernanceError):
    """尝试用迟到数据改写已经发出的现场命令。"""


class SegregationError(GovernanceError):
    """违反职责分离：算法版本维护人不得独自关闭本版本异常。"""


class WorkOrderStateError(GovernanceError):
    """工单状态或阶段流转不合法。"""


class CaseStateError(GovernanceError):
    """案件裁决状态不合法（如已确认同一风险后再次裁决）。"""


# ---------------------------------------------------------------- 枚举


class Source(str, Enum):
    """预警来源。"""

    VIDEO = "video"          # 视频分析
    CURRENT = "current"      # 电流监测
    LOCATION = "location"    # 定位
    WEATHER = "weather"      # 气象
    HYDROLOGY = "hydrology"  # 水利（积涝场景）
    PUBLIC_REPORT = "public_report"  # 人工举报
    INSURANCE = "insurance"  # 安责险报告


class Scenario(str, Enum):
    """业务场景，各场景使用各自的核验规则。"""

    EV_CHARGING = "ev_charging"          # 电动自行车充停
    HOT_WORK = "hot_work"                # 电焊/动火作业
    URBAN_FLOODING = "urban_flooding"    # 城乡积涝
    LIABILITY_INSURANCE = "liability_insurance"  # 安责险


class Decision(str, Enum):
    """人工对关联建议的裁决。"""

    SAME_RISK = "same_risk"        # 同一风险：归并入既有案件
    INDEPENDENT = "independent"    # 相互独立：各自立案
    FALSE_ALARM = "false_alarm"    # 算法误报：不立案，进入待复核


class CaseStatus(str, Enum):
    OPEN = "open"                  # 已接入，等待关联裁决
    MERGED = "merged"              # 经裁决并入另一案件
    ACTIVE = "active"              # 已确认立案，处置中
    DISMISSED = "dismissed"        # 裁决为误报，待复核或已结案


class ActionKind(str, Enum):
    """自动核验可以给出的辅助结论。"""

    SUPPORT_FILING = "support_filing"    # 建议立案
    MANUAL_REVIEW = "manual_review"      # 证据不足，转人工复核
    DO_NOT_FILE = "do_not_file"          # 不支持立案


class WorkStage(str, Enum):
    """闭环处置阶段，只能依次追加，不可回退或插入。"""

    TEMP_CONTROL = "temp_control"  # 临时控制
    RECTIFICATION = "rectification"  # 整改
    RECHECK = "recheck"            # 复核
    RESOLUTION = "resolution"      # 解除

    @property
    def order(self) -> int:
        return list(WorkStage).index(self)


class ReviewStatus(str, Enum):
    PENDING = "pending"    # 待复核（停机恢复后重新出现）
    APPROVED = "approved"  # 复核确认误报
    REJECTED = "rejected"  # 复核推翻误报，转立案


# ---------------------------------------------------------------- 值对象


@dataclass(frozen=True)
class SourceEvent:
    """来源事件原样留存：内容不可变、不可删除。

    ``dedupe_key`` 为来源方去重标识（来源+来源流水号），相同键再次
    到达时只返回既有事件，绝不新建工单。
    ``payload`` 为原始报文，隐私字段在转派时才做最小化处理。
    """

    event_id: str
    source: Source
    scenario: Scenario
    dedupe_key: str
    observed_at: str   # ISO8601，现场事实时刻
    received_at: str   # ISO8601，平台接收时刻
    payload: dict[str, Any]
    algorithm_version: str | None = None  # 算法/规则版本，人工举报为 None
    location_hint: str | None = None      # 粗粒度位置，用于关联建议
    subject_ref: str | None = None        # 涉及住户/从业人员的标识引用


@dataclass(frozen=True)
class Evidence:
    """随案件留存的一条证据（来源事件快照或核验结论）。"""

    event_id: str
    source: Source
    observed_at: str
    payload_excerpt: dict[str, Any]
    received_at: str


@dataclass(frozen=True)
class CorrelationSuggestion:
    """系统给出的关联建议与理由，仅供人工参考。"""

    candidate_case_id: str
    score: float
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_case_id": self.candidate_case_id,
            "score": round(self.score, 3),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class VerificationResult:
    """场景核验规则输出。

    自动结论永远只是辅助：``action`` 不直接产生工单，立案必须经人工确认。
    """

    scenario: Scenario
    action: ActionKind
    rule_version: str
    findings: tuple[str, ...]
    checked_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario.value,
            "action": self.action.value,
            "rule_version": self.rule_version,
            "findings": list(self.findings),
            "checked_at": self.checked_at,
        }


@dataclass(frozen=True)
class StageRecord:
    """一条处置阶段记录，追加后不可修改。"""

    stage: WorkStage
    operator: str
    recorded_at: str
    detail: str = ""
    command_issued_at: str | None = None  # 现场命令发出时刻


@dataclass(frozen=True)
class CaseView:
    """案件聚合的只读视图，由仓储重放事件得到。"""

    case_id: str
    status: CaseStatus
    scenario: Scenario
    events: tuple[SourceEvent, ...] = ()
    suggestions: tuple[CorrelationSuggestion, ...] = ()
    decision: Decision | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    decision_reason: str = ""
    merged_into: str | None = None
    verifications: tuple[VerificationResult, ...] = ()
    work_order_id: str | None = None
    false_alarm: "FalseAlarmView | None" = None
    # 责任链版本号：领取、升级、合并、转派每次 +1，用于乐观并发控制
    chain_version: int = 0


@dataclass(frozen=True)
class FalseAlarmView:
    """误报关闭申请与双人复核状态。"""

    review_id: str
    case_id: str
    algorithm_version: str
    proposed_by: str
    proposed_at: str
    reason: str
    status: ReviewStatus = ReviewStatus.PENDING
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    review_note: str = ""


@dataclass(frozen=True)
class WorkOrderView:
    """工单（处置责任链）只读视图。"""

    work_order_id: str
    case_id: str
    responsible_unit: str
    arrive_deadline: str
    status: str  # active / escalated / transferred / resolved
    assignee: str | None = None
    chain_version: int = 0
    stages: tuple[StageRecord, ...] = ()
    transfers: tuple[dict[str, Any], ...] = ()
    escalations: tuple[dict[str, Any], ...] = ()
    reevaluations: tuple[dict[str, Any], ...] = ()
    created_at: str = ""


@dataclass(frozen=True)
class EvidencePackage:
    """转给其他部门的最小证据包：只含对方履责所需字段。"""

    target_unit: str
    purpose: str
    case_id: str
    scenario: Scenario
    evidence: tuple[dict[str, Any], ...]
    redacted_fields: tuple[str, ...]
    assembled_at: str
    rule_version: str
