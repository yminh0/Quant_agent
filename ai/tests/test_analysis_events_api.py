"""SSE activity is a user-visible part of the analysis-job contract."""

from __future__ import annotations

from fastapi.testclient import TestClient

from ai_graph.analysis_capacity import AnalysisCapacityGate
from ai_graph.api import ANALYSIS_JOB_EVENTS_PATH, create_app
from ai_graph.auth import DisabledSessionResolver
from ai_graph.jobs import InMemoryAnalysisJobStore, run_job_sync
from ai_graph.schemas import APIEnvelope


def _closed_job_with_events() -> tuple[TestClient, str]:
    store = InMemoryAnalysisJobStore()
    app = create_app(store, session_resolver=DisabledSessionResolver())
    job = store.create_job("RSI strategy", user_id="local-dev-user")
    app.state.job_events.publish(job.job_id, {"kind": "stage", "stage": "interpreting"})
    app.state.job_events.publish(job.job_id, {"kind": "stage", "stage": "backtest"})
    app.state.job_events.close(job.job_id)
    return TestClient(app), job.job_id


def test_analysis_event_stream_replays_ordered_events_then_done() -> None:
    client, job_id = _closed_job_with_events()

    response = client.get(ANALYSIS_JOB_EVENTS_PATH.format(job_id=job_id))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text == (
        'id: 1\ndata: {"kind": "stage", "stage": "interpreting"}\n\n'
        'id: 2\ndata: {"kind": "stage", "stage": "backtest"}\n\n'
        "event: done\ndata: {}\n\n"
    )


def test_analysis_event_stream_resumes_after_last_event_id() -> None:
    client, job_id = _closed_job_with_events()

    response = client.get(
        ANALYSIS_JOB_EVENTS_PATH.format(job_id=job_id), headers={"Last-Event-ID": "1"}
    )

    assert response.status_code == 200
    assert response.text == (
        'id: 2\ndata: {"kind": "stage", "stage": "backtest"}\n\nevent: done\ndata: {}\n\n'
    )


def test_analysis_event_stream_hides_other_users_job() -> None:
    store = InMemoryAnalysisJobStore()
    client = TestClient(create_app(store, session_resolver=DisabledSessionResolver()))
    foreign = store.create_job("private strategy", user_id="someone-else")

    missing_response = client.get(ANALYSIS_JOB_EVENTS_PATH.format(job_id="missing"))
    foreign_response = client.get(ANALYSIS_JOB_EVENTS_PATH.format(job_id=foreign.job_id))

    assert missing_response.status_code == foreign_response.status_code == 404
    assert missing_response.json() == foreign_response.json()


def test_capacity_timeout_closes_the_analysis_event_stream() -> None:
    store = InMemoryAnalysisJobStore()
    app = create_app(store, session_resolver=DisabledSessionResolver())
    job = store.create_job("queued strategy", user_id="local-dev-user")
    capacity = AnalysisCapacityGate(max_concurrency=1, queue_wait_seconds=0.01)

    def should_not_run(_query: str, _trace_id: str) -> APIEnvelope:
        raise AssertionError("capacity rejection must happen before execution")

    with capacity.slot():
        failed = run_job_sync(
            store,
            job.job_id,
            should_not_run,
            events=app.state.job_events,
            capacity=capacity,
        )

    response = TestClient(app).get(ANALYSIS_JOB_EVENTS_PATH.format(job_id=job.job_id))

    assert failed.status.value == "failed"
    assert response.status_code == 200
    assert "event: done\ndata: {}\n\n" in response.text
