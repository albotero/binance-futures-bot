"""Versioned closed-bar strategy shared by research and execution."""

from futures_bot.strategy.pullback import (
    EntrySetup,
    StrategyConfiguration,
    StrategyEvaluation,
    evaluate_pullback,
)

__all__ = [
    "EntrySetup",
    "StrategyConfiguration",
    "StrategyEvaluation",
    "evaluate_pullback",
]
