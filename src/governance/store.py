"""仅追加记录存储与快照恢复。

领域状态全部由追加记录重放得到：

- 事件、建议、裁决、责任链环节、措施、移送包等“事实记录”只追加；
- 工单等可变状态每次变更产生新版本，旧版本保留，写入采用
  比较并交换（CAS）——基版本不匹配即抛 :class:`ConcurrencyConflict`，
  以保证基层领取、管理升级、分析合并同时发生时责任链不分叉；
- ``snapshot()`` 导出可序列化状态，``restore()`` 在停机恢复后
  重建现场：逾期任务、待复核误报重新出现。

本实现为进程内存储，便于测试与离线运行；接口保持小而稳，
可换为带事务的持久实现。
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any, Generic, TypeVar

from src.governance.model import (
    ConcurrencyConflict,
    CorrelationDecision,
    CorrelationGroup,
    EventRecord,
    Escalation,
    FalseReviewRequest,
    GovernanceError,
    MergeSuggestion,
    Reevaluation,
    ResponsibilityAction,
    ResponsibilityLink,
    Task,
    TransferPacket,
    VerificationResult,
)

T = TypeVar("T")


@dataclass
class _Versioned(Generic[T]):
    value: T
    version: int


class AppendStore:
    """线程安全的追加记录 + 版本化状态容器。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._seq = 0
        self._wall = 0
        self._events: dict[str, EventRecord] = {}
        self._source_index: dict[tuple[str, str], str] = {}
        self._suggestions: list[MergeSuggestion] = []
        self._suggestion_pairs: set[tuple[str, str]] = set()
        self._groups: dict[str, _Versioned[CorrelationGroup]] = {}
        self._decisions: list[CorrelationDecision] = []
        self._tasks: dict[str, _Versioned[Task]] = {}
        self._links: list[ResponsibilityLink] = []
        self._actions: list[ResponsibilityAction] = []
        self._escalations: list[Escalation] = []
        self._false_requests: list[FalseReviewRequest] = []
        self._transfers: list[TransferPacket] = []
        self._reevals: list[Reevaluation] = []
        self._verifications: dict[str, list[VerificationResult]] = {}
        # 事件 -> 所属工单（SAME_RISK 归并后登记）
        self._event_case: dict[str, str] = {}
        # 组内事件 -> 决策
        self._event_decision: dict[str, CorrelationDecision] = {}

    # ------------------------------------------------------------ 序号与时钟
    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    def now(self) -> int:
        """当前业务时刻（单调时钟，测试可拨）。"""
        with self._lock:
            return self._wall

    def advance_clock(self, ticks: int = 1) -> int:
        with self._lock:
            if ticks < 1:
                raise ValueError("时钟只能向前")
            self._wall += ticks
            return self._wall

    def set_clock(self, value: int) -> None:
        """停机恢复时还原时钟，不允许拨回到历史时刻之前。"""
        with self._lock:
            if value < self._wall:
                raise ValueError("时钟不可倒拨")
            self._wall = value

    # ------------------------------------------------------------ 事件
    def add_event(self, event: EventRecord) -> bool:
        """登记来源事件。相同 (source, source_event_id) 再次到达返回 False。"""
        key = (event.source.value, event.source_event_id)
        with self._lock:
            if key in self._source_index:
                return False
            self._source_index[key] = event.event_id
            self._events[event.event_id] = event
            return True

    def get_event(self, event_id: str) -> EventRecord:
        return self._events[event_id]

    def has_source_event(self, source: str, source_event_id: str) -> bool:
        return (source, source_event_id) in self._source_index

    def get_event_by_source(self, source: str, source_event_id: str) -> EventRecord | None:
        event_id = self._source_index.get((source, source_event_id))
        return self._events[event_id] if event_id else None

    def list_events(self) -> list[EventRecord]:
        return list(self._events.values())

    def append_suggestion(self, suggestion: MergeSuggestion) -> None:
        with self._lock:
            pair = tuple(
                sorted((suggestion.left_event_id, suggestion.right_event_id))
            )
            if pair in self._suggestion_pairs:
                return
            self._suggestion_pairs.add(pair)
            self._suggestions.append(suggestion)

    def list_suggestions(self) -> list[MergeSuggestion]:
        return list(self._suggestions)

    # ------------------------------------------------------------ 关联组
    def put_group(self, group: CorrelationGroup, base_version: int | None = None) -> None:
        with self._lock:
            held = self._groups.get(group.group_id)
            if held is None:
                if base_version not in (None, 0):
                    raise ConcurrencyConflict("关联组已被其他操作建立")
                self._groups[group.group_id] = _Versioned(group, group.version)
                return
            if held.version != base_version:
                raise ConcurrencyConflict("关联组已被并发裁决，请刷新后重试")
            self._groups[group.group_id] = _Versioned(group, group.version)

    def get_group(self, group_id: str) -> CorrelationGroup:
        return self._groups[group_id].value

    def get_group_version(self, group_id: str) -> int:
        return self._groups[group_id].version

    def list_groups(self) -> list[CorrelationGroup]:
        return [v.value for v in self._groups.values()]

    def append_decision(self, decision: CorrelationDecision) -> None:
        with self._lock:
            self._decisions.append(decision)
            self._event_decision[decision.event_id] = decision

    def list_decisions(self) -> list[CorrelationDecision]:
        return list(self._decisions)

    def decision_of(self, event_id: str) -> CorrelationDecision | None:
        return self._event_decision.get(event_id)

    # ------------------------------------------------------------ 工单
    def put_task(self, task: Task, base_version: int | None = None) -> Task:
        """比较并交换写入工单。返回写入后的工单。"""
        with self._lock:
            held = self._tasks.get(task.case_id)
            if held is None:
                if base_version is not None and base_version != 0:
                    raise ConcurrencyConflict("工单状态已变化，请刷新后重试")
                self._tasks[task.case_id] = _Versioned(task, task.version)
                return task
            if base_version is not None and held.version != base_version:
                raise ConcurrencyConflict(
                    f"工单{task.case_id}已被并发操作更新"
                    f"（基层领取、升级督办或预警归并同时发生）"
                )
            self._tasks[task.case_id] = _Versioned(task, task.version)
            return task

    def get_task(self, case_id: str) -> Task:
        return self._tasks[case_id].value

    def get_task_version(self, case_id: str) -> int:
        return self._tasks[case_id].version

    def list_tasks(self) -> list[Task]:
        return [v.value for v in self._tasks.values()]

    def map_event_to_case(self, event_id: str, case_id: str) -> None:
        with self._lock:
            self._event_case[event_id] = case_id

    def case_of_event(self, event_id: str) -> str | None:
        return self._event_case.get(event_id)

    # ------------------------------------------------------------ 责任链
    def append_link(self, link: ResponsibilityLink) -> None:
        with self._lock:
            # 单链校验：前驱必须是当前末环；同一环序号不可重复
            if self._links:
                tail = self._links_by_case(link.case_id)
                if tail:
                    last = tail[-1]
                    if link.predecessor_seq != last.seq:
                        raise ConcurrencyConflict(
                            f"责任链分叉：末环为{last.seq}，"
                            f"收到的新环却指向前驱{link.predecessor_seq}"
                        )
            self._links.append(link)

    def _links_by_case(self, case_id: str) -> list[ResponsibilityLink]:
        return [link for link in self._links if link.case_id == case_id]

    def list_links(self, case_id: str | None = None) -> list[ResponsibilityLink]:
        if case_id is None:
            return list(self._links)
        return self._links_by_case(case_id)

    def append_action(self, action: ResponsibilityAction) -> None:
        with self._lock:
            self._actions.append(action)

    def replace_action(self, updated: ResponsibilityAction) -> None:
        """措施状态推进（如 pending→done）时原位替换，下令时刻永不改变。"""
        with self._lock:
            for idx, item in enumerate(self._actions):
                if item.action_id == updated.action_id:
                    if updated.ordered_at != item.ordered_at:
                        raise GovernanceError("措施下令时刻不可改写")
                    self._actions[idx] = updated
                    return
            raise KeyError(updated.action_id)

    def get_action(self, action_id: str) -> ResponsibilityAction:
        for item in self._actions:
            if item.action_id == action_id:
                return item
        raise KeyError(action_id)

    def list_actions(self, case_id: str | None = None) -> list[ResponsibilityAction]:
        if case_id is None:
            return list(self._actions)
        return [a for a in self._actions if a.case_id == case_id]

    def append_escalation(self, escalation: Escalation) -> None:
        with self._lock:
            self._escalations.append(escalation)

    def list_escalations(self, case_id: str | None = None) -> list[Escalation]:
        if case_id is None:
            return list(self._escalations)
        return [e for e in self._escalations if e.case_id == case_id]

    def append_false_request(self, request: FalseReviewRequest) -> None:
        with self._lock:
            self._false_requests.append(request)

    def list_false_requests(self) -> list[FalseReviewRequest]:
        return list(self._false_requests)

    def replace_false_request(self, request_id: str, reviewed: FalseReviewRequest) -> None:
        with self._lock:
            for idx, item in enumerate(self._false_requests):
                if item.request_id == request_id:
                    self._false_requests[idx] = reviewed
                    return
            raise KeyError(request_id)

    def append_transfer(self, packet: TransferPacket) -> None:
        with self._lock:
            self._transfers.append(packet)

    def list_transfers(self) -> list[TransferPacket]:
        return list(self._transfers)

    def append_reevaluation(self, reeval: Reevaluation) -> None:
        with self._lock:
            self._reevals.append(reeval)

    def list_reevaluations(self, case_id: str | None = None) -> list[Reevaluation]:
        if case_id is None:
            return list(self._reevals)
        return [r for r in self._reevals if r.case_id == case_id]

    # ------------------------------------------------------------ 核验结论
    def record_verification(self, event_id: str, result: VerificationResult) -> None:
        with self._lock:
            self._verifications.setdefault(event_id, []).append(result)

    def list_verifications(self, event_id: str | None = None) -> list[VerificationResult]:
        if event_id is None:
            return [r for rs in self._verifications.values() for r in rs]
        return list(self._verifications.get(event_id, ()))

    # ------------------------------------------------------------ 快照
    def snapshot(self) -> dict[str, Any]:
        """导出全部追加记录，供停机持久化。记录以数据类形式保存。"""
        with self._lock:
            return {
                "seq": self._seq,
                "wall": self._wall,
                "events": list(self._events.values()),
                "suggestions": list(self._suggestions),
                "groups": [(g.value, g.version) for g in self._groups.values()],
                "decisions": list(self._decisions),
                "tasks": [(t.value, t.version) for t in self._tasks.values()],
                "links": list(self._links),
                "actions": list(self._actions),
                "escalations": list(self._escalations),
                "false_requests": list(self._false_requests),
                "transfers": list(self._transfers),
                "reevaluations": list(self._reevals),
                "verifications": {k: list(v) for k, v in self._verifications.items()},
                "event_case": dict(self._event_case),
            }

    def restore(self, snap: dict[str, Any]) -> None:
        """从快照重建。停机前未完成的逾期任务与待复核误报随之重现。"""
        with self._lock:
            self._seq = snap["seq"]
            self._wall = snap.get("wall", 0)
            self._events = {e.event_id: e for e in snap["events"]}
            self._source_index = {
                (e.source.value, e.source_event_id): e.event_id for e in snap["events"]
            }
            self._suggestions = list(snap["suggestions"])
            self._suggestion_pairs = {
                tuple(sorted((s.left_event_id, s.right_event_id)))
                for s in self._suggestions
            }
            self._groups = {
                g.group_id: _Versioned(g, ver) for g, ver in snap["groups"]
            }
            self._decisions = list(snap["decisions"])
            self._event_decision = {d.event_id: d for d in self._decisions}
            self._tasks = {t.case_id: _Versioned(t, ver) for t, ver in snap["tasks"]}
            self._links = list(snap["links"])
            self._actions = list(snap["actions"])
            self._escalations = list(snap["escalations"])
            self._false_requests = list(snap["false_requests"])
            self._transfers = list(snap["transfers"])
            self._reevals = list(snap["reevaluations"])
            self._verifications = {
                k: list(v) for k, v in snap.get("verifications", {}).items()
            }
            self._event_case = dict(snap["event_case"])


def with_bumped_version(task: Task, **changes: Any) -> Task:
    """工单状态推进的小工具：版本号加一，刷新末序号。"""
    changes.setdefault("version", task.version + 1)
    return replace(task, **changes)
