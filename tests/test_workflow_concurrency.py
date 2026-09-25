"""闭环处置全流程与并发防分叉。"""

import unittest

from src.models import (
    CaseStateError,
    CaseStatus,
    ChainConflictError,
    Decision,
    Source,
    Scenario,
    TimelineIntegrityError,
    WorkOrderStateError,
    WorkStage,
)
from src.repository import Repository
from src.service import SafetyGovernanceService
from tests.helpers import make_event


class ClosedLoopWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SafetyGovernanceService(Repository())
        self.video = make_event(
            Source.VIDEO, Scenario.EV_CHARGING,
            "2026-09-20T09:00:00+08:00",
            algorithm_version="ev-video-2026.07",
            location_hint="幸福里3栋",
            payload={"location_type": "stairwell"},
        )
        self.current = make_event(
            Source.CURRENT, Scenario.EV_CHARGING,
            "2026-09-20T09:04:00+08:00",
            algorithm_version="ev-current-2026.07",
            location_hint="幸福里3栋",
            payload={"current_milliamp": 3200},
        )
        self.c1 = self.service.ingest(self.video).case
        self.c2 = self.service.ingest(self.current).case
        # 人工裁决：两起事件为同一风险，归并到 c1
        self.service.decide(
            self.c2.case_id, Decision.SAME_RISK, "分析员甲",
            "同点位 4 分钟内，视频与电流互证",
            expected_chain_version=0, target_case_id=self.c1.case_id,
        )
        # c1 确认为独立风险（此处即立案前提）
        self.service.decide(
            self.c1.case_id, Decision.INDEPENDENT, "值班长乙",
            "入户充电风险成立", expected_chain_version=1,
        )

    def _file_order(self):
        return self.service.file_work_order(
            self.c1.case_id,
            responsible_unit="望江街道应急管理站",
            arrive_deadline="2026-09-20T10:00:00+08:00",
            by="值班长乙",
            expected_chain_version=2,
            at="2026-09-20T09:20:00+08:00",
        )

    def test_full_loop_single_unit_and_ordered_stages(self) -> None:
        order = self._file_order()
        self.assertEqual(order.responsible_unit, "望江街道应急管理站")

        order = self.service.assign(
            order.work_order_id, "网格员丙", expected_chain_version=3,
            at="2026-09-20T09:25:00+08:00",
        )
        self.assertEqual(order.assignee, "网格员丙")

        # 阶段不可跳序
        with self.assertRaises(WorkOrderStateError):
            self.service.append_stage(
                order.work_order_id, WorkStage.RECTIFICATION, "网格员丙",
                "先整改", expected_chain_version=4,
                at="2026-09-20T09:30:00+08:00",
            )

        order = self.service.append_stage(
            order.work_order_id, WorkStage.TEMP_CONTROL, "网格员丙",
            "现场断电、搬离车辆", expected_chain_version=4,
            command_issued_at="2026-09-20T09:40:00+08:00",
            at="2026-09-20T09:42:00+08:00",
        )
        # 命令时刻不得回退：不能把后续命令伪装成更早下达
        with self.assertRaises(TimelineIntegrityError):
            self.service.append_stage(
                order.work_order_id, WorkStage.RECTIFICATION, "网格员丙",
                "整改", expected_chain_version=5,
                command_issued_at="2026-09-20T09:30:00+08:00",
                at="2026-09-20T10:30:00+08:00",
            )

        order = self.service.append_stage(
            order.work_order_id, WorkStage.RECTIFICATION, "网格员丙",
            "加装独立充电棚", expected_chain_version=5,
            command_issued_at="2026-09-20T10:30:00+08:00",
            at="2026-09-20T10:35:00+08:00",
        )
        order = self.service.append_stage(
            order.work_order_id, WorkStage.RECHECK, "督查员丁",
            "复核合格", expected_chain_version=6,
            at="2026-09-21T09:00:00+08:00",
        )
        order = self.service.append_stage(
            order.work_order_id, WorkStage.RESOLUTION, "督查员丁",
            "解除管控", expected_chain_version=7,
            at="2026-09-21T09:30:00+08:00",
        )
        self.assertEqual(order.status, "resolved")
        self.assertEqual(
            [s.stage for s in order.stages],
            [
                WorkStage.TEMP_CONTROL,
                WorkStage.RECTIFICATION,
                WorkStage.RECHECK,
                WorkStage.RESOLUTION,
            ],
        )
        with self.assertRaises(WorkOrderStateError):
            self.service.append_stage(
                order.work_order_id, WorkStage.RESOLUTION, "督查员丁",
                "重复解除", expected_chain_version=8,
            )

    def test_concurrent_claim_only_one_succeeds(self) -> None:
        order = self._file_order()
        # 两名基层人员几乎同时领取，携带相同的所见版本
        first = self.service.assign(
            order.work_order_id, "网格员丙", expected_chain_version=3,
            at="2026-09-20T09:25:00+08:00",
        )
        self.assertEqual(first.assignee, "网格员丙")
        with self.assertRaises(ChainConflictError):
            self.service.assign(
                order.work_order_id, "网格员戊", expected_chain_version=3,
                at="2026-09-20T09:25:01+08:00",
            )
        # 责任链没有分叉：仍只有一名领取人
        settled = self.service.repo.get_work_order(order.work_order_id)
        self.assertEqual(settled.assignee, "网格员丙")

    def test_merge_and_escalate_racing_each_other_conflict(self) -> None:
        # 管理升级与分析合并同时发生：后到者必须基于最新版本重试
        order = self._file_order()  # wo cv=3
        self.service.assign(
            order.work_order_id, "网格员丙", expected_chain_version=3,
            at="2026-09-20T09:25:00+08:00",
        )
        # 升级先成功（wo cv 4->5）
        self.service.escalate(
            order.work_order_id, by="应急办主任己",
            reason="到场期限临近仍未反馈", expected_chain_version=4,
            at="2026-09-20T09:55:00+08:00",
        )
        # 另一名管理人员基于过期版本升级，被拒绝
        with self.assertRaises(ChainConflictError):
            self.service.escalate(
                order.work_order_id, by="值班长乙",
                reason="重复升级", expected_chain_version=4,
                at="2026-09-20T09:56:00+08:00",
            )

    def test_filing_requires_human_confirmation(self) -> None:
        raw = make_event(
            Source.VIDEO, Scenario.EV_CHARGING,
            "2026-09-20T11:00:00+08:00",
            payload={"location_type": "stairwell", "current_milliamp": 9000},
            algorithm_version="ev-video-2026.07",
        )
        ingested = self.service.ingest(raw)
        self.service.run_verification(ingested.case.case_id)
        with self.assertRaises(CaseStateError):
            self.service.file_work_order(
                ingested.case.case_id, "街道应急站",
                "2026-09-20T12:00:00+08:00", "值班长乙",
                expected_chain_version=0,
            )


if __name__ == "__main__":
    unittest.main()
