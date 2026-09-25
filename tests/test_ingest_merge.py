"""事件接入、原样留存与人工归并裁决测试。

覆盖：
- 所有来源事件原样留存，自动核验仅辅助；
- 重复来源事件不生成建议、不新建工单；
- 系统只给关联建议与理由，三态裁决（同一风险/相互独立/误报）；
- 低置信度记录不会自动消失；
- 立案必须人工确认，自动结论不能代替确认。
"""

import unittest

from tests._fixtures import build_service
from src.governance.model import (
    DecisionKind,
    GroupStatus,
    RiskType,
)

BIKE = dict(
    risk_type=RiskType.BIKE_CHARGING,
    location="幸福里3幢楼道",
    algorithm_version="bike-cam-v5",
)


class IngestAndCorrelationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()

    def test_raw_event_is_preserved_verbatim(self) -> None:
        event, created = self.service.ingest(
            **BIKE,
            source="video",
            source_event_id="cam-7/20260925090001",
            occurred_at=900,
            confidence=0.91,
            raw={"frame": "f-7781", "raw_score": 0.912, "note": "原样报文"},
            payload={"zone": "楼道内", "indoor_charging": True},
            event_id="evt-bike-1",
        )
        self.assertTrue(created)
        fetched = self.service.get_event("evt-bike-1")
        self.assertEqual(
            fetched.raw, {"frame": "f-7781", "raw_score": 0.912, "note": "原样报文"}
        )
        self.assertEqual(fetched.ingested_seq, 1)

    def test_duplicate_source_event_creates_nothing(self) -> None:
        kwargs = dict(
            **BIKE,
            source="video",
            source_event_id="cam-7/dup-01",
            occurred_at=900,
            confidence=0.9,
        )
        first, created1 = self.service.ingest(**kwargs)
        second, created2 = self.service.ingest(**kwargs)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertIs(first, second)
        self.assertEqual(self.service.list_open_suggestions(), [])
        self.assertEqual(len(self.service.list_tasks()), 0)

    def test_multisource_triggers_suggestion_with_reasons(self) -> None:
        self.service.ingest(
            **BIKE,
            source="current",
            source_event_id="meter-2/20260925090005",
            occurred_at=905,
            confidence=0.88,
            event_id="evt-bike-a",
            payload={"indoor_charging": True},
        )
        self.service.ingest(
            **BIKE,
            source="video",
            source_event_id="cam-7/20260925090001",
            occurred_at=900,
            confidence=0.91,
            event_id="evt-bike-b",
            payload={"indoor_charging": True},
        )
        suggestions = self.service.list_open_suggestions()
        self.assertEqual(len(suggestions), 1)
        suggestion = suggestions[0]
        self.assertGreaterEqual(suggestion.score, 0.6)
        joined = "".join(suggestion.reasons)
        self.assertIn("位置相同", joined)
        self.assertIn("来源互补", joined)
        self.assertIn("同一算法版本", joined)

    def test_low_confidence_record_is_kept_not_dropped(self) -> None:
        self.service.ingest(
            risk_type=RiskType.BIKE_CHARGING,
            source="manual",
            source_event_id="hotline-20260925-18",
            occurred_at=1000,
            confidence=0.33,  # 置信度低，但不得直接消失
            location="安居苑车棚",
            event_id="evt-low-1",
            payload={"zone": "指定充电桩", "indoor_charging": False},
        )
        self.service.ingest(
            risk_type=RiskType.BIKE_CHARGING,
            source="video",
            source_event_id="cam-9/0007",
            occurred_at=1000,
            confidence=0.83,
            location="安居苑车棚",
            algorithm_version="bike-cam-v5",
            event_id="evt-low-2",
            payload={"indoor_charging": False, "blocks_escape": False},
        )
        self.assertIsNotNone(self.service.get_event("evt-low-1"))
        suggestions = self.service.list_open_suggestions()
        self.assertEqual(len(suggestions), 1)  # 低置信记录同样进入人工裁决视野

    def test_decision_flow_same_independent_false(self) -> None:
        ids = self._ingest_three_sources_same_risk()
        group = self.service.list_groups()[0]
        self.assertEqual(group.status, GroupStatus.OPEN)
        anchor = group.anchor_event_id
        members = [eid for eid in group.member_event_ids if eid != anchor]

        # 裁为同一风险必须指定已存在的工单
        with self.assertRaisesRegex(Exception, "同一风险必须指定"):
            self.service.decide_correlation(
                "analyst-li", group.group_id, members[0], DecisionKind.SAME_RISK
            )

        task = self.service.file_case(
            "mgr-zhao",
            event_id=anchor,
            responsible_unit_code="street-station",
            on_site_deadline=3600,
            confirm=True,
        )
        self.service.decide_correlation(
            "analyst-li",
            group.group_id,
            members[0],
            DecisionKind.SAME_RISK,
            target_case_id=task.case_id,
        )
        # 同一风险：证据并入，不新建工单
        self.assertEqual(len(self.service.list_tasks()), 1)
        self.assertEqual(len(self.service.get_task(task.case_id).linked_event_ids), 2)

        # 第二条裁为相互独立，另案
        self.service.decide_correlation(
            "analyst-li", group.group_id, members[1], DecisionKind.INDEPENDENT
        )
        self.assertEqual(self.service.list_groups()[0].status, GroupStatus.RESOLVED)
        another = self.service.file_case(
            "mgr-zhao",
            event_id=members[1],
            responsible_unit_code="street-station",
            on_site_deadline=3600,
            confirm=True,
        )
        self.assertNotEqual(another.case_id, task.case_id)
        self.assertEqual(ids, {"evt-x1", "evt-x2", "evt-x3"})

    def test_false_alarm_decision_keeps_event_but_blocks_filing(self) -> None:
        self.service.ingest(
            risk_type=RiskType.BIKE_CHARGING,
            source="video",
            source_event_id="cam-1/1",
            occurred_at=500,
            confidence=0.7,
            location="北苑2幢",
            algorithm_version="bike-cam-v5",
            event_id="evt-fa",
            payload={"indoor_charging": True},
        )
        self.service.ingest(
            risk_type=RiskType.BIKE_CHARGING,
            source="manual",
            source_event_id="line-1/1",
            occurred_at=500,
            confidence=0.6,
            location="北苑2幢",
            event_id="evt-fa-2",
            payload={},
        )
        group = self.service.list_groups()[0]
        non_anchor = next(eid for eid in group.member_event_ids if eid != group.anchor_event_id)
        self.service.decide_correlation(
            "analyst-li",
            group.group_id,
            non_anchor,
            DecisionKind.FALSE_ALARM,
            note="回放确认是雨伞投影",
        )
        # 事件仍原样留存
        self.assertIsNotNone(self.service.get_event(non_anchor))
        with self.assertRaisesRegex(Exception, "算法误报"):
            self.service.file_case(
                "mgr-zhao",
                event_id=non_anchor,
                responsible_unit_code="street-station",
                on_site_deadline=3600,
                confirm=True,
            )

    def test_only_analyst_decides_and_no_auto_filing(self) -> None:
        self.service.ingest(
            **BIKE,
            source="video",
            source_event_id="cam-8/1",
            occurred_at=700,
            confidence=0.99,
            event_id="evt-auto",
            payload={"indoor_charging": True, "blocks_escape": True},
        )
        # 自动核验给出明确违规，但没有任何工单被自动建立
        self.assertEqual(self.service.list_tasks(), [])
        verification = self.service.verification_of("evt-auto")[0]
        self.assertTrue(verification.assists_filing)
        self.assertFalse(verification.passed)
        # 未显式人工确认，拒绝立案
        with self.assertRaisesRegex(Exception, "人工显式确认"):
            self.service.file_case(
                "mgr-zhao",
                event_id="evt-auto",
                responsible_unit_code="street-station",
                on_site_deadline=3600,
            )
        # 基层人员无权做关联裁决
        with self.assertRaisesRegex(Exception, "无权"):
            self.service.decide_correlation(
                "front-qian", "grp-x", "evt-auto", DecisionKind.SAME_RISK
            )

    def _ingest_three_sources_same_risk(self) -> set[str]:
        specs = [
            ("video", "cam-1/202609251", 900, "evt-x1", 0.9),
            ("current", "meter-1/202609251", 905, "evt-x2", 0.85),
            ("manual", "hotline-20260925-1", 910, "evt-x3", 0.8),
        ]
        ids: set[str] = set()
        for source, seid, ts, eid, conf in specs:
            _, created = self.service.ingest(
                **BIKE,
                source=source,
                source_event_id=seid,
                occurred_at=ts,
                confidence=conf,
                event_id=eid,
                payload={"indoor_charging": True},
            )
            self.assertTrue(created)
            ids.add(eid)
        return ids


if __name__ == "__main__":
    unittest.main()
