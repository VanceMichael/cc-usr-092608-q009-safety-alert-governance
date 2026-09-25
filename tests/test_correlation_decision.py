"""人工裁决：同一风险归并、相互独立、算法误报分流。"""

import unittest

from src.models import CaseStatus, Decision, Source, Scenario
from src.repository import Repository
from src.service import SafetyGovernanceService
from tests.helpers import make_event


class DecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SafetyGovernanceService(Repository())
        self.video = make_event(
            Source.VIDEO, Scenario.EV_CHARGING,
            "2026-09-20T09:00:00+08:00",
            algorithm_version="ev-video-2026.07",
            location_hint="幸福里3栋",
        )
        self.current = make_event(
            Source.CURRENT, Scenario.EV_CHARGING,
            "2026-09-20T09:03:00+08:00",
            algorithm_version="ev-current-2026.07",
            location_hint="幸福里3栋",
        )
        self.c1 = self.service.ingest(self.video).case
        self.c2 = self.service.ingest(self.current).case

    def test_same_risk_merges_events_into_target(self) -> None:
        merged = self.service.decide(
            self.c2.case_id,
            Decision.SAME_RISK,
            by="分析员甲",
            reason="视频与电流同点位、3 分钟内，判定为同起入户充电",
            expected_chain_version=self.c2.chain_version,
            target_case_id=self.c1.case_id,
        )
        self.assertEqual(merged.status, CaseStatus.MERGED)
        self.assertEqual(merged.merged_into, self.c1.case_id)
        target = self.service.repo.get_case(self.c1.case_id)
        event_ids = {e.event_id for e in target.events}
        self.assertEqual(event_ids, {self.video.event_id, self.current.event_id})

    def test_independent_decision_is_required_before_filing(self) -> None:
        decided = self.service.decide(
            self.c1.case_id,
            Decision.INDEPENDENT,
            by="值班长乙",
            reason="与邻栋举报非同一风险",
            expected_chain_version=self.c1.chain_version,
        )
        self.assertEqual(decided.status, CaseStatus.ACTIVE)

    def test_case_cannot_be_decided_twice(self) -> None:
        self.service.decide(
            self.c1.case_id, Decision.INDEPENDENT, "值班长乙", "独立",
            expected_chain_version=self.c1.chain_version,
        )
        with self.assertRaisesRegex(Exception, "不可重复裁决"):
            self.service.decide(
                self.c1.case_id, Decision.SAME_RISK, "分析员甲", "改判",
                expected_chain_version=self.c1.chain_version + 1,
                target_case_id=self.c2.case_id,
            )

    def test_false_alarm_requires_dedicated_review_flow(self) -> None:
        from src.models import CaseStateError

        with self.assertRaises(CaseStateError):
            self.service.decide(
                self.c2.case_id, Decision.FALSE_ALARM, "分析员甲", "误报",
                expected_chain_version=self.c2.chain_version,
            )


if __name__ == "__main__":
    unittest.main()
