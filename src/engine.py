"""限时活动系统"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta


def _parse_time(value, is_end=False):
    """解析时间，支持 'YYYY-MM-DD' 与 'YYYY-MM-DD HH:MM:SS'。
    纯日期的结束时间按当天 23:59:59 处理。"""
    if isinstance(value, datetime):
        return value
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    day = datetime.strptime(value, "%Y-%m-%d")
    if is_end:
        return day + timedelta(days=1) - timedelta(seconds=1)
    return day


@dataclass
class Activity:
    aid: str
    name: str
    start_date: str  # 支持精确到秒，如 "2026-01-01 10:00:00"
    end_date: str
    rewards: dict
    min_level: int
    warmup_start: str | None = None  # 预热期开始时间，预热期内可见但不可参与
    min_server_days: int = 0  # 开服天数要求
    min_register_days: int = 0  # 注册天数要求
    settle_delay_seconds: int = 3600  # 结算延迟窗口，默认1小时
    max_claim_per_reward: int = 1  # 每个奖励每账号限领次数
    item_convert: dict = field(default_factory=dict)  # 过期道具转换: item_id -> (目标道具, 比例)


class ActivitySystem:
    RANK_CACHE_TTL_SECONDS = 300  # 排行榜缓存5分钟

    def __init__(self):
        self.activities: dict[str, Activity] = {}
        self.accounts: dict[str, str] = {}  # player_id -> account_id
        self.progress = {}  # account_id -> {aid: progress}
        self.claimed = {}  # account_id -> {aid: {reward_id: count}}
        self._claim_requests = {}  # account_id -> {aid: set(request_id)}
        self.scores = {}  # aid -> {account_id: score}
        self._rank_cache = {}  # aid -> (cached_at, ranking)
        self.items = {}  # account_id -> {item_id: {"count", "expire_at", "aid"}}
        self.settled = {}  # aid -> 最终排名

    # ---------- 账号体系 ----------

    def bind_account(self, player_id, account_id):
        """角色绑定账号，同账号多角色共享进度与奖励。"""
        self.accounts[player_id] = account_id

    def _account_of(self, player_id):
        return self.accounts.get(player_id, player_id)

    # ---------- 开启时间（精确到秒 + 预热期） ----------

    def _window(self, act):
        return _parse_time(act.start_date), _parse_time(act.end_date, is_end=True)

    def is_warmup(self, aid, now=None):
        act = self.activities[aid]
        if not act.warmup_start:
            return False
        now = now or datetime.now()
        start, _ = self._window(act)
        return _parse_time(act.warmup_start) <= now < start

    def is_open(self, aid, now=None):
        """活动是否开启：精确到时分秒，预热期内不算开启。"""
        act = self.activities[aid]
        now = now or datetime.now()
        start, end = self._window(act)
        return start <= now <= end

    # ---------- 进度（按账号统一） ----------

    def add_progress(self, player_id, aid, amount):
        account_id = self._account_of(player_id)
        self.progress.setdefault(account_id, {})
        self.progress[account_id][aid] = self.progress[account_id].get(aid, 0) + amount

    def get_progress(self, player_id, aid):
        account_id = self._account_of(player_id)
        return self.progress.get(account_id, {}).get(aid, 0)

    # ---------- 奖励领取（幂等） ----------

    def claim_reward(self, player_id, aid, reward_id, request_id=None):
        """领取奖励，幂等：同奖励超次数或同 request_id 重复请求只生效一次。"""
        account_id = self._account_of(player_id)
        claimed = self.claimed.setdefault(account_id, {}).setdefault(aid, {})
        requests = self._claim_requests.setdefault(account_id, {}).setdefault(aid, set())
        if request_id is not None:
            if request_id in requests:
                return None  # 重复请求，直接忽略
            requests.add(request_id)
        act = self.activities[aid]
        if claimed.get(reward_id, 0) >= act.max_claim_per_reward:
            return None  # 已达领取上限
        claimed[reward_id] = claimed.get(reward_id, 0) + 1
        return act.rewards.get(reward_id)

    # ---------- 排行榜（实时更新 + 缓存） ----------

    def submit_score(self, player_id, aid, score, now=None):
        """提交成绩。活动结束后的结算延迟窗口内仍接受补交。"""
        act = self.activities[aid]
        now = now or datetime.now()
        _, end = self._window(act)
        if now > end + timedelta(seconds=act.settle_delay_seconds):
            return False  # 超过结算窗口，拒绝
        account_id = self._account_of(player_id)
        board = self.scores.setdefault(aid, {})
        board[account_id] = max(board.get(account_id, 0), score)
        self._rank_cache.pop(aid, None)  # 成绩变化，缓存失效
        return True

    def get_ranking(self, aid, now=None):
        """实时排名，带5分钟缓存。"""
        now = now or datetime.now()
        cached = self._rank_cache.get(aid)
        if cached:
            cached_at, ranking = cached
            if (now - cached_at).total_seconds() < self.RANK_CACHE_TTL_SECONDS:
                return list(ranking)
        board = self.scores.get(aid, {})
        ranking = sorted(board.items(), key=lambda kv: (-kv[1], kv[0]))
        self._rank_cache[aid] = (now, ranking)
        return list(ranking)

    # ---------- 活动道具（过期清理/转换） ----------

    def grant_item(self, player_id, item_id, count=1, aid=None, expire_at=None):
        account_id = self._account_of(player_id)
        bag = self.items.setdefault(account_id, {})
        if item_id in bag:
            bag[item_id]["count"] += count
            if expire_at is not None:
                bag[item_id]["expire_at"] = _parse_time(expire_at, is_end=True)
        else:
            bag[item_id] = {
                "count": count,
                "expire_at": _parse_time(expire_at, is_end=True) if expire_at else None,
                "aid": aid,
            }

    def get_items(self, player_id, now=None):
        """查询道具，先清理已过期的。"""
        account_id = self._account_of(player_id)
        self.cleanup_items(now=now)
        return {iid: dict(info) for iid, info in self.items.get(account_id, {}).items()}

    def cleanup_items(self, aid=None, now=None):
        """清理过期道具：活动配置了转换规则的按比例转换，否则删除。"""
        now = now or datetime.now()
        for account_id, bag in list(self.items.items()):
            for item_id, info in list(bag.items()):
                if aid is not None and info.get("aid") != aid:
                    continue
                expire_at = info.get("expire_at")
                if expire_at is None or now <= expire_at:
                    continue
                act = self.activities.get(info.get("aid") or "")
                convert = act.item_convert.get(item_id) if act else None
                if convert:
                    target_id, rate = convert
                    converted = info["count"] * rate
                    del bag[item_id]
                    if converted > 0:
                        target = bag.setdefault(
                            target_id, {"count": 0, "expire_at": None, "aid": None}
                        )
                        target["count"] += converted
                else:
                    del bag[item_id]
            if not bag:
                del self.items[account_id]

    # ---------- 参与条件（多维度） ----------

    def can_participate(self, player_id, aid, player_level,
                        server_open_days=None, register_days=None,
                        register_time=None, now=None):
        """参与条件：等级 + 开服天数 + 注册时间，且活动已开启。"""
        act = self.activities[aid]
        now = now or datetime.now()
        if not self.is_open(aid, now=now):
            return False
        if player_level < act.min_level:
            return False
        if act.min_server_days > 0:
            if server_open_days is None or server_open_days < act.min_server_days:
                return False
        if act.min_register_days > 0:
            if register_days is None:
                if register_time is None:
                    return False
                register_days = (now - _parse_time(register_time)).days
            if register_days < act.min_register_days:
                return False
        return True

    # ---------- 结算（延迟窗口） ----------

    def is_settling(self, aid, now=None):
        """活动已结束但仍在结算延迟窗口内。"""
        act = self.activities[aid]
        now = now or datetime.now()
        _, end = self._window(act)
        return end < now <= end + timedelta(seconds=act.settle_delay_seconds)

    def can_settle(self, aid, now=None):
        """结算窗口已过，所有成绩均已入库，可以结算。"""
        act = self.activities[aid]
        now = now or datetime.now()
        _, end = self._window(act)
        return now > end + timedelta(seconds=act.settle_delay_seconds)

    def settle(self, aid, now=None):
        """结算：窗口未结束返回 None；结算后锁定最终排名并清理活动道具。"""
        if not self.can_settle(aid, now=now):
            return None
        if aid not in self.settled:
            self.settled[aid] = self.get_ranking(aid, now=now)
            self.cleanup_items(aid=aid, now=now)
        return list(self.settled[aid])
