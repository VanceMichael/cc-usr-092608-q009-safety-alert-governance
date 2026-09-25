"""安全预警证据归并与闭环督办领域服务。

模块划分：

- ``model``      不可变事件、归并组、工单、责任链、督办动作等领域记录
- ``rules``      充停位置、动火资质、积水阈值、安责险承诺四类核验规则
- ``disclosure`` 跨部门移送时的履责证据最小披露矩阵与脱敏
- ``store``      仅追加记录、快照与停机恢复
- ``service``    对外门面：接入、归并裁决、立案、责任链、误报复核、影响分析
"""

from src.governance.model import (
    ActionKind,
    ActionStatus,
    Actor,
    ActorRole,
    AuthorizationError,
    CaseStatus,
    ConcurrencyConflict,
    CorrelationGroup,
    DecisionKind,
    DispositionAction,
    Escalation,
    EventRecord,
    FalseReviewRequest,
    GovernanceError,
    GroupStatus,
    MergeSuggestion,
    ProposalKind,
    Reevaluation,
    ResponsibilityAction,
    ResponsibilityLink,
    RiskType,
    RuleLevel,
    SourceKind,
    Task,
    TransferPacket,
    Unit,
    VerificationResult,
)
from src.governance.service import GovernanceService

__all__ = [
    "ActionKind",
    "ActionStatus",
    "Actor",
    "ActorRole",
    "AuthorizationError",
    "CaseStatus",
    "ConcurrencyConflict",
    "CorrelationGroup",
    "DecisionKind",
    "DispositionAction",
    "Escalation",
    "EventRecord",
    "FalseReviewRequest",
    "GovernanceError",
    "GovernanceService",
    "GroupStatus",
    "MergeSuggestion",
    "ProposalKind",
    "Reevaluation",
    "ResponsibilityAction",
    "ResponsibilityLink",
    "RiskType",
    "RuleLevel",
    "SourceKind",
    "Task",
    "TransferPacket",
    "Unit",
    "VerificationResult",
]
