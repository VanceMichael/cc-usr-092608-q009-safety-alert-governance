"""停机恢复与按算法版本的系统性误报影响分析。"""

import tempfile
import unittest
from pathlib import Path

from src.models import (
    CaseStatus,
    Decision,
    ReviewStatus,
    Source,
    Scenario,
    WorkStage,
)
from src.repository import Repository
from src.service import SafetyGovernanceService
from tests.helpers import make_event

BAD_VERSION = "ev-video-2026.07"
GOOD_VERSION = "ev-video-2026.08"


class RecoveryAndImpactTest(unittest.TestCase):
    def _build_state(self, path: Path) -> None:
        """模拟停机前的运行状态：一个逾期未解除工单 + 一条待复核误报。"""
        service = SafetyGovernanceService(Repository(path))
        service.register_algorithm(
            BAD_VERSION, {"算法维护人庚"}, at="2026-09-01T00:00:00+08:00"
        )

        # 工单：立案后领取并做了临时控制，但到场期限是 10:00——停机恢复时已逾期
        active = make_event(
            Source.VIDEO, Scenario.EV_CHARGING,
            "2026-09-20T08:00:00+08:00",
            algorithm_version=BAD_VERSION,
            location_hint="幸福里3栋",
        )
        c1 = service.ingest(active).case
        service.decide(
            c1.case_id, Decision.INDEPENDENT, "值班长乙", "风险成立",
            expected_chain_version=0, at="2026-09-20T08:30:00+08:00",
        )
        order = service.file_work_order(
            c1.case_id, "望江街道应急管理站",
            arrive_deadline="2026-09-20T10:00:00+08:00", by="值班长乙",
            expected_chain_version=1, at="2026-09-20T09:00:00+08:00",
        )
        order = service.assign(
            order.work_order_id, "网格员丙", expected_chain_version=2,
            at="2026-09-20T09:10:00+08:00",
        )
        service.append_stage(
            order.work_order_id, WorkStage.TEMP_CONTROL, "网格员丙",
            "断电", expected_chain_version=3,
            at="2026-09-20T09:30:00+08:00",
        )

        # 已确认误报关闭的案件（BAD_VERSION）
        dismissed = make_event(
            Source.VIDEO, Scenario.EV_CHARGING,
            "2026-09-20T08:05:00+08:00",
            algorithm_version=BAD_VERSION,
            event_id="EVT-DISMISSED",
            dedupe_key="video:dismissed",
            location_hint="幸福里5栋",
        )
        c2 = service.ingest(dismissed).case
        rid = service.propose_false_alarm(
            c2.case_id, proposed_by="分析员甲",
            reason="夜间逆光疑似批量误判", expected_chain_version=0,
            at="2026-09-20T08:40:00+08:00",
        )
        service.review_false_alarm(
            rid, reviewed_by="督查员丁", approve=True,
            note="现场无充电行为", at="2026-09-20T09:00:00+08:00",
        )

        # 待复核误报：停机期间悬而未决，恢复后必须重新出现
        pending = make_event(
            Source.VIDEO, Scenario.EV_CHARGING,
            "2026-09-20T08:08:00+08:00",
            algorithm_version=BAD_VERSION,
            event_id="EVT-PENDING",
            dedupe_key="video:pending",
            location_hint="幸福里6栋",
        )
        c3 = service.ingest(pending).case
        service.propose_false_alarm(
            c3.case_id, proposed_by="分析员甲",
            reason="同一批夜间样本", expected_chain_version=0,
            at="2026-09-20T08:45:00+08:00",
        )

        # 新版本产生的无关事件，不应进入 BAD_VERSION 影响面
        fresh = make_event(
            Source.VIDEO, Scenario.EV_CHARGING,
            "2026-09-22T08:00:00+08:00",
            algorithm_version=GOOD_VERSION,
            event_id="EVT-FRESH",
            dedupe_key="video:fresh",
        )
        service.ingest(fresh)

    def test_replay_restores_everything_and_surfaces_overdue_and_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            self._build_state(path)

            # 模拟停机：用同一份日志重新打开仓储
            recovered_repo = Repository(path)
            service = SafetyGovernanceService(recovered_repo)

            self.assertEqual(len(recovered_repo.list_events()), 4)
            overdue = service.list_overdue_orders(
                now="2026-09-21T08:00:00+08:00"
            )
            self.assertEqual(len(overdue), 1)
            order = overdue[0]
            self.assertEqual(order.responsible_unit, "望江街道应急管理站")
            self.assertEqual(order.assignee, "网格员丙")
            self.assertEqual(len(order.stages), 1)
            # 恢复后可继续处置，且仍受阶段顺序与版本号约束
            continued = service.append_stage(
                order.work_order_id, WorkStage.RECTIFICATION, "网格员丙",
                "继续整改", expected_chain_version=4,
                at="2026-09-21T08:30:00+08:00",
            )
            self.assertEqual(len(continued.stages), 2)

            pending = service.list_pending_false_alarms()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].algorithm_version, BAD_VERSION)

            # 维护人登记同样恢复，职责分离在停机后仍然生效
            with self.assertRaises(Exception):
                service.review_false_alarm(
                    pending[0].review_id, reviewed_by="算法维护人庚",
                    approve=True, note="停机后仍不能独自关闭",
                )

    def test_impact_analysis_finds_batch_by_algorithm_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            self._build_state(path)
            service = SafetyGovernanceService(Repository(path))

            impact = service.analyze_algorithm_version_impact(BAD_VERSION)
            self.assertEqual(len(impact["events"]), 3)
            self.assertEqual(
                impact["confirmed_false_alarm_cases"], ["CASE-EVT-DISMISSED"]
            )
            self.assertEqual(
                impact["pending_review_cases"], ["CASE-EVT-PENDING"]
            )
            # 处置中的历史事件被标记为可能受同类错误影响，供批量排查
            potentially = impact["potentially_affected_cases"]
            self.assertEqual(len(potentially), 1)

            good = service.analyze_algorithm_version_impact(GOOD_VERSION)
            self.assertEqual(len(good["events"]), 1)
            self.assertEqual(good["confirmed_false_alarm_cases"], [])

    def test_replay_is_idempotent_and_seq_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            self._build_state(path)
            repo = Repository(path)
            before = len(repo.raw_log())
            # 再次重放同一文件不应产生重复状态
            Repository(path)
            self.assertEqual(len(Repository(path).raw_log()), before)

            # 恢复后新追加的事件序号继续递增，不覆盖历史
            service = SafetyGovernanceService(Repository(path))
            event = make_event(
                Source.CURRENT, Scenario.EV_CHARGING,
                "2026-09-22T09:00:00+08:00",
                event_id="EVT-AFTER-RECOVERY",
                dedupe_key="current:after",
            )
            result = service.ingest(event)
            self.assertFalse(result.duplicate)
            self.assertGreater(len(service.repo.raw_log()), before)


if __name__ == "__main__":
    unittest.main()
