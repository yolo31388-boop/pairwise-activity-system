"""限时活动 7 个运营事故修复验证"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.engine import Activity, ActivitySystem

T = "%Y-%m-%d %H:%M:%S"


def make_act(**kw):
    defaults = dict(aid="a1", name="test",
                    start_date="2026-01-01 10:00:00",
                    end_date="2026-01-10 20:00:00",
                    rewards={"r1": "gold"}, min_level=10)
    defaults.update(kw)
    return Activity(**defaults)


class TestTime:
    def test_open_checks_exact_time(self):
        # fix1: 开启精确到时分秒，活动当天0点不能提前开
        acs = ActivitySystem()
        acs.activities["a1"] = make_act()
        assert not acs.is_open("a1", datetime(2026, 1, 1, 0, 0, 0))
        assert not acs.is_open("a1", datetime(2026, 1, 1, 9, 59, 59))
        assert acs.is_open("a1", datetime(2026, 1, 1, 10, 0, 0))
        assert acs.is_open("a1", datetime(2026, 1, 10, 20, 0, 0))
        assert not acs.is_open("a1", datetime(2026, 1, 10, 20, 0, 1))

    def test_warmup_period(self):
        # fix1: 预热期可展示但不可参与
        acs = ActivitySystem()
        acs.activities["a1"] = make_act(warmup_start="2025-12-30 00:00:00")
        assert acs.is_warmup("a1", datetime(2025, 12, 31, 12, 0, 0))
        assert not acs.is_open("a1", datetime(2025, 12, 31, 12, 0, 0))
        assert acs.status("a1", datetime(2026, 1, 5, 12, 0, 0)) == "open"


class TestProgress:
    def test_progress_shared_by_account(self):
        # fix2: 同账号多角色共享进度，小号不能重复刷
        acs = ActivitySystem()
        acs.activities["a1"] = make_act()
        acs.register_player("role1", account_id="acc1")
        acs.register_player("role2", account_id="acc1")
        now = datetime(2026, 1, 5, 12, 0, 0)
        acs.add_progress("role1", "a1", 3, now=now)
        acs.add_progress("role2", "a1", 2, now=now)
        assert acs.get_progress("role1", "a1") == 5
        assert acs.get_progress("role2", "a1") == 5
        assert acs.progress["acc1"]["a1"] == 5


class TestClaim:
    def test_claim_idempotent(self):
        # fix3: 重复领取只生效一次
        acs = ActivitySystem()
        acs.activities["a1"] = make_act()
        assert acs.claim_reward("p1", "a1", "r1") == "gold"
        assert acs.claim_reward("p1", "a1", "r1") == "gold"
        assert acs.claimed["p1"]["a1"]["r1"] == 1

    def test_claim_request_id_replay(self):
        # fix3: 相同 request_id 重放不重复发放
        acs = ActivitySystem()
        acs.activities["a1"] = make_act(claim_limit=3)
        acs.claim_reward("p1", "a1", "r1", request_id="req-1")
        acs.claim_reward("p1", "a1", "r1", request_id="req-1")
        acs.claim_reward("p1", "a1", "r1", request_id="req-1")
        assert acs.claimed["p1"]["a1"]["r1"] == 1


class TestRanking:
    def test_ranking_realtime_with_cache(self):
        # fix4: 提交成绩后立即可查排名，且结果有缓存
        acs = ActivitySystem()
        acs.activities["a1"] = make_act()
        now = datetime(2026, 1, 5, 12, 0, 0)
        acs.submit_score("p1", "a1", 100, now=now)
        acs.submit_score("p2", "a1", 200, now=now)
        assert acs.get_ranking("a1") == [("p2", 200), ("p1", 100)]
        assert "a1" in acs._rank_cache  # 已缓存
        acs.submit_score("p1", "a1", 300, now=now)  # 新成绩使缓存失效
        assert acs.get_ranking("a1")[0] == ("p1", 300)


class TestItems:
    def test_activity_items_expire_and_convert(self):
        # fix5: 活动道具结算后过期删除，配置转换的自动兑换
        acs = ActivitySystem()
        acs.activities["a1"] = make_act(item_convert={"token": "coin"})
        acs.grant_item("p1", "token", 10, aid="a1")
        acs.grant_item("p1", "ticket", 1, aid="a1")
        during = datetime(2026, 1, 5, 12, 0, 0)
        assert acs.get_item("p1", "token", now=during) == 10
        after = datetime(2026, 1, 10, 21, 0, 1)  # 过了结算窗口
        assert acs.get_item("p1", "token", now=after) == 0
        assert acs.get_item("p1", "ticket", now=after) == 0
        assert acs.get_item("p1", "coin", now=after) == 10  # token 已转换


class TestParticipation:
    def test_multi_dimension_conditions(self):
        # fix6: 等级 + 开服天数 + 注册时间多维度校验
        acs = ActivitySystem()
        acs.activities["a1"] = make_act(
            min_level=10, min_server_days=30,
            register_before="2026-01-01 00:00:00")
        now = datetime(2026, 1, 5, 12, 0, 0)
        # 老区老玩家：通过
        acs.register_player("old", level=20, register_time="2025-06-01 00:00:00",
                            server_open_time="2025-06-01 00:00:00")
        assert acs.can_participate("old", "a1", now=now)
        # 新区玩家：开服不足30天，不能参加老区累计充值活动
        acs.register_player("new", level=50, register_time="2025-06-01 00:00:00",
                            server_open_time="2026-01-01 00:00:00")
        assert not acs.can_participate("new", "a1", now=now)
        # 注册太晚：不满足
        acs.register_player("late", level=50, register_time="2026-01-02 00:00:00",
                            server_open_time="2025-06-01 00:00:00")
        assert not acs.can_participate("late", "a1", now=now)
        # 等级不够：不满足
        acs.register_player("low", level=5, register_time="2025-06-01 00:00:00",
                            server_open_time="2025-06-01 00:00:00")
        assert not acs.can_participate("low", "a1", now=now)


class TestSettle:
    def test_settle_delay_window(self):
        # fix7: 结算延迟窗口内成绩仍入库，窗口结束后才结算
        acs = ActivitySystem()
        acs.activities["a1"] = make_act(settle_delay=3600)
        acs.submit_score("p1", "a1", 100, now=datetime(2026, 1, 10, 19, 59, 59))
        # 活动结束后、窗口内最后一秒提交的成绩仍被统计
        assert acs.submit_score("p2", "a1", 999, now=datetime(2026, 1, 10, 20, 0, 1))
        # 窗口未结束不能结算
        assert acs.settle("a1", now=datetime(2026, 1, 10, 20, 30, 0)) is None
        # 窗口结束后结算，包含延迟提交的成绩
        final = acs.settle("a1", now=datetime(2026, 1, 10, 21, 0, 1))
        assert final[0] == ("p2", 999)
        # 结算后不再接受成绩
        assert not acs.submit_score("p3", "a1", 1000, now=datetime(2026, 1, 10, 21, 0, 2))
