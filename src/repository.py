"""事件溯源仓储。

所有状态变化只以**追加事件**形式落盘（JSONL，每行一个事件），
来源事件原样留存、不可修改、不可删除。停机恢复时重放日志即可重建：
案件、工单、误报复核、去重索引、责任链版本号全部还原，逾期任务与
待复核误报随之重新出现。

并发控制采用责任链版本号（乐观锁）：领取、升级、合并、转派、追加
阶段都会令版本 +1，客户端须带所见版本提交，版本不符抛
``ChainConflictError``——基层领取、管理升级、分析合并同时发生时
只有一个能成功，防止责任链分叉。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import (
    CaseStatus,
    CaseView,
    ChainConflictError,
    CorrelationSuggestion,
    FalseAlarmView,
    ReviewStatus,
    Source,
    SourceEvent,
    Scenario,
    StageRecord,
    VerificationResult,
    ActionKind,
    WorkOrderView,
    WorkStage,
)


@dataclass(frozen=True)
class DomainEvent:
    seq: int
    type: str
    ts: str
    data: dict[str, Any]


# 需要做责任链版本校验的事件类型
_VERSIONED_TYPES = frozenset(
    {
        "case.decided",
        "work_order.created",
        "work_order.assigned",
        "work_order.escalated",
        "work_order.transferred",
        "stage.appended",
        "false_alarm.proposed",
    }
)


class Repository:
    """追加式仓储。``path`` 为 None 时仅在内存中运行（测试用）。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.RLock()
        self._seq = 0
        self._log: list[DomainEvent] = []

        # 重放得到的状态
        self._events: dict[str, SourceEvent] = {}
        self._cases: dict[str, dict[str, Any]] = {}
        self._work_orders: dict[str, dict[str, Any]] = {}
        self._reviews: dict[str, FalseAlarmView] = {}
        self._dedupe: dict[str, str] = {}
        self._maintainers: dict[str, frozenset[str]] = {}

        if self._path and self._path.exists():
            self._replay()

    # ------------------------------------------------------------ 基础

    @property
    def exclusive(self) -> threading.RLock:
        """校验+追加必须持有的排他锁（配合 with 使用）。"""
        return self._lock

    def _append(self, etype: str, data: dict[str, Any]) -> DomainEvent:
        expected = data.get("expected_chain_version")
        if etype in _VERSIONED_TYPES:
            current = self._chain_version_of(etype, data)
            if expected is not None and expected != current:
                raise ChainConflictError(
                    f"责任链已被他人更新（所见版本 {expected}，当前版本 {current}）"
                )

        self._seq += 1
        event = DomainEvent(seq=self._seq, type=etype, ts=data.get("at", ""), data=data)
        self._log.append(event)
        if self._path is not None:
            line = json.dumps(
                {"seq": event.seq, "type": event.type, "ts": event.ts, "data": event.data},
                ensure_ascii=False,
            )
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
        self._apply(event)
        return event

    def append(self, etype: str, data: dict[str, Any]) -> DomainEvent:
        """在排他锁内追加事件。"""
        with self._lock:
            return self._append(etype, data)

    def _chain_version_of(self, etype: str, data: dict[str, Any]) -> int:
        if etype == "case.decided" or etype == "false_alarm.proposed":
            case = self._cases.get(data["case_id"])
            return case["chain_version"] if case else 0
        if etype == "work_order.created":
            case = self._cases.get(data["case_id"])
            return case["chain_version"] if case else 0
        wo = self._work_orders.get(data.get("work_order_id", ""))
        return wo["chain_version"] if wo else 0

    # ------------------------------------------------------------ 重放

    def _replay(self) -> None:
        assert self._path is not None
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                event = DomainEvent(
                    seq=raw["seq"], type=raw["type"], ts=raw["ts"], data=raw["data"]
                )
                self._seq = max(self._seq, event.seq)
                self._apply(event)

    def _apply(self, event: DomainEvent) -> None:
        d = event.data
        kind = event.type

        if kind == "event.received":
            source_event = _source_event_from_dict(d["event"])
            self._events[source_event.event_id] = source_event
            self._dedupe[source_event.dedupe_key] = source_event.event_id

        elif kind == "algorithm.registered":
            self._maintainers[d["version"]] = frozenset(d["maintainers"])

        elif kind == "case.opened":
            self._cases[d["case_id"]] = {
                "case_id": d["case_id"],
                "scenario": Scenario(d["scenario"]),
                "status": CaseStatus.OPEN,
                "event_ids": list(d["event_ids"]),
                "suggestions": [],
                "decision": None,
                "decided_by": None,
                "decided_at": None,
                "decision_reason": "",
                "merged_into": None,
                "verifications": [],
                "work_order_id": None,
                "false_alarm": None,
                "chain_version": 0,
                "opened_at": d["at"],
            }

        elif kind == "correlation.suggested":
            case = self._cases[d["case_id"]]
            case["suggestions"] = [
                CorrelationSuggestion(
                    candidate_case_id=s["candidate_case_id"],
                    score=s["score"],
                    reasons=tuple(s["reasons"]),
                )
                for s in d["suggestions"]
            ]

        elif kind == "case.decided":
            case = self._cases[d["case_id"]]
            case["decision"] = d["decision"]
            case["decided_by"] = d["by"]
            case["decided_at"] = d["at"]
            case["decision_reason"] = d.get("reason", "")
            case["chain_version"] += 1
            if d["decision"] == "same_risk":
                case["status"] = CaseStatus.MERGED
                case["merged_into"] = d["target_case_id"]
                target = self._cases[d["target_case_id"]]
                for event_id in case["event_ids"]:
                    if event_id not in target["event_ids"]:
                        target["event_ids"].append(event_id)
                target["chain_version"] += 1
            elif d["decision"] == "false_alarm":
                case["status"] = CaseStatus.DISMISSED
            else:  # independent：确认立案资格，工单另行创建
                case["status"] = CaseStatus.ACTIVE

        elif kind == "verification.recorded":
            case = self._cases[d["case_id"]]
            case["verifications"].append(_verification_from_dict(d["result"]))

        elif kind == "work_order.created":
            case = self._cases[d["case_id"]]
            case["status"] = CaseStatus.ACTIVE
            case["work_order_id"] = d["work_order_id"]
            case["chain_version"] += 1
            self._work_orders[d["work_order_id"]] = {
                "work_order_id": d["work_order_id"],
                "case_id": d["case_id"],
                "responsible_unit": d["responsible_unit"],
                "arrive_deadline": d["arrive_deadline"],
                "status": "active",
                "assignee": None,
                "chain_version": case["chain_version"],
                "stages": [],
                "transfers": [],
                "escalations": [],
                "reevaluations": [],
                "created_at": d["at"],
            }

        elif kind == "work_order.assigned":
            wo = self._work_orders[d["work_order_id"]]
            wo["assignee"] = d["assignee"]
            wo["chain_version"] += 1

        elif kind == "stage.appended":
            wo = self._work_orders[d["work_order_id"]]
            wo["stages"].append(
                StageRecord(
                    stage=WorkStage(d["stage"]),
                    operator=d["operator"],
                    recorded_at=d["at"],
                    detail=d.get("detail", ""),
                    command_issued_at=d.get("command_issued_at"),
                )
            )
            wo["chain_version"] += 1
            if d["stage"] == WorkStage.RESOLUTION.value:
                wo["status"] = "resolved"

        elif kind == "work_order.escalated":
            wo = self._work_orders[d["work_order_id"]]
            wo["status"] = "escalated"
            wo["escalations"].append(dict(d))
            wo["chain_version"] += 1

        elif kind == "work_order.transferred":
            wo = self._work_orders[d["work_order_id"]]
            wo["responsible_unit"] = d["to_unit"]
            wo["assignee"] = None  # 接收单位重新派人，责任单位始终唯一
            wo["status"] = "transferred"
            wo["transfers"].append(dict(d))
            wo["chain_version"] += 1

        elif kind == "measure.reevaluated":
            wo = self._work_orders[d["work_order_id"]]
            wo["reevaluations"].append(dict(d))
            # 再评估只追加后续措施，不动责任链版本，也不改既有命令

        elif kind == "false_alarm.proposed":
            review = FalseAlarmView(
                review_id=d["review_id"],
                case_id=d["case_id"],
                algorithm_version=d["algorithm_version"],
                proposed_by=d["proposed_by"],
                proposed_at=d["at"],
                reason=d["reason"],
            )
            self._reviews[d["review_id"]] = review
            self._cases[d["case_id"]]["false_alarm"] = review
            self._cases[d["case_id"]]["chain_version"] += 1

        elif kind == "false_alarm.reviewed":
            old = self._reviews[d["review_id"]]
            reviewed = FalseAlarmView(
                review_id=old.review_id,
                case_id=old.case_id,
                algorithm_version=old.algorithm_version,
                proposed_by=old.proposed_by,
                proposed_at=old.proposed_at,
                reason=old.reason,
                status=ReviewStatus(d["status"]),
                reviewed_by=d["by"],
                reviewed_at=d["at"],
                review_note=d.get("note", ""),
            )
            self._reviews[d["review_id"]] = reviewed
            case = self._cases[old.case_id]
            case["false_alarm"] = reviewed
            case["chain_version"] += 1
            if reviewed.status is ReviewStatus.APPROVED:
                case["status"] = CaseStatus.DISMISSED
            else:  # rejected：误报被推翻，重新进入立案流程
                case["status"] = CaseStatus.ACTIVE
                case["decision"] = None
                case["decided_by"] = None

        else:
            raise ValueError(f"未知事件类型：{kind}")

    # ------------------------------------------------------------ 查询

    def get_event(self, event_id: str) -> SourceEvent | None:
        return self._events.get(event_id)

    def find_by_dedupe_key(self, dedupe_key: str) -> SourceEvent | None:
        event_id = self._dedupe.get(dedupe_key)
        return self._events.get(event_id) if event_id else None

    def list_events(self) -> list[SourceEvent]:
        return [self._events[k] for k in self._dedupe.values()]

    def get_case(self, case_id: str) -> CaseView | None:
        raw = self._cases.get(case_id)
        if raw is None:
            return None
        return CaseView(
            case_id=raw["case_id"],
            status=raw["status"],
            scenario=raw["scenario"],
            events=tuple(self._events[eid] for eid in raw["event_ids"]),
            suggestions=tuple(raw["suggestions"]),
            decision=raw["decision"],
            decided_by=raw["decided_by"],
            decided_at=raw["decided_at"],
            decision_reason=raw["decision_reason"],
            merged_into=raw["merged_into"],
            verifications=tuple(raw["verifications"]),
            work_order_id=raw["work_order_id"],
            false_alarm=raw["false_alarm"],
            chain_version=raw["chain_version"],
        )

    def list_cases(self, status: CaseStatus | None = None) -> list[CaseView]:
        views = [self.get_case(cid) for cid in self._cases]
        views = [v for v in views if v is not None]
        if status is not None:
            views = [v for v in views if v.status is status]
        return sorted(views, key=lambda v: v.case_id)

    def get_work_order(self, work_order_id: str) -> WorkOrderView | None:
        raw = self._work_orders.get(work_order_id)
        if raw is None:
            return None
        return WorkOrderView(
            work_order_id=raw["work_order_id"],
            case_id=raw["case_id"],
            responsible_unit=raw["responsible_unit"],
            arrive_deadline=raw["arrive_deadline"],
            status=raw["status"],
            assignee=raw["assignee"],
            chain_version=raw["chain_version"],
            stages=tuple(raw["stages"]),
            transfers=tuple(raw["transfers"]),
            escalations=tuple(raw["escalations"]),
            reevaluations=tuple(raw["reevaluations"]),
            created_at=raw["created_at"],
        )

    def list_work_orders(self) -> list[WorkOrderView]:
        return sorted(
            (self.get_work_order(wid) for wid in self._work_orders),
            key=lambda w: w.work_order_id if w else "",
        )  # type: ignore[arg-type]

    def list_pending_reviews(self) -> list[FalseAlarmView]:
        return sorted(
            (r for r in self._reviews.values() if r.status is ReviewStatus.PENDING),
            key=lambda r: r.review_id,
        )

    def maintainers_of(self, version: str) -> frozenset[str]:
        return self._maintainers.get(version, frozenset())

    def raw_log(self) -> list[DomainEvent]:
        """只读访问追加日志（审计/测试用）。"""
        return list(self._log)


# ---------------------------------------------------------------- 序列化


def _source_event_from_dict(d: dict[str, Any]) -> SourceEvent:
    return SourceEvent(
        event_id=d["event_id"],
        source=Source(d["source"]),
        scenario=Scenario(d["scenario"]),
        dedupe_key=d["dedupe_key"],
        observed_at=d["observed_at"],
        received_at=d["received_at"],
        payload=d["payload"],
        algorithm_version=d.get("algorithm_version"),
        location_hint=d.get("location_hint"),
        subject_ref=d.get("subject_ref"),
    )


def source_event_to_dict(event: SourceEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "source": event.source.value,
        "scenario": event.scenario.value,
        "dedupe_key": event.dedupe_key,
        "observed_at": event.observed_at,
        "received_at": event.received_at,
        "payload": event.payload,
        "algorithm_version": event.algorithm_version,
        "location_hint": event.location_hint,
        "subject_ref": event.subject_ref,
    }


def _verification_from_dict(d: dict[str, Any]) -> VerificationResult:
    return VerificationResult(
        scenario=Scenario(d["scenario"]),
        action=ActionKind(d["action"]),
        rule_version=d["rule_version"],
        findings=tuple(d["findings"]),
        checked_at=d["checked_at"],
    )
