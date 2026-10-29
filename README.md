# Employee Discovery & Email Verification Pipeline

Production-oriented FastAPI application that discovers employees for company domains, retrieves professional contact data via Apollo's official API, applies configurable email verification rules, and exports clean lead datasets.

> **Status:** Phase 13 complete — Docker Compose development stack.  
> Production deployment documentation remains for Phase 14.

## Stack

- Python 3.12+
- FastAPI / Pydantic / Uvicorn
- SQLAlchemy 2.x + Alembic (Phase 2+)
- PostgreSQL (production) / SQLite (local)
- httpx, pandas, Celery, Redis (later phases)
- pytest

## Project layout

```text
app/
  main.py                 # FastAPI entrypoint
  config.py               # Environment-backed settings
  api/routes/health.py    # GET /health
  database/
    base.py               # Declarative base
    enums.py              # Job / contact status enums
    models.py             # Company, Person, EmailVerification, Job, JobItem
    session.py            # Engine + session helpers
  utils/email.py          # Email normalization for dedupe
  utils/domain.py         # Website/domain normalization
  schemas/company.py      # Company API schemas
  services/company_service.py
  services/apollo.py          # Official Apollo API client
  schemas/apollo.py           # Typed Apollo request/response models
  utils/retry.py              # Exponential backoff
  utils/logging.py            # Secret-safe structured logging
  repositories/company_repository.py
  api/routes/companies.py # Import / list / get
  workers/                # Phase 8+
alembic/
  versions/0001_initial_schema.py
tests/
data/input/
data/output/
```

## Phase 4: Apollo API client

Implemented against Apollo's current official docs only:

| Capability | Official endpoint | Credits | Notes |
|---|---|---|---|
| Auth check | `GET https://api.apollo.io/v1/auth/health` | 0 | Validates API key |
| People search | `POST /api/v1/mixed_people/api_search` | 0 | **Does not return emails/phones**; master API key required; max 100/page; max 1,000 domains/request |
| People enrich | `POST /api/v1/people/match` | 1–9 when data found | Single person |
| Bulk enrich | `POST /api/v1/people/bulk_match` | 1–9/person when data found | **Max 10 people/request** |

Auth header: `x-api-key` ([Authentication](https://docs.apollo.io/reference/authentication)).

Rate limits are plan-specific. The client retries `429/5xx` with exponential backoff and respects `Retry-After` when present. Concurrency is capped by `APOLLO_CONCURRENCY`.

### Configure

```env
APOLLO_API_KEY=your_apollo_api_key_here
REQUEST_TIMEOUT=30.0
MAX_RETRIES=3
APOLLO_CONCURRENCY=5
```

### Phase 4 tests (mocked — no live Apollo calls)

```bash
pytest tests/test_apollo_client.py tests/test_retry.py -q
```

### Known limitations (from Apollo docs)

1. People API Search never returns email/phone — enrichment is required later.
2. Bulk enrichment hard-caps at 10 people per request.
3. Exact rate-limit ceilings depend on your Apollo plan (`Settings → Integrations → API Keys → Usage`).
4. Phone reveal / waterfall enrichment require a public HTTPS `webhook_url` (not wired in Phase 4).

## Phase 5: employee search

Search Apollo for people at an imported company domain using configurable job titles.

### Filters supported

- Company domain (from stored company)
- Job titles via `TARGET_JOB_TITLES`, named profiles, or per-request override
- Seniority (`person_seniorities`)
- Location (`person_locations` / `organization_locations`)
- Apollo `contact_email_status` (`verified`, `unverified`, `likely to engage`, `unavailable`)

Named profiles: `executive`, `sales`, `marketing`, `technology`, `finance`, `custom`.

### API

```http
POST /companies/{company_id}/people/search
GET  /people
GET  /people/{person_id}
GET  /people/job-title-profiles
```

Example body:

```json
{
  "job_title_profile": "executive",
  "contact_email_status": ["verified"],
  "person_seniorities": ["c_suite", "founder"],
  "per_page": 25,
  "max_pages": 2
}
```

Persists `apollo_person_id`, name/title metadata, and `final_status=unverified`.  
**Emails are not stored in Phase 5** — Apollo People API Search does not return them.

### Phase 5 tests (mocked Apollo)

```bash
pytest tests/test_people_search.py tests/test_job_title_profiles.py -q
```

## Phase 6: Apollo enrichment

Uses official Bulk People Enrichment:

`POST https://api.apollo.io/api/v1/people/bulk_match` (max **10** people/request)

### Behavior

1. Load people with `apollo_person_id` (optionally only missing email)
2. Enrich in batches of ≤10
3. Persist email, `apollo_email_status`, LinkedIn, name/title updates
4. Apply `APOLLO_REQUIRED_EMAIL_STATUS` (default `verified`)
5. Pass → `final_status=unverified` (awaiting Phase 7 external verification)
6. Fail / no match / duplicate email → `final_status=rejected` + `rejection_reason`

### API

```http
POST /companies/{company_id}/people/enrich
POST /people/enrich
```

Example:

```json
{
  "only_missing_email": true,
  "force": false,
  "limit": 100
}
```

### Phase 6 tests (mocked)

```bash
pytest tests/test_enrichment.py tests/test_acceptance.py -q
```

## Phase 7: external email verification

Provider abstraction with ZeroBounce v2 as the first implementation.

Official endpoint: `GET https://api.zerobounce.net/v2/validate`  
Auth: `api_key` query param (never logged).

### Acceptance rule

`final_status = accepted` only when:

1. Apollo email status == `APOLLO_REQUIRED_EMAIL_STATUS` (default `verified`)
2. External status == `EXTERNAL_REQUIRED_EMAIL_STATUS` (default `valid`)

Otherwise contacts are stored as rejected/invalid/risky/etc. with reasons.

### API

```http
POST /companies/{company_id}/people/verify
POST /people/verify
```

Stores `email_verifications` rows: provider, status, reason, checked_at, sanitized raw response.

### Configure

```env
EMAIL_VERIFIER_PROVIDER=zerobounce
EMAIL_VERIFIER_API_KEY=...
EMAIL_VERIFIER_ENABLED=true
EXTERNAL_REQUIRED_EMAIL_STATUS=valid
EMAIL_VERIFIER_CONCURRENCY=5
```

### Phase 7 tests (mocked)

```bash
pytest tests/test_email_verification.py tests/test_acceptance.py -q
```

## Phase 8: job processing and Celery

Jobs process companies in background batches: **search → enrich → verify**.

### API

```http
POST   /jobs
GET    /jobs
GET    /jobs/{id}
POST   /jobs/{id}/start
POST   /jobs/{id}/pause
POST   /jobs/{id}/resume
POST   /jobs/{id}/cancel
POST   /jobs/{id}/retry-failed
GET    /jobs/{id}/progress
GET    /jobs/{id}/results
```

Job states: `pending`, `running`, `paused`, `completed`, `failed`, `cancelled`  
Item states: `pending`, `searching`, `enriching`, `verifying`, `completed`, `failed`

### Run locally

Terminal 1 — API:

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Terminal 2 — Redis must be available at `REDIS_URL`, then:

```powershell
celery -A app.workers.celery_app.celery_app worker --loglevel=INFO
```

### Phase 8 tests

```bash
pytest tests/test_jobs.py -q
```

## Phase 9: retry and resume

- Job items checkpoint after each stage (`searching` → `enriching` → `verifying`)
- Resume continues from the current stage (does not redo earlier stages)
- Transient errors (429/5xx/timeouts) retry with exponential backoff (`MAX_RETRIES`)
- Permanent 4xx/auth/validation errors fail immediately
- Failures stored in `processing_errors` (provider, operation, retryable, attempt)
- Job run options persisted in `jobs.options_json` for resume continuity

```http
GET /jobs/{id}/errors
```

```bash
alembic upgrade head
pytest tests/test_retry_resume.py tests/test_jobs.py -q
```

## Phase 10: CSV exports

Generates three files under `data/output/{job_id}/`:

- `accepted_contacts.csv` — only `final_status=accepted`
- `rejected_contacts.csv` — non-accepted contacts + `rejection_reason`
- `processing_errors.csv` — tracked API/pipeline failures

Accepted columns:

`company_name, company_domain, employee_name, job_title, email, apollo_email_status, external_verification_status, linkedin_url, final_status`

```http
GET /jobs/{id}/export
GET /jobs/{id}/export/accepted_contacts.csv
GET /jobs/{id}/export/rejected_contacts.csv
GET /jobs/{id}/export/processing_errors.csv
```

```bash
pytest tests/test_exports.py -q
```

## Phase 11: API endpoints

All primary specification endpoints are registered and covered by OpenAPI.

| Method | Path | Auth |
|--------|------|------|
| GET | `/health` | public |
| GET | `/` | public |
| POST | `/companies/import` | API key when configured |
| GET | `/companies` | API key when configured |
| GET | `/companies/{id}` | API key when configured |
| POST | `/jobs` | API key when configured |
| GET | `/jobs` | API key when configured |
| GET | `/jobs/{id}` | API key when configured |
| POST | `/jobs/{id}/start\|pause\|resume\|cancel\|retry-failed` | API key when configured |
| GET | `/jobs/{id}/progress\|results\|errors\|export` | API key when configured |
| GET | `/jobs/{id}/export/{filename}` | API key when configured |

Also available from earlier phases: people search/enrich/verify routes.

### API hardening in this phase

- Consistent JSON errors (`detail`, `error_code`, `request_id`)
- `X-Request-ID` on responses
- Upload size guard via `MAX_UPLOAD_SIZE_MB`
- OpenAPI `ApiKeyAuth` (`X-API-Key`) when `API_KEY` is set
- Optional CORS via `CORS_ORIGINS`

```bash
pytest tests/test_api_surface.py -q
```

## Phase 12: tests

Spec-required coverage (all mocked — no live Apollo/ZeroBounce calls):

| Area | Primary tests |
|------|----------------|
| Domain normalization | `test_domain.py`, `test_spec_unit_coverage.py` |
| CSV validation / duplicates | `test_company_import.py`, `test_spec_unit_coverage.py` |
| Email normalization | `test_database.py`, `test_spec_unit_coverage.py` |
| Job state transitions | `test_jobs.py`, `test_spec_unit_coverage.py` |
| Acceptance rules | `test_acceptance.py`, `test_spec_unit_coverage.py` |
| Retry logic | `test_retry.py`, `test_retry_resume.py` |
| Apollo API (mocked) | `test_apollo_client.py`, `test_provider_integrations.py` |
| Email verifier (mocked) | `test_email_verification.py`, `test_provider_integrations.py` |
| End-to-end pipeline (mocked) | `test_pipeline_integration.py` |

```bash
pytest -q
```

## Phase 13: Docker development environment

Services:

| Service | Role |
|---------|------|
| `db` | PostgreSQL 16 |
| `redis` | Celery broker/backend |
| `api` | FastAPI + Alembic migrations on start |
| `worker` | Celery worker |

### Start

```powershell
cd "$env:USERPROFILE\employee-discovery"
Copy-Item .env.docker.example .env.docker
# Edit .env.docker and set APOLLO_API_KEY / EMAIL_VERIFIER_API_KEY for live runs
docker compose up --build
```

API: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)  
Health: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)

CSV input/output is mounted at `./data` → `/app/data`.

### Useful commands

```powershell
docker compose ps
docker compose logs -f api worker
docker compose exec api alembic current
docker compose down
```

### Verify success

1. `docker compose ps` shows `api`, `worker`, `db`, `redis` healthy/running
2. `curl http://127.0.0.1:8000/health` returns `"status":"ok"`
3. Swagger loads at `/docs`

## Phase 3: CSV import and domain normalization

### Supported CSV columns

- `company_website` and/or `company_domain` (at least one required)
- optional `company_name`

Domains are normalized to a canonical host (lowercase, no scheme/path/`www.`).

### Import via API

```powershell
cd "$env:USERPROFILE\employee-discovery"
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

In another terminal:

```powershell
curl.exe -X POST "http://127.0.0.1:8000/companies/import" `
  -F "file=@data/input/sample_companies.csv;type=text/csv"
```

Or use Swagger at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) → `POST /companies/import`.

### Import summary fields

- `total_rows`
- `valid_companies` (newly inserted)
- `invalid_rows`
- `duplicate_domains` (duplicates within the CSV)
- `existing_domains_skipped` (already in DB)

Also available:

- `GET /companies`
- `GET /companies/{id}`

### Phase 3 tests

```bash
pytest tests/test_domain.py tests/test_company_import.py -q
```

### Verify success

1. Sample CSV import returns `201` with expected summary counts
2. `GET /companies` lists normalized unique domains
3. Phase 3 pytest suite passes

## Phase 2: database and migrations

### Apply migrations (SQLite local default)

With the virtualenv active:

```powershell
cd "$env:USERPROFILE\employee-discovery"
.\.venv\Scripts\Activate.ps1
alembic upgrade head
```

This creates `employee_discovery.db` (or whatever `DATABASE_URL` points to) with:

- `companies` (unique `domain`)
- `people` (unique `normalized_email`, unique `apollo_person_id`)
- `email_verifications`
- `jobs`
- `job_items` (unique `(job_id, company_id)`)

### Postgres (optional)

Set in `.env`:

```env
DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/employee_discovery
```

Then run `alembic upgrade head` again.

### Phase 2 tests

```bash
pytest tests/test_database.py tests/test_migrations.py tests/test_health.py -q
```

### Verify success

1. `alembic current` shows `0001_initial_schema (head)`
2. SQLite file exists (local) or Postgres tables are present
3. Phase 2 pytest suite passes
4. `GET /health` still works after migrations

## Easy local UI (recommended)

1. Put API keys in `.env` (`APOLLO_API_KEY`, optionally `EMAIL_VERIFIER_API_KEY`)
2. Apply migrations once: `alembic upgrade head`
3. Start only the API:

```powershell
cd "$env:USERPROFILE\employee-discovery"
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

4. Open **http://127.0.0.1:8000/app**
5. Upload a small CSV (start with ~10 websites)
6. Watch progress and browse accepted/rejected contacts in the UI
7. Download CSVs when the run finishes

Quick UI runs in-process (no Redis/Celery). Default cap: **25 companies** (`QUICK_RUN_MAX_COMPANIES`).

> This uses Apollo + email verification APIs — it does not scrape websites.

### 1. Create a virtual environment

**Windows (PowerShell):**

```powershell
cd "$env:USERPROFILE\employee-discovery"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**macOS / Linux:**

```bash
cd ~/employee-discovery
python3.12 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Configure environment

```powershell
Copy-Item .env.example .env
```

Edit `.env` as needed. For Phase 1, defaults are enough to run the health endpoint.  
**Never commit `.env`.** API keys stay in environment variables only.

### 4. Run the FastAPI server

From the project root (with the virtualenv active):

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### 5. Open Swagger docs

Browser: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

### 6. Test `GET /health`

```bash
curl http://127.0.0.1:8000/health
```

Expected JSON shape:

```json
{
  "status": "ok",
  "service": "Employee Discovery Pipeline",
  "version": "0.1.0",
  "environment": "development",
  "timestamp": "2026-07-24T09:00:00+00:00"
}
```

### Run Phase 1 tests

```bash
pytest tests/test_health.py -q
```

## Development roadmap

| Phase | Focus |
|------:|-------|
| 1 | Project setup + health endpoint |
| 2 | Database models + Alembic migrations |
| 3 | CSV import + domain normalization |
| 4 | Apollo API client (official docs only) |
| 5 | Employee search |
| 6 | Apollo enrichment |
| 7 | External email verification |
| 8 | Job processing + Celery |
| 9 | Retry + resume |
| 10 | CSV exports |
| 11 | Remaining API endpoints |
| 12 | Broader test suite |
| 13 | Docker development environment (**current**) |
| 14 | Production deployment docs |

## Security notes

- Do not hard-code Apollo or email-verifier API keys.
- Do not log credentials.
- Do not scrape LinkedIn/websites when official APIs are available.
- Start with a small (~10 company) sample before large datasets.

## License

Proprietary / internal use unless otherwise specified.
