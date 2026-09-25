"""测试共用：单位、人员与服务装配。"""

from __future__ import annotations

from src.governance.model import Actor, ActorRole, Unit
from src.governance.service import GovernanceService

STATION = Unit("street-station", "街道应急管理站")
FIRE = Unit("fire-rescue", "消防救援机构")
WATER = Unit("water-authority", "水利部门")
INSURER = Unit("insurer", "安责险承保机构")

UNITS = (STATION, FIRE, WATER, INSURER)


def build_service() -> GovernanceService:
    service = GovernanceService()
    for unit in UNITS:
        service.register_unit(unit)

    service.register_actor(Actor("mgr-zhao", "赵督导", ActorRole.MANAGER, STATION.code))
    service.register_actor(Actor("front-qian", "钱前", ActorRole.FRONTLINE, STATION.code))
    service.register_actor(Actor("front-sun", "孙力", ActorRole.FRONTLINE, STATION.code))
    service.register_actor(Actor("analyst-li", "李分析", ActorRole.ANALYST))
    service.register_actor(Actor("keeper-zhou", "周维护", ActorRole.RULE_KEEPER))
    service.register_actor(Actor("admin-wu", "吴审核", ActorRole.ADMIN))
    service.register_actor(Actor("insurer-chen", "陈保险", ActorRole.INSURANCE_STAFF, INSURER.code))

    # 周维护负责 bike-cam-v5 与 hotwork-v9 两个算法版本
    service.register_version_maintainer("bike-cam-v5", "keeper-zhou")
    service.register_version_maintainer("hotwork-v9", "keeper-zhou")
    return service
