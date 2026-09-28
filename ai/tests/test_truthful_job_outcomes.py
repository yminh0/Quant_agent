"""A job must expose its actual terminal outcome for every user query."""

from __future__ import annotations

from ai_graph.analysis_capacity import AnalysisCapacityGate
from ai_graph.jobs import InMemoryAnalysisJobStore, run_job_sync
from ai_graph.schemas import APIEnvelope, EnvelopeStatus, UserPayload

TRIGGER_LIKE_QUERY = "거래량 기반 퀀트 전략"


def _clarification(_query: str, trace_id: str | None) -> APIEnvelope:
    return APIEnvelope(
        status=EnvelopeStatus.NEED_CLARIFICATION,
        trace_id=trace_id or "trace-clarification",
        user_payload=UserPayload(
            headline="추가 확인이 필요합니다.", message="조건을 확인해 주세요."
        ),
        debug_ref="clarification:conditions",
        retryable=False,
    )


def test_trigger_like_query_preserves_clarification() -> None:
    store = InMemoryAnalysisJobStore()
    job = store.create(TRIGGER_LIKE_QUERY)

    completed = run_job_sync(store, job.job_id, _clarification)

    assert completed.status.value == "completed"
    assert completed.result is not None
    assert completed.result.status is EnvelopeStatus.NEED_CLARIFICATION
    assert completed.result.user_payload.performance is None


def test_trigger_like_query_preserves_runner_failure() -> None:
    store = InMemoryAnalysisJobStore()
    job = store.create(TRIGGER_LIKE_QUERY)

    def unavailable(_query: str, _trace_id: str | None) -> APIEnvelope:
        raise RuntimeError("backtest engine unavailable")

    failed = run_job_sync(store, job.job_id, unavailable)

    assert failed.status.value == "failed"
    assert failed.result is not None
    assert failed.result.status is EnvelopeStatus.FAILED
    assert failed.result.user_payload.performance is None


def test_trigger_like_query_preserves_capacity_timeout() -> None:
    store = InMemoryAnalysisJobStore()
    job = store.create(TRIGGER_LIKE_QUERY)
    capacity = AnalysisCapacityGate(max_concurrency=1, queue_wait_seconds=0.01)

    def should_not_run(_query: str, _trace_id: str | None) -> APIEnvelope:
        raise AssertionError("capacity rejection must happen before execution")

    with capacity.slot():
        failed = run_job_sync(store, job.job_id, should_not_run, capacity=capacity)

    assert failed.status.value == "failed"
    assert failed.result is not None
    assert failed.result.status is EnvelopeStatus.FAILED
    assert failed.result.user_payload.performance is None
