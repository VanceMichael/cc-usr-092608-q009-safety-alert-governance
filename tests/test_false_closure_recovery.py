"""误报关闭四眼原则、停机恢复与按算法版本倒查测试。"""

import json
import unittest
from pathlib import Path

from tests._fixtures import build_service
from src.governance.model import (
    Actor,
    ActorRole,
    AuthorizationError,
    RiskType,
)


def _ingest_batch(service, prefix: str, version: str, count: int,
                  location: str = "某小区车棚") -> list[str]:
    ids = []
    for i in range(count):
        eid = f"{prefix}-{i}"
        service.ingest(
            risk_type=RiskType.BIKE_CHARGING,
            source="video",
            source_event_id=f"{prefix}/src-{i}",
            occurred_at=1000 + i,
            confidence=0.42,  # 批量低置信异常
            location=location,
            algorithm_version=version,
            event_id=eid,
            payload={"zone": "画面反光区域"},
        )
        ids.append(eid)
    return ids


class FourEyesFalseClosureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()
        # 一名同时维护 hotwork-v9 的管理人员——不得审批该版本的关闭
        self.service.register_actor(
            Actor("mgr-zheng", "郑管理", ActorRole.MANAGER, "street-station")
        )
        self.service.register_version_maintainer("hotwork-v9", "mgr-zheng")

    def test_version_maintainer_cannot_close_alone(self) -> None:
        ids = _ingest_batch(self.service, "evt-false", "bike-cam-v5", 3)
        request = self.service.request_false_closure(
            "keeper-zhou", event_ids=set(ids), reason="夜间光线变化导致批量误识别"
        )
        self.assertFalse(request.approved)
        self.assertIsNone(request.reviewed_by)

        # 待复核列表中可见，事件原样留存，不会直接消失
        pending = self.service.pending_false_requests()
        self.assertEqual([r.request_id for r in pending], [request.request_id])
        for eid in ids:
            self.assertIsNotNone(self.service.get_event(eid))

        # 维护人本人不能自批（角色也不允许）
        with self.assertRaises(AuthorizationError):
            self.service.review_false_closure("keeper-zhou", request.request_id, True)

        # 同版本维护人若具有管理角色也不能审批
        hot_ids = _ingest_batch(self.service, "evt-hot", "hotwork-v9", 2)
        hot_request = self.service.request_false_closure(
            "keeper-zhou", event_ids=set(hot_ids), reason="火花与晨雾混淆"
        )
        with self.assertRaisesRegex(AuthorizationError, "维护人不能审批"):
            self.service.review_false_closure("mgr-zheng", hot_request.request_id, True)

        # 非维护人的其他管理人员可以四眼复核批准
        approved = self.service.review_false_closure(
            "mgr-zhao", request.request_id, True
        )
        self.assertTrue(approved.approved)
        self.assertEqual(approved.reviewed_by, "mgr-zhao")
        remaining = self.service.pending_false_requests()
        self.assertEqual([r.request_id for r in remaining], [hot_request.request_id])
        # 批准关闭的事件记录依然留存，可追溯
        for eid in ids:
            self.assertIsNotNone(self.service.get_event(eid))

    def test_rejected_closure_stays_for_handling(self) -> None:
        ids = _ingest_batch(self.service, "evt-rej", "bike-cam-v5", 1)
        request = self.service.request_false_closure(
            "keeper-zhou", event_ids=set(ids), reason="疑似算法抖动"
        )
        reviewed = self.service.review_false_closure(
            "mgr-zhao", request.request_id, False
        )
        self.assertFalse(reviewed.approved)
        # 驳回后事件仍可人工确认立案，不会消失
        task = self.service.file_case(
            "mgr-zhao",
            event_id=ids[0],
            responsible_unit_code="street-station",
            on_site_deadline=5000,
            confirm=True,
        )
        self.assertEqual(task.linked_event_ids, frozenset(ids))

    def test_closure_rejects_mixed_versions_and_active_cases(self) -> None:
        v5 = _ingest_batch(self.service, "evt-v5", "bike-cam-v5", 1)
        v9 = _ingest_batch(self.service, "evt-v9", "hotwork-v9", 1)
        with self.assertRaisesRegex(Exception, "同一算法版本"):
            self.service.request_false_closure(
                "keeper-zhou", event_ids=set(v5 + v9), reason="混批"
            )

        # 在办工单内的事件不能被关闭消失
        task = self.service.file_case(
            "mgr-zhao",
            event_id=v9[0],
            responsible_unit_code="street-station",
            on_site_deadline=5000,
            confirm=True,
        )
        with self.assertRaisesRegex(Exception, "仍在处置工单中"):
            self.service.request_false_closure(
                "keeper-zhou", event_ids=set(v9), reason="试图抹掉在办事件"
            )
        self.assertIsNotNone(self.service.get_task(task.case_id))


class RecoveryAndVersionImpactTest(unittest.TestCase):
    def test_overdue_and_pending_false_reappear_after_restart(self) -> None:
        service = build_service()
        service.ingest(
            risk_type=RiskType.BIKE_CHARGING,
            source="video",
            source_event_id="cam-r/1",
            occurred_at=10,
            confidence=0.9,
            location="复兴路2号",
            algorithm_version="bike-cam-v5",
            event_id="evt-r1",
            payload={"indoor_charging": True},
        )
        task = service.file_case(
            "mgr-zhao",
            event_id="evt-r1",
            responsible_unit_code="street-station",
            on_site_deadline=500,
            confirm=True,
        )
        false_ids = _ingest_batch(service, "evt-rf", "bike-cam-v5", 2)
        false_request = service.request_false_closure(
            "keeper-zhou", event_ids=set(false_ids), reason="批量误识别待复核"
        )
        service.advance_clock(600)  # 工单逾期，申请尚未复核 —— 此时停机

        snap = service.snapshot()
        recovered = type(service).restore(snap)

        # 逾期任务重新出现
        overdue = recovered.overdue_tasks(600)
        self.assertEqual([t.case_id for t in overdue], [task.case_id])
        # 待复核误报重新出现
        pending = recovered.pending_false_requests()
        self.assertEqual([r.request_id for r in pending], [false_request.request_id])
        # 身份与版本维护关系恢复，四眼原则继续生效
        with self.assertRaises(AuthorizationError):
            recovered.review_false_closure("keeper-zhou", false_request.request_id, True)
        recovered.review_false_closure("mgr-zhao", false_request.request_id, True)

        # 恢复后相同来源事件再到达，仍不新建工单/事件
        _, created = recovered.ingest(
            risk_type=RiskType.BIKE_CHARGING,
            source="video",
            source_event_id="cam-r/1",
            occurred_at=10,
            confidence=0.9,
            location="复兴路2号",
            algorithm_version="bike-cam-v5",
        )
        self.assertFalse(created)
        self.assertEqual(len(recovered.list_tasks()), 1)

        # 责任链完整恢复且仍是单链
        chain = recovered.responsibility_chain(task.case_id)
        self.assertEqual(chain[0].action, "file")

    def test_version_impact_finds_cohort_of_historical_events(self) -> None:
        service = build_service()
        # bike-cam-v5 批量事件：两条立案、两条申请误报并批准、两条未处理
        all_ids = _ingest_batch(service, "evt-imp", "bike-cam-v5", 6)
        filed_a, filed_b = all_ids[0], all_ids[1]
        false_ids = set(all_ids[2:4])
        # 噪声：另一版本的事件不应被卷入
        _ingest_batch(service, "evt-other", "bike-cam-v4", 2, location="别处")

        service.file_case(
            "mgr-zhao", event_id=filed_a,
            responsible_unit_code="street-station", on_site_deadline=9000,
            confirm=True,
        )
        service.file_case(
            "mgr-zhao", event_id=filed_b,
            responsible_unit_code="street-station", on_site_deadline=9000,
            confirm=True,
        )
        request = service.request_false_closure(
            "keeper-zhou", event_ids=false_ids, reason="同类误识别模式"
        )
        service.review_false_closure("mgr-zhao", request.request_id, True)

        impact = service.version_impact("bike-cam-v5")
        self.assertEqual(impact.event_ids, frozenset(all_ids))
        self.assertEqual(impact.false_event_ids, false_ids)
        self.assertEqual(len(impact.case_ids), 2)
        self.assertIn("倒查", impact.reason)

        # 另一版本不受影响
        other = service.version_impact("bike-cam-v4")
        self.assertEqual(len(other.event_ids), 2)
        self.assertEqual(other.false_event_ids, frozenset())


class SnapshotContractTest(unittest.TestCase):
    def test_snapshot_contains_contract_keys(self) -> None:
        service = build_service()
        service.ingest(
            risk_type=RiskType.BIKE_CHARGING,
            source="video",
            source_event_id="cam-c/1",
            occurred_at=1,
            confidence=0.9,
            location="契约点",
            algorithm_version="bike-cam-v5",
            payload={"indoor_charging": True},
        )
        snap = service.snapshot()
        contract = json.loads(
            Path("contracts/snapshot.schema.json").read_text(encoding="utf-8")
        )
        for key in contract["required"]:
            self.assertIn(key, snap, f"快照缺少契约字段 {key}")
        for key in contract["properties"]["service"]["required"]:
            self.assertIn(key, snap["service"])


if __name__ == "__main__":
    unittest.main()
