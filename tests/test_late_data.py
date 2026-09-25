"""迟到的气象/定位数据：可促使再评估，但不能改写已发命令。"""

import unittest

from src.models import (
    Decision,
    GovernanceError,
    Source,
    Scenario,
    WorkStage,
)
from src.repository import Repository
from src.service import SafetyGovernanceService
from tests.helpers import make_event


class LateDataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SafetyGovernanceService(Repository())
        report = make_event(
            Source.PUBLIC_REPORT,
            Scenario.URBAN_FLOODING,
            "2026-09-21T13:00:00+08:00",
            location_hint="望江下穿通道",
            payload={"site_type": "underpass", "water_depth_mm": 180},
        )
        self.case = self.service.ingest(report).case
        self.service.decide(
            self.case.case_id, Decision.INDEPENDENT, "值班长乙",
            "积水达关注档，现场举报可信", expected_chain_version=0,
        )
        self.order = self.service.file_work_order(
            self.case.case_id,
            responsible_unit="区综合行政执法局",
            arrive_deadline="2026-09-21T14:00:00+08:00",
            by="值班长乙",
            expected_chain_version=1,
            at="2026-09-21T13:20:00+08:00",
        )
        self.order = self.service.assign(
            self.order.work_order_id, "抢险员辛", expected_chain_version=2,
            at="2026-09-21T13:25:00+08:00",
        )
        self.order = self.service.append_stage(
            self.order.work_order_id, WorkStage.TEMP_CONTROL, "抢险员辛",
            "入口设置警示、封路", expected_chain_version=3,
            command_issued_at="2026-09-21T13:40:00+08:00",
            at="2026-09-21T13:42:00+08:00",
        )

    def test_late_weather_triggers_reevaluation_and_new_measures_only(self) -> None:
        # 气象数据晚到：观测在 12:30，平台 14:10 才收到
        late_weather = make_event(
            Source.WEATHER,
            Scenario.URBAN_FLOODING,
            "2026-09-21T12:30:00+08:00",
            received_at="2026-09-21T14:10:00+08:00",
            payload={"rain_mm": 95, "forecast": "上游强降雨持续"},
            location_hint="望江下穿通道",
        )
        opened = self.service.ingest(late_weather).case
        # 人工裁决：迟到气象与既有积涝为同一风险，并入处置中案件
        self.service.decide(
            opened.case_id, Decision.SAME_RISK, "分析员甲",
            "晚到气象数据与望江下穿积涝同点同源",
            expected_chain_version=0, target_case_id=self.case.case_id,
            at="2026-09-21T14:12:00+08:00",
        )

        record = self.service.reevaluate_with_late_event(
            self.order.work_order_id, late_weather.event_id,
            by="值班长乙",
            note="上游强降雨持续，解除前必须再次复核水位",
            at="2026-09-21T14:15:00+08:00",
        )
        self.assertEqual(record["late_source"], "weather")

        order = self.service.repo.get_work_order(self.order.work_order_id)
        self.assertEqual(len(order.reevaluations), 1)
        # 既有临时控制命令原封不动，仍是 13:40 发出
        self.assertEqual(
            order.stages[0].command_issued_at, "2026-09-21T13:40:00+08:00"
        )

        # 再评估促成后续措施：追加整改（强排），命令时刻必须晚于既有命令
        order = self.service.append_stage(
            order.work_order_id, WorkStage.RECTIFICATION, "抢险员辛",
            "启动移动泵车强排", expected_chain_version=4,
            command_issued_at="2026-09-21T14:20:00+08:00",
            at="2026-09-21T14:22:00+08:00",
        )
        self.assertEqual(len(order.stages), 2)

    def test_late_data_cannot_be_used_to_backfill_a_command(self) -> None:
        # 试图凭迟到数据声称 13:30（早于 13:40 的封路命令）已下达整改命令
        late_location = make_event(
            Source.LOCATION,
            Scenario.URBAN_FLOODING,
            "2026-09-21T12:40:00+08:00",
            received_at="2026-09-21T14:30:00+08:00",
            payload={"gps": "30.2N,120.1E"},
            location_hint="望江下穿通道",
        )
        opened = self.service.ingest(late_location).case
        self.service.decide(
            opened.case_id, Decision.SAME_RISK, "分析员甲",
            "晚到定位与现场轨迹一致",
            expected_chain_version=0, target_case_id=self.case.case_id,
            at="2026-09-21T14:32:00+08:00",
        )
        self.service.reevaluate_with_late_event(
            self.order.work_order_id, late_location.event_id,
            by="值班长乙", note="轨迹复核",
            at="2026-09-21T14:35:00+08:00",
        )
        from src.models import TimelineIntegrityError

        with self.assertRaises(TimelineIntegrityError):
            self.service.append_stage(
                self.order.work_order_id, WorkStage.RECTIFICATION, "抢险员辛",
                "声称当时已命令强排", expected_chain_version=4,
                command_issued_at="2026-09-21T13:30:00+08:00",
                at="2026-09-21T14:40:00+08:00",
            )

    def test_reevaluation_rejects_unrelated_or_non_geo_sources(self) -> None:
        other = make_event(
            Source.VIDEO, Scenario.URBAN_FLOODING,
            "2026-09-21T13:10:00+08:00",
        )
        self.service.ingest(other)
        with self.assertRaises(GovernanceError):
            self.service.reevaluate_with_late_event(
                self.order.work_order_id, other.event_id,
                by="值班长乙", note="视频不属再评估数据源",
            )


if __name__ == "__main__":
    unittest.main()
