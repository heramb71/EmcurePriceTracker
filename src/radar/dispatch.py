"""Alert selection: cooldown + daily budget + digest batching.

Pure decision logic (no I/O) so it is unit-testable. Given the gated, ranked
hits it decides which fire as individual Telegram messages (highest-confidence,
within the daily budget, not in cooldown) and which collapse into one digest —
so a 6-stock × 5-signal scan can never flood the channel.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src.radar.signals import SignalHit


@dataclass
class AlertGate:
    """Mutable per-process alert state (cooldowns + daily budget)."""

    max_per_day: int = 12
    cooldown_minutes: int = 90
    _day: str = ""
    _sent_today: int = 0
    _last_alerted: dict[tuple[str, str], datetime] = field(default_factory=dict)

    def _roll_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        if today != self._day:
            self._day = today
            self._sent_today = 0

    def select(
        self,
        ranked: list[tuple[SignalHit, int, int]],
        now: datetime,
    ) -> tuple[list[tuple[SignalHit, int, int]], list[tuple[SignalHit, int]]]:
        """Return (individual, digest).

        ``ranked`` must already be gate-filtered (confidence > SCORE_GATE) and
        sorted high→low. Mutates internal cooldown/budget state for the chosen
        individual alerts.
        """
        self._roll_day(now)
        individual: list[tuple[SignalHit, int, int]] = []
        digest: list[tuple[SignalHit, int]] = []
        cooldown = timedelta(minutes=self.cooldown_minutes)

        for hit, conf, rank in ranked:
            key = (hit.stock, hit.signal_type)
            last = self._last_alerted.get(key)
            if last is not None and now - last < cooldown:
                continue  # still cooling down — drop entirely
            if self._sent_today < self.max_per_day:
                individual.append((hit, conf, rank))
                self._last_alerted[key] = now
                self._sent_today += 1
            else:
                digest.append((hit, conf))  # over budget → batch into the digest
        return individual, digest
