"""Explicit, dependency-free orchestration for an analysis run.

The HTTP API and durable job runner own admission, cancellation, deadlines and
event delivery.  This module owns only the in-process sequence of analysis
stages.  Keeping that boundary small makes every terminal route visible here
and avoids having a second control-flow implementation when an optional graph
library is or is not installed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from ai_graph.state import QuantAgentState

PipelineResult = QuantAgentState | dict[str, Any]
PipelineNode = Callable[[QuantAgentState], PipelineResult]
StageInvoker = Callable[[str, PipelineNode, QuantAgentState], PipelineResult]
Route = Callable[[QuantAgentState], str]


@dataclass(frozen=True)
class AnalysisPipelineStages:
    """The only valid stage handlers and terminal routing decisions."""

    supervisor: PipelineNode
    ambiguity: PipelineNode
    data: PipelineNode
    research: PipelineNode
    backtest_code: PipelineNode
    backtest: PipelineNode
    signal: PipelineNode
    risk_manager: PipelineNode
    report: PipelineNode
    envelope: PipelineNode
    route_after_ambiguity: Route
    route_after_data: Route
    route_after_research: Route


class ExplicitAnalysisPipeline:
    """Run one analysis through its auditable linear state machine.

    Nodes return patches rather than mutating a hidden graph state.  The next
    branch is determined only after that patch is merged, and every branch ends
    at ``Envelope``.  Thus a clarification or unavailable-data result cannot
    accidentally flow into backtesting or be turned into a successful report.
    """

    def __init__(self, stages: AnalysisPipelineStages, *, invoke_stage: StageInvoker) -> None:
        self._stages = stages
        self._invoke_stage = invoke_stage

    def invoke(self, initial_state: Mapping[str, Any]) -> QuantAgentState:
        state = cast(QuantAgentState, dict(initial_state))
        state = self._apply(state, "Supervisor", self._stages.supervisor)
        state = self._apply(state, "Ambiguity Classifier", self._stages.ambiguity)

        if self._stages.route_after_ambiguity(state) == "final":
            return self._finalize(state)

        state = self._apply(state, "Data", self._stages.data)
        if self._stages.route_after_data(state) != "ready":
            return self._finalize(state)

        state = self._apply(state, "Research", self._stages.research)
        if self._stages.route_after_research(state) != "ready":
            return self._finalize(state)

        state = self._apply(state, "BacktestCode", self._stages.backtest_code)
        state = self._apply(state, "Backtest", self._stages.backtest)
        state = self._apply(state, "Signal", self._stages.signal)
        state = self._apply(state, "Risk Manager", self._stages.risk_manager)
        state = self._apply(state, "Report", self._stages.report)
        return self._finalize(state)

    def _finalize(self, state: QuantAgentState) -> QuantAgentState:
        return self._apply(state, "Envelope", self._stages.envelope)

    def _apply(self, state: QuantAgentState, name: str, node: PipelineNode) -> QuantAgentState:
        patch = self._invoke_stage(name, node, state)
        if not isinstance(patch, Mapping):
            raise TypeError(f"analysis stage {name!r} must return a mapping")
        return cast(QuantAgentState, {**state, **patch})
