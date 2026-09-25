"""误报关闭的职责分离：算法版本维护人不得独自关闭本版本异常。"""

import unittest

from src.models import (
    CaseStatus,
    Decision,
    ReviewStatus,
    SegregationError,
    Source,
    Scenario,
)
from src.repository import Repository
from src.service import SafetyGovernanceService
from tests.helpers import make_event


VERSION = "ev-video-2026.07"
MAINTAINER = "算法维护人庚"
ANALYST = "分析员甲"
SUPERVISOR = "督查员丁"  # 非维护人，可担任第二复核人


class FalseAlarmSegregationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SafetyGovernanceService(Repository())
        self.service.register_algorithm(
            VERSION, {MAINTAINER, "算法维护人壬"},
            at="2026-09-01T00:00:00+08:00",
        )

    def _open_case(self):
        event = make_event(
            Source.VIDEO, Scenario.EV_CHARGING,
            "2026-09-20T08:00:00+08:00",
            algorithm_version=VERSION,
            payload={"location_type": "corridor"},
        )
        return self.service.ingest(event).case

    def test_maintainer_cannot_solely_close_even_when_they_propose(self) -> None:
        case = self._open_case()
        review_id = self.service.propose_false_alarm(
            case.case_id, proposed_by=MAINTAINER,
            reason="该版本夜间识别疑似批量误判",
            expected_chain_version=0,
        )
        # 申请人本人不能复核
        with self.assertRaises(SegregationError):
            self.service.review_false_alarm(
                review_id, reviewed_by=MAINTAINER, approve=True, note="自批"
            )
        # 同版本的另一名维护人也不能复核关闭
        with self.assertRaises(SegregationError):
            self.service.review_false_alarm(
                review_id, reviewed_by="算法维护人壬", approve=True,
                note="同组维护人互批",
            )
        # 非维护人复核通过后才真正关闭
        closed = self.service.review_false_alarm(
            review_id, reviewed_by=SUPERVISOR, approve=True,
            note="抽查现场无充电行为，确认误报",
        )
        self.assertEqual(closed.status, CaseStatus.DISMISSED)
        self.assertEqual(closed.false_alarm.status, ReviewStatus.APPROVED)

    def test_maintainer_cannot_review_proposal_made_by_others(self) -> None:
        case = self._open_case()
        review_id = self.service.propose_false_alarm(
            case.case_id, proposed_by=ANALYST,
            reason="疑似逆光误识别", expected_chain_version=0,
        )
        with self.assertRaises(SegregationError):
            self.service.review_false_alarm(
                review_id, reviewed_by=MAINTAINER, approve=True, note="维护人关单"
            )

    def test_rejected_false_alarm_returns_case_to_filing(self) -> None:
        case = self._open_case()
        review_id = self.service.propose_false_alarm(
            case.case_id, proposed_by=ANALYST,
            reason="疑似误判", expected_chain_version=0,
        )
        reopened = self.service.review_false_alarm(
            review_id, reviewed_by=SUPERVISOR, approve=False,
            note="现场复核确有飞线充电，误报不成立",
        )
        self.assertEqual(reopened.status, CaseStatus.ACTIVE)
        self.assertIsNone(reopened.decision)
        # 推翻误报后可正常立案
        order = self.service.file_work_order(
            reopened.case_id, "街道应急站",
            "2026-09-20T10:00:00+08:00", by=SUPERVISOR,
            expected_chain_version=2, at="2026-09-20T09:00:00+08:00",
        )
        self.assertTrue(order.work_order_id)

    def test_pending_review_blocks_double_proposal(self) -> None:
        case = self._open_case()
        self.service.propose_false_alarm(
            case.case_id, proposed_by=ANALYST,
            reason="第一次申请", expected_chain_version=0,
        )
        with self.assertRaises(Exception):
            self.service.propose_false_alarm(
                case.case_id, proposed_by=ANALYST,
                reason="重复申请", expected_chain_version=1,
            )


if __name__ == "__main__":
    unittest.main()
