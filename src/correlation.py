"""关联建议引擎。

系统只**建议**关联并给出可解释的理由，最终由人工裁决为同一风险、
相互独立或算法误报（裁决在 service 层完成）。建议结果不修改任何
来源事件，也不自动合并工单。

评分因子全部可解释，且保持确定性：分数相同时按案件编号排序，保证
重放与测试稳定。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import CaseView, CaseStatus, CorrelationSuggestion, SourceEvent


# 评分权重（合计不超过 1.0）
W_SCENARIO = 0.35
W_LOCATION = 0.30
W_TIME_NEAR = 0.25    # 15 分钟内
W_TIME_WIDE = 0.12    # 60 分钟内
W_CROSS_SOURCE = 0.10

NEAR_WINDOW = timedelta(minutes=15)
WIDE_WINDOW = timedelta(minutes=60)

# 已驳回/已并入的案件不再作为关联候选
ELIGIBLE_STATUSES = frozenset({CaseStatus.OPEN, CaseStatus.ACTIVE})


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def suggest_for(
    event: SourceEvent, candidates: list[CaseView]
) -> list[CorrelationSuggestion]:
    """为一条新到事件返回排序后的关联建议（可能为空）。"""

    suggestions: list[CorrelationSuggestion] = []
    observed = _parse(event.observed_at)
    for case in candidates:
        if case.status not in ELIGIBLE_STATUSES or not case.events:
            continue
        score = 0.0
        reasons: list[str] = []

        if case.scenario is event.scenario:
            score += W_SCENARIO
            reasons.append("属于同一业务场景")

        anchor = case.events[0]
        if (
            event.location_hint
            and anchor.location_hint
            and event.location_hint == anchor.location_hint
        ):
            score += W_LOCATION
            reasons.append(f"位置线索一致：{event.location_hint}")

        delta = min(
            abs(observed - _parse(other.observed_at)) for other in case.events
        )
        if delta <= NEAR_WINDOW:
            score += W_TIME_NEAR
            reasons.append(f"观测时刻相差 {int(delta.total_seconds() // 60)} 分钟，在 15 分钟内")
        elif delta <= WIDE_WINDOW:
            score += W_TIME_WIDE
            reasons.append(f"观测时刻相差 {int(delta.total_seconds() // 60)} 分钟，在 60 分钟内")

        other_sources = {e.source for e in case.events}
        if event.source not in other_sources:
            score += W_CROSS_SOURCE
            source_names = "、".join(sorted(s.value for s in other_sources))
            reasons.append(f"与既有 {source_names} 来源形成跨源印证")

        if reasons:
            suggestions.append(
                CorrelationSuggestion(
                    candidate_case_id=case.case_id,
                    score=min(round(score, 3), 1.0),
                    reasons=tuple(reasons),
                )
            )

    suggestions.sort(key=lambda s: (-s.score, s.candidate_case_id))
    return suggestions
