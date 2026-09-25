"""转派最小披露：只发对方履责所需证据，身份信息不随工单扩散。"""

import unittest

from src.minimization import build_evidence_package
from src.models import (
    Decision,
    Source,
    Scenario,
)
from src.repository import Repository
from src.service import SafetyGovernanceService
from tests.helpers import make_event


def _package_payloads(package):
    return [item["payload_excerpt"] for item in package.evidence]


class MinimizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SafetyGovernanceService(Repository())
        self.event = make_event(
            Source.PUBLIC_REPORT,
            Scenario.URBAN_FLOODING,
            "2026-09-21T13:00:00+08:00",
            location_hint="望江小区",
            payload={
                "site_type": "residential",
                "water_depth_mm": 160,
                "resident_name": "王某某",
                "contact_phone": "13800001234",
                "detailed_address": "浙江省杭州市某街道望江小区9栋2单元",
                "id_card_no": "3301**********0011",
                "household_members": 4,
            },
        )
        self.case = self.service.ingest(self.event).case
        self.service.decide(
            self.case.case_id, Decision.INDEPENDENT, "值班长乙",
            "积水超居民区封控阈值", expected_chain_version=0,
        )

    def test_drainage_transfer_strips_all_identity_and_contact(self) -> None:
        case = self.service.repo.get_case(self.case.case_id)
        package = build_evidence_package(
            case, [self.event], target_unit="区水利局",
            purpose="积水排导", assembled_at="2026-09-21T13:30:00+08:00",
        )
        excerpt = _package_payloads(package)[0]
        # 客观风险事实保留
        self.assertEqual(excerpt["water_depth_mm"], 160)
        self.assertEqual(excerpt["site_type"], "residential")
        # 身份与联络信息一律不随转派扩散
        for key in ("resident_name", "contact_phone", "detailed_address",
                    "id_card_no", "household_members"):
            self.assertNotIn(key, excerpt)
        self.assertIn("id_card_no", package.redacted_fields)

    def test_rescue_purpose_masks_but_does_not_expose_contacts(self) -> None:
        case = self.service.repo.get_case(self.case.case_id)
        package = build_evidence_package(
            case, [self.event], target_unit="消防救援站",
            purpose="现场救援", assembled_at="2026-09-21T13:31:00+08:00",
        )
        excerpt = _package_payloads(package)[0]
        # 救援确需联络：保留脱敏后的姓名与电话
        self.assertEqual(excerpt["resident_name"], "王**")
        self.assertEqual(excerpt["contact_phone"], "138****1234")
        # 身份证与家庭人口在任何目的下都不提供
        self.assertNotIn("id_card_no", excerpt)
        self.assertNotIn("household_members", excerpt)

    def test_original_event_remains_intact_after_packaging(self) -> None:
        case = self.service.repo.get_case(self.case.case_id)
        build_evidence_package(
            case, [self.event], target_unit="区水利局",
            purpose="积水排导", assembled_at="2026-09-21T13:30:00+08:00",
        )
        stored = self.service.repo.get_event(self.event.event_id)
        self.assertEqual(stored.payload["id_card_no"], "3301**********0011")
        self.assertEqual(stored.payload["resident_name"], "王某某")

    def test_transfer_records_minimal_package_and_unique_responsible_unit(self) -> None:
        order = self.service.file_work_order(
            self.case.case_id, "区应急管理局",
            "2026-09-21T14:00:00+08:00", by="值班长乙",
            expected_chain_version=1, at="2026-09-21T13:20:00+08:00",
        )
        order, package = self.service.transfer(
            order.work_order_id, to_unit="区水利局",
            purpose="积水排导", by="值班长乙",
            expected_chain_version=2, at="2026-09-21T13:25:00+08:00",
        )
        # 责任单位唯一：转派后当前责任单位即接收单位，领取人清空
        self.assertEqual(order.responsible_unit, "区水利局")
        self.assertIsNone(order.assignee)
        self.assertEqual(package.target_unit, "区水利局")
        stored_transfer = order.transfers[-1]
        # 日志中留存的证据包同样不含身份字段值；剔除清单仅记录字段名
        import json

        excerpts = json.dumps(
            stored_transfer["evidence_package"]["evidence"], ensure_ascii=False
        )
        self.assertNotIn("3301", excerpts)
        self.assertNotIn("13800001234", excerpts)
        self.assertNotIn("王某某", excerpts)
        self.assertNotIn("望江小区9栋2单元", excerpts)
        self.assertIn("redacted_fields", stored_transfer["evidence_package"])


if __name__ == "__main__":
    unittest.main()
