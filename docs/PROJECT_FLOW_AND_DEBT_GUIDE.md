# QuantAgent 프로젝트 흐름 및 부채 해소 가이드

> 기준: 최초 작성 2026-07-18(`main`, commit `a005edf`), 2026-10-01 코드와 대조해 현재 구조·흐름을 갱신
>
> 목적: 새 팀원이 **무엇이 실제 실행되는지**, **데이터가 어디서 와서 어디로 가는지**, **왜 구조가 헷갈리는지**, **어떤 순서로 정리해야 하는지**를 한 문서에서 이해하게 한다.

## 문서의 표시 규칙

- **사실**: 저장소의 코드·설정·테스트·문서로 직접 확인한 내용
- **해석**: 여러 사실을 종합한 결론
- **제안**: 부채를 줄이기 위한 목표 상태
- **미확인**: 저장소만으로는 알 수 없고 실제 서버나 팀 결정을 확인해야 하는 내용

이 문서의 현재 구조 설명은 사실을 우선하며, 목표 구조와 정리 순서는 제안으로 구분한다.

---

## 1. 한 장으로 보는 결론

### 현재의 실질적인 MVP 실행 경로

**사실:** 현재 일반 배포 워크플로는 `combined_main.py` 하나(내부 :18011, `--workers 1`)와 FE 게이트웨이(`fe/scripts/production-gateway.mjs`, 내부 :18010)를 기동한다. `combined_main`은 Backend 앱을 `/`에, AI 앱을 `/ai-api`에 마운트하므로 Backend와 AI가 한 프로세스에서 함께 뜬다. 브라우저의 `/ai-api` 요청은 게이트웨이를 거쳐 combined 프로세스의 AI `/analysis-jobs`로 전달된다.

```mermaid
flowchart LR
    U["사용자 브라우저"] -->|"https://qt-agent.kro.kr:38010"| T["외부 터널<br/>38010 → 18010 · 38011 → 18011"]
    T --> FE["FE 게이트웨이 · production-gateway.mjs<br/>내부 :18010"]
    FE -->|"/ai-api/analysis-jobs"| AI["combined backend · FastAPI<br/>내부 :18011"]
    AI --> G["분석 그래프<br/>해석 → 데이터 → 연구 → 코드 → 백테스트 → 신호 → 리스크 → 리포트"]
    G --> BT["backtest_module"]
    BT --> G
    G --> ENV["공개 APIEnvelope"]
    ENV --> FE

    DE["DE · Airflow/수집 스크립트"] --> MDB[("시장 데이터 DB<br/>meta/raw/core/feature/mart")]
    MDB -. "DSN 설정 시" .-> G
    FIX["fixture/mock"] -. "DSN·AOAI 미설정 시(로컬 전용)" .-> G

    BE["Backend · 인증/이메일/리포트 아카이브<br/>combined 프로세스의 / 마운트"] --> ADB[("서비스 DB · app")]
```

근거:

- FE 개발 프록시: [`fe/vite.config.ts`](../fe/vite.config.ts#L35)
- FE가 호출하는 AI endpoint: [`fe/src/config/appConfig.ts`](../fe/src/config/appConfig.ts#L17)
- Backend·AI 마운트: [`combined_main.py`](../combined_main.py#L95)
- 일반 배포의 프로세스 기동: [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml#L794), [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml#L803)
- 운영 가이드가 정의한 MVP spine: [`docs/OPERATIONS.md`](OPERATIONS.md#mvp-spine)

배포 workflow는 새 venv에 `backtest_module`, `backend`, `ai`를 함께 설치하고 `import backtest_module, quantstats`로 백테스트 의존성을 확인한다. 근거: [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml#L640)

### 가장 중요한 네 가지 구조적 사실

1. **현재 사용자 분석 경로는 FE → combined 프로세스의 AI 앱이다.** Backend(인증, 서비스 DB, 이메일, 리포트 아카이브)는 같은 프로세스에 마운트되어 함께 배포된다.
2. **`POST /analysis-jobs`는 job을 큐에 넣고 즉시 `201`을 반환한다.** 그래프는 백그라운드에서 실행되고, 클라이언트는 `GET /analysis-jobs/{job_id}` 폴링과 `/events` 스트림으로 진행 상황을 받는다.
3. **같은 제품 개념이 여러 구현으로 존재한다.** FE가 두 벌(`fe/`, `backend/fe-api-preview/`)이고, `StrategySpec`도 공개 계약과 엔진 계약으로 나뉘어 정의되어 있다.
4. **mock/fixture와 실제 데이터가 함께 존재한다.** 로컬에서는 DSN이 없으면 fixture를 쓰지만, 릴리스 프로필은 fixture 분석을 거부하고 DSN이 설정된 상태의 DB 실패도 fixture로 바꾸지 않는다.

---

## 2. 저장소 지도

| 경로 | 실제 역할 | 현재 흐름에서의 위치 | 핵심 진입점 |
| --- | --- | --- | --- |
| `fe/` | 사용자 화면, 분석 요청, 결과 projection, 브라우저 저장 | 일반 배포의 공개 UI | `src/main.tsx`, `src/App.tsx`, `src/pages/AppPage.tsx` |
| `ai/` | 자연어 분석 API, 분석 그래프, LLM·데이터 adapter, 공개 envelope | 일반 배포의 분석 서비스 | `ai_graph/api.py`, `ai_graph/graph.py` |
| `backtest_module/` | 시그널 실행, 주문 체결, 성과 계산 | AI 그래프가 호출하는 엔진 | `backtest_module/backtest_module/backtest.py` |
| `DE/` | KRX/KIS/DART/BOK/SEIBro 수집, 정규화, 지표 계산, 품질 검사 | 분석 전에 DB를 채우는 독립 파이프라인 | `airflow/dags/quant_agent_data_engineering.py` |
| `service_db/` | 사용자·전략·AI 실행·백테스트·리포트·이메일 스키마 | Backend·AI 영속 경로의 DB 계약 | `migrations/011...027` |
| `backend/` | Google OAuth, 세션, 이메일 발송, 리포트 아카이브, 시세 티커, 생성 코드 subprocess executor(서비스 계층) | `combined_main.py`로 AI와 같은 프로세스에서 배포 | `app/main.py`, `api/routes/` |
| `backend/fe-api-preview/` | FE의 별도 preview 사본 | 일반 FE와 병렬로 존재 | 별도 `package.json`, `src/` |
| `.github/workflows/` | 테스트, 일반 배포, DE 배포, 서버 health | 운영 자동화 | `code-check.yml`, `deploy.yml`, `deploy-de.yml` |

### 권장 읽기 순서

1. 이 문서
2. [`ai/README_AI.md`](../ai/README_AI.md)
3. [`DE/docs/data_engineering_runbook.md`](../DE/docs/data_engineering_runbook.md)
4. [`service_db/docs/service_db_erd.md`](../service_db/docs/service_db_erd.md)
5. [`fe/README.md`](../fe/README.md)
6. Backend 작업이 필요할 때만 [`backend/docs/google-auth-backend-implementation.md`](../backend/docs/google-auth-backend-implementation.md)와 [`backend/docs/code-review-remediation-report.md`](../backend/docs/code-review-remediation-report.md)

---

## 3. 시작부터 끝까지: 실제 MVP 분석 흐름

### 3.1 분석 전: DE가 시장 데이터를 준비한다

Airflow의 일일 DAG(`quant_agent_daily_data_engineering`)는 매일 오전 10시(Asia/Seoul)에 직전 날짜를 대상으로 아래 작업을 조정한다. 같은 파일에 OHLCV 보정 DAG(매일 07:00), WICS 섹터 스냅샷 DAG(매주 월 06:00), AI 프롬프트 보존 정리 DAG(매일 05:00), 수동 실행용 10년 백필 DAG가 함께 있다.

```mermaid
flowchart TD
    CAL["거래일 결정<br/>core.trading_calendar"] --> OHLCV["기본 OHLCV 수집"]
    OHLCV --> META["종목 메타데이터 갱신"]
    OHLCV --> KIS["KIS 수정주가 수집"]
    OHLCV --> BOK["BOK 거시 데이터 수집"]
    META --> DART["DART 재무 데이터 수집"]
    KIS --> TA["TA 지표 계산"]
    META --> QA["데이터 품질 검사"]
    TA --> QA
    DART --> QA
    BOK --> QA

    OHLCV --> RAW[("raw")]
    RAW --> CORE[("core")]
    CORE --> FEATURE[("feature")]
    FEATURE --> MART[("mart/view")]
    QA --> OBS[("meta.data_quality_issue<br/>lineage/ingestion log")]
```

작업 의존성은 [`DE/airflow/dags/quant_agent_data_engineering.py`](../DE/airflow/dags/quant_agent_data_engineering.py#L118)와 같은 파일의 task chaining 부분([`L223`](../DE/airflow/dags/quant_agent_data_engineering.py#L223))에서 확인할 수 있다.

### 데이터 계층의 의미

| 계층 | 의미 | 대표 데이터 |
| --- | --- | --- |
| `meta` | 수집 실행, cursor, API 요청, 품질, lineage, 종목 universe | `ingestion_run`, `data_quality_issue`, `view_common_stock_universe` |
| `raw` | 원천 응답과 원문 근거 | OHLCV 응답, DART/BOK 응답, analyst report |
| `core` | 정규화된 기준 데이터 | 종목, 거래일, OHLCV |
| `feature` | 모델·전략이 직접 사용할 파생 데이터 | 수정주가, TA 지표, 재무·거시 feature |
| `mart` | 조회 편의를 위한 as-of view | feature frame, universe, BOK macro |
| `app` | 사용자와 서비스 실행 결과 | 전략, AI trace, 백테스트, 리포트, 이메일 |

스키마 소유권은 명확하다.

- `meta/raw/core/feature/mart`: `DE/migrations`
- `app`: `service_db/migrations`

근거: [`service_db/docs/service_db_erd.md`](../service_db/docs/service_db_erd.md#L61)

### 3.2 사용자가 FE에 진입한다

1. `fe/src/main.tsx`가 React 앱을 시작한다.
2. `fe/src/App.tsx`가 브라우저 경로를 직접 판별한다. 별도 router 라이브러리는 없다.
3. 보호 route 여부는 브라우저 `localStorage`의 `quantagent.auth.session.v1` 존재로 판단한다.
4. 개발 환경에서는 site password gate와 test login이 존재한다.

근거:

- route 분기: [`fe/src/App.tsx`](../fe/src/App.tsx#L24)
- FE 세션 저장: [`fe/src/api/authClient.ts`](../fe/src/api/authClient.ts#L5)
- 임시 test session: [`fe/src/api/authClient.ts`](../fe/src/api/authClient.ts#L66)

**중요:** FE의 route guard는 화면 표시용이다. 실제 AI API 인증이 켜져 있으면 AI는 `qa_session` cookie를 Redis에서 검증한다. `localStorage` 값만으로 API 권한이 생기지는 않는다.

근거: [`ai/ai_graph/auth.py`](../ai/ai_graph/auth.py#L15), [`ai/ai_graph/auth.py`](../ai/ai_graph/auth.py#L85)

### 3.3 사용자가 자연어 전략을 제출한다

`AppPage`는 사용자의 문장을 `createAnalysisJob(query)`에 넘긴다.

```mermaid
sequenceDiagram
    actor User as 사용자
    participant FE as FE AppPage
    participant Vite as /ai-api proxy
    participant API as AI FastAPI
    participant Store as Job Store
    participant Graph as Analysis Graph

    User->>FE: 자연어 전략 입력
    FE->>Vite: POST /ai-api/analysis-jobs
    Vite->>API: POST /analysis-jobs
    API->>Store: create_job(query, user_id) · QUEUED
    API-->>FE: 201 AnalysisJob (queued)
    API->>Graph: background task · run_job_sync → run_analysis
    Graph-->>Store: APIEnvelope로 complete/fail
    loop 2초 간격
        FE->>API: GET /analysis-jobs/{job_id}
        API-->>FE: 진행 중 또는 완료된 AnalysisJob
    end
    FE->>FE: localStorage 저장 + 화면 projection
```

근거:

- FE submit: [`fe/src/pages/AppPage.tsx`](../fe/src/pages/AppPage.tsx#L572)
- HTTP client: [`fe/src/api/quantAgentClient.ts`](../fe/src/api/quantAgentClient.ts#L258), [`fe/src/api/quantAgentClient.ts`](../fe/src/api/quantAgentClient.ts#L327)
- API가 job을 큐에 넣고 즉시 반환: [`ai/ai_graph/api.py`](../ai/ai_graph/api.py#L1422)
- job 실행: [`ai/ai_graph/jobs.py`](../ai/ai_graph/jobs.py#L827)

### job + polling 동작

FE는 2초 간격으로 job을 폴링한다([`AppPage.tsx`](../fe/src/pages/AppPage.tsx#L28)). 동시 실행 상한(`AI_ANALYSIS_MAX_CONCURRENCY`, 기본 1)을 넘은 job은 거절되지 않고 `QUEUED`로 대기하며, 요청 전역 deadline(`AI_JOB_DEADLINE_SECONDS`, 기본 1800초)을 넘기면 실패로 끝난다. 자세한 값은 [`docs/OPERATIONS.md`](OPERATIONS.md#분석-동시성-상한)를 본다.

### 3.4 AI 그래프가 입력을 해석한다

AI 그래프는 LangGraph 설치 여부와 관계없이 고정된 순서(`NODE_SEQUENCE`)를 직접 실행하는 `ExplicitAnalysisPipeline`을 사용한다.

```mermaid
flowchart TD
    S["Supervisor<br/>query 정규화, trace/debug_ref 생성"] --> A["Ambiguity Classifier<br/>웹서치로 요청을 실행 가능한 전략으로 확정<br/>(resolved_query) / 범위 밖이면 rejected"]
    A --> D["Data<br/>semantic slot, 요구 데이터, 검색, DB/fixture<br/>이후 모든 노드는 resolved_query 를 사용"]
    D -->|"READY"| R["Research<br/>Bull / Bear / Judge"]
    D -->|"clarification 또는 rejected"| E["Envelope"]
    R --> C["BacktestCode<br/>후보 코드 생성·AST 검증"]
    C --> B["Backtest<br/>후보 실행·성과 비교·선정"]
    B --> SG["Signal<br/>BUY / HOLD / DROP"]
    SG --> RM["Risk Manager<br/>시장 위험 규칙 적용"]
    RM --> RP["Report<br/>web + email projection"]
    RP --> E
    E --> OUT["공개 APIEnvelope"]
```

그래프 정의: [`ai/ai_graph/graph.py`](../ai/ai_graph/graph.py#L132), [`ai/ai_graph/graph.py`](../ai/ai_graph/graph.py#L170)

### 각 단계의 입력과 출력

| 단계 | 핵심 입력 | 핵심 출력 | 실패/분기 |
| --- | --- | --- | --- |
| Supervisor | 사용자 문장 | 정규화 query, trace, debug_ref | 빈 입력이면 실패 |
| Ambiguity | query + local KB | ambiguity 분류, 후보 3개, 질문 | READY가 아니면 무거운 분석 중단 가능 |
| Data | semantic slots | 요구 데이터, provenance, 가격, 후보, L4 evidence | DB 미설정 시 fixture |
| Research | 전략 의도 + 데이터 | bull/bear/judge debate | mock 또는 AOAI fallback |
| BacktestCode | StrategySpec | 검증된 코드 후보 | 모두 실패하면 deterministic fallback 또는 실패 |
| Backtest | 코드 + 가격/지표 | 후보별 성과, 선택 후보 | AST/실행/데이터 계약 실패 |
| Signal | 선택 백테스트 + evidence | BUY/HOLD/DROP, confidence | 누락 데이터는 confidence에 반영 |
| Risk Manager | 신호 + macro snapshot | 조정된 신호와 조정 사유 | 기본 macro 값 사용 가능 |
| Report | 전략·성과·리스크 | web/email report projection | LLM 실패 시 deterministic summary |
| Envelope | 전체 state | 공개 payload와 internal debug 분리 | 공개 응답은 내부 prompt/state 제외 |

### 3.5 Data 단계가 실제 DB와 fixture 중 하나를 선택한다

AI는 다음 순서로 DB DSN을 찾는다.

1. `AI_DATABASE_DSN`
2. `QUANT_DB_DSN`
3. `DATABASE_URL`

DSN이 있으면 PostgreSQL에서 다음 데이터를 읽는다.

| 용도 | 테이블/view |
| --- | --- |
| KIS 수정주가 | `feature.kis_adjusted_ohlcv_daily` |
| momentum/trend/volatility/volume 지표 | `feature.ta_*_ticker_daily` |
| 종목 universe | `meta.view_common_stock_universe` |
| 애널리스트 근거 | `raw.analyst_report_summary` |
| 거시 상태 | `mart.bok_macro_asof` |

근거: [`ai/ai_graph/data_sources/db.py`](../ai/ai_graph/data_sources/db.py#L35), [`ai/ai_graph/data_sources/db.py`](../ai/ai_graph/data_sources/db.py#L2248)

DSN이 없으면 `source: fixture`로 표시된 fixture bundle을 반환한다. 다만 릴리스 프로필(`APP_ENV`/`AI_RELEASE_PROFILE`이 `release`/`production`)에서는 fixture 분석 자체를 `fixture_mode_forbidden_in_release`로 거부한다([`ai/ai_graph/data_sources/db.py`](../ai/ai_graph/data_sources/db.py#L2338)). **DSN이 설정된 상태에서 PostgreSQL 조회가 실패하면 fixture로 전환하지 않고 실패로 끝난다.** 결과의 출처는 `pipeline_data_source.source`(`postgres` 또는 `fixture`)로 확인한다.

fixture bundle 자체에는 가격행이 없으므로 로컬 fixture 실행의 Backtest 단계는 내장된 가격 fixture(`DEFAULT_BACKTEST_PRICE_ROWS`)를 사용한다([`ai/ai_graph/nodes/backtest.py`](../ai/ai_graph/nodes/backtest.py#L231)).

### 3.6 LLM은 mock이 기본이고 AOAI는 opt-in이다

`AI_LLM_PROVIDER`가 없거나 `mock`이면 `MockLLMClient`, `aoai`이면 role별 또는 전역 Azure OpenAI 설정을 사용한다.

역할별 호출은 주로 다음 위치에 있다.

- Research: bull / bear / judge
- Backtest code: code generation
- Signal: bull / bear / judge
- Report: bull / bear / judge
- Strategy description

근거: [`ai/ai_graph/llm/factory.py`](../ai/ai_graph/llm/factory.py#L29)

### 3.7 생성 코드를 검증하고 백테스트한다

현재 직접 AI 경로는 다음 순서를 사용한다.

1. StrategySpec에서 코드 생성 계획을 만든다.
2. LLM 또는 deterministic template로 후보 코드를 만든다.
3. AST validator로 import와 금지 동작을 검사한다.
4. 허용된 builtins만 둔 namespace에서 `exec`한다.
5. `build_signals(rows)`를 호출해 시그널을 만든다.
6. `backtest_module` 엔진으로 주문·포지션·비용·성과를 계산한다.
7. 후보별 objective score를 비교하고 최고 후보를 선택한다.

근거:

- 후보 생성: [`ai/ai_graph/nodes/backtest_code.py`](../ai/ai_graph/nodes/backtest_code.py#L80)
- 직접 실행: [`ai/ai_graph/nodes/backtest.py`](../ai/ai_graph/nodes/backtest.py#L320)
- 엔진 adapter: [`ai/ai_graph/nodes/backtest.py`](../ai/ai_graph/nodes/backtest.py#L103)

### Backend에 남아 있는 fenced subprocess executor

Backend의 공개 route `/ai/backtests/generate-and-run`은 AI 그래프의 `/analysis-jobs`와 같은 기능을 다른 인증·실행 모델로 제공하던 두 번째 surface였기 때문에 제거되었다([`backend/app/main.py`](../backend/app/main.py#L142)). 서비스 계층(`app.services.ai_backtest_*`)은 남아 있으며, 임시 디렉터리, 별도 process group, resource limit, 실행 process identity 저장, release fence를 사용하는 fenced subprocess executor를 소유한다.

다만 이것은 완전한 container/network sandbox는 아니다. child가 parent 환경을 복사하고 OS subprocess/resource limit로 격리하는 수준이므로 secret 전달과 network 접근 정책은 별도로 검증해야 한다.

근거:

- service orchestration: [`backend/app/services/ai_backtest_flow.py`](../backend/app/services/ai_backtest_flow.py)
- subprocess executor: [`backend/app/services/ai_backtest_runtime.py`](../backend/app/services/ai_backtest_runtime.py)
- child runner: [`backend/app/services/ai_backtest_subprocess_runner.py`](../backend/app/services/ai_backtest_subprocess_runner.py)

**해석:** 공개 경로는 하나로 정리되었지만, AI 그래프는 아직 생성 코드를 in-process `exec`로 실행한다([`ai/ai_graph/nodes/backtest.py`](../ai/ai_graph/nodes/backtest.py#L3285)). Backend의 fenced executor로 옮기는 작업은 남아 있다.

### 3.8 Signal, Risk, Report가 사용자 결과를 만든다

Signal은 선택된 후보의 Sharpe와 drawdown, L4 evidence, bull/bear/judge 결과를 이용해 `BUY`, `HOLD`, `DROP`을 결정한다. 누락된 생산 adapter도 bear case와 confidence에 반영한다.

Risk Manager는 세 가지 시장 규칙과 포트폴리오 집중도 규칙을 적용한다. 시장 규칙의 입력은 `mart.bok_macro_asof`에서 채우며, 값이 없으면 해당 규칙은 "통과"가 아니라 "평가 안 함"으로 건너뛴다.

| 조건 | 조정 |
| --- | --- |
| KOSPI 종가 변화율 ≤ -5% (창고에 지수 시계열이 없어 유니버스 평균 proxy 사용) | BUY → HOLD, confidence ≤ 0.7 |
| 환율 일변화 절대값 > 2% | BUY confidence ≤ 0.7 |
| VKOSPI > 30 (현재 창고에 VKOSPI 시계열이 없어 실제로는 평가되지 않음) | BUY confidence ≤ 0.6 |
| 스크리닝 후보의 집중도 | 집중된 포트폴리오일수록 confidence 감액(최대 33%) |

근거: [`ai/ai_graph/nodes/risk_manager.py`](../ai/ai_graph/nodes/risk_manager.py#L19), [`ai/ai_graph/nodes/risk_manager.py`](../ai/ai_graph/nodes/risk_manager.py#L42)

Report는 같은 결과로 두 projection을 만든다.

- `web_projection`: FE 상세 화면용
- `email_projection`: 이메일 요약용

근거: [`ai/ai_graph/nodes/report.py`](../ai/ai_graph/nodes/report.py#L9)

### 3.9 APIEnvelope가 내부 정보와 공개 정보를 분리한다

최종 응답은 `APIEnvelope`로 고정된다. 대표 상태는 다음과 같다.

- `ready`
- `need_clarification`
- `rejected`
- `failed`

공개 payload에는 사용자 결과, StrategySpec, trace/debug reference, retry 가능 여부가 포함된다. node 전체 state, raw prompt, 내부 검증 trace는 공개 payload에서 제외한다.

근거: [`ai/ai_graph/schemas.py`](../ai/ai_graph/schemas.py#L192), [`ai/README_AI.md`](../ai/README_AI.md#L174)

### 3.10 FE가 결과를 화면 모델로 변환한다

FE는 AI 응답을 그대로 그리지 않는다. `quantAgentClient.ts`가 fixture 기반 화면 모델 위에 AI 결과를 overlay한다.

```mermaid
flowchart LR
    FIX["FE fixture<br/>기본 화면·목록·샘플"] --> MERGE["mergeAnalysisJobIntoOverview"]
    AI["AI AnalysisJob<br/>StrategySpec·performance·report"] --> MERGE
    MERGE --> VIEW["Overview / Trading / Performance / Reports"]
    VIEW --> LS[("localStorage<br/>최근 job·대화 이력")]
```

브라우저에 저장되는 주요 상태:

| key | 의미 |
| --- | --- |
| `quantagent.auth.session.v1` | FE가 보는 로그인 상태 |
| `quantagent.latest-analysis-job.v1` | 마지막 AI job |
| `quantagent.chat-conversations.v1` | 최대 8개 대화와 job 배열 |
| `quantagent.notification-settings.v1` | 로컬 알림 설정 |
| `quantagent.email-digest-strategies.v1` | 로컬 이메일 전략 선택 |

근거: [`fe/src/api/quantAgentClient.ts`](../fe/src/api/quantAgentClient.ts#L189), [`fe/src/pages/AppPage.tsx`](../fe/src/pages/AppPage.tsx#L27), [`fe/src/api/preferencesClient.ts`](../fe/src/api/preferencesClient.ts#L3), [`fe/src/utils/userScopedStorage.ts`](../fe/src/utils/userScopedStorage.ts)

---

## 4. 별도 Backend 경로는 무엇을 하는가

Backend는 단순 proxy가 아니라 `combined_main.py`의 `/`에 마운트되는 FastAPI 앱이다. 등록된 router는 다음과 같다([`backend/app/main.py`](../backend/app/main.py#L134)).

| router | 역할 |
| --- | --- |
| `health`, `readiness` | `/health`, `/readiness` |
| `auth` | Google OAuth와 Redis 기반 세션 (`/api/v1/...`과 legacy 경로) |
| `ai_account_tokens` | AI 계정 토큰 |
| `reports_pdf_temp` | 애널리스트 PDF 임시 수집 |
| `fe_contract` | `/api/v1/api-status`, `/api/v1/runs`, `/api/v1/reports` — 완료된 AI job을 사용자 소유 실행·리포트로 서비스 DB에 기록·조회. 하위에 `email_reports`(리포트 이메일 설정·재발송) router를 포함 |
| `market_ticker` | 시세 티커 |
| `pages` | 페이지 라우트 |

Backend의 주요 책임:

- Google OAuth와 Redis 기반 세션
- CSRF/origin/cookie 정책
- `app.*` 서비스 DB 접근
- 리포트 이메일 outbox와 발송 워커
- 생성 코드 fenced subprocess executor(서비스 계층, 공개 route 없음)
- PDF 임시 수집

---

## 5. 런타임 모드와 상태 저장 위치

### 5.1 기본값과 opt-in

| 관심사 | 기본/현재 코드 동작 | 실제 기능 opt-in |
| --- | --- | --- |
| LLM | `mock` | `AI_LLM_PROVIDER=aoai` + AOAI 설정 |
| 시장 데이터 | fixture (릴리스 프로필에서는 거부) | DB DSN 설정 |
| AI job store | memory | `AI_JOB_STORE=persistent` + DSN (DSN이 없으면 기동 시 `JobStoreConfigurationError`) |
| AI audit sink | noop | 승인된 Postgres Gate B 설정 |
| AI auth | enabled, fail-closed | 로컬만 `AUTH_ENABLED=0`; 운영은 Redis 필요 |
| graph runtime | `ExplicitAnalysisPipeline` (순차 실행 고정) | — |
| 수용 기준 게이트 | dev `report_only`, release `enforced` | `AI_VALIDATION_GATES` |
| Backend DB | `DATABASE_URL` 필수 | 별도 fallback 없음 |
| Backend Redis | auth 기능에 필요 | `REDIS_URL` |

### 기본값을 이해할 때의 함정

- AI 인증은 기본 활성화지만, 일반 배포 workflow는 `AUTH_ENABLED`, `REDIS_URL`을 직접 주입하지 않는다. 서버의 셸 환경이나 `~/mvp_sp2/quant-proj/.env`에서 읽는다.
- 실제 서버 shell 환경에 값이 주입되어 있을 수 있으므로 배포 성공 여부는 저장소만으로 확정할 수 없다. 배포 후 `deployed-release-smoke.yml`이 readiness와 실제 분석 1건을 검증한다.

### 5.2 상태의 실제 위치

| 상태 | 위치 | 수명 |
| --- | --- | --- |
| FE 로그인 표시 | browser localStorage | 브라우저별 |
| FE 최근 job/대화 | browser localStorage | 브라우저별 |
| AI job 기본 | Python process memory (`AI_JOB_STORE=persistent`면 PostgreSQL `app`) | memory는 재시작 시 소실 |
| AI debug store 기본 | Python process memory | 재시작 시 소실 |
| Google session | Redis | TTL/로그아웃까지 |
| 시장·feature 데이터 | PostgreSQL `meta/raw/core/feature/mart` | migration/retention 정책 |
| 사용자·실행·리포트 | PostgreSQL `app` | service DB 정책 |
| Backend child input/output | 임시 디렉터리 | 실행 종료 시 삭제 |

---

## 6. 인지부채 진단

인지부채는 “코드가 틀렸다”가 아니라 “올바른 정신 모델을 만드는 데 불필요한 추론이 많이 든다”는 뜻이다.

| 우선순위 | 인지부채 | 확인된 근거 | 생기는 혼란 | 해소 원칙 |
| --- | --- | --- | --- | --- |
| C0 | ~~공개 실행 경로가 두 이야기로 존재~~ **해소** | `combined_main.py`가 Backend와 AI를 한 프로세스로 배포하고, Backend의 `/ai/backtests/generate-and-run`은 제거됨 | — | — |
| C0 | ~~job/polling 이름과 동기 동작 불일치~~ **해소** | POST가 job을 큐에 넣고 즉시 반환, 그래프는 background task로 실행 | — | — |
| C0 | 화면 데이터가 fixture와 AI 결과의 합성 | FE hybrid projection | 어떤 숫자가 실제 분석 결과인지 판단 어려움 | field-level provenance 표시 또는 fixture 제거 |
| C1 | ~~동일 URL 계약의 mock Backend adapter~~ **해소** | Backend `fe_contract`는 `/api/v1` 아래에서 완료된 AI job을 서비스 DB에 기록·조회하며 `/analysis-jobs`를 갖지 않음 | — | — |
| C1 | StrategySpec가 여러 의미로 중복 | AI 공개 계약, engine, signal에 각각 정의 (`quantagent_strategy`는 저장소에서 제거됨) | “canonical”이라는 이름이 실제 의존성과 다름 | 용도별 이름 + 단일 adapter |
| C1 | FE가 두 벌 | `fe/`, `backend/fe-api-preview/` | 어떤 화면을 수정해야 하는지 불명확 | 단일 FE만 유지, preview는 build artifact로 대체 |
| C1 | ~~graph라는 이름과 fallback 순차 runtime~~ **해소** | `ExplicitAnalysisPipeline`이 LangGraph 여부와 관계없이 고정 순서를 실행 | — | — |
| C2 | 대형 파일이 많은 책임을 흡수 | `graph.py` 4,127줄 등 | 변경 영향 범위를 읽기 어렵고 리뷰 비용 증가 | 추상화 추가가 아니라 안정된 경계 기준으로 파일만 분리 |
| C2 | 생성 산출물이 추적됨 | 31,567줄 `report.md`, 약 73MB `node_results.json` (추적되던 `.pyc`는 제거됨) | 검색 결과와 코드량 통계 왜곡 | 생성물은 CI artifact로 이동하고 Git에서 제거 |

### StrategySpec 중복을 정확히 이해하는 법

현재 “하나로 합치면 된다”보다 먼저 역할을 분리해야 한다.

- `ai/ai_graph/schemas.py::StrategySpec`: 사용자에게 공개되는 분석 계약
- `backtest_module/.../models.py::StrategySpec`: 백테스트 실행 설정이 풍부한 엔진 계약
- `ai_graph/nodes/signal.py`의 signal models: 단일 시점 신호 계산용 로컬 계약

(독립 실험 패키지 `quantagent_strategy`의 StrategySpec은 패키지와 함께 저장소에서 제거되었다.)

**제안:** 공개 계약과 엔진 계약은 억지로 하나로 합치지 않는다. 이름을 명확히 하고 `ai_graph/nodes/backtest.py`의 adapter를 유일한 변환 경계로 둔다.

---

## 7. 기술부채 진단

### P0 — 운영·보안·정합성에 직접 영향

| 항목 | 사실 | 위험 | 완료 조건 |
| --- | --- | --- | --- |
| ~~배포가 개발 서버를 사용~~ **해소** | AI는 `uvicorn combined_main:app --workers 1`(reload 없음), FE는 build 결과를 `production-gateway.mjs`로 서빙 | — | — |
| 배포가 테스트 성공에 의존하지 않음 | `code-check.yml`과 `deploy.yml`은 서로 독립된 main-push workflow (배포 전 offline release-trust gate는 있음) | CI 실패와 배포 성공이 동시에 가능 | 검증 성공 artifact/revision만 배포 |
| Node 버전 불일치 | CI와 배포의 release-trust job은 24.15.0, 배포의 나머지 job setup은 20 | 재현성 저하, 로컬·CI·서버 차이 | 하나의 버전을 모든 문서·CI·배포에 고정 |
| ~~clean venv의 백테스트 의존성 누락~~ **해소** | 배포가 `backtest_module`, `backend`, `ai`를 함께 설치하고 `import backtest_module, quantstats`로 확인 | — | — |
| ~~Backend 기능이 일반 배포에 없음~~ **해소** | `combined_main.py`로 AI와 같은 프로세스에서 기동 | — | — |
| 생성 코드 실행 모델 이중화 | Backend 공개 route는 제거됐지만 AI는 여전히 in-process `exec`, fenced subprocess executor는 Backend 서비스 계층에만 있음 | 생성 코드가 가장 강한 경계를 통과하지 않음 | AI 그래프가 fenced executor를 호출하도록 전환 |
| 인증 상태 이중화 | FE localStorage guard, 서버 cookie/Redis auth | 화면은 로그인인데 API는 401이거나 반대인 상태 | 앱 시작 시 `/auth/me`를 canonical session source로 사용 |
| 배포 종료가 포트만 신뢰 | 18011/18010 listener PID에 소유권 확인 없이 TERM/KILL | 같은 포트를 쓰는 다른 process 종료 가능 | 저장한 PID·시작시각·실행파일·argv를 검증한 뒤 소유 process만 종료 |

배포 근거: [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml#L162), [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml#L794)

### P1 — 변경 비용·회귀 위험에 영향

| 항목 | 사실 | 위험 | 완료 조건 |
| --- | --- | --- | --- |
| ~~백테스트 소스가 두 위치에서 갈라짐~~ **해소** | 엔진 소스는 `backtest_module/backtest_module/` 하나만 남음 | — | — |
| FE 사본이 이미 drift | 동일 파일도 있고 서로 다른 파일도 다수 | 수정 누락, bug 재발 | preview 사본 제거; 한 FE build만 사용 |
| 영속 migration의 전역 history 부재 | service DB 문서가 수동 baseline과 경로 기반 식별을 요구 | 재적용/순서 오류 | DE+service DB를 아우르는 단일 migration ledger와 검증 명령 |
| job store가 기본 memory | 재시작 시 job/result 소실 (`AI_JOB_STORE=persistent`는 DSN이 없으면 fail-closed) | 복원·감사·다중 process 불가 | 운영에서는 persistent를 요구 |
| ~~persistent adapter의 실제 조립점이 없음~~ **해소** | AI app이 DSN으로 `PostgresAnalysisJobRepository`를 조립하고 `/readiness`에 active mode를 노출 | — | — |
| 호환 endpoint의 인증/소유권 누락 | AI daily digest와 Backend FE contract 쓰기 route 일부에 사용자 dependency가 없음 | Backend 노출 시 사용자 격리·CSRF 계약 불일치 | dev-only router 격리 또는 canonical auth dependency 적용 |
| CI가 전체 suite를 실행하지 않음 | backend 일부 smoke, AI 일부 contract만 일반 workflow에서 실행 | 통과하지 않은 영역의 회귀가 main에 유입 | 변경 경로별 전체 lint/test matrix 또는 최소한 full unit suites |
| FE interaction 테스트 부재 | `npm test`는 `fe/scripts/*.test.mts` node test + typecheck + build이며, 화면 interaction test는 없음 | interaction/polling/provenance 회귀 | 핵심 submit→ready/clarification/error 3개만 자동화 |
| ~~LangGraph dependency/의도 불명확~~ **해소** | 순차 `ExplicitAnalysisPipeline`으로 공식화 | — | — |
| ~~readiness가 핵심 기능을 검사하지 않음~~ **해소** | `/readiness`·`/ai-api/readiness`를 `readiness-semantic-gate.mjs`로 검사하고, `deployed-release-smoke.yml`이 배포 후 실제 분석 1건을 검증 | — | — |

### P2 — 저장소 위생·탐색 비용

| 항목 | 사실 | 조치 |
| --- | --- | --- |
| 생성 artifact가 저장소를 지배 | `node_results.json` 약 72MB·2,083,379줄, `report.md` 31,567줄 | golden input만 남기고 결과는 CI artifact로 업로드 |
| ~~`.pyc` 3개 추적~~ **해소** | `quantagent_strategy`와 함께 제거됨 | — |
| 파일 크기 집중 | graph/repository/flow/client가 800~4,100줄 | 새 framework 없이 안정된 책임 경계로만 분할 |
| DE dependency 선언 | `DE/requirements.txt`는 `.gitignore` 대상이라 저장소에 의존성 선언이 없음 | 재현 가능한 단일 선언을 저장소에 둠 |
| ~~DE 문서와 DAG drift~~ **해소** | `DE/README.md`의 DAG 설명을 실제 DAG에 맞춤 | — |
| 배포 workflow 중복 | DE 변경이 일반 배포와 DE 배포를 모두 실행하며 concurrency group도 공유 | 서비스별 변경 경로와 배포 책임을 분리 |

---

## 8. 부채를 줄인 목표 구조

### 목표: 공개 경로 하나, 실행 경계 하나, 계약별 소유자 하나

```mermaid
flowchart LR
    U["브라우저"] --> GW["Backend · 단일 공개 ingress"]
    GW --> AUTH["Auth/Session"]
    GW --> API["Analysis/Report API"]
    API --> AI["AI Orchestrator"]
    AI --> EX["단일 Generated-Code Executor<br/>AST + subprocess limits"]
    EX --> ENG["backtest_module"]
    AI --> MDB[("시장 데이터 DB<br/>DE 소유")]
    GW --> ADB[("서비스 DB<br/>service_db 소유")]
    DE["Airflow/DE"] --> MDB
    AI --> LLM["Mock 또는 AOAI"]
    AI --> GW
    GW --> U
```

### 목표 구조의 결정 사항

| 주제 | 제안 | 이유 |
| --- | --- | --- |
| 공개 ingress | Backend 하나 | auth, cookie, CSRF, 서비스 DB, API base URL을 한 경계로 모음 |
| AI API | 내부 서비스 또는 Backend 호출 library | 사용자에게 두 API surface를 노출하지 않음 |
| 생성 코드 | Backend의 fenced subprocess 원칙을 유일한 실행 경계로 재사용 | 이미 있는 강한 경계를 버리지 않음 |
| job 모델 | 먼저 sync를 정직하게 표현 | queue/worker는 p95 시간이나 동시성 요구가 생길 때만 추가 |
| StrategySpec | 공개 spec과 engine spec을 구분, adapter 하나 | 서로 다른 책임을 억지로 합치지 않음 |
| FE | `fe/` 하나 | preview 복제 대신 동일 build에 mock mode를 둠 |
| provenance | 모든 결과 section에 `source: fixture | postgres | aoai | deterministic` | 혼합 결과 오해 방지 |
| migration | 한 ledger에서 DE/app 적용 이력 기록 | schema owner는 유지하되 적용 상태는 통합 |

### 당장 추가하지 않을 것

- 별도 microservice 추가
- 새 queue/worker dependency
- 새 schema registry 제품
- 새로운 frontend framework/router
- 새 abstraction 계층

이들은 현재 부채의 원인이 아니다. 먼저 중복을 지우고 실제 경로를 하나로 만드는 것이 더 작고 효과가 크다.

---

## 9. 실행 순서가 있는 해소 로드맵

### Phase 0 — 진실을 하나로 고정

목표: 팀이 같은 시스템을 말하게 한다.

- [ ] “일반 배포의 공개 ingress는 Backend다 / AI 직결이다” 중 하나를 ADR로 결정
- [ ] 실제 서버의 process, reverse proxy, env injection, 공개 URL을 read-only로 확인
- [x] `/health`뿐 아니라 인증된 `/analysis-jobs` smoke를 추가
- [ ] mock/fixture/live 결과를 응답과 화면에서 구분
- [x] sync job을 공식화하고 fake progress를 제거하거나, 실제 async 전환을 별도 계획으로 분리

완료 조건: 한 장짜리 배포 다이어그램과 실제 process/URL이 일치한다.

### Phase 1 — 중복을 삭제

목표: 같은 역할을 수정할 파일이 하나만 남게 한다.

- [ ] `backend/fe-api-preview`의 실제 consumer 확인 후 제거
- [x] 백테스트 import path를 하나로 고정하고 중복 root module 제거
- [x] `quantagent_strategy` consumer가 없음을 검증한 뒤 보관/삭제 결정
- [ ] tracked `.pyc`와 생성 report 제거
- [x] mock contract endpoint는 `/dev` namespace 또는 test app으로 격리

완료 조건: FE, 백테스트 엔진, 분석 endpoint 각각 canonical 경로가 하나다.

### Phase 2 — 실행 경계를 통합

목표: 어느 route로 실행해도 같은 안전·정합성 규칙을 적용한다.

- [ ] 생성 코드 실행을 fenced subprocess 하나로 통합
- [ ] AST validator 중복을 하나로 통합
- [ ] 공개 spec → engine spec adapter를 단일 함수와 contract test로 고정
- [ ] auth source를 `/auth/me` + HttpOnly cookie로 통일
- [ ] Backend/AI 사이 trace_id, user_id, strategy_id 전달 계약 고정

완료 조건: 생성 코드를 in-process `exec`하는 공개 경로가 없다.

### Phase 3 — 영속성과 운영 계약을 고정

목표: 재시작과 배포 뒤에도 실행을 설명할 수 있게 한다.

- [ ] 운영 job store를 persistent 또는 명시적 sync response로 고정
- [ ] DE와 service DB migration ledger 통합
- [x] production command에서 `--reload`와 Vite dev server 제거
- [ ] Python/Node 버전을 README·CI·배포에서 통일
- [ ] 운영 audit sink 활성화 조건을 실제 환경에서 검증

완료 조건: 배포 후 trace 하나로 request → model → code → execution → report를 조회할 수 있다.

### Phase 4 — 최소 회귀 방지

목표: 복잡한 suite가 아니라 핵심 흐름이 깨지면 바로 알게 한다.

- [ ] FE: ready / clarification / error 3개 흐름
- [ ] API: auth-on + persistent + DB fixture 통합 smoke
- [ ] Executor: 금지 import, timeout, memory, 정상 실행 4개
- [ ] Contract: public StrategySpec → engine spec → result projection 1개 e2e
- [ ] Deployment: production process와 protected endpoint health

완료 조건: 위 다섯 종류의 check가 main merge 전에 자동 실행된다.

---

## 10. 변경할 때 어디를 봐야 하는가

| 변경 목적 | 먼저 볼 파일 | 같이 확인할 계약 |
| --- | --- | --- |
| 사용자 입력/화면 | `fe/src/pages/AppPage.tsx` | `types/quantagent.ts`, `quantAgentClient.ts` |
| API endpoint | `ai/ai_graph/api.py` | `jobs.py`, FE client, contract tests |
| 자연어 해석 | `ai/ai_graph/graph.py`, `ai/ai_graph/research_contract.py` | StrategySpec, 실행 스펙 schema |
| DB 조회 | `ai/ai_graph/data_sources/db.py` | DE migration/view, data availability |
| 코드 생성 | `ai/ai_graph/nodes/backtest_code.py` | AST validator, LLM prompt/schema |
| 백테스트 | `ai/ai_graph/nodes/backtest.py` | `backtest_module`, spec adapter |
| 신호/리스크 | `nodes/signal.py`, `nodes/risk_manager.py` | macro source와 fallback |
| 리포트 | `nodes/report.py` | FE report projection, service DB report schema |
| 인증 | `backend/app/api/routes/auth.py` | AI `auth.py`, Redis key/cookie 이름 |
| 영속 AI backtest | `backend/app/services/ai_backtest_flow.py` | repository, migrations 011/015/016 |
| 시장 데이터 | `DE/airflow/dags/...` | source client, repository, quality test |
| DB schema | `DE/migrations`, `service_db/migrations` | 실제 적용 ledger와 replay test |

### trace 기반 장애 추적 순서

```mermaid
flowchart LR
    UI["FE 오류/빈 화면"] --> JOB["job_id 확인"]
    JOB --> TRACE["trace_id/debug_ref 확인"]
    TRACE --> API["AI job 상태·failure_cause"]
    API --> DATA["data source metadata/provenance"]
    API --> AUDIT["model/agent/error audit"]
    AUDIT --> EXEC["code_id/execution_run_id"]
    EXEC --> DB["backtest/report row"]
```

진단 원칙:

1. 화면 fixture 문제인지 AI result 문제인지 먼저 분리한다.
2. `job_id`보다 `trace_id`를 서비스 간 공통 키로 사용한다.
3. DB fallback, mock LLM, noop audit 여부를 먼저 확인한다.
4. generated code 내용보다 validation/execution status를 먼저 본다.
5. 실제 서버 문제는 `/health`만으로 정상 판정하지 않는다.

---

## 11. 검증 명령의 기준

기존 상세 실행 절차는 각 하위 README를 따른다. 새 umbrella script나 dependency는 만들지 않는다.

### 변경 영역별 최소 검증

| 영역 | 최소 검증 |
| --- | --- |
| FE | `npm --prefix fe run test` + 핵심 화면 smoke |
| AI | `pytest`의 graph/API/contract 대상 + `ruff` |
| Backtest | `pytest backtest_module/tests` |
| Backend | `pytest backend/tests/unit` 중 변경 서비스 전체 |
| DE | 해당 source/ingestion/quality test + DAG import |
| Migration | static SQL test + disposable DB replay |
| 배포 | 실제 production process 확인 + 보호 endpoint smoke |

**사실:** 현재 일반 CI는 이 전체 표를 모두 실행하지 않는다. 특히 FE의 `test` script는 `fe/scripts/*.test.mts` node test와 typecheck·build이며 화면 interaction test가 없다([`fe/package.json`](../fe/package.json#L11)).

---

## 12. 저장소만으로 확정할 수 없는 것

다음은 실제 서버 또는 팀 결정 확인이 필요하다.

1. 일반 서버에서 `AUTH_ENABLED`, `REDIS_URL`, DB DSN, AOAI 설정이 어떤 방식으로 export되는가
2. 외부 reverse proxy가 FE·AI·Backend 경로를 어떻게 나누는가
3. ~~Backend가 workflow 밖에서 별도 process manager로 기동되는가~~ — 해소: `combined_main.py`로 AI와 함께 기동된다
4. 공용 DB의 실제 migration ledger와 저장소 migration이 완전히 일치하는가
5. `backend/fe-api-preview`에 저장소 밖 consumer가 있는가
6. 운영에서 AI job 결과와 audit raw logging을 어느 기간 보존해야 하는가

이 항목들은 코드 변경 전에 read-only 운영 확인으로 닫아야 한다.

---

## 13. 최종 정신 모델

현재 QuantAgent는 하나의 완성된 monolith라기보다 다음 세 축이 한 저장소에 모인 상태다.

1. **실제로 배포되는 MVP:** FE → combined 프로세스(AI + Backend) → backtest → FE
2. **시장 데이터 공급망:** DE → 시장 데이터 DB → AI
3. **운영형 서비스 기능:** Backend → auth/service DB/이메일 (fenced execution은 아직 AI 경로에 연결되지 않음)

인지부채를 줄이는 핵심은 문서를 더 많이 만드는 것이 아니라 **이 세 축 중 공개 실행 경로를 하나로 결정하고 중복 구현을 지우는 것**이다. 기술부채를 줄이는 핵심은 **생성 코드 실행, 인증, job 상태, 데이터 provenance를 경로마다 다르게 두지 않는 것**이다.

가장 작은 올바른 순서는 다음과 같다.

> 실제 배포 확인 → canonical ingress 결정 → 중복 삭제 → executor/auth 통합 → 영속성 → 최소 e2e
