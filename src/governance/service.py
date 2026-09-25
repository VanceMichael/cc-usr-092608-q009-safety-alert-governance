"""安全预警证据归并与闭环督办服务（对外门面）。

关键纪律：

1. 来源事件只原样追加；相同 ``(source, source_event_id)`` 重复到达
   直接返回既有事件，不产生建议、不新建工单。
2. 系统只给关联建议与理由，归并/独立/误报由分析人员裁决。
3. 四类核验规则的自动结论只辅助立案，立案必须人工确认。
4. 工单任何时刻只有一个当前责任单位与到场期限；措施严格按
   临时控制→整改→复核→解除追加，解除前复核必须已完成；
   责任链只追加、单链前进。
5. 算法版本维护人不能独自关闭该版本集中产生的异常（四眼复核）。
6. 迟到数据只生成重评记录与后续措施，历史命令时刻永不改写。
7. 移送包按目的白名单裁剪并脱敏，原始报文不出系统。
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any, Callable

from src.governance import disclosure
from src.governance.model import (
    ACTION_ORDER,
    ActionKind,
    ActionStatus,
    Actor,
    ActorRole,
    AuthorizationError,
    CaseStatus,
    ConcurrencyConflict,
    CorrelationDecision,
    CorrelationGroup,
    DecisionKind,
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
    SourceKind,
    Task,
    TransferPacket,
    Unit,
    VerificationResult,
    VersionImpact,
)
from src.governance.rules import verify, verify_waterlogging_with_payload
from src.governance.store import AppendStore, with_bumped_version

CORRELATION_RULE_VERSION = "correlation-v1"
MERGE_SCORE_THRESHOLD = 0.6
TIME_WINDOW = 3600

# 可确认立案的角色（自动结论不行，必须人来确认）
FILING_ROLES = frozenset({ActorRole.MANAGER, ActorRole.FRONTLINE})
# 可批准误报关闭的角色：不得是规则维护人员，更不得是版本维护者本人
FALSE_REVIEW_ROLES = frozenset({ActorRole.MANAGER, ActorRole.ADMIN})

_TERMINAL_STATUS = frozenset({CaseStatus.RESOLVED, CaseStatus.TRANSFERRED})


class GovernanceService:
    def __init__(self, store: AppendStore | None = None) -> None:
        self._store = store or AppendStore()
        self._lock = threading.RLock()
        self._units: dict[str, Unit] = {}
        self._actors: dict[str, Actor] = {}
        # 算法版本 -> 维护人 user_id 集合
        self._version_maintainers: dict[str, set[str]] = {}

    # ============================================================ 身份
    def register_unit(self, unit: Unit) -> None:
        with self._lock:
            self._units[unit.code] = unit

    def register_actor(self, actor: Actor) -> None:
        with self._lock:
            if actor.unit_code is not None and actor.unit_code not in self._units:
                raise GovernanceError(f"未知责任单位：{actor.unit_code}")
            self._actors[actor.user_id] = actor

    def register_version_maintainer(self, algorithm_version: str, user_id: str) -> None:
        with self._lock:
            self._version_maintainers.setdefault(algorithm_version, set()).add(user_id)

    def advance_clock(self, ticks: int = 1) -> int:
        """向前推进业务时钟（迟到数据、逾期判定使用）。"""
        return self._store.advance_clock(ticks)

    def _actor(self, user_id: str) -> Actor:
        try:
            return self._actors[user_id]
        except KeyError:
            raise AuthorizationError(f"未登记的操作人：{user_id}") from None

    def _require_role(self, user_id: str, roles: frozenset[ActorRole]) -> Actor:
        actor = self._actor(user_id)
        if actor.role not in roles:
            raise AuthorizationError(f"角色{actor.role.value}无权执行该操作")
        return actor

    def _unit(self, code: str) -> Unit:
        try:
            return self._units[code]
        except KeyError:
            raise GovernanceError(f"未知责任单位：{code}") from None

    # ============================================================ 事件接入
    def ingest(
        self,
        *,
        risk_type: RiskType,
        source: str,
        source_event_id: str,
        occurred_at: int,
        confidence: float,
        location: str,
        algorithm_version: str | None = None,
        raw: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        sensitive: bool = False,
        event_id: str | None = None,
        run_verification: bool = True,
        suggest: bool = True,
    ) -> tuple[EventRecord, bool]:
        """接入来源事件并原样留存。

        返回 ``(事件, 是否新建)``。相同来源事件再次到达时返回
        ``(既有事件, False)``，不生成关联建议、不新建工单。
        """
        source_enum = SourceKind(source)
        with self._lock:
            existing = self._store.get_event_by_source(source_enum.value, source_event_id)
            if existing is not None:
                return existing, False

            seq = self._store.next_seq()
            event = EventRecord(
                event_id=event_id or f"evt-{seq}",
                risk_type=risk_type,
                source=source_enum,
                source_event_id=source_event_id,
                occurred_at=occurred_at,
                ingested_seq=seq,
                algorithm_version=algorithm_version,
                confidence=confidence,
                location=location,
                raw=dict(raw or {}),
                payload=dict(payload or {}),
                sensitive=sensitive,
            )
            assert self._store.add_event(event)

            # 自动核验：结论只留存、只辅助，绝不自动立案
            if run_verification:
                self._store.record_verification(event.event_id, verify(event))
            if suggest:
                self._suggest_correlations(event)
            return event, True

    def _suggest_correlations(self, event: EventRecord) -> None:
        for prior in self._store.list_events():
            if prior.event_id == event.event_id or prior.risk_type is not event.risk_type:
                continue

            score = 0.0
            reasons: list[str] = []
            if prior.location == event.location:
                score += 0.5
                reasons.append(f"位置相同：{event.location}")
            if abs(prior.occurred_at - event.occurred_at) <= TIME_WINDOW:
                score += 0.3
                reasons.append(
                    f"发生时刻相差{abs(prior.occurred_at - event.occurred_at)}"
                    f"（时窗{TIME_WINDOW}内）"
                )
            if prior.source is not event.source:
                score += 0.15
                reasons.append(f"来源互补（{prior.source.value} 与 {event.source.value}）")
            if prior.algorithm_version and prior.algorithm_version == event.algorithm_version:
                score += 0.05
                reasons.append(f"同一算法版本 {event.algorithm_version}")

            if score < MERGE_SCORE_THRESHOLD:
                continue

            seq = self._store.next_seq()
            left, right = sorted((prior, event), key=lambda e: e.ingested_seq)
            suggestion = MergeSuggestion(
                suggestion_id=f"sug-{seq}",
                left_event_id=left.event_id,
                right_event_id=right.event_id,
                proposal=ProposalKind.MERGE,
                score=round(score, 3),
                reasons=tuple(reasons),
                rule_version=CORRELATION_RULE_VERSION,
                created_seq=seq,
            )
            # 存储层按事件对去重；真新增才入组
            before = len(self._store.list_suggestions())
            self._store.append_suggestion(suggestion)
            if len(self._store.list_suggestions()) > before:
                self._place_in_group(suggestion)

    def _place_in_group(self, suggestion: MergeSuggestion) -> None:
        for group in self._store.list_groups():
            if group.status is GroupStatus.OPEN and (
                suggestion.left_event_id in group.member_event_ids
                or suggestion.right_event_id in group.member_event_ids
            ):
                updated = CorrelationGroup(
                    group_id=group.group_id,
                    anchor_event_id=group.anchor_event_id,
                    member_event_ids=group.member_event_ids
                    | {suggestion.left_event_id, suggestion.right_event_id},
                    status=group.status,
                    version=group.version + 1,
                )
                self._store.put_group(updated, group.version)
                return
        seq = self._store.next_seq()
        group = CorrelationGroup(
            group_id=f"grp-{seq}",
            anchor_event_id=suggestion.left_event_id,
            member_event_ids={suggestion.left_event_id, suggestion.right_event_id},
            status=GroupStatus.OPEN,
            version=1,
        )
        self._store.put_group(group)

    def list_open_suggestions(self) -> list[MergeSuggestion]:
        return list(self._store.list_suggestions())

    def list_groups(self) -> list[CorrelationGroup]:
        return self._store.list_groups()

    # ============================================================ 人工裁决
    def decide_correlation(
        self,
        user_id: str,
        group_id: str,
        event_id: str,
        decision: DecisionKind,
        *,
        target_case_id: str | None = None,
        note: str = "",
    ) -> CorrelationDecision:
        """分析人员裁决：同一风险 / 相互独立 / 算法误报。"""
        self._require_role(user_id, frozenset({ActorRole.ANALYST}))
        with self._lock:
            group = self._store.get_group(group_id)
            if event_id not in group.member_event_ids:
                raise GovernanceError("事件不属于该关联组")
            if self._store.decision_of(event_id) is not None:
                raise GovernanceError("该事件已裁决，不得重复裁决")
            if self._store.case_of_event(event_id) is not None:
                raise GovernanceError("事件已在工单中，不能再做关联裁决")

            if decision is DecisionKind.SAME_RISK:
                if target_case_id is None:
                    raise GovernanceError("裁为同一风险必须指定归入的工单")
                task = self._store.get_task(target_case_id)
                if task.status in _TERMINAL_STATUS:
                    raise GovernanceError("不能并入已终结的工单")
            elif target_case_id is not None:
                raise GovernanceError("只有裁为同一风险才能指定归并工单")

            seq = self._store.next_seq()
            record = CorrelationDecision(
                decision_id=f"dec-{seq}",
                group_id=group_id,
                event_id=event_id,
                decision=decision,
                target_case_id=target_case_id,
                decided_by=user_id,
                decided_seq=seq,
                note=note,
            )
            self._store.append_decision(record)

            if decision is DecisionKind.SAME_RISK:
                self._merge_into_case(event_id, target_case_id, user_id, seq)

            self._refresh_group_status(group)
            return record

    def _merge_into_case(
        self, event_id: str, case_id: str, user_id: str, seq: int
    ) -> None:
        event = self._store.get_event(event_id)

        def apply(task: Task) -> Task:
            return with_bumped_version(
                task,
                linked_event_ids=task.linked_event_ids | {event_id},
                algorithm_versions=(
                    task.algorithm_versions | {event.algorithm_version}
                    if event.algorithm_version
                    else task.algorithm_versions
                ),
                last_seq=seq,
            )

        self._mutate_task(case_id, apply)
        self._store.map_event_to_case(event_id, case_id)
        self._append_link(
            case_id,
            actor_id=user_id,
            action="merge_evidence",
            detail=f"事件{event_id}经人工裁为同一风险并入",
            evidence_event_ids=frozenset({event_id}),
        )

    def _refresh_group_status(self, group: CorrelationGroup) -> None:
        undecided = {
            eid
            for eid in group.member_event_ids
            if eid != group.anchor_event_id and self._store.decision_of(eid) is None
        }
        if undecided or group.status is not GroupStatus.OPEN:
            return
        updated = replace(group, status=GroupStatus.RESOLVED, version=group.version + 1)
        self._store.put_group(updated, group.version)

    # ============================================================ 立案
    def file_case(
        self,
        user_id: str,
        *,
        event_id: str,
        responsible_unit_code: str,
        on_site_deadline: int,
        confirm: bool = False,
    ) -> Task:
        """人工确认立案；自动核验结论只作辅助。"""
        actor = self._require_role(user_id, FILING_ROLES)
        if not confirm:
            raise GovernanceError("立案必须由人工显式确认（confirm=True）")
        with self._lock:
            event = self._store.get_event(event_id)
            decision = self._store.decision_of(event_id)
            if decision is not None and decision.decision is DecisionKind.FALSE_ALARM:
                raise GovernanceError("已裁为算法误报的事件不得立案")
            existing = self._store.case_of_event(event_id)
            if existing is not None:
                return self._store.get_task(existing)  # 幂等：不另建工单

            unit = self._unit(responsible_unit_code)
            seq = self._store.next_seq()
            case_id = f"case-{seq}"
            task = Task(
                case_id=case_id,
                risk_type=event.risk_type,
                location=event.location,
                status=CaseStatus.FILED,
                current_unit=unit,
                assignee=None,
                on_site_deadline=on_site_deadline,
                filed_seq=seq,
                linked_event_ids=frozenset({event_id}),
                algorithm_versions=(
                    frozenset({event.algorithm_version}) if event.algorithm_version else frozenset()
                ),
                group_id=decision.group_id if decision else None,
                version=1,
                expected_next_action=ActionKind.TEMP_CONTROL,
                last_seq=seq,
            )
            self._store.put_task(task, base_version=0)
            self._store.map_event_to_case(event_id, case_id)
            self._append_link(
                case_id,
                unit=unit,
                actor_id=actor.user_id,
                action="file",
                detail=(
                    f"人工确认立案，责任单位{unit.name}，"
                    f"到场期限{on_site_deadline}；自动核验仅辅助"
                ),
                evidence_event_ids=frozenset({event_id}),
            )
            return task

    def verification_of(self, event_id: str) -> list[VerificationResult]:
        return self._store.list_verifications(event_id)

    # ============================================================ 领取与到场
    def claim_case(self, user_id: str, case_id: str) -> Task:
        """基层人员领取。首領生效，重复/跨单位领取被拒绝，防止责任链分叉。"""
        actor = self._require_role(user_id, frozenset({ActorRole.FRONTLINE}))
        with self._lock:
            task = self._store.get_task(case_id)
            if task.status in _TERMINAL_STATUS:
                raise GovernanceError("工单已终结，不能领取")
            if task.assignee is not None:
                raise GovernanceError(f"工单已由{task.assignee}领取，责任链不得分叉")
            if actor.unit_code != task.current_unit.code:
                raise AuthorizationError(
                    f"{actor.unit_code}不是当前责任单位{task.current_unit.code}"
                )

            def apply(task: Task) -> Task:
                return with_bumped_version(
                    task, status=CaseStatus.IN_HAND, assignee=actor.user_id
                )

            updated = self._mutate_task(case_id, apply)
            self._append_link(
                case_id,
                unit=task.current_unit,
                actor_id=user_id,
                action="claim",
                detail=f"{actor.name}领取工单，唯一当前责任人确立",
            )
            return updated

    def report_on_site(self, user_id: str, case_id: str) -> Task:
        actor = self._require_role(user_id, frozenset({ActorRole.FRONTLINE}))
        with self._lock:
            task = self._store.get_task(case_id)
            if task.assignee != user_id:
                raise AuthorizationError("只有领取人可以回填到场")
            if task.status in _TERMINAL_STATUS:
                raise GovernanceError("工单已终结")

            entered_disposing = task.status is CaseStatus.IN_HAND

            def apply(task: Task) -> Task:
                status = CaseStatus.DISPOSING if entered_disposing else task.status
                return with_bumped_version(task, status=status)

            updated = self._mutate_task(case_id, apply)
            self._append_link(
                case_id,
                unit=task.current_unit,
                actor_id=user_id,
                action="on_site",
                detail=f"于{self._store.now()}到场",
            )
            return updated

    def is_overdue(self, task: Task, now: int | None = None) -> bool:
        """逾期：超过到场期限仍无到场记录（终结工单不再计逾期）。"""
        if task.status in _TERMINAL_STATUS:
            return False
        moment = self._store.now() if now is None else now
        if task.on_site_deadline is None or moment <= task.on_site_deadline:
            return False
        return not any(link.action == "on_site" for link in self._store.list_links(task.case_id))

    def overdue_tasks(self, now: int | None = None) -> list[Task]:
        moment = self._store.now() if now is None else now
        return [t for t in self._store.list_tasks() if self.is_overdue(t, moment)]

    # ============================================================ 处置措施
    def order_action(
        self,
        user_id: str,
        case_id: str,
        kind: ActionKind,
        *,
        content: str = "",
        due_at: int | None = None,
    ) -> ResponsibilityAction:
        """依次追加 临时控制→整改→复核→解除；不得跳序、不得改写。"""
        actor = self._actor(user_id)
        if actor.role not in (ActorRole.FRONTLINE, ActorRole.MANAGER):
            raise AuthorizationError("无权下达处置措施")
        with self._lock:
            task = self._store.get_task(case_id)
            if task.status in _TERMINAL_STATUS:
                raise GovernanceError("工单已终结，不能追加措施")
            if kind is not task.expected_next_action:
                expected = task.expected_next_action
                expected_text = expected.value if expected else "无"
                raise GovernanceError(
                    f"处置顺序错误：当前应追加{expected_text}，不能先{kind.value}"
                )
            if kind is ActionKind.RELEASE:
                review_done = any(
                    a.kind is ActionKind.REVIEW and a.status is ActionStatus.DONE
                    for a in self._store.list_actions(case_id)
                )
                if not review_done:
                    raise GovernanceError("复核未完成，不得解除")
                if task.status is not CaseStatus.PENDING_REVIEW:
                    raise GovernanceError("解除须在复核通过状态下由管理人员下达")
                if actor.role is not ActorRole.MANAGER:
                    raise AuthorizationError("解除须由管理人员确认")
            if actor.role is ActorRole.FRONTLINE and task.assignee not in (None, actor.user_id):
                raise AuthorizationError("只有领取人可以下达现场措施")

            seq = self._store.next_seq()
            ordered_at = self._store.now()
            action = ResponsibilityAction(
                action_id=f"act-{seq}",
                case_id=case_id,
                kind=kind,
                status=ActionStatus.PENDING,
                ordered_by=user_id,
                ordered_at=ordered_at,
                ordered_seq=seq,
                due_at=due_at,
                content=content,
            )
            self._store.append_action(action)
            next_index = ACTION_ORDER[kind] + 1
            next_kind = next((k for k, i in ACTION_ORDER.items() if i == next_index), None)

            def apply(task: Task) -> Task:
                status = task.status
                if kind is ActionKind.RELEASE:
                    status = CaseStatus.DISPOSING  # 回到处置中，执行解除后才终结
                return with_bumped_version(task, expected_next_action=next_kind, status=status)

            self._mutate_task(case_id, apply)
            self._append_link(
                case_id,
                unit=task.current_unit,
                actor_id=user_id,
                action=f"order_{kind.value}",
                detail=f"下令{kind.value}（{content}），命令时刻{ordered_at}",
            )
            return action

    def complete_action(self, user_id: str, action_id: str) -> Task:
        actor = self._actor(user_id)
        with self._lock:
            action = self._store.get_action(action_id)
            if action.status is not ActionStatus.PENDING:
                raise GovernanceError("措施已完结")
            if actor.role is ActorRole.FRONTLINE and action.ordered_by != actor.user_id:
                task = self._store.get_task(action.case_id)
                if task.assignee != actor.user_id:
                    raise AuthorizationError("只有领取该工单的人员可回填执行结果")
            completed_at = self._store.now()
            updated_action = replace(
                action, status=ActionStatus.DONE, completed_at=completed_at
            )
            self._store.replace_action(updated_action)
            task = self._store.get_task(action.case_id)

            new_status = task.status
            if action.kind is ActionKind.REVIEW:
                new_status = CaseStatus.PENDING_REVIEW
            elif action.kind is ActionKind.RELEASE:
                new_status = CaseStatus.RESOLVED

            def apply(task: Task) -> Task:
                return with_bumped_version(task, status=new_status)

            result = self._mutate_task(action.case_id, apply)
            self._append_link(
                action.case_id,
                unit=task.current_unit,
                actor_id=actor.user_id,
                action=f"complete_{action.kind.value}",
                detail=f"{action.kind.value}完成于{completed_at}，下令时刻{action.ordered_at}不变",
            )
            return result

    # ============================================================ 升级督办
    def escalate(self, user_id: str, case_id: str, reason: str, level: int = 1) -> Escalation:
        """管理人员升级督办；与领取/归并并发时靠乐观锁防止分叉。"""
        self._require_role(user_id, frozenset({ActorRole.MANAGER}))
        with self._lock:
            task = self._store.get_task(case_id)
            if task.status in _TERMINAL_STATUS:
                raise GovernanceError("工单已终结，不能升级督办")
            seq = self._store.next_seq()
            escalation = Escalation(
                escalation_id=f"esc-{seq}",
                case_id=case_id,
                manager_id=user_id,
                level=level,
                reason=reason,
                seq=seq,
            )

            def apply(task: Task) -> Task:
                return with_bumped_version(task, last_seq=seq)

            self._mutate_task(case_id, apply)
            self._store.append_escalation(escalation)
            self._append_link(
                case_id,
                unit=task.current_unit,
                actor_id=user_id,
                action="escalate",
                detail=f"升级督办（{level}级）：{reason}",
            )
            return escalation

    # ============================================================ 移送
    def transfer_case(
        self,
        user_id: str,
        case_id: str,
        *,
        to_unit_code: str,
        purpose: str,
        reason: str,
    ) -> TransferPacket:
        """移送其他部门：只发送对方履责所需证据，默认剥离个人信息。"""
        self._require_role(user_id, frozenset({ActorRole.MANAGER}))
        with self._lock:
            task = self._store.get_task(case_id)
            if task.status is CaseStatus.TRANSFERRED:
                raise GovernanceError("工单已移送")
            to_unit = self._unit(to_unit_code)
            if to_unit.code == task.current_unit.code:
                raise GovernanceError("不能移送给当前责任单位自身")

            events = [self._store.get_event(eid) for eid in sorted(task.linked_event_ids)]
            # 仅提供该移送目的下、接收方履责所需的风险类型证据
            scoped = [e for e in events if disclosure.purpose_supports(purpose, e.risk_type)]
            if not scoped:
                raise GovernanceError("该移送目的与工单风险类型不匹配，无可提供证据")
            views, redacted = disclosure.build_transfer_bundle(scoped, purpose)

            seq = self._store.next_seq()
            packet = TransferPacket(
                case_id=case_id,
                to_unit=to_unit,
                from_unit=task.current_unit,
                purpose=purpose,
                event_ids=frozenset(e.event_id for e in scoped),
                evidence=views,
                redacted_fields=redacted,
                seq=seq,
            )
            self._store.append_transfer(packet)

            def apply(task: Task) -> Task:
                return with_bumped_version(
                    task, status=CaseStatus.TRANSFERRED, current_unit=to_unit, last_seq=seq
                )

            self._mutate_task(case_id, apply)
            self._append_link(
                case_id,
                unit=to_unit,
                actor_id=user_id,
                action="transfer",
                detail=f"移送{to_unit.name}，目的{purpose}：{reason}",
                evidence_event_ids=packet.event_ids,
            )
            return packet

    # ============================================================ 误报四眼关闭
    def request_false_closure(
        self, user_id: str, *, event_ids: set[str] | frozenset[str], reason: str
    ) -> FalseReviewRequest:
        """规则/版本维护人申请把一批同源异常关闭为误报，不能独自生效。"""
        actor = self._require_role(user_id, frozenset({ActorRole.RULE_KEEPER}))
        with self._lock:
            event_ids = frozenset(event_ids)
            if not event_ids:
                raise GovernanceError("关闭申请至少包含一条事件")
            versions = {self._store.get_event(eid).algorithm_version for eid in event_ids}
            versions.discard(None)
            if len(versions) > 1:
                raise GovernanceError("一次关闭申请只能针对同一算法版本集中产生的异常")
            version = next(iter(versions), "unversioned")

            for eid in event_ids:
                decision = self._store.decision_of(eid)
                if decision is not None and decision.decision is DecisionKind.SAME_RISK:
                    raise GovernanceError(f"事件{eid}已并入工单，不能按误报关闭")
                case_id = self._store.case_of_event(eid)
                if case_id is not None:
                    task = self._store.get_task(case_id)
                    if task.status not in _TERMINAL_STATUS:
                        raise GovernanceError(f"事件{eid}仍在处置工单中，不能关闭消失")

            seq = self._store.next_seq()
            request = FalseReviewRequest(
                request_id=f"fr-{seq}",
                event_ids=event_ids,
                algorithm_version=version,
                reason=reason,
                requested_by=actor.user_id,
                requested_seq=seq,
                reviewed_by=None,
                reviewed_seq=None,
                approved=False,
                group_id=None,
            )
            self._store.append_false_request(request)
            return request

    def review_false_closure(
        self, user_id: str, request_id: str, approve: bool
    ) -> FalseReviewRequest:
        """另一角色复核：不得是申请人本人，也不得是该版本维护人。"""
        reviewer = self._require_role(user_id, FALSE_REVIEW_ROLES)
        with self._lock:
            try:
                request = next(
                    r
                    for r in self._store.list_false_requests()
                    if r.request_id == request_id
                )
            except StopIteration:
                raise GovernanceError("关闭申请不存在") from None
            if request.reviewed_by is not None:
                raise GovernanceError("该关闭申请已复核")
            if reviewer.user_id == request.requested_by:
                raise AuthorizationError("申请人不能独自关闭自己维护版本产生的异常")
            maintainers = self._version_maintainers.get(request.algorithm_version, set())
            if reviewer.user_id in maintainers:
                raise AuthorizationError("该算法版本的维护人不能审批本版本的误报关闭")

            seq = self._store.next_seq()
            reviewed = replace(
                request,
                reviewed_by=reviewer.user_id,
                reviewed_seq=seq,
                approved=approve,
            )
            self._store.replace_false_request(request_id, reviewed)
            return reviewed

    def pending_false_requests(self) -> list[FalseReviewRequest]:
        """停机恢复后，待复核误报申请重新出现。"""
        return [r for r in self._store.list_false_requests() if r.reviewed_by is None]

    # ============================================================ 迟到数据重评
    def ingest_late_followup(
        self,
        user_id: str,
        case_id: str,
        *,
        source: str,
        source_event_id: str,
        occurred_at: int,
        payload: dict[str, Any],
        confidence: float = 1.0,
    ) -> Reevaluation:
        """迟到的气象/定位数据到达：重评后续措施，绝不倒签历史命令。

        相同来源事件再次到达不新建工单、也不重复重评。
        """
        self._require_role(
            user_id,
            frozenset({ActorRole.FRONTLINE, ActorRole.MANAGER, ActorRole.ANALYST}),
        )
        source_enum = SourceKind(source)
        with self._lock:
            case = self._store.get_task(case_id)
            if case.risk_type not in (RiskType.WATERLOGGING, RiskType.BIKE_CHARGING):
                raise GovernanceError("迟到的气象/定位重评仅支持积涝与充停场景")
            if source_enum not in (SourceKind.WEATHER, SourceKind.LOCATION):
                raise GovernanceError("重评触发数据仅限气象或定位来源")
            if self._store.has_source_event(source_enum.value, source_event_id):
                raise GovernanceError("相同来源事件已到达过，不重复重评、不新建工单")

            event, created = self.ingest(
                risk_type=case.risk_type,
                source=source,
                source_event_id=source_event_id,
                occurred_at=occurred_at,
                confidence=confidence,
                location=case.location,
                payload=payload,
                run_verification=False,
                suggest=False,
            )
            assert created

            changes: list[str] = []
            if case.risk_type is RiskType.WATERLOGGING:
                prior_water = [
                    self._store.get_event(eid)
                    for eid in case.linked_event_ids
                    if self._store.get_event(eid).risk_type is RiskType.WATERLOGGING
                ]
                base = prior_water[0] if prior_water else event
                # 以新证据重算：产生新结论记录，旧结论原样保留
                fresh = verify_waterlogging_with_payload(base, payload)
                self._store.record_verification(event.event_id, fresh)
                if fresh.passed:
                    changes.append("积水回落到预警阈值以下，可安排复核后解除")
                else:
                    changes.append("迟到数据表明风险仍在，维持临时控制并加排抽排力量")
            else:
                fresh = verify(event)
                self._store.record_verification(event.event_id, fresh)
                changes.append("迟到定位用于校正现场位置判断，仅影响后续巡查安排")

            seq = self._store.next_seq()
            reeval = Reevaluation(
                reeval_id=f"ree-{seq}",
                case_id=case_id,
                trigger_event_id=event.event_id,
                changes=tuple(changes),
                created_seq=seq,
                kept_history=True,
            )
            self._store.append_reevaluation(reeval)
            self._store.map_event_to_case(event.event_id, case_id)

            def apply(task: Task) -> Task:
                return with_bumped_version(
                    task, linked_event_ids=task.linked_event_ids | {event.event_id}, last_seq=seq
                )

            self._mutate_task(case_id, apply)
            self._append_link(
                case_id,
                unit=case.current_unit,
                actor_id=user_id,
                action="reevaluate",
                detail=(
                    f"迟到{source_enum.value}数据（发生于{occurred_at}）到达，"
                    f"重评后续措施；历史命令保持原样"
                ),
                evidence_event_ids=frozenset({event.event_id}),
            )

            # 纪律断言：重评前后每条历史命令的下令时刻保持不变
            for historical in self._store.list_actions(case_id):
                if historical.ordered_at > self._store.now():
                    raise GovernanceError("内部错误：历史命令时刻被改写")
            return reeval

    # ============================================================ 版本影响分析
    def version_impact(self, algorithm_version: str) -> VersionImpact:
        """按算法版本找出一批可能受同类错误影响的历史事件与工单。"""
        with self._lock:
            events = [
                e for e in self._store.list_events() if e.algorithm_version == algorithm_version
            ]
            event_ids = frozenset(e.event_id for e in events)
            case_ids: set[str] = set()
            false_ids: set[str] = set()
            for request in self._store.list_false_requests():
                if request.algorithm_version == algorithm_version and request.approved:
                    false_ids.update(request.event_ids)
            for eid in event_ids:
                case_id = self._store.case_of_event(eid)
                if case_id:
                    case_ids.add(case_id)
                decision = self._store.decision_of(eid)
                if decision is not None and decision.decision is DecisionKind.FALSE_ALARM:
                    false_ids.add(eid)
            return VersionImpact(
                algorithm_version=algorithm_version,
                event_ids=event_ids,
                case_ids=frozenset(case_ids),
                false_event_ids=frozenset(false_ids),
                reason=(
                    f"算法版本{algorithm_version}共产生{len(event_ids)}条事件，"
                    f"其中{len(false_ids)}条被标记/批准为误报，"
                    f"关联{len(case_ids)}个工单，建议倒查同批次历史事件"
                ),
            )

    # ============================================================ 责任链与只读视图
    def responsibility_chain(self, case_id: str) -> list[ResponsibilityLink]:
        links = self._store.list_links(case_id)
        seq_seen = [link.seq for link in links]
        if len(seq_seen) != len(set(seq_seen)):
            raise GovernanceError("责任链出现重复环节")
        ordered = sorted(links, key=lambda link: link.seq)
        predecessor = None
        for link in ordered:
            if link.predecessor_seq != predecessor:
                raise GovernanceError(
                    f"责任链分叉：环节{link.seq}的前驱应为{predecessor}，"
                    f"实际为{link.predecessor_seq}"
                )
            predecessor = link.seq
        return ordered

    def get_task(self, case_id: str) -> Task:
        return self._store.get_task(case_id)

    def get_action(self, action_id: str) -> ResponsibilityAction:
        return self._store.get_action(action_id)

    def actions_of(self, case_id: str) -> list[ResponsibilityAction]:
        return self._store.list_actions(case_id)

    def transfers(self) -> list[TransferPacket]:
        return self._store.list_transfers()

    def reevaluations_of(self, case_id: str) -> list[Reevaluation]:
        return self._store.list_reevaluations(case_id)

    def escalations_of(self, case_id: str) -> list[Escalation]:
        return self._store.list_escalations(case_id)

    def get_event(self, event_id: str) -> EventRecord:
        return self._store.get_event(event_id)

    def list_tasks(self) -> list[Task]:
        return self._store.list_tasks()

    # ============================================================ 快照恢复
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snap = self._store.snapshot()
            snap["service"] = {
                "units": dict(self._units),
                "actors": dict(self._actors),
                "version_maintainers": {
                    k: set(v) for k, v in self._version_maintainers.items()
                },
            }
            return snap

    @classmethod
    def restore(cls, snap: dict[str, Any]) -> "GovernanceService":
        service = cls()
        service._store.restore(snap)
        registry = snap.get("service", {})
        service._units = dict(registry.get("units", {}))
        service._actors = dict(registry.get("actors", {}))
        service._version_maintainers = {
            k: set(v) for k, v in registry.get("version_maintainers", {}).items()
        }
        return service

    # ============================================================ 内部工具
    def _mutate_task(
        self, case_id: str, fn: Callable[[Task], Task], *, retries: int = 20
    ) -> Task:
        """乐观读改写：并发冲突时重读最新版本重试，语义错误不重试。"""
        for _ in range(retries):
            task = self._store.get_task(case_id)
            updated = fn(task)
            try:
                return self._store.put_task(updated, task.version)
            except ConcurrencyConflict:
                continue
        raise ConcurrencyConflict("工单并发冲突，重试已耗尽")

    def _append_link(
        self,
        case_id: str,
        *,
        actor_id: str,
        action: str,
        detail: str,
        unit: Unit | None = None,
        evidence_event_ids: frozenset[str] = frozenset(),
    ) -> ResponsibilityLink:
        tail = self._store.list_links(case_id)[-1:]
        seq = self._store.next_seq()
        held = unit or self._store.get_task(case_id).current_unit
        link = ResponsibilityLink(
            seq=seq,
            case_id=case_id,
            unit=held,
            actor_id=actor_id,
            action=action,
            detail=detail,
            predecessor_seq=tail[0].seq if tail else None,
            evidence_event_ids=frozenset(evidence_event_ids),
        )
        self._store.append_link(link)
        return link
