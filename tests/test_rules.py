"""四类场景核验规则测试：规则独立、版本独立、结论只辅助立案。"""

import unittest

from src.governance.model import EventRecord, RiskType, RuleLevel, SourceKind
from src.governance.rules import (
    BIKE_RULE_VERSION,
    HOT_WORK_RULE_VERSION,
    INSURANCE_RULE_VERSION,
    WATER_RULE_VERSION,
    verify,
    verify_waterlogging_with_payload,
)


def _event(risk_type: RiskType, payload: dict, source: SourceKind = SourceKind.VIDEO) -> EventRecord:
    return EventRecord(
        event_id="evt-t",
        risk_type=risk_type,
        source=source,
        source_event_id="src-t",
        occurred_at=0,
        ingested_seq=1,
        algorithm_version=None,
        confidence=0.9,
        location="测试点",
        payload=payload,
    )


class BikeChargingRuleTest(unittest.TestCase):
    def test_indoor_and_blocks_escape_is_breach_but_only_assists(self) -> None:
        result = verify(
            _event(RiskType.BIKE_CHARGING, {"indoor_charging": True, "blocks_escape": True})
        )
        self.assertEqual(result.level, RuleLevel.BREACH)
        self.assertEqual(result.rule_version, BIKE_RULE_VERSION)
        self.assertTrue(result.assists_filing)
        self.assertFalse(result.passed)
        self.assertEqual(len(result.findings), 2)

    def test_designated_zone_passes(self) -> None:
        result = verify(
            _event(RiskType.BIKE_CHARGING, {"zone": "室外集中充电棚"})
        )
        self.assertEqual(result.level, RuleLevel.PASS)
        self.assertTrue(result.passed)

    def test_unknown_zone_is_attention_not_breach(self) -> None:
        result = verify(_event(RiskType.BIKE_CHARGING, {"zone": "画面边缘"}))
        self.assertEqual(result.level, RuleLevel.ATTENTION)
        self.assertTrue(result.assists_filing)


class HotWorkRuleTest(unittest.TestCase):
    def test_permit_and_cert_each_independent(self) -> None:
        # 有许可、无资质 → 违规
        result = verify(
            _event(
                RiskType.HOT_WORK,
                {"permit_present": True, "permit_valid": True,
                 "welder_cert_present": False, "fire_watch_present": True},
            )
        )
        self.assertEqual(result.level, RuleLevel.BREACH)
        self.assertTrue(any("资质" in f for f in result.findings))

        # 齐备但无监护 → 存疑，不直接判违规
        result2 = verify(
            _event(
                RiskType.HOT_WORK,
                {"permit_present": True, "permit_valid": True,
                 "welder_cert_present": True, "welder_cert_valid": True,
                 "fire_watch_present": False},
            )
        )
        self.assertEqual(result2.level, RuleLevel.ATTENTION)

        # 全部齐备 → 通过
        result3 = verify(
            _event(
                RiskType.HOT_WORK,
                {"permit_present": True, "permit_valid": True,
                 "welder_cert_present": True, "welder_cert_valid": True,
                 "fire_watch_present": True},
            )
        )
        self.assertEqual(result3.level, RuleLevel.PASS)
        self.assertEqual(result3.rule_version, HOT_WORK_RULE_VERSION)


class WaterloggingRuleTest(unittest.TestCase):
    def test_thresholds_grade_depth(self) -> None:
        below = verify(_event(RiskType.WATERLOGGING, {"depth_cm": 5}, SourceKind.WEATHER))
        self.assertEqual(below.level, RuleLevel.PASS)

        warn = verify(_event(RiskType.WATERLOGGING, {"depth_cm": 18}, SourceKind.WEATHER))
        self.assertEqual(warn.level, RuleLevel.ATTENTION)

        danger = verify(_event(RiskType.WATERLOGGING, {"depth_cm": 30}, SourceKind.WEATHER))
        self.assertEqual(danger.level, RuleLevel.BREACH)

    def test_missing_depth_is_attention_not_pass(self) -> None:
        result = verify(_event(RiskType.WATERLOGGING, {}, SourceKind.WEATHER))
        self.assertEqual(result.level, RuleLevel.ATTENTION)
        self.assertIn("缺测", "".join(result.findings))

    def test_late_data_recomputes_as_new_result_without_touching_old(self) -> None:
        early = _event(RiskType.WATERLOGGING, {"depth_cm": 30}, SourceKind.WEATHER)
        early_result = verify(early)
        self.assertEqual(early_result.level, RuleLevel.BREACH)

        # 迟到的气象数据显示已回落
        late_result = verify_waterlogging_with_payload(early, {"depth_cm": 4})
        self.assertEqual(late_result.level, RuleLevel.PASS)
        # 旧结论与旧事件均未被修改
        self.assertEqual(verify(early).level, RuleLevel.BREACH)
        self.assertEqual(early.payload["depth_cm"], 30)
        self.assertEqual(early_result.rule_version, WATER_RULE_VERSION)


class InsuranceRuleTest(unittest.TestCase):
    def test_policy_service_sla(self) -> None:
        inactive = verify(_event(RiskType.LIABILITY_INSURANCE, {"policy_active": False},
                                 SourceKind.INSURANCE))
        self.assertEqual(inactive.level, RuleLevel.BREACH)

        in_progress = verify(
            _event(
                RiskType.LIABILITY_INSURANCE,
                {"policy_active": True, "service_promised": True, "elapsed_hours": 5},
                SourceKind.INSURANCE,
            )
        )
        self.assertEqual(in_progress.level, RuleLevel.ATTENTION)

        overdue = verify(
            _event(
                RiskType.LIABILITY_INSURANCE,
                {"policy_active": True, "service_promised": True, "elapsed_hours": 30},
                SourceKind.INSURANCE,
            )
        )
        self.assertEqual(overdue.level, RuleLevel.BREACH)

        done = verify(
            _event(
                RiskType.LIABILITY_INSURANCE,
                {"policy_active": True, "service_record_exists": True},
                SourceKind.INSURANCE,
            )
        )
        self.assertEqual(done.level, RuleLevel.PASS)
        self.assertEqual(done.rule_version, INSURANCE_RULE_VERSION)


class RuleIsolationTest(unittest.TestCase):
    def test_each_risk_type_uses_own_rule_and_version(self) -> None:
        versions = {
            RiskType.BIKE_CHARGING: BIKE_RULE_VERSION,
            RiskType.HOT_WORK: HOT_WORK_RULE_VERSION,
            RiskType.WATERLOGGING: WATER_RULE_VERSION,
            RiskType.LIABILITY_INSURANCE: INSURANCE_RULE_VERSION,
        }
        self.assertEqual(len(set(versions.values())), 4)
        for risk_type, version in versions.items():
            result = verify(_event(risk_type, {}))
            self.assertEqual(result.rule_version, version)
            self.assertEqual(result.risk_type, risk_type)
            # 任何自动结论都只能辅助，不能自动立案
            self.assertIsInstance(result.assists_filing, bool)


if __name__ == "__main__":
    unittest.main()
