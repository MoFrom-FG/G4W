"""HOT/WARM/COLD bounded tier policy for vector retrieval (no hard delete).

Default capacity ratios: 0.75 / 0.20 / 0.05.
TTL metadata (days): HOT=30, WARM=180, COLD=365.
Tombstones mark logical removal; physical purge is out of scope (0 hard delete).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


class Tier(str, Enum):
    HOT = "HOT"
    WARM = "WARM"
    COLD = "COLD"


# Frozen defaults from H3 plan (execution plan T1)
HOT_RATIO = 0.75
WARM_RATIO = 0.20
COLD_RATIO = 0.05
TTL_DAYS = {Tier.HOT: 30, Tier.WARM: 180, Tier.COLD: 365}


@dataclass
class TierRecord:
    """Metadata for one indexed chunk/item."""

    item_id: str
    tier: Tier = Tier.HOT
    created_at: float = field(default_factory=time.time)
    last_access_at: float = field(default_factory=time.time)
    size_bytes: int = 0
    tombstone: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def age_days(self, now: Optional[float] = None) -> float:
        ts = float(now if now is not None else time.time())
        return max(0.0, (ts - float(self.created_at)) / 86400.0)


@dataclass
class TierPolicy:
    """Assign and bound items into HOT/WARM/COLD without hard deletes."""

    hot_ratio: float = HOT_RATIO
    warm_ratio: float = WARM_RATIO
    cold_ratio: float = COLD_RATIO
    ttl_days: Dict[Tier, int] = field(default_factory=lambda: dict(TTL_DAYS))
    # Soft capacity: max live (non-tombstone) items across all tiers.
    max_items: int = 10_000
    # Optional byte budget (0 = ignore).
    max_bytes: int = 0

    def __post_init__(self) -> None:
        total = float(self.hot_ratio) + float(self.warm_ratio) + float(self.cold_ratio)
        if total <= 0:
            raise ValueError("tier ratios must sum > 0")
        # Normalize if slightly off
        self.hot_ratio = float(self.hot_ratio) / total
        self.warm_ratio = float(self.warm_ratio) / total
        self.cold_ratio = float(self.cold_ratio) / total

    def capacity_for(self, tier: Tier) -> int:
        """Max live items allowed in a tier given max_items."""
        r = {
            Tier.HOT: self.hot_ratio,
            Tier.WARM: self.warm_ratio,
            Tier.COLD: self.cold_ratio,
        }[tier]
        n = max(1, int(self.max_items * r))
        return n

    def assign_by_age(self, age_days: float) -> Tier:
        """Map age (days) to tier using TTL cutoffs (HOT youngest)."""
        hot_ttl = int(self.ttl_days.get(Tier.HOT, 30))
        warm_ttl = int(self.ttl_days.get(Tier.WARM, 180))
        if age_days <= hot_ttl:
            return Tier.HOT
        if age_days <= warm_ttl:
            return Tier.WARM
        return Tier.COLD

    def assign_record(self, rec: TierRecord, now: Optional[float] = None) -> Tier:
        if rec.tombstone:
            return rec.tier
        tier = self.assign_by_age(rec.age_days(now))
        rec.tier = tier
        return tier

    def mark_tombstone(self, rec: TierRecord) -> TierRecord:
        """Logical delete only — never unlink storage."""
        rec.tombstone = True
        return rec

    def live_records(self, records: Iterable[TierRecord]) -> List[TierRecord]:
        return [r for r in records if not r.tombstone]

    def counts_by_tier(self, records: Iterable[TierRecord]) -> Dict[Tier, int]:
        out = {Tier.HOT: 0, Tier.WARM: 0, Tier.COLD: 0}
        for r in self.live_records(records):
            out[r.tier] = out.get(r.tier, 0) + 1
        return out

    def within_capacity(self, records: Sequence[TierRecord]) -> bool:
        live = self.live_records(records)
        if len(live) > self.max_items:
            return False
        if self.max_bytes > 0:
            total_b = sum(max(0, int(r.size_bytes)) for r in live)
            if total_b > self.max_bytes:
                return False
        counts = self.counts_by_tier(live)
        for t in Tier:
            if counts.get(t, 0) > self.capacity_for(t):
                return False
        return True

    def rebalance(self, records: List[TierRecord], now: Optional[float] = None) -> List[str]:
        """Demote oldest overflow from HOT→WARM→COLD; never hard-delete.

        Returns list of item_ids whose tier changed.
        """
        now_ts = float(now if now is not None else time.time())
        changed: List[str] = []
        for r in records:
            if r.tombstone:
                continue
            old = r.tier
            self.assign_record(r, now_ts)
            if r.tier != old:
                changed.append(r.item_id)

        # Enforce per-tier caps by demoting oldest in overflow tiers
        demote_chain = [Tier.HOT, Tier.WARM, Tier.COLD]
        for idx, tier in enumerate(demote_chain[:-1]):
            cap = self.capacity_for(tier)
            live_tier = [r for r in records if not r.tombstone and r.tier == tier]
            live_tier.sort(key=lambda r: r.last_access_at)
            overflow = len(live_tier) - cap
            if overflow <= 0:
                continue
            next_tier = demote_chain[idx + 1]
            for r in live_tier[:overflow]:
                r.tier = next_tier
                changed.append(r.item_id)

        # Global max_items: mark excess oldest as tombstone (still no hard delete)
        live = self.live_records(records)
        if len(live) > self.max_items:
            live_sorted = sorted(live, key=lambda r: r.last_access_at)
            excess = len(live_sorted) - self.max_items
            for r in live_sorted[:excess]:
                self.mark_tombstone(r)
                changed.append(r.item_id)
        return changed

    def filter_searchable(
        self,
        records: Sequence[TierRecord],
        tiers: Optional[Sequence[Tier]] = None,
    ) -> List[str]:
        """Return live item_ids in allowed tiers (default: all)."""
        allow = set(tiers) if tiers else {Tier.HOT, Tier.WARM, Tier.COLD}
        return [
            r.item_id
            for r in records
            if (not r.tombstone) and r.tier in allow
        ]


def default_policy(max_items: int = 10_000) -> TierPolicy:
    return TierPolicy(max_items=max_items)
