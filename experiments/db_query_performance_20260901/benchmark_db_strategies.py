"""Rollback-safe PostgreSQL read benchmark for the natural-language screen path.

This file is intentionally isolated from the application packages. Every experiment
uses a new PostgreSQL connection and only creates TEMP objects. Closing the connection
drops the temporary tables, views, indexes, partitions, and statistics automatically.
No production table, view, index, migration, or application source is changed.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from ai_graph.data_sources.db import (
    DataSourceConfig,
    PostgresPipelineDataSource,
    _feature_frame_sql,
    _jsonb_projection,
    _mart_frame_sql,
    _MOMENTUM_KEYS,
    _TREND_KEYS,
    _VOLATILITY_KEYS,
    _VOLUME_KEYS,
)
from ai_graph.data_sources.sectors import extract_sector_from_query, get_known_sectors
from ai_graph.quant_strategy import rsi_trade_rules


QUERY = (
    "\ubc18\ub3c4\uccb4 \uc12f\ud130에서 RSI 30 \uc774\ud558에서 "
    "\ub9e4수할 \uc885\ubaa9 \ucc3e아줘"
)
TRADE_RULES = rsi_trade_rules(QUERY)
RSI_MAX = float(TRADE_RULES.entry_threshold)
BENCHMARK_TIMEOUT_MS = int(os.getenv("AI_BENCHMARK_TIMEOUT_MS", "120000"))
BENCHMARK_REPETITIONS = int(os.getenv("AI_BENCHMARK_REPETITIONS", "3"))
PARALLEL_WORKERS = int(os.getenv("AI_BENCHMARK_PARALLEL_WORKERS", "4"))
WORK_MEM = os.getenv("AI_BENCHMARK_WORK_MEM", "64MB")
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = SCRIPT_DIR / "benchmark_results.json"
TEMP_OBJECT_SUFFIX = uuid.uuid4().hex[:10]


@dataclass(frozen=True)
class Context:
    as_of: date
    sector: str
    mart_sql: str
    raw_sql: str
    raw_params: dict[str, Any]


@dataclass(frozen=True)
class Strategy:
    strategy_id: str
    title: str
    family: str
    description: str
    setup: Callable[[Any, Context], None] = field(default=lambda _conn, _ctx: None)
    query: Callable[[Any, Context], list[dict[str, Any]]] = field(default=lambda _conn, _ctx: [])
    settings: dict[str, str] = field(default_factory=dict)
    expected_unsupported: bool = False


def _connect(config: DataSourceConfig) -> Any:
    if not config.database_dsn:
        raise RuntimeError("AI_DATABASE_DSN is not configured")
    return psycopg.connect(
        config.database_dsn,
        connect_timeout=config.connect_timeout_seconds,
        row_factory=dict_row,
    )


def _set_benchmark_timeout(conn: Any) -> None:
    conn.execute(
        "SELECT set_config('statement_timeout', %s, false)",
        [f"{BENCHMARK_TIMEOUT_MS}ms"],
    )


def _numeric_projection(column: str, keys: dict[str, str]) -> str:
    expressions: list[str] = []
    for alias, source in keys.items():
        spellings = (source, f"{source}_2.0") if source.startswith("BB") else (source,)
        reads = ", ".join(f"{column}->>'{spelling}'" for spelling in spellings)
        expressions.append(f"(COALESCE({reads}))::numeric AS {alias}")
    return ",\n        ".join(expressions)


def _typed_select(source: str, *, filtered: bool = True, projection: str = "wide") -> str:
    trend = _numeric_projection("trend_values", _TREND_KEYS)
    volatility = _numeric_projection("volatility_values", _VOLATILITY_KEYS)
    momentum = _numeric_projection(
        "momentum_values",
        {key: value for key, value in _MOMENTUM_KEYS.items() if key != "rsi"},
    )
    volume = _numeric_projection("volume_values", _VOLUME_KEYS)
    if projection == "narrow":
        columns = "as_of_date AS time, base_ticker AS ticker, name, sector, close, " \
            "(COALESCE(momentum_values->>'RSI_14', momentum_values->>'rsi_14'))::numeric AS rsi"
    else:
        columns = (
            "as_of_date AS time, base_ticker AS ticker, name, sector, close, "
            "(COALESCE(momentum_values->>'RSI_14', momentum_values->>'rsi_14'))::numeric AS rsi, "
            f"{trend},\n        {volatility},\n        {momentum},\n        {volume}"
        )
    predicate = "WHERE sector = %(sector)s AND rsi <= %(rsi_max)s" if filtered else ""
    return f"""
        SELECT *
        FROM (
            SELECT {columns}
            FROM {source}
        ) typed
        {predicate}
        ORDER BY ticker
    """


def _base_params(ctx: Context) -> dict[str, Any]:
    return {"as_of": ctx.as_of, "prev": ctx.as_of, "sector": ctx.sector, "rsi_max": RSI_MAX}


def _fetch_rows(conn: Any, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return list(conn.execute(sql, params or {}).fetchall())


def _setup_raw_temp(conn: Any, ctx: Context) -> None:
    conn.execute(
        f"""
        CREATE TEMP TABLE tmp_feature_json ON COMMIT DROP AS
        SELECT frame.*
        FROM ({ctx.raw_sql}) frame
        """,
        ctx.raw_params,
    )


def _setup_typed_temp(conn: Any, ctx: Context) -> None:
    _setup_raw_temp(conn, ctx)
    conn.execute(
        f"CREATE TEMP TABLE tmp_feature_typed ON COMMIT DROP AS "
        f"SELECT * FROM ({_typed_select('tmp_feature_json', filtered=False)}) typed"
    )


def _setup_generated_temp(conn: Any, ctx: Context) -> None:
    _setup_raw_temp(conn, ctx)
    conn.execute("""
        ALTER TABLE tmp_feature_json
        ADD COLUMN rsi_generated numeric GENERATED ALWAYS AS
          ((COALESCE(momentum_values->>'RSI_14', momentum_values->>'rsi_14'))::numeric) STORED
    """)


def _setup_partitioned_temp(conn: Any, ctx: Context, partition_key: str) -> None:
    _setup_typed_temp(conn, ctx)
    parent = f"tmp_feature_partitioned_{partition_key}_{TEMP_OBJECT_SUFFIX}"
    default_partition = f"{parent}_default"
    if partition_key == "sector":
        conn.execute(f"""
            CREATE TEMP TABLE {parent} (
                ticker text, name text, sector text, close numeric, rsi numeric
            ) PARTITION BY LIST (sector) ON COMMIT DROP
        """)
        conn.execute(f"""
            CREATE TEMP TABLE {default_partition}
            PARTITION OF {parent} DEFAULT ON COMMIT DROP
        """)
    else:
        conn.execute(f"""
            CREATE TEMP TABLE {parent} (
                ticker text, name text, sector text, close numeric, rsi numeric, time date
            ) PARTITION BY RANGE (time) ON COMMIT DROP
        """)
        conn.execute(f"""
            CREATE TEMP TABLE {default_partition}
            PARTITION OF {parent} DEFAULT ON COMMIT DROP
        """)
    if partition_key == "sector":
        conn.execute(f"""
            INSERT INTO {parent} (ticker, name, sector, close, rsi)
            SELECT ticker, name, sector, close, rsi FROM tmp_feature_typed
        """)
    else:
        conn.execute(f"""
            INSERT INTO {parent} (ticker, name, sector, close, rsi, time)
            SELECT base_ticker, name, sector, close,
                   (COALESCE(momentum_values->>'RSI_14', momentum_values->>'rsi_14'))::numeric,
                   as_of_date
            FROM tmp_feature_json
        """)


def _setup_materialized_view(conn: Any, ctx: Context) -> None:
    _setup_typed_temp(conn, ctx)
    conn.execute("""
        CREATE TEMP MATERIALIZED VIEW tmp_feature_mv AS
        SELECT ticker, name, sector, close, rsi FROM tmp_feature_typed
    """)


def _setup_filter_table(conn: Any, ctx: Context) -> None:
    _setup_typed_temp(conn, ctx)
    conn.execute("CREATE TEMP TABLE tmp_sector_filter (sector text PRIMARY KEY) ON COMMIT DROP")
    conn.execute("INSERT INTO tmp_sector_filter (sector) VALUES (%s)", [ctx.sector])


def _setup_prepare_table(conn: Any, ctx: Context) -> None:
    _setup_typed_temp(conn, ctx)
    conn.execute("DEALLOCATE ALL")
    conn.execute("""
        PREPARE benchmark_rsi(text, numeric) AS
        SELECT ticker, name, sector, close, rsi
        FROM tmp_feature_typed
        WHERE sector = $1 AND rsi <= $2
        ORDER BY ticker
    """)


def _query_current(conn: Any, ctx: Context) -> list[dict[str, Any]]:
    sql = f"""
        SELECT *
        FROM ({ctx.mart_sql}) current_frame
        WHERE rsi <= %(rsi_max)s
        ORDER BY ticker
    """
    return _fetch_rows(conn, sql, _base_params(ctx))


def _query_cte(conn: Any, ctx: Context, materialized: bool) -> list[dict[str, Any]]:
    marker = "MATERIALIZED " if materialized else ""
    sql = f"""
        WITH frame AS {marker}(
            SELECT frame.*
            FROM ({ctx.raw_sql}) frame
        )
        {_typed_select('frame')}
    """
    return _fetch_rows(conn, sql, _base_params(ctx))


def _query_temp_json(conn: Any, _ctx: Context) -> list[dict[str, Any]]:
    return _fetch_rows(conn, _typed_select("tmp_feature_json"), _base_params(_ctx))


def _query_temp_typed(conn: Any, ctx: Context, projection: str = "wide") -> list[dict[str, Any]]:
    columns = "ticker, name, sector, close, rsi" if projection == "narrow" else "*"
    return _fetch_rows(
        conn,
        f"""
        SELECT {columns}
        FROM tmp_feature_typed
        WHERE sector = %(sector)s AND rsi <= %(rsi_max)s
        ORDER BY ticker
        """,
        _base_params(ctx),
    )


def _query_generated(conn: Any, ctx: Context) -> list[dict[str, Any]]:
    return _fetch_rows(
        conn,
        """
        SELECT base_ticker AS ticker, name, sector, close, rsi_generated AS rsi
        FROM tmp_feature_json
        WHERE sector = %(sector)s AND rsi_generated <= %(rsi_max)s
        ORDER BY base_ticker
        """,
        _base_params(ctx),
    )


def _query_partitioned(conn: Any, ctx: Context, partition_key: str) -> list[dict[str, Any]]:
    parent = f"tmp_feature_partitioned_{partition_key}_{TEMP_OBJECT_SUFFIX}"
    return _fetch_rows(
        conn,
        f"""
        SELECT ticker, name, sector, close, rsi
        FROM {parent}
        WHERE sector = %(sector)s AND rsi <= %(rsi_max)s
        ORDER BY ticker
        """,
        _base_params(ctx),
    )


def _query_mv(conn: Any, ctx: Context) -> list[dict[str, Any]]:
    return _fetch_rows(
        conn,
        """
        SELECT ticker, name, sector, close, rsi
        FROM tmp_feature_mv
        WHERE sector = %(sector)s AND rsi <= %(rsi_max)s
        ORDER BY ticker
        """,
        _base_params(ctx),
    )


def _query_join_filter(conn: Any, ctx: Context) -> list[dict[str, Any]]:
    return _fetch_rows(
        conn,
        """
        SELECT f.ticker, f.name, f.sector, f.close, f.rsi
        FROM tmp_feature_typed f
        JOIN tmp_sector_filter s ON s.sector = f.sector
        WHERE f.rsi <= %(rsi_max)s
        ORDER BY f.ticker
        """,
        _base_params(ctx),
    )


def _query_prepared(conn: Any, _ctx: Context) -> list[dict[str, Any]]:
    statement = sql.SQL("EXECUTE benchmark_rsi({}, {})").format(
        sql.Literal(_ctx.sector),
        sql.Literal(RSI_MAX),
    )
    return list(conn.execute(statement).fetchall())


def _query_any_array(conn: Any, ctx: Context) -> list[dict[str, Any]]:
    return _fetch_rows(
        conn,
        """
        SELECT ticker, name, sector, close, rsi
        FROM tmp_feature_typed
        WHERE sector = ANY(%(sectors)s) AND rsi <= %(rsi_max)s
        ORDER BY ticker
        """,
        {"sectors": [ctx.sector], "rsi_max": RSI_MAX},
    )


def _query_current_projection(conn: Any, ctx: Context) -> list[dict[str, Any]]:
    sql = f"""
        SELECT ticker, name, sector, close, rsi
        FROM ({ctx.mart_sql}) current_frame
        WHERE rsi <= %(rsi_max)s
        ORDER BY ticker
    """
    return _fetch_rows(conn, sql, _base_params(ctx))


def _setup_typed_analyze(conn: Any, ctx: Context) -> None:
    _setup_typed_temp(conn, ctx)
    conn.execute("ANALYZE tmp_feature_typed")


def _make_strategies() -> list[Strategy]:
    def setup_index(index_sql: str) -> Callable[[Any, Context], None]:
        def apply(conn: Any, ctx: Context) -> None:
            _setup_typed_temp(conn, ctx)
            conn.execute(index_sql)

        return apply

    def setup_setting(base_setup: Callable[[Any, Context], None], setting: str) -> Callable[[Any, Context], None]:
        def apply(conn: Any, ctx: Context) -> None:
            base_setup(conn, ctx)
            conn.execute(f"SET {setting}")

        return apply

    return [
        Strategy("S01", "현재 mart feature-frame SQL", "baseline", "현재 코드가 사용하는 feature-frame 뷰 대체 SQL과 동일한 읽기.", query=_query_current),
        Strategy("S02", "CTE predicate pushdown", "planner", "RSI predicate를 외부 SELECT가 아닌 typed CTE 경계 안에서 적용.", query=lambda c, x: _query_cte(c, x, False)),
        Strategy("S03", "CTE MATERIALIZED", "planner", "공통 frame CTE를 명시적으로 materialize.", query=lambda c, x: _query_cte(c, x, True)),
        Strategy("S04", "TEMP table from feature frame", "materialization", "실제 one-day feature frame을 TEMP TABLE로 물질화.", setup=_setup_raw_temp, query=_query_temp_json),
        Strategy("S05", "TEMP table + ANALYZE", "statistics", "TEMP TABLE 생성 후 통계 수집.", setup=_setup_typed_analyze, query=_query_temp_typed),
        Strategy("S06", "TEMP B-tree sector", "btree", "sector 선택도에 대한 B-tree.", setup=setup_index("CREATE INDEX tmp_sector_idx ON tmp_feature_typed (sector)"), query=_query_temp_typed),
        Strategy("S07", "TEMP B-tree sector/rsi", "btree", "sector와 RSI predicate를 함께 지원하는 복합 B-tree.", setup=setup_index("CREATE INDEX tmp_sector_rsi_idx ON tmp_feature_typed (sector, rsi)"), query=_query_temp_typed),
        Strategy("S08", "TEMP B-tree rsi/sector", "btree", "RSI 선행 복합 B-tree 대조군.", setup=setup_index("CREATE INDEX tmp_rsi_sector_idx ON tmp_feature_typed (rsi, sector)"), query=_query_temp_typed),
        Strategy("S09", "covering index", "index-only", "필요 컬럼을 INCLUDE한 covering index.", setup=setup_index("CREATE INDEX tmp_cover_idx ON tmp_feature_typed (sector, rsi) INCLUDE (ticker, name, close)"), query=_query_temp_typed, ),
        Strategy("S10", "partial RSI index", "partial-index", "현재 전략의 RSI 조건을 partial index predicate로 고정.", setup=setup_index(f"CREATE INDEX tmp_partial_rsi_idx ON tmp_feature_typed (sector, ticker) WHERE rsi <= {RSI_MAX}"), query=_query_temp_typed),
        Strategy("S11", "BRIN time index", "brin", "시계열 테이블에 적합한 BRIN 대조군.", setup=setup_index("CREATE INDEX tmp_time_brin_idx ON tmp_feature_typed USING brin (time)"), query=_query_temp_typed),
        Strategy("S12", "JSONB raw table + ANALYZE", "jsonb", "JSONB 원본을 유지하되 planner 통계를 수집.", setup=lambda c, x: (_setup_raw_temp(c, x), c.execute("ANALYZE tmp_feature_json")), query=_query_temp_json),
        Strategy("S13", "JSONB GIN default", "jsonb", "JSONB containment/operator 검색용 기본 GIN.", setup=lambda c, x: (_setup_raw_temp(c, x), c.execute("CREATE INDEX tmp_momentum_gin ON tmp_feature_json USING gin (momentum_values)")), query=_query_temp_json),
        Strategy("S14", "JSONB GIN path_ops", "jsonb", "JSONB containment 검색용 path_ops GIN.", setup=lambda c, x: (_setup_raw_temp(c, x), c.execute("CREATE INDEX tmp_momentum_path_gin ON tmp_feature_json USING gin (momentum_values jsonb_path_ops)")), query=_query_temp_json),
        Strategy("S15", "typed RSI projection", "jsonb", "RSI JSONB path를 numeric 컬럼으로 선계산.", setup=_setup_typed_temp, query=lambda c, x: _query_temp_typed(c, x, "narrow")),
        Strategy("S16", "generated RSI column", "jsonb", "JSONB RSI path를 generated stored column으로 고정.", setup=_setup_generated_temp, query=_query_generated),
        Strategy("S17", "typed wide projection", "jsonb", "전략에 필요한 typed indicator projection을 TEMP TABLE에 저장.", setup=_setup_typed_temp, query=_query_temp_typed),
        Strategy("S18", "TEMP materialized view", "materialization", "PostgreSQL의 TEMP MATERIALIZED VIEW 지원 여부를 실제로 검증하는 격리 실험.", setup=_setup_materialized_view, query=_query_mv, expected_unsupported=True),
        Strategy("S19", "sector partition pruning", "partitioning", "TEMP table을 sector LIST partition으로 분할.", setup=lambda c, x: _setup_partitioned_temp(c, x, "sector"), query=lambda c, x: _query_partitioned(c, x, "sector")),
        Strategy("S20", "time partition pruning", "partitioning", "TEMP table을 time RANGE partition으로 분할.", setup=lambda c, x: _setup_partitioned_temp(c, x, "time"), query=lambda c, x: _query_partitioned(c, x, "time")),
        Strategy("S21", "filter relation join", "join-shape", "sector predicate를 작은 relation과 JOIN.", setup=_setup_filter_table, query=_query_join_filter),
        Strategy("S22", "ANY(array) predicate", "predicate", "scalar equality 대신 ANY(array) predicate.", setup=_setup_typed_temp, query=_query_any_array),
        Strategy("S23", "prepared statement", "plan-cache", "동일 SQL을 PREPARE/EXECUTE로 반복 실행.", setup=_setup_prepare_table, query=_query_prepared),
        Strategy("S24", "narrow projection", "projection", "결과에 필요한 ticker/name/sector/close/RSI만 반환.", setup=_setup_typed_temp, query=lambda c, x: _query_temp_typed(c, x, "narrow")),
        Strategy("S25", "work_mem benchmark setting", "session-setting", "정렬/해시를 위한 세션 work_mem 상향.", setup=setup_setting(_setup_typed_temp, f"work_mem = '{WORK_MEM}'"), query=_query_temp_typed),
        Strategy("S26", "parallel workers benchmark setting", "parallel", "세션 parallel worker 상한을 설정.", setup=setup_setting(_setup_typed_temp, f"max_parallel_workers_per_gather = {PARALLEL_WORKERS}"), query=_query_temp_typed),
        Strategy("S27", "JIT off", "jit", "짧은 반복 query에서 JIT compilation 비용을 제거하는 대조군.", setup=setup_setting(_setup_typed_temp, "jit = off"), query=_query_temp_typed),
    ]


def _signature(rows: list[dict[str, Any]]) -> dict[str, Any]:
    keys = []
    for row in rows:
        keys.append(
            (
                str(row.get("ticker") or row.get("base_ticker") or ""),
                str(row.get("rsi") or row.get("rsi_generated") or ""),
            )
        )
    keys.sort()
    return {"row_count": len(rows), "rows": keys[:100]}


def _context(config: DataSourceConfig) -> Context:
    source = PostgresPipelineDataSource(config)
    with _connect(config) as conn:
        _set_benchmark_timeout(conn)
        sectors = get_known_sectors(conn=conn)
        sector = extract_sector_from_query(QUERY, sectors)
        if not sector:
            raise RuntimeError("sector extraction failed")
        as_of = source._resolve_screening_date(conn)
        if as_of is None:
            raise RuntimeError("screening date unavailable")
    raw_sql = _feature_frame_sql("as_of")
    return Context(
        as_of=as_of,
        sector=sector,
        mart_sql=_mart_frame_sql(sector=True),
        raw_sql=raw_sql,
        raw_params={"as_of": as_of},
    )


def _run_strategy_once(strategy: Strategy, config: DataSourceConfig, ctx: Context) -> dict[str, Any]:
    started = time.perf_counter()
    record: dict[str, Any] = {
        "strategy_id": strategy.strategy_id,
        "title": strategy.title,
        "family": strategy.family,
        "description": strategy.description,
        "settings": strategy.settings,
        "status": "failed",
    }
    try:
        with _connect(config) as conn:
            _set_benchmark_timeout(conn)
            setup_started = time.perf_counter()
            strategy.setup(conn, ctx)
            record["setup_seconds"] = round(time.perf_counter() - setup_started, 6)
            query_started = time.perf_counter()
            rows = strategy.query(conn, ctx)
            record["query_seconds"] = round(time.perf_counter() - query_started, 6)
            record["signature"] = _signature(rows)
            record["status"] = "ok"
    except Exception as exc:  # Record unsupported experiments without touching production objects.
        record["error_type"] = type(exc).__name__
        record["error"] = str(exc)[:500]
        record["traceback_tail"] = traceback.format_exc().splitlines()[-6:]
        if strategy.expected_unsupported:
            record["status"] = "unsupported"
    record["wall_seconds"] = round(time.perf_counter() - started, 6)
    return record


def run_strategy(
    strategy: Strategy,
    config: DataSourceConfig,
    ctx: Context,
    repetitions: int,
) -> dict[str, Any]:
    samples = [_run_strategy_once(strategy, config, ctx) for _ in range(max(1, repetitions))]
    successful = [sample for sample in samples if sample["status"] == "ok"]
    unsupported = [sample for sample in samples if sample["status"] == "unsupported"]
    first = samples[0]
    record = {
        key: first[key]
        for key in ("strategy_id", "title", "family", "description", "settings")
        if key in first
    }
    record["repetitions"] = len(samples)
    record["status"] = (
        "ok"
        if len(successful) == len(samples)
        else "unsupported"
        if len(unsupported) == len(samples)
        else "partial"
        if successful
        else "failed"
    )
    record["samples"] = [
        {
            key: sample[key]
            for key in (
                "status",
                "setup_seconds",
                "query_seconds",
                "wall_seconds",
                "signature",
                "error_type",
                "error",
            )
            if key in sample
        }
        for sample in samples
    ]
    if successful:
        for key in ("setup_seconds", "query_seconds", "wall_seconds"):
            record[key] = round(
                statistics.median(sample[key] for sample in successful),
                6,
            )
        record["db_seconds"] = round(
            statistics.median(
                sample["setup_seconds"] + sample["query_seconds"]
                for sample in successful
            ),
            6,
        )
        signatures = [sample["signature"] for sample in successful]
        record["signature"] = signatures[0]
        record["signature_consistent"] = all(signature == signatures[0] for signature in signatures)
    else:
        for key in ("error_type", "error", "traceback_tail"):
            if key in first:
                record[key] = first[key]
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--repetitions", type=int, default=BENCHMARK_REPETITIONS)
    args = parser.parse_args()
    config = DataSourceConfig.from_env()
    if not config.database_dsn:
        raise SystemExit("AI_DATABASE_DSN is required; fixture/mock fallback is forbidden")
    ctx = _context(config)
    started = datetime.now(timezone.utc)
    results = [run_strategy(strategy, config, ctx, args.repetitions) for strategy in _make_strategies()]
    payload = {
        "measured_at_utc": started.isoformat(),
        "source": "postgres",
        "dsn_env": config.database_dsn_env,
        "database_dsn_exposed": False,
        "query_label": "natural-language RSI oversold sector screen",
        "screening_profile": "rsi_rebound",
        "rsi_entry_operator": rsi_trade_rules(QUERY).entry_operator,
        "rsi_entry_threshold": rsi_trade_rules(QUERY).entry_threshold,
        "sector": ctx.sector,
        "as_of": ctx.as_of.isoformat(),
        "benchmark_timeout_ms": BENCHMARK_TIMEOUT_MS,
        "repetitions": args.repetitions,
        "rollback_policy": "connection close drops all TEMP objects",
        "strategies": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "source": payload["source"],
        "strategy_count": len(results),
        "ok_count": sum(item["status"] == "ok" for item in results),
        "unsupported_count": sum(item["status"] == "unsupported" for item in results),
        "failed_count": sum(item["status"] == "failed" for item in results),
        "as_of": payload["as_of"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
