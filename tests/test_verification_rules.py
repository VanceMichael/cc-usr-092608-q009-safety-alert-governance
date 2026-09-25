"""四个场景各自核验规则的测试；自动结论只辅助立案。"""

import unittest

from src.models import ActionKind, Source, Scenario
from src.verification import verify_events
from tests.helpers import make_event


class EVChargingRuleTest(unittest.TestCase):
    scenario = Scenario.EV_CHARGING
    source = Source.VIDEO

    def test_forbidden_location_and_overcurrent_support_filing(self) -> None:
        event = make_event(
            self.source, self.scenario, "2026-09-20T08:00:00+08:00",
            payload={"location_type": "stairwell", "current_milliamp": 3200},
        )
        result = verify_events(self.scenario, [event], "2026-09-20T08:01:00+08:00")
        self.assertEqual(result.action, ActionKind.SUPPORT_FILING)
        self.assertTrue(any("禁止" in f or "不属于" in f for f in result.findings))

    def test_single_dimension_only_requires_manual_review(self) -> None:
        event = make_event(
            self.source, self.scenario, "2026-09-20T08:00:00+08:00",
            payload={"location_type": "designated_shed", "current_milliamp": 3200},
        )
        result = verify_events(self.scenario, [event], "2026-09-20T08:01:00+08:00")
        self.assertEqual(result.action, ActionKind.MANUAL_REVIEW)

    def test_compliant_does_not_support_filing(self) -> None:
        event = make_event(
            self.source, self.scenario, "2026-09-20T08:00:00+08:00",
            payload={"location_type": "outdoor_station", "current_milliamp": 800},
        )
        result = verify_events(self.scenario, [event], "2026-09-20T08:01:00+08:00")
        self.assertEqual(result.action, ActionKind.DO_NOT_FILE)


class HotWorkRuleTest(unittest.TestCase):
    scenario = Scenario.HOT_WORK

    def _event(self, payload: dict):
        return make_event(
            Source.VIDEO, self.scenario, "2026-09-20T10:00:00+08:00",
            payload=payload,
        )

    def test_no_permit_and_no_qualification_support_filing(self) -> None:
        result = verify_events(
            self.scenario, [self._event({})], "2026-09-20T10:01:00+08:00"
        )
        self.assertEqual(result.action, ActionKind.SUPPORT_FILING)
        self.assertEqual(len(result.findings), 2)

    def test_valid_permit_and_certificate_do_not_file(self) -> None:
        event = self._event(
            {
                "hot_work_permit": {
                    "status": "valid",
                    "valid_from": "2026-09-20T08:00:00+08:00",
                    "valid_to": "2026-09-20T18:00:00+08:00",
                },
                "welder_qualification": {
                    "status": "valid",
                    "valid_to": "2027-09-20T00:00:00+08:00",
                },
            }
        )
        result = verify_events(self.scenario, [event], "2026-09-20T10:01:00+08:00")
        self.assertEqual(result.action, ActionKind.DO_NOT_FILE)

    def test_expired_certificate_requires_manual_review(self) -> None:
        event = self._event(
            {
                "hot_work_permit": {
                    "status": "valid",
                    "valid_from": "2026-09-20T08:00:00+08:00",
                    "valid_to": "2026-09-20T18:00:00+08:00",
                },
                "welder_qualification": {
                    "status": "valid",
                    "valid_to": "2025-01-01T00:00:00+08:00",
                },
            }
        )
        result = verify_events(self.scenario, [event], "2026-09-20T10:01:00+08:00")
        self.assertEqual(result.action, ActionKind.MANUAL_REVIEW)
        self.assertTrue(any("资质" in f for f in result.findings))


class UrbanFloodingRuleTest(unittest.TestCase):
    scenario = Scenario.URBAN_FLOODING

    def _event(self, site: str, depth: int | None):
        return make_event(
            Source.HYDROLOGY, self.scenario, "2026-09-21T14:00:00+08:00",
            payload={"site_type": site, "water_depth_mm": depth},
        )

    def test_underpass_control_threshold_supports_filing(self) -> None:
        result = verify_events(
            self.scenario,
            [self._event("underpass", 300)],
            "2026-09-21T14:05:00+08:00",
        )
        self.assertEqual(result.action, ActionKind.SUPPORT_FILING)

    def test_residential_attention_band_requires_manual_review(self) -> None:
        result = verify_events(
            self.scenario,
            [self._event("residential", 100)],
            "2026-09-21T14:05:00+08:00",
        )
        self.assertEqual(result.action, ActionKind.MANUAL_REVIEW)

    def test_thresholds_are_site_specific(self) -> None:
        # 100mm 在居民区是关注档，在下穿通道低于关注阈值
        residential = verify_events(
            self.scenario, [self._event("residential", 100)],
            "2026-09-21T14:05:00+08:00",
        )
        underpass = verify_events(
            self.scenario, [self._event("underpass", 100)],
            "2026-09-21T14:05:00+08:00",
        )
        self.assertEqual(residential.action, ActionKind.MANUAL_REVIEW)
        self.assertEqual(underpass.action, ActionKind.DO_NOT_FILE)

    def test_missing_depth_waits_for_weather_data(self) -> None:
        result = verify_events(
            self.scenario,
            [make_event(Source.WEATHER, self.scenario,
                        "2026-09-21T14:00:00+08:00", payload={"rain_mm": 40})],
            "2026-09-21T14:05:00+08:00",
        )
        self.assertEqual(result.action, ActionKind.MANUAL_REVIEW)


class LiabilityInsuranceRuleTest(unittest.TestCase):
    scenario = Scenario.LIABILITY_INSURANCE

    def _event(self, payload: dict):
        return make_event(
            Source.INSURANCE, self.scenario, "2026-09-22T09:00:00+08:00",
            payload=payload,
        )

    def test_response_beyond_commitment_supports_filing(self) -> None:
        event = self._event(
            {
                "policy": {"status": "active"},
                "service_commitment": {"response_within_minutes": 60},
                "insurer_response_minutes": 95,
            }
        )
        result = verify_events(self.scenario, [event], "2026-09-22T09:10:00+08:00")
        self.assertEqual(result.action, ActionKind.SUPPORT_FILING)
        self.assertTrue(any("超出承诺" in f for f in result.findings))

    def test_response_within_commitment_does_not_file(self) -> None:
        event = self._event(
            {
                "policy": {"status": "active"},
                "service_commitment": {"response_within_minutes": 60},
                "insurer_response_minutes": 30,
            }
        )
        result = verify_events(self.scenario, [event], "2026-09-22T09:10:00+08:00")
        self.assertEqual(result.action, ActionKind.DO_NOT_FILE)

    def test_no_response_yet_requires_manual_review(self) -> None:
        event = self._event(
            {
                "policy": {"status": "active"},
                "service_commitment": {"response_within_minutes": 60},
            }
        )
        result = verify_events(self.scenario, [event], "2026-09-22T09:10:00+08:00")
        self.assertEqual(result.action, ActionKind.MANUAL_REVIEW)


class AutomationAssistsOnlyTest(unittest.TestCase):
    def test_verification_result_never_creates_work_order_alone(self) -> None:
        service_event = make_event(
            Source.VIDEO, Scenario.EV_CHARGING,
            "2026-09-20T08:00:00+08:00",
            payload={"location_type": "stairwell", "current_milliamp": 5000},
        )
        from src.repository import Repository
        from src.service import SafetyGovernanceService

        service = SafetyGovernanceService(Repository())
        ingested = service.ingest(service_event)
        result = service.run_verification(ingested.case.case_id)
        self.assertEqual(result.action, ActionKind.SUPPORT_FILING)
        # 自动建议立案，但案件仍 OPEN、没有工单
        self.assertIsNone(ingested.case.work_order_id)
        self.assertEqual(
            service.repo.get_case(ingested.case.case_id).work_order_id, None
        )


if __name__ == "__main__":
    unittest.main()
