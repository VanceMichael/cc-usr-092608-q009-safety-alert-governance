"""安全预警证据归并与闭环督办服务门面。

把事件接入、关联裁决、场景核验、立案处置、误报治理、停机恢复组织为
一个事务边界明确的应用服务。关键不变量：

1. 来源事件原样留存；相同 ``dedupe_key`` 再次到达不新建工单。
2. 系统只给关联建议与理由，同一风险/相互独立/算法误报一律人工裁决。
3. 场景自动核验结论只能辅助立案，未经人工裁决不得产生工单。
4. 任一时刻工单只有唯一当前责任单位与一个到场期限。
5. 临时控制→整改→复核→解除严格依次追加；命令时刻单调，迟到数据
   只能触发"再评估+后续措施"，不得改写已发命令。
6. 转派只携带按履责目的最小化后的证据包。
7. 算法版本维护人不得独自关闭该版本集中产生的异常（双人复核）。
8. 领取、升级、合并、转派均带责任链版本号，冲突即抛
   ``ChainConflictError``，防止责任链分叉。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .correlation import suggest_for
from .minimization import build_evidence_package
from .models import (
    CaseStateError,
    CaseStatus,
    CaseView,
    ChainConflictError,
    Decision,
    EvidencePackage,
    GovernanceError,
    ReviewStatus,
    SegregationError,
    Source,
    SourceEvent,
    Scenario,
    TimelineIntegrityError,
    VerificationResult,
    WorkOrderStateError,
    WorkOrderView,
    WorkStage,
)
from .repository import Repository, source_event_to_dict
from .verification import verify_events


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _require_version(seen: int, current: int) -> None:
    """并发守卫：调用方所见版本必须等于当前责任链版本。

    在所有领域状态校验**之前**执行，使领取/升级/合并同时发生时
    失败方稳定得到 ChainConflictError，而不是被状态校验掩盖。
    """
    if seen != current:
        raise ChainConflictError(
            f"责任链已被他人更新（所见版本 {seen}，当前版本 {current}）"
        )


@dataclass(frozen=True)
class IngestResult:
    """接入结果：``duplicate`` 为真时 ``case`` 为事件首次归属的旧案件。"""

    duplicate: bool
    event: SourceEvent
    case: CaseView
    suggestions: tuple  # tuple[CorrelationSuggestion, ...]


class SafetyGovernanceService:
    def __init__(self, repository: Repository) -> None:
        self.repo = repository

    # -------------------------------------------------------- 版本登记

    def register_algorithm(self, version: str, maintainers: set[str] | frozenset[str],
                           at: str | None = None) -> None:
        """登记算法版本及其维护人（用于误报关闭的职责分离）。"""
        if not maintainers:
            raise GovernanceError("算法版本必须至少登记一名维护人")
        with self.repo.exclusive:
            self.repo.append(
                "algorithm.registered",
                {
                    "version": version,
                    "maintainers": sorted(maintainers),
                    "at": at or now_iso(),
                },
            )

    # ------------------------------------------------------------ 接入

    def ingest(self, event: SourceEvent) -> IngestResult:
        """接入一条来源事件。

        相同来源事件（同 ``dedupe_key``）再次到达：原样返回既有记录，
        不新建案件/工单。新事件则原样留存、单开待裁决案件并给出关联
        建议——建议不自动合并任何东西。
        """
        with self.repo.exclusive:
            existing = self.repo.find_by_dedupe_key(event.dedupe_key)
            if existing is not None:
                case = self._case_owning_event(existing.event_id)
                return IngestResult(
                    duplicate=True,
                    event=existing,
                    case=case,
                    suggestions=(),
                )

            at = event.received_at
            self.repo.append("event.received", {"event": source_event_to_dict(event)})
            case_id = f"CASE-{event.event_id}"
            self.repo.append(
                "case.opened",
                {
                    "case_id": case_id,
                    "scenario": event.scenario.value,
                    "event_ids": [event.event_id],
                    "at": at,
                },
            )
            case = self.repo.get_case(case_id)
            assert case is not None
            candidates = [
                c
                for c in self.repo.list_cases()
                if c.case_id != case_id
            ]
            suggestions = tuple(suggest_for(event, candidates))
            self.repo.append(
                "correlation.suggested",
                {
                    "case_id": case_id,
                    "at": at,
                    "suggestions": [s.as_dict() for s in suggestions],
                },
            )
            case = self.repo.get_case(case_id)
            assert case is not None
            return IngestResult(
                duplicate=False, event=event, case=case, suggestions=suggestions
            )

    def _case_owning_event(self, event_id: str) -> CaseView:
        for case in self.repo.list_cases():
            if any(e.event_id == event_id for e in case.events):
                return case
        raise GovernanceError(f"事件 {event_id} 无归属案件")

    # -------------------------------------------------------- 人工裁决

    def decide(
        self,
        case_id: str,
        decision: Decision,
        by: str,
        reason: str,
        expected_chain_version: int,
        target_case_id: str | None = None,
        at: str | None = None,
    ) -> CaseView:
        """人工对关联建议作出裁决。

        - SAME_RISK：并入目标案件，本案件不再单独立案；
        - INDEPENDENT：确认独立风险，进入可立案状态；
        - FALSE_ALARM 不走本方法，须经 :meth:`propose_false_alarm`
          的双人复核流程。
        """
        if decision is Decision.FALSE_ALARM:
            raise CaseStateError("算法误报须走 propose_false_alarm 双人复核流程")
        with self.repo.exclusive:
            case = self.repo.get_case(case_id)
            if case is None:
                raise CaseStateError(f"案件不存在：{case_id}")
            _require_version(expected_chain_version, case.chain_version)
            if case.status not in (CaseStatus.OPEN, CaseStatus.ACTIVE) or case.decision:
                raise CaseStateError("案件已经裁决，不可重复裁决")

            target = None
            if decision is Decision.SAME_RISK:
                if not target_case_id or target_case_id == case_id:
                    raise CaseStateError("同一风险裁决必须指定不同的目标案件")
                target = self.repo.get_case(target_case_id)
                if target is None or target.status not in (
                    CaseStatus.OPEN,
                    CaseStatus.ACTIVE,
                ):
                    raise CaseStateError("目标案件不存在或已终结，不能并入")

            self.repo.append(
                "case.decided",
                {
                    "case_id": case_id,
                    "decision": decision.value,
                    "target_case_id": target_case_id,
                    "by": by,
                    "reason": reason,
                    "at": at or now_iso(),
                    "expected_chain_version": expected_chain_version,
                },
            )
            updated = self.repo.get_case(case_id)
            assert updated is not None
            return updated

    # -------------------------------------------------------- 误报治理

    def _case_algorithm_version(self, case: CaseView) -> str | None:
        for event in case.events:
            if event.algorithm_version:
                return event.algorithm_version
        return None

    def propose_false_alarm(
        self,
        case_id: str,
        proposed_by: str,
        reason: str,
        expected_chain_version: int,
        at: str | None = None,
    ) -> str:
        """提出算法误报关闭申请，进入待复核。不允许独自关闭。"""
        with self.repo.exclusive:
            case = self.repo.get_case(case_id)
            if case is None:
                raise CaseStateError(f"案件不存在：{case_id}")
            _require_version(expected_chain_version, case.chain_version)
            if case.work_order_id is not None:
                raise CaseStateError("工单已立案，误报申请须按处置流程处理")
            if case.status not in (CaseStatus.OPEN, CaseStatus.ACTIVE):
                raise CaseStateError("当前案件状态不允许提出误报申请")
            if case.false_alarm is not None:
                raise CaseStateError("该案件已有误报复核申请")

            version = self._case_algorithm_version(case)
            if version is None:
                raise CaseStateError("非算法来源事件不适用误报关闭流程")

            review_id = f"REVIEW-{case_id}"
            self.repo.append(
                "false_alarm.proposed",
                {
                    "review_id": review_id,
                    "case_id": case_id,
                    "algorithm_version": version,
                    "proposed_by": proposed_by,
                    "reason": reason,
                    "at": at or now_iso(),
                    "expected_chain_version": expected_chain_version,
                },
            )
            return review_id

    def review_false_alarm(
        self,
        review_id: str,
        reviewed_by: str,
        approve: bool,
        note: str,
        at: str | None = None,
    ) -> CaseView:
        """第二人复核。

        - 复核人不得是申请人本人；
        - 复核人不得是该算法版本的维护人——维护人不能关闭本版本
          集中产生的异常（即便申请是其本人提出，也须由他人把关）。
        """
        with self.repo.exclusive:
            pending = {
                r.review_id: r for r in self.repo.list_pending_reviews()
            }.get(review_id)
            if pending is None:
                raise CaseStateError("待复核误报不存在或已复核")
            if reviewed_by == pending.proposed_by:
                raise SegregationError("申请人不得自行复核误报关闭申请")
            maintainers = self.repo.maintainers_of(pending.algorithm_version)
            if reviewed_by in maintainers:
                raise SegregationError(
                    f"{reviewed_by} 是算法版本 {pending.algorithm_version} 的维护人，"
                    "不得独自关闭该版本产生的异常"
                )

            self.repo.append(
                "false_alarm.reviewed",
                {
                    "review_id": review_id,
                    "by": reviewed_by,
                    "status": (
                        ReviewStatus.APPROVED.value if approve else ReviewStatus.REJECTED.value
                    ),
                    "note": note,
                    "at": at or now_iso(),
                },
            )
            case = self.repo.get_case(pending.case_id)
            assert case is not None
            return case

    # -------------------------------------------------------- 场景核验

    def run_verification(
        self, case_id: str, checked_at: str | None = None
    ) -> VerificationResult:
        """执行该案件场景自己的核验规则并留存结论（仅辅助立案）。"""
        with self.repo.exclusive:
            case = self.repo.get_case(case_id)
            if case is None:
                raise CaseStateError(f"案件不存在：{case_id}")
            result = verify_events(
                case.scenario, list(case.events), checked_at or now_iso()
            )
            self.repo.append(
                "verification.recorded",
                {"case_id": case_id, "result": result.as_dict()},
            )
            return result

    # ------------------------------------------------------------ 立案

    def file_work_order(
        self,
        case_id: str,
        responsible_unit: str,
        arrive_deadline: str,
        by: str,
        expected_chain_version: int,
        at: str | None = None,
    ) -> WorkOrderView:
        """人工确认处置后立案：明确唯一当前责任单位与到场期限。

        未经人工裁决（案件仍 OPEN）或自动核验未建议立案，都不能代替
        人工确认；每个案件至多一个工单，保证责任单位唯一。
        """
        with self.repo.exclusive:
            case = self.repo.get_case(case_id)
            if case is None:
                raise CaseStateError(f"案件不存在：{case_id}")
            _require_version(expected_chain_version, case.chain_version)
            human_confirmed = bool(case.decision) or (
                case.false_alarm is not None
                and case.false_alarm.status is ReviewStatus.REJECTED
            )
            if case.status is not CaseStatus.ACTIVE or not human_confirmed:
                raise CaseStateError("未经人工独立风险确认，不得立案")
            if case.work_order_id is not None:
                raise WorkOrderStateError("该案件已存在工单，不得重复立案")
            ts = at or now_iso()
            if _parse(arrive_deadline) <= _parse(ts):
                raise WorkOrderStateError("到场期限必须晚于立案时刻")

            work_order_id = f"WO-{case_id}"
            self.repo.append(
                "work_order.created",
                {
                    "work_order_id": work_order_id,
                    "case_id": case_id,
                    "responsible_unit": responsible_unit,
                    "arrive_deadline": arrive_deadline,
                    "by": by,
                    "at": ts,
                    "expected_chain_version": expected_chain_version,
                },
            )
            order = self.repo.get_work_order(work_order_id)
            assert order is not None
            return order

    def assign(
        self,
        work_order_id: str,
        assignee: str,
        expected_chain_version: int,
        at: str | None = None,
    ) -> WorkOrderView:
        """基层人员领取任务。并发领取时只有一人成功（版本号控制）。"""
        with self.repo.exclusive:
            order = self._require_active_order(work_order_id)
            _require_version(expected_chain_version, order.chain_version)
            if order.assignee is not None:
                raise WorkOrderStateError(
                    f"任务已由 {order.assignee} 领取，责任链不得分叉"
                )
            self.repo.append(
                "work_order.assigned",
                {
                    "work_order_id": work_order_id,
                    "assignee": assignee,
                    "at": at or now_iso(),
                    "expected_chain_version": expected_chain_version,
                },
            )
            updated = self.repo.get_work_order(work_order_id)
            assert updated is not None
            return updated

    def append_stage(
        self,
        work_order_id: str,
        stage: WorkStage,
        operator: str,
        detail: str,
        expected_chain_version: int,
        command_issued_at: str | None = None,
        at: str | None = None,
    ) -> WorkOrderView:
        """依次追加 临时控制→整改→复核→解除。

        ``command_issued_at`` 是命令对现场实际发出的时刻，必须晚于
        既有命令——不允许借迟到数据把新命令伪造成"当时已下达"。
        """
        with self.repo.exclusive:
            order = self._require_active_order(work_order_id)
            _require_version(expected_chain_version, order.chain_version)
            if order.stages and order.stages[-1].stage is WorkStage.RESOLUTION:
                raise WorkOrderStateError("工单已解除，不可再追加阶段")
            expected_stage = (
                WorkStage.TEMP_CONTROL
                if not order.stages
                else list(WorkStage)[order.stages[-1].stage.order + 1]
            )
            if stage is not expected_stage:
                raise WorkOrderStateError(
                    f"阶段必须依次追加，当前应追加 {expected_stage.value}，"
                    f"而非 {stage.value}"
                )
            ts = at or now_iso()
            issued = command_issued_at or ts
            if order.stages and order.stages[-1].command_issued_at:
                if _parse(issued) < _parse(order.stages[-1].command_issued_at):
                    raise TimelineIntegrityError(
                        "新命令的发出时刻早于既有命令，"
                        "迟到数据不得被伪造成当时现场已收到的命令"
                    )

            self.repo.append(
                "stage.appended",
                {
                    "work_order_id": work_order_id,
                    "stage": stage.value,
                    "operator": operator,
                    "detail": detail,
                    "command_issued_at": issued,
                    "at": ts,
                    "expected_chain_version": expected_chain_version,
                },
            )
            updated = self.repo.get_work_order(work_order_id)
            assert updated is not None
            return updated

    def escalate(
        self,
        work_order_id: str,
        by: str,
        reason: str,
        expected_chain_version: int,
        at: str | None = None,
    ) -> WorkOrderView:
        """管理人员升级督办。升级不改变唯一当前责任单位。"""
        with self.repo.exclusive:
            order = self._require_active_order(work_order_id)
            _require_version(expected_chain_version, order.chain_version)
            self.repo.append(
                "work_order.escalated",
                {
                    "work_order_id": work_order_id,
                    "by": by,
                    "reason": reason,
                    "at": at or now_iso(),
                    "expected_chain_version": expected_chain_version,
                },
            )
            updated = self.repo.get_work_order(work_order_id)
            assert updated is not None
            return updated

    def transfer(
        self,
        work_order_id: str,
        to_unit: str,
        purpose: str,
        by: str,
        expected_chain_version: int,
        at: str | None = None,
    ) -> tuple[WorkOrderView, EvidencePackage]:
        """转派其他部门：只发送对方履责所需的最小证据包。"""
        with self.repo.exclusive:
            order = self._require_active_order(work_order_id)
            _require_version(expected_chain_version, order.chain_version)
            if to_unit == order.responsible_unit:
                raise WorkOrderStateError("接收单位与当前责任单位相同")
            case = self.repo.get_case(order.case_id)
            assert case is not None
            ts = at or now_iso()
            package = build_evidence_package(
                case=case,
                events=list(case.events),
                target_unit=to_unit,
                purpose=purpose,
                assembled_at=ts,
            )
            self.repo.append(
                "work_order.transferred",
                {
                    "work_order_id": work_order_id,
                    "from_unit": order.responsible_unit,
                    "to_unit": to_unit,
                    "by": by,
                    "purpose": purpose,
                    "at": ts,
                    "evidence_package": {
                        "target_unit": package.target_unit,
                        "purpose": package.purpose,
                        "rule_version": package.rule_version,
                        "redacted_fields": list(package.redacted_fields),
                        "evidence": [dict(item) for item in package.evidence],
                    },
                    "expected_chain_version": expected_chain_version,
                },
            )
            updated = self.repo.get_work_order(work_order_id)
            assert updated is not None
            return updated, package

    # ------------------------------------------------------ 迟到数据

    def reevaluate_with_late_event(
        self,
        work_order_id: str,
        late_event_id: str,
        by: str,
        note: str,
        at: str | None = None,
    ) -> dict:
        """以迟到的气象/定位数据促使重新评估后续措施。

        迟到事件必须是已被归并到本工单案件的事件；再评估只追加记录，
        不修改任何已发命令。后续措施仍通过 :meth:`append_stage` 正常
        追加，且同样受命令时刻单调约束。
        """
        with self.repo.exclusive:
            order = self._require_active_order(work_order_id)
            late_event = self.repo.get_event(late_event_id)
            if late_event is None:
                raise GovernanceError(f"事件不存在：{late_event_id}")
            case = self.repo.get_case(order.case_id)
            assert case is not None
            if not any(e.event_id == late_event_id for e in case.events):
                raise GovernanceError("迟到事件未归并到本工单所属案件")
            if late_event.source not in (Source.WEATHER, Source.LOCATION, Source.HYDROLOGY):
                raise GovernanceError("再评估仅接受迟到的气象、定位或水利数据")

            ts = at or now_iso()
            record = {
                "work_order_id": work_order_id,
                "late_event_id": late_event_id,
                "late_source": late_event.source.value,
                "observed_at": late_event.observed_at,
                "received_at": late_event.received_at,
                "by": by,
                "note": note,
                "at": ts,
            }
            self.repo.append("measure.reevaluated", record)
            return record

    # ------------------------------------------------------------ 查询

    def _require_active_order(self, work_order_id: str) -> WorkOrderView:
        order = self.repo.get_work_order(work_order_id)
        if order is None:
            raise WorkOrderStateError(f"工单不存在：{work_order_id}")
        if order.status == "resolved":
            raise WorkOrderStateError("工单已解除")
        return order

    def list_overdue_orders(self, now: str | None = None) -> list[WorkOrderView]:
        """逾期任务：到场期限已过且尚未解除。停机恢复后自动重现。"""
        moment = _parse(now or now_iso())
        return [
            order
            for order in self.repo.list_work_orders()
            if order.status != "resolved" and _parse(order.arrive_deadline) < moment
        ]

    def list_pending_false_alarms(self):
        """待复核误报。停机恢复后自动重现。"""
        return self.repo.list_pending_reviews()

    def analyze_algorithm_version_impact(self, version: str) -> dict:
        """按算法版本找出可能受同类错误影响的历史事件与案件。

        - 经复核确认误报关闭的案件：已受影响；
        - 该版本产生、但仍在处置或已并入其他案件的历史事件：可能受
          影响，供分析人员批量排查。
        """
        affected_events = [
            event
            for event in self.repo.list_events()
            if event.algorithm_version == version
        ]
        event_ids = {e.event_id for e in affected_events}

        dismissed_cases: list[str] = []
        pending_cases: list[str] = []
        potentially_affected_cases: list[str] = []
        for case in self.repo.list_cases():
            touches = any(e.event_id in event_ids for e in case.events)
            if not touches:
                continue
            if (
                case.false_alarm is not None
                and case.false_alarm.algorithm_version == version
                and case.false_alarm.status is ReviewStatus.APPROVED
            ):
                dismissed_cases.append(case.case_id)
            elif (
                case.false_alarm is not None
                and case.false_alarm.status is ReviewStatus.PENDING
            ):
                pending_cases.append(case.case_id)
            else:
                potentially_affected_cases.append(case.case_id)

        return {
            "algorithm_version": version,
            "events": [source_event_to_dict(e) for e in affected_events],
            "confirmed_false_alarm_cases": dismissed_cases,
            "pending_review_cases": pending_cases,
            "potentially_affected_cases": potentially_affected_cases,
        }
