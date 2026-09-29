"""限时活动系统 - 修复7个运营事故

fix1 开启时间精确到时分秒，支持预热期(warmup)
fix2 活动进度按账号统一，同账号多角色共享
fix3 奖励领取幂等，重复请求只生效一次
fix4 排行榜实时更新 + 缓存(默认5分钟刷新)
fix5 活动道具有过期时间，结束后自动清理/转换
fix6 参与条件包含开服天数、等级、注册时间等多维度
fix7 结算有延迟窗口(settle_delay)，窗口内成绩仍入库
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def parse_time(value, end_of_day=False):
    """统一解析时间；只给日期时，结束端按 23:59:59 处理。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in _TIME_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d" and end_of_day:
            parsed = parsed.replace(hour=23, minute=59, second=59)
        return parsed
    raise ValueError(f"无法解析时间: {value!r}")


@dataclass
class Activity:
    aid: str
    name: str
    start_date: str                      # 支持 "YYYY-MM-DD HH:MM:SS" 精确到秒
    end_date: str
    rewards: dict
    min_level: int
    warmup_start: str | None = None      # 预热期开始：可展示不可参与
    settle_delay: int = 3600             # fix7 结算延迟窗口（秒），默认1小时
    min_server_days: int = 0             # fix6 开服天数下限
    max_server_days: int | None = None   # fix6 开服天数上限（如仅限老区）
    register_after: str | None = None    # fix6 注册时间下限
    register_before: str | None = None   # fix6 注册时间上限
    claim_limit: int = 1                 # fix3 每账号每奖励限领次数
    item_convert: dict = field(default_factory=dict)  # fix5 过期道具 -> 转换道具

    @property
    def start_at(self):
        return parse_time(self.start_date)

    @property
    def end_at(self):
        return parse_time(self.end_date, end_of_day=True)

    @property
    def warmup_at(self):
        return parse_time(self.warmup_start) if self.warmup_start else self.start_at

    @property
    def settle_at(self):
        return self.end_at + timedelta(seconds=self.settle_delay)


class ActivitySystem:
    RANK_CACHE_TTL = 300  # fix4 排行榜缓存有效期（秒），最长5分钟刷新一次

    def __init__(self):
        self.activities: dict[str, Activity] = {}
        self.progress = {}          # account_id -> {aid: progress}   fix2 按账号
        self.claimed = {}           # account_id -> {aid: {reward_id: count}}
        self._accounts = {}         # player_id -> account_id
        self._players = {}          # player_id -> 等级/注册时间/开服时间
        self._scores = {}           # aid -> {account_id: best_score}
        self._rank_cache = {}       # aid -> (cached_at, ranking)
        self._items = {}            # account_id -> {item_id: {count, expire_at, aid}}
        self._claim_requests = {}   # request_id -> result            fix3 幂等键
        self._settled = set()
        self._lock = threading.RLock()

    # ---------- 账号体系（fix2） ----------

    def bind_account(self, player_id, account_id):
        self._accounts[player_id] = account_id

    def account_of(self, player_id):
        return self._accounts.get(player_id, player_id)

    def register_player(self, player_id, level=1, register_time=None,
                        server_open_time=None, account_id=None):
        if account_id is not None:
            self.bind_account(player_id, account_id)
        self._players[player_id] = {
            "level": level,
            "register_time": register_time,
            "server_open_time": server_open_time,
        }

    # ---------- 开启时间（fix1） ----------

    def status(self, aid, now=None):
        act = self.activities[aid]
        now = now or datetime.now()
        if now < act.warmup_at:
            return "not_started"
        if now < act.start_at:
            return "warmup"
        if now <= act.end_at:
            return "open"
        if now <= act.settle_at:
            return "settling"
        return "closed"

    def is_open(self, aid, now=None):
        # fix1: 精确到时分秒，预热期不算开启
        return self.status(aid, now) == "open"

    def is_warmup(self, aid, now=None):
        return self.status(aid, now) == "warmup"

    # ---------- 参与条件（fix6） ----------

    def can_participate(self, player_id, aid, player_level=None, now=None):
        act = self.activities[aid]
        now = now or datetime.now()
        info = self._players.get(player_id, {})
        level = player_level if player_level is not None else info.get("level", 0)
        if level < act.min_level:
            return False
        server_open = parse_time(info.get("server_open_time"))
        if act.min_server_days > 0 or act.max_server_days is not None:
            if server_open is None:
                return False
            server_days = (now - server_open).days
            if server_days < act.min_server_days:
                return False
            if act.max_server_days is not None and server_days > act.max_server_days:
                return False
        register_time = parse_time(info.get("register_time"))
        if act.register_after is not None:
            if register_time is None or register_time < parse_time(act.register_after):
                return False
        if act.register_before is not None:
            if register_time is None or register_time > parse_time(act.register_before):
                return False
        return True

    # ---------- 进度（fix2） ----------

    def add_progress(self, player_id, aid, amount, now=None):
        if not self.is_open(aid, now):
            return False
        account = self.account_of(player_id)
        with self._lock:
            record = self.progress.setdefault(account, {})
            record[aid] = record.get(aid, 0) + amount
        return True

    def get_progress(self, player_id, aid):
        return self.progress.get(self.account_of(player_id), {}).get(aid, 0)

    # ---------- 奖励领取（fix3） ----------

    def claim_reward(self, player_id, aid, reward_id, request_id=None):
        act = self.activities[aid]
        account = self.account_of(player_id)
        with self._lock:
            if request_id is not None and request_id in self._claim_requests:
                return self._claim_requests[request_id]  # 同一请求重放，不重复发放
            record = self.claimed.setdefault(account, {}).setdefault(aid, {})
            if record.get(reward_id, 0) < act.claim_limit:
                record[reward_id] = record.get(reward_id, 0) + 1
            result = act.rewards.get(reward_id)  # 重复领取返回原结果但计数不变
            if request_id is not None:
                self._claim_requests[request_id] = result
            return result

    # ---------- 排行榜（fix4 + fix7） ----------

    def submit_score(self, player_id, aid, score, now=None):
        now = now or datetime.now()
        # fix7: 结算延迟窗口内仍接受成绩，保证最后一秒提交的数据入库
        if self.status(aid, now) not in ("open", "settling"):
            return False
        account = self.account_of(player_id)
        with self._lock:
            scores = self._scores.setdefault(aid, {})
            if score > scores.get(account, float("-inf")):
                scores[account] = score
                self._rank_cache.pop(aid, None)  # 数据变化，缓存失效
        return True

    def get_ranking(self, aid, now=None):
        with self._lock:
            cached = self._rank_cache.get(aid)
            if cached and time.monotonic() - cached[0] < self.RANK_CACHE_TTL:
                return list(cached[1])
            ranking = sorted(self._scores.get(aid, {}).items(),
                             key=lambda kv: (-kv[1], kv[0]))
            self._rank_cache[aid] = (time.monotonic(), ranking)
            return list(ranking)

    # ---------- 活动道具（fix5） ----------

    def grant_item(self, player_id, item_id, count=1, aid=None, expire_at=None):
        account = self.account_of(player_id)
        if expire_at is None and aid is not None:
            expire_at = self.activities[aid].settle_at  # 活动道具结算后自动过期
        with self._lock:
            items = self._items.setdefault(account, {})
            entry = items.setdefault(item_id, {"count": 0, "expire_at": None, "aid": aid})
            entry["count"] += count
            entry["expire_at"] = parse_time(expire_at)
            entry["aid"] = aid

    def get_item(self, player_id, item_id, now=None):
        self.cleanup_expired(now)
        return self._items.get(self.account_of(player_id), {}).get(item_id, {}).get("count", 0)

    def cleanup_expired(self, now=None):
        """删除过期道具；活动配置了 item_convert 时自动转换。"""
        now = now or datetime.now()
        removed = {}
        conversions = []
        with self._lock:
            for account, items in self._items.items():
                for item_id in list(items):
                    entry = items[item_id]
                    expire_at = entry["expire_at"]
                    if expire_at is None or now <= expire_at:
                        continue
                    removed.setdefault(account, {})[item_id] = entry["count"]
                    aid = entry["aid"]
                    target = self.activities[aid].item_convert.get(item_id) if aid else None
                    if target:
                        conversions.append((account, target, entry["count"]))
                    del items[item_id]
            for account, target, count in conversions:
                entry = self._items[account].setdefault(
                    target, {"count": 0, "expire_at": None, "aid": None})
                entry["count"] += count
        return removed

    # ---------- 结算（fix7） ----------

    def settle(self, aid, now=None):
        """结算延迟窗口结束后才允许结算；返回最终排行榜。"""
        now = now or datetime.now()
        if now < self.activities[aid].settle_at:
            return None
        with self._lock:
            if aid in self._settled:
                return self.get_ranking(aid)
            self._settled.add(aid)
            for account, items in self._items.items():
                for item_id in list(items):
                    if items[item_id]["aid"] == aid:
                        items[item_id]["expire_at"] = now  # 活动道具到期清理
        self.cleanup_expired(now)
        return self.get_ranking(aid)
