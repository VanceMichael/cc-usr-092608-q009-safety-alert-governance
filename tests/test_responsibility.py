"""处置责任链、并发防分叉、移送最小披露、迟到数据不倒签测试。"""

import concurrent.futures
import unittest

from tests._fixtures import FIRE, WATER, build_service
from src.governance import disclosure
from src.governance.model import (
    ActionKind,
    ActionStatus,
    AuthorizationError,
    CaseStatus,
    DecisionKind,
    RiskType,
)


class ResponsibilityChainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()
        self.event, _ = self.service.ingest(
            risk_type=RiskType.BIKE_CHARGING,
            source="video",
            source_event_id="cam-1/case-1",
            occurred_at=100,
            confidence=0.9,
            location="幸福里3幢楼道",
            algorithm_version="bike-cam-v5",
            raw={"resident_name": "张某某", "resident_phone": "13900000000",
                 "frame": "f-1"},
            payload={"indoor_charging": True, "blocks_escape": True,
                     "welder_name": "无", "license_plate": "浙A00000"},
            sensitive=True,
            event_id="evt-case-1",
        )
        self.task = self.service.file_case(
            "mgr-zhao",
            event_id="evt-case-1",
            responsible_unit_code="street-station",
            on_site_deadline=1000,
            confirm=True,
        )
        self.case_id = self.task.case_id

    def test_single_claimant_and_unique_current_unit(self) -> None:
        claimed = self.service.claim_case("front-qian", self.case_id)
        self.assertEqual(claimed.status, CaseStatus.IN_HAND)
        self.assertEqual(claimed.assignee, "front-qian")
        self.assertEqual(claimed.current_unit.code, "street-station")

        with self.assertRaisesRegex(Exception, "已由"):
            self.service.claim_case("front-sun", self.case_id)
        with self.assertRaises(AuthorizationError):
            # 保险机构人员属于外单位，不能领取街道应急管理站的工单
            self.service.claim_case("insurer-chen", self.case_id)

    def test_concurrent_claim_escalate_merge_never_forks_chain(self) -> None:
        # 再造两个同地事件供分析人员并发归并
        e2, _ = self.service.ingest(
            risk_type=RiskType.BIKE_CHARGING,
            source="current",
            source_event_id="meter-1/case-1",
            occurred_at=105,
            confidence=0.86,
            location="幸福里3幢楼道",
            algorithm_version="bike-cam-v5",
            event_id="evt-case-2",
            payload={"indoor_charging": True},
        )
        group = self.service.list_groups()[0]

        results: dict[str, object] = {}

        def claim() -> None:
            try:
                results["claim"] = self.service.claim_case("front-qian", self.case_id)
            except Exception as exc:  # noqa: BLE001
                results["claim"] = exc

        def claim_rival() -> None:
            try:
                results["claim2"] = self.service.claim_case("front-sun", self.case_id)
            except Exception as exc:  # noqa: BLE001
                results["claim2"] = exc

        def escalate() -> None:
            try:
                results["escalate"] = self.service.escalate(
                    "mgr-zhao", self.case_id, "临近晚间充电高峰", level=1
                )
            except Exception as exc:  # noqa: BLE001
                results["escalate"] = exc

        def merge() -> None:
            try:
                results["merge"] = self.service.decide_correlation(
                    "analyst-li", group.group_id, "evt-case-2",
                    DecisionKind.SAME_RISK,
                    target_case_id=self.case_id,
                )
            except Exception as exc:  # noqa: BLE001
                results["merge"] = exc

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(claim), pool.submit(claim_rival),
                       pool.submit(escalate), pool.submit(merge)]
            concurrent.futures.wait(futures)

        # 恰有一人领取成功
        successful_claims = [
            v for k, v in results.items()
            if k.startswith("claim") and not isinstance(v, Exception)
        ]
        self.assertEqual(len(successful_claims), 1)
        # 升级与归并成功
        self.assertNotIsInstance(results["escalate"], Exception)
        self.assertNotIsInstance(results["merge"], Exception)

        # 责任链仍是一条单链，且唯一责任人不变
        chain = self.service.responsibility_chain(self.case_id)
        self.assertGreaterEqual(len(chain), 4)
        predecessor = None
        for link in chain:
            self.assertEqual(link.predecessor_seq, predecessor)
            predecessor = link.seq
        task = self.service.get_task(self.case_id)
        self.assertIn("evt-case-2", task.linked_event_ids)
        self.assertIsNotNone(task.assignee)
        self.assertEqual(task.current_unit.code, "street-station")

    def test_actions_follow_fixed_order_and_release_needs_review(self) -> None:
        self.service.claim_case("front-qian", self.case_id)
        self.service.report_on_site("front-qian", self.case_id)

        # 不能跳过临时控制直接整改
        with self.assertRaisesRegex(Exception, "处置顺序"):
            self.service.order_action("front-qian", self.case_id, ActionKind.RECTIFY)

        a1 = self.service.order_action(
            "front-qian", self.case_id, ActionKind.TEMP_CONTROL, content="切断飞线电源"
        )
        self.service.complete_action("front-qian", a1.action_id)

        a2 = self.service.order_action(
            "front-qian", self.case_id, ActionKind.RECTIFY, content="搬入集中充电棚"
        )
        self.service.complete_action("front-qian", a2.action_id)

        # 复核未完成不能解除
        a3 = self.service.order_action("front-qian", self.case_id, ActionKind.REVIEW)
        with self.assertRaisesRegex(Exception, "复核未完成"):
            self.service.order_action("mgr-zhao", self.case_id, ActionKind.RELEASE)
        self.service.complete_action("front-qian", a3.action_id)
        self.assertEqual(
            self.service.get_task(self.case_id).status, CaseStatus.PENDING_REVIEW
        )

        # 基层人员不能解除，须管理人员确认
        with self.assertRaises(AuthorizationError):
            self.service.order_action("front-qian", self.case_id, ActionKind.RELEASE)
        a4 = self.service.order_action(
            "mgr-zhao", self.case_id, ActionKind.RELEASE, content="复核通过，解除管控"
        )
        self.service.complete_action("mgr-zhao", a4.action_id)
        self.assertEqual(self.service.get_task(self.case_id).status, CaseStatus.RESOLVED)

        kinds = [a.kind for a in self.service.actions_of(self.case_id)]
        self.assertEqual(
            kinds,
            [ActionKind.TEMP_CONTROL, ActionKind.RECTIFY, ActionKind.REVIEW, ActionKind.RELEASE],
        )

    def test_overdue_reappears_after_recovery(self) -> None:
        # 立案后无人到场，时钟越过到场期限
        self.service.advance_clock(1001)
        overdue = self.service.overdue_tasks()
        self.assertEqual([t.case_id for t in overdue], [self.case_id])

        snap = self.service.snapshot()
        recovered = type(self.service).restore(snap)
        recovered.advance_clock(10)
        self.assertEqual(
            [t.case_id for t in recovered.overdue_tasks()], [self.case_id]
        )


class TransferDisclosureTest(unittest.TestCase):
    def test_transfer_packet_is_minimal_and_redacted(self) -> None:
        service = build_service()
        event, _ = service.ingest(
            risk_type=RiskType.BIKE_CHARGING,
            source="video",
            source_event_id="cam-9/t1",
            occurred_at=100,
            confidence=0.9,
            location="南苑1幢",
            algorithm_version="bike-cam-v5",
            raw={"resident_name": "住户姓名", "resident_phone": "139******",
                 "employee_name": "从业人员姓名"},
            payload={
                "indoor_charging": True,
                "blocks_escape": True,
                "zone": "楼道",
                "resident_name": "住户姓名",
                "welder_name": "工人姓名",
                "license_plate": "浙A12345",
                "irreparable_internal_note": "内部研判草稿",
            },
            sensitive=True,
            event_id="evt-tx",
        )
        task = service.file_case(
            "mgr-zhao",
            event_id="evt-tx",
            responsible_unit_code="street-station",
            on_site_deadline=900,
            confirm=True,
        )
        packet = service.transfer_case(
            "mgr-zhao", task.case_id,
            to_unit_code=FIRE.code,
            purpose=disclosure.PURPOSE_FIRE_ENFORCEMENT,
            reason="室内充电堵占通道，需消防执法核查",
        )

        self.assertEqual(packet.to_unit.code, FIRE.code)
        self.assertEqual(len(packet.evidence), 1)
        view = packet.evidence[0]
        self.assertNotIn("raw", view)
        self.assertNotIn("sensitive", view)
        # 仅白名单字段
        self.assertEqual(
            set(view["payload"].keys()),
            {"zone", "indoor_charging", "blocks_escape"},
        )
        flattened = repr(packet.evidence)
        self.assertNotIn("住户姓名", flattened)
        self.assertNotIn("139******", flattened)
        self.assertNotIn("浙A12345", flattened)
        self.assertNotIn("内部研判草稿", flattened)
        joined = ",".join(packet.redacted_fields)
        self.assertIn("payload.resident_name", joined)
        self.assertIn("raw", joined)

        # 移送后当前责任单位唯一且变更，本方工单标记移送
        moved = service.get_task(task.case_id)
        self.assertEqual(moved.status, CaseStatus.TRANSFERRED)
        self.assertEqual(moved.current_unit.code, FIRE.code)

    def test_purpose_mismatch_rejects_transfer(self) -> None:
        service = build_service()
        service.ingest(
            risk_type=RiskType.WATERLOGGING,
            source="weather",
            source_event_id="g-1/w",
            occurred_at=100,
            confidence=0.8,
            location="下穿立交",
            event_id="evt-w",
            payload={"depth_cm": 30},
        )
        task = service.file_case(
            "mgr-zhao",
            event_id="evt-w",
            responsible_unit_code="street-station",
            on_site_deadline=900,
            confirm=True,
        )
        # 积涝事件不能按消防执法目的移送（白名单不匹配）
        with self.assertRaisesRegex(Exception, "不匹配"):
            service.transfer_case(
                "mgr-zhao", task.case_id,
                to_unit_code=FIRE.code,
                purpose=disclosure.PURPOSE_FIRE_ENFORCEMENT,
                reason="试错",
            )
        packet = service.transfer_case(
            "mgr-zhao", task.case_id,
            to_unit_code=WATER.code,
            purpose=disclosure.PURPOSE_FLOOD_CONTROL,
            reason="积水达危险阈值，请调度抽排",
        )
        self.assertEqual(packet.evidence[0]["payload"], {"depth_cm": 30.0})


class LateDataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()

    def test_late_weather_reevaluates_but_never_backdates_orders(self) -> None:
        service = self.service
        service.ingest(
            risk_type=RiskType.WATERLOGGING,
            source="video",
            source_event_id="cam-w/1",
            occurred_at=100,
            confidence=0.8,
            location="下穿立交",
            event_id="evt-w1",
            payload={"depth_cm": 30},
        )
        task = service.file_case(
            "mgr-zhao",
            event_id="evt-w1",
            responsible_unit_code="street-station",
            on_site_deadline=500,
            confirm=True,
        )
        service.claim_case("front-qian", task.case_id)
        service.report_on_site("front-qian", task.case_id)
        order = service.order_action(
            "front-qian", task.case_id, ActionKind.TEMP_CONTROL,
            content="封路、布设围挡",
        )
        first_ordered_at = order.ordered_at
        first_ordered_seq = order.ordered_seq

        # 时钟推进后，一条更早时刻的气象数据迟到
        service.advance_clock(200)
        reeval = service.ingest_late_followup(
            "front-qian", task.case_id,
            source="weather",
            source_event_id="weather-bureau/late-7",
            occurred_at=50,  # 数据发生在现场命令之前，但现在才到
            payload={"depth_cm": 8},
        )
        self.assertTrue(reeval.kept_history)
        self.assertIn("回落", "".join(reeval.changes))

        # 历史命令的下令时刻与序号均未被改写、未被伪造提前
        unchanged = self.service.get_action(order.action_id)
        self.assertEqual(unchanged.ordered_at, first_ordered_at)
        self.assertEqual(unchanged.ordered_seq, first_ordered_seq)
        self.assertEqual(unchanged.status, ActionStatus.PENDING)

        # 相同迟到来源事件再次到达：不新建工单、不重复重评
        with self.assertRaisesRegex(Exception, "不重复重评"):
            service.ingest_late_followup(
                "front-qian", task.case_id,
                source="weather",
                source_event_id="weather-bureau/late-7",
                occurred_at=50,
                payload={"depth_cm": 8},
            )
        self.assertEqual(len(service.list_tasks()), 1)

        # 旧核验结论保留（当时 BREACH），新结论另存（现在 PASS）
        old_results = service.verification_of("evt-w1")
        self.assertEqual(len(old_results), 1)
        new_results = service.verification_of(reeval.trigger_event_id)
        self.assertTrue(new_results[0].passed)


if __name__ == "__main__":
    unittest.main()
