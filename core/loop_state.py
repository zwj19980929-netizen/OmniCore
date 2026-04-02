"""
LoopState — cross-turn execution state tracking for the OmniCore graph.

Tracks replan count, error recovery, context compaction, adaptive skip,
and turn count so that downstream consumers (e.g. R5 Plan Reminder) can
make informed decisions without reaching back into the raw state dict.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class LoopState:
    """Tracks mutable, cross-turn execution metadata."""

    replan_count: int = 0
    max_replan: int = 3
    error_recovery_count: int = 0
    compact_applied: bool = False
    current_stage: str = ""
    resume_after_stage: Optional[str] = None
    adaptive_skip_applied: bool = False
    turn_count: int = 0
    # R5: tracks the turn when a task last changed status (for Reminder)
    last_status_change_turn: int = 0

    # -- helpers -------------------------------------------------------

    def can_replan(self) -> bool:
        return self.replan_count < self.max_replan

    def increment_replan(self) -> None:
        self.replan_count += 1

    def increment_turn(self) -> None:
        self.turn_count += 1

    def should_skip_stage(self, stage_order: int, resume_order: int) -> bool:
        """Return True when checkpoint-resume requires skipping *stage_order*."""
        if self.resume_after_stage is None:
            return False
        return stage_order <= resume_order

    # -- serialization -------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "replan_count": self.replan_count,
            "error_recovery_count": self.error_recovery_count,
            "compact_applied": self.compact_applied,
            "current_stage": self.current_stage,
            "resume_after_stage": self.resume_after_stage,
            "adaptive_skip_applied": self.adaptive_skip_applied,
            "turn_count": self.turn_count,
            "last_status_change_turn": self.last_status_change_turn,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LoopState":
        if not data:
            return cls()
        return cls(
            replan_count=data.get("replan_count", 0),
            error_recovery_count=data.get("error_recovery_count", 0),
            compact_applied=data.get("compact_applied", False),
            current_stage=data.get("current_stage", ""),
            resume_after_stage=data.get("resume_after_stage"),
            adaptive_skip_applied=data.get("adaptive_skip_applied", False),
            turn_count=data.get("turn_count", 0),
            last_status_change_turn=data.get("last_status_change_turn", 0),
        )
