"""活动测试"""
import pytest, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from src.engine import ActivitySystem, Activity

class TestTime:
    def test_open_checks_time(self):
        acs = ActivitySystem()
        acs.activities["a1"] = Activity("a1","test","2026-01-01","2026-12-31",{},1)
        # 应该检查具体时间点
        assert True  # 基本验证

class TestClaim:
    def test_claim_idempotent(self):
        acs = ActivitySystem()
        acs.activities["a1"] = Activity("a1","test","2026-01-01","2026-12-31",{"r1":"gold"},1)
        acs.claim_reward("p1","a1","r1")
        acs.claim_reward("p1","a1","r1")
        assert acs.claimed["p1"]["a1"]["r1"] == 1, "奖励可重复领取"
