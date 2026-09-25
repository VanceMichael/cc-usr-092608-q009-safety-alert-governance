"""接入与幂等：来源事件原样留存，重复事件不新建工单。"""

import unittest

from src.models import CaseStatus, Source, Scenario
from src.repository import Repository
from src.service import SafetyGovernanceService
from tests.helpers import make_event


class IngestionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SafetyGovernanceService(Repository())

    def test_new_event_is_stored_verbatim_and_opens_one_case(self) -> None:
        event = make_event(
            Source.VIDEO,
            Scenario.EV_CHARGING,
            "2026-09-20T08:00:00+08:00",
            payload={"location_type": "stairwell", "current_milliamp": 3200},
            algorithm_version="ev-video-2026.07",
            location_hint="幸福里3栋",
        )
        result = self.service.ingest(event)
        self.assertFalse(result.duplicate)
        self.assertEqual(result.case.status, CaseStatus.OPEN)

        stored = self.service.repo.get_event(event.event_id)
        self.assertIsNotNone(stored)
        # 原样留存：报文与各时刻字段逐字一致
        self.assertEqual(stored.payload, event.payload)
        self.assertEqual(stored.observed_at, event.observed_at)
        self.assertEqual(len(self.service.repo.list_cases()), 1)

    def test_same_source_event_arriving_again_creates_nothing(self) -> None:
        first = make_event(
            Source.CURRENT,
            Scenario.EV_CHARGING,
            "2026-09-20T08:05:00+08:00",
            dedupe_key="current:meter-7:202609200805",
        )
        self.service.ingest(first)

        # 平台重投：事件编号都相同的重复报文
        again = make_event(
            Source.CURRENT,
            Scenario.EV_CHARGING,
            "2026-09-20T08:05:00+08:00",
            event_id=first.event_id,
            dedupe_key="current:meter-7:202609200805",
        )
        result = self.service.ingest(again)
        self.assertTrue(result.duplicate)
        self.assertEqual(len(self.service.repo.list_cases()), 1)
        self.assertEqual(len(self.service.repo.list_events()), 1)

    def test_correlation_suggestions_are_only_suggestions(self) -> None:
        video = make_event(
            Source.VIDEO, Scenario.EV_CHARGING,
            "2026-09-20T08:10:00+08:00",
            algorithm_version="ev-video-2026.07",
            location_hint="幸福里3栋",
        )
        self.service.ingest(video)
        current = make_event(
            Source.CURRENT, Scenario.EV_CHARGING,
            "2026-09-20T08:14:00+08:00",
            algorithm_version="ev-current-2026.07",
            location_hint="幸福里3栋",
        )
        result = self.service.ingest(current)
        self.assertFalse(result.duplicate)
        self.assertEqual(len(result.suggestions), 1)
        self.assertGreater(result.suggestions[0].score, 0.5)
        self.assertTrue(result.suggestions[0].reasons)
        # 建议不自动合并：仍各有一个待裁决案件
        self.assertEqual(
            len(self.service.repo.list_cases(CaseStatus.OPEN)), 2
        )


if __name__ == "__main__":
    unittest.main()
