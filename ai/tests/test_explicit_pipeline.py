from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from ai_graph.pipeline import AnalysisPipelineStages, ExplicitAnalysisPipeline, PipelineNode
from ai_graph.state import QuantAgentState


def _node(patch: Mapping[str, Any]) -> PipelineNode:
    def run(_state: QuantAgentState) -> dict[str, Any]:
        return dict(patch)

    return run


def _pipeline(
    *, ambiguity_route: str = "data", data_route: str = "ready", research_route: str = "ready"
):
    stages = AnalysisPipelineStages(
        supervisor=_node({}),
        ambiguity=_node({}),
        data=_node({}),
        research=_node({}),
        backtest_code=_node({}),
        backtest=_node({}),
        signal=_node({}),
        risk_manager=_node({}),
        report=_node({}),
        envelope=_node({"envelope": {"status": "ready"}}),
        route_after_ambiguity=lambda _state: ambiguity_route,
        route_after_data=lambda _state: data_route,
        route_after_research=lambda _state: research_route,
    )
    called: list[str] = []

    def invoke_stage(
        name: str, node: PipelineNode, state: QuantAgentState
    ) -> QuantAgentState | dict[str, Any]:
        called.append(name)
        return node(state)

    return ExplicitAnalysisPipeline(stages, invoke_stage=invoke_stage), called


def test_explicit_pipeline_runs_the_single_complete_path() -> None:
    pipeline, called = _pipeline()

    result = pipeline.invoke({"user_query": "RSI strategy"})

    assert called == [
        "Supervisor",
        "Ambiguity Classifier",
        "Data",
        "Research",
        "BacktestCode",
        "Backtest",
        "Signal",
        "Risk Manager",
        "Report",
        "Envelope",
    ]
    assert result.get("envelope") == {"status": "ready"}


@pytest.mark.parametrize(
    ("ambiguity_route", "data_route", "research_route", "expected"),
    [
        ("final", "ready", "ready", ["Supervisor", "Ambiguity Classifier", "Envelope"]),
        ("data", "final", "ready", ["Supervisor", "Ambiguity Classifier", "Data", "Envelope"]),
        (
            "data",
            "ready",
            "final",
            ["Supervisor", "Ambiguity Classifier", "Data", "Research", "Envelope"],
        ),
    ],
)
def test_terminal_routes_do_not_reach_backtest(
    ambiguity_route: str,
    data_route: str,
    research_route: str,
    expected: list[str],
) -> None:
    pipeline, called = _pipeline(
        ambiguity_route=ambiguity_route,
        data_route=data_route,
        research_route=research_route,
    )

    result = pipeline.invoke({"user_query": "not executable"})

    assert called == expected
    assert "backtest" not in result


def test_stage_must_return_a_state_patch() -> None:
    stages = AnalysisPipelineStages(
        supervisor=lambda _state: None,  # type: ignore[return-value]
        ambiguity=_node({}),
        data=_node({}),
        research=_node({}),
        backtest_code=_node({}),
        backtest=_node({}),
        signal=_node({}),
        risk_manager=_node({}),
        report=_node({}),
        envelope=_node({}),
        route_after_ambiguity=lambda _state: "final",
        route_after_data=lambda _state: "final",
        route_after_research=lambda _state: "final",
    )
    pipeline = ExplicitAnalysisPipeline(stages, invoke_stage=lambda _name, node, state: node(state))

    with pytest.raises(TypeError, match="must return a mapping"):
        pipeline.invoke({"user_query": "invalid"})
