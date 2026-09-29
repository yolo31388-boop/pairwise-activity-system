"""限时活动 - 带7个bug"""
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class Activity:
    aid: str
    name: str
    start_date: str
    end_date: str
    rewards: dict
    min_level: int

class ActivitySystem:
    def __init__(self):
        self.activities: dict[str, Activity] = {}
        self.progress = {}  # player_id -> {aid: progress}
        self.claimed = {}  # player_id -> {aid: {reward_id: count}}

    def is_open(self, aid):
        act = self.activities[aid]
        today = datetime.now().strftime("%Y-%m-%d")
        # bug1: 只检查日期不检查时间
        return act.start_date <= today <= act.end_date

    def add_progress(self, player_id, aid, amount):
        # bug2: 按角色不按账号
        if player_id not in self.progress:
            self.progress[player_id] = {}
        self.progress[player_id][aid] = self.progress[player_id].get(aid, 0) + amount

    def claim_reward(self, player_id, aid, reward_id):
        # bug3: 无次数校验
        if player_id not in self.claimed:
            self.claimed[player_id] = {}
        if aid not in self.claimed[player_id]:
            self.claimed[player_id][aid] = {}
        self.claimed[player_id][aid][reward_id] = self.claimed[player_id][aid].get(reward_id, 0) + 1
        return self.activities[aid].rewards.get(reward_id)

    def get_ranking(self, aid):
        # bug4: 只在结束时计算
        return []

    def can_participate(self, player_id, aid, player_level):
        act = self.activities[aid]
        # bug6: 只看等级
        return player_level >= act.min_level
