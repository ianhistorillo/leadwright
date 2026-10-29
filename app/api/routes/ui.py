"""Local web UI + quick-run pipeline endpoints."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.config import get_settings, reload_settings
from app.database.enums import JobStatus
from app.database.session import get_db, get_session_factory
from app.services.quick_pipeline import QuickPipelineService

router = APIRouter(tags=["ui"])

WEB_DIR = Path(__file__).resolve().parents[3] / "web"
_TERMINAL = {
    JobStatus.COMPLETED.value,
    JobStatus.FAILED.value,
    JobStatus.CANCELLED.value,
}


def _view_fingerprint(view: dict[str, Any]) -> str:
    """Compact change detector so we only push when the UI would change."""
    progress = view.get("progress") or {}
    job = progress.get("job") or {}
    return "|".join(
        [
            str(job.get("status") or ""),
            str(job.get("processed_companies") or 0),
            str(progress.get("pending") or 0),
            str(progress.get("searching") or 0),
            str(progress.get("enriching") or 0),
            str(progress.get("verifying") or 0),
            str(progress.get("completed") or 0),
            str(progress.get("failed") or 0),
            str(progress.get("percent_complete") or 0),
            str(view.get("people_found") or 0),
            str(len(view.get("guessed_people") or [])),
            str(len(view.get("accepted_people") or [])),
            str(len(view.get("rejected_people") or [])),
            "1" if view.get("export") else "0",
        ]
    )


@router.get("/app", include_in_schema=False)
async def app_ui() -> FileResponse:
    """Serve the local operator UI."""
    index = WEB_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="UI not found")
    return FileResponse(index)


def _parse_form_bool(value: str | bool, *, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    normalized = (value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


@router.post("/ui/quick-run", dependencies=[Depends(require_api_key)])
async def quick_run(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    job_title_profile: str = Form("custom"),
    max_pages: int = Form(1),
    run_external_verification: str = Form("false"),
    pipeline_mode: str = Form("search_only_guess"),
    skip_existing_people: str = Form("false"),
    job_name: str = Form("ui-quick-run"),
    db: Session = Depends(get_db),
) -> dict:
    """
    Easy path: upload CSV → import companies → run pipeline in-process.

    Modes:
    - search_only_guess: Apollo search + local email guesses (0 enrich credits)
    - search_and_enrich: Apollo search + email enrichment (uses credits)

    ZeroBounce is disabled for UI runs.
    """
    settings = reload_settings()
    filename = file.filename or "upload.csv"
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv uploads are allowed")

    content = await file.read()
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds maximum upload size of {settings.max_upload_size_mb} MB",
        )

    if not (settings.apollo_api_key or "").strip():
        raise HTTPException(
            status_code=400,
            detail="APOLLO_API_KEY is not set in .env — required for discovery.",
        )

    if max_pages < 1 or max_pages > 1000:
        raise HTTPException(
            status_code=400,
            detail="max_pages must be between 1 and 1000",
        )

    mode = (pipeline_mode or "search_only_guess").strip().lower()
    if mode == "full":
        mode = "search_and_enrich"
    new_only = _parse_form_bool(skip_existing_people, default=False)
    service = QuickPipelineService(db)
    try:
        job, import_summary, options = service.start_from_csv(
            content,
            filename=filename,
            job_name=job_name,
            job_title_profile=job_title_profile or None,
            max_pages=max_pages,
            run_external_verification=False,
            pipeline_mode=mode,
            skip_existing_people=new_only,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Capture options for background task; processing opens its own DB session.
    background_tasks.add_task(service.process_until_done, job.id, options)

    return {
        "job_id": job.id,
        "job": job.model_dump(mode="json"),
        "import_summary": import_summary.model_dump(mode="json"),
        "pipeline_mode": mode,
        "skip_existing_people": new_only,
        "job_titles": options.get("job_titles") or [],
        "message": "Pipeline started. Connect to /ui/jobs/{job_id}/stream for live progress.",
    }


@router.get("/ui/jobs/latest", dependencies=[Depends(require_api_key)])
def latest_job(db: Session = Depends(get_db)) -> dict:
    """Most recent job id for UI resume-after-refresh."""
    from app.services.job_service import JobService

    listed = JobService(db).list_jobs(offset=0, limit=1)
    if not listed.items:
        raise HTTPException(status_code=404, detail="No jobs found")
    job = listed.items[0]
    return {"job_id": job.id, "status": job.status, "name": job.name}


@router.get("/ui/config/target-job-titles", dependencies=[Depends(require_api_key)])
def current_target_job_titles() -> dict:
    """Return the TARGET_JOB_TITLES currently loaded from .env (after reload)."""
    from app.services.job_title_profiles import resolve_job_titles

    settings = reload_settings()
    titles = resolve_job_titles("custom", settings=settings)
    return {
        "profile": "custom",
        "target_job_titles": titles,
        "count": len(titles),
    }


@router.post("/ui/jobs/{job_id}/continue", dependencies=[Depends(require_api_key)])
async def continue_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """
    Continue an interrupted UI job (after refresh or API restart).

    Restarts in-process processing from the last company checkpoint when needed.
    """
    service = QuickPipelineService(db)
    try:
        result = service.prepare_continue(job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if result.get("should_start"):
        background_tasks.add_task(
            service.process_until_done,
            job_id,
            result.get("options") or {},
        )

    return {
        "job_id": result["job_id"],
        "status": result["status"],
        "worker_started": bool(result.get("should_start")),
        "message": result.get("message"),
    }


@router.post("/ui/backfill-company-names", dependencies=[Depends(require_api_key)])
async def backfill_company_names(
    request: Request,
    file: UploadFile = File(...),
    max_pages_per_domain: int = Form(5),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """
    Upload accepted_contacts.csv and stream progress while filling company_name.

    Uses organization.name from People API Search only — 0 enrichment credits.
    SSE events: progress, done, error.
    """
    import asyncio

    from app.services.company_name_backfill import CompanyNameBackfillService

    settings = get_settings()
    filename = file.filename or "accepted_contacts.csv"
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv uploads are allowed")

    content = await file.read()
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds maximum upload size of {settings.max_upload_size_mb} MB",
        )

    if not (settings.apollo_api_key or "").strip():
        raise HTTPException(
            status_code=400,
            detail="APOLLO_API_KEY is not set in .env — required for Apollo Search.",
        )

    if max_pages_per_domain < 1 or max_pages_per_domain > 50:
        raise HTTPException(
            status_code=400,
            detail="max_pages_per_domain must be between 1 and 50",
        )

    out_name = filename.rsplit(".", 1)[0] + "_apollo_company_names.csv"
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def on_progress(payload: dict[str, Any]) -> None:
        await queue.put({"event": "progress", "data": payload})

    async def runner() -> None:
        try:
            result = await CompanyNameBackfillService(db).backfill_csv(
                content,
                max_pages_per_domain=max_pages_per_domain,
                on_progress=on_progress,
            )
            # Final done event (csv already included in last progress completed).
            await queue.put(
                {
                    "event": "done",
                    "data": {
                        "filename": out_name,
                        "total_rows": result.total_rows,
                        "filled": result.filled,
                        "already_exact": result.already_exact,
                        "unresolved": result.unresolved,
                        "domains_searched": result.domains_searched,
                        "domains_total": result.domains_total,
                        "apollo_calls": result.apollo_calls,
                        "percent": 100,
                        "phase": "completed",
                        "notes": result.notes,
                        "csv_base64": __import__("base64")
                        .b64encode(result.csv_bytes)
                        .decode("ascii"),
                    },
                }
            )
        except ValueError as exc:
            await queue.put({"event": "error", "data": {"detail": str(exc)}})
        except Exception as exc:  # noqa: BLE001
            await queue.put(
                {"event": "error", "data": {"detail": f"Backfill failed: {exc}"}}
            )
        finally:
            await queue.put(None)

    async def event_gen() -> AsyncIterator[str]:
        task = asyncio.create_task(runner())
        try:
            yield f"event: progress\ndata: {json.dumps({'phase': 'starting', 'percent': 0})}\n\n"
            while True:
                if await request.is_disconnected():
                    task.cancel()
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    break
                yield (
                    f"event: {item['event']}\n"
                    f"data: {json.dumps(item['data'], separators=(',', ':'))}\n\n"
                )
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/ui/backfill-contact-enrich/latest", dependencies=[Depends(require_api_key)])
def latest_contact_enrich_run() -> dict:
    """Return the newest resumable enrich run checkpoint, if any."""
    from app.services.enrich_run_store import EnrichRunStore

    latest = EnrichRunStore.latest_resumable()
    return {"run": latest}


@router.get(
    "/ui/backfill-contact-enrich/{run_id}/download",
    dependencies=[Depends(require_api_key)],
)
def download_contact_enrich_checkpoint(run_id: str) -> FileResponse:
    """Download the latest checkpoint CSV for an enrich run (partial results OK)."""
    from app.services.enrich_run_store import EnrichRunStore

    store = EnrichRunStore(run_id)
    if not store.checkpoint_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No enrich checkpoint found for run_id={run_id}",
        )
    state = store.load_state()
    source = state.get("source_filename") or "accepted_contacts.csv"
    out_name = str(source).rsplit(".", 1)[0] + "_apollo_enriched_partial.csv"
    return FileResponse(
        path=store.checkpoint_path,
        media_type="text/csv",
        filename=out_name,
    )


@router.post("/ui/backfill-contact-enrich", dependencies=[Depends(require_api_key)])
async def backfill_contact_enrich(
    request: Request,
    file: UploadFile | None = File(None),
    only_incomplete: str = Form("true"),
    only_enrich_if_has_email: str = Form("true"),
    max_people: int = Form(500),
    stop_after_valid_emails: int = Form(0),
    max_pages_per_domain: int = Form(5),
    resume_run_id: str = Form(""),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """
    Upload accepted_contacts.csv and stream progress while filling employee_name + email.

    Uses Apollo People Enrichment (~1 credit/person when data is found).
    Pass resume_run_id to continue a checkpointed run without re-billing processed rows.
    SSE events: progress, done, error.
    """
    import asyncio

    from app.services.contact_enrich_backfill import ContactEnrichBackfillService
    from app.services.enrich_run_store import EnrichRunStore

    settings = get_settings()
    resume_id = (resume_run_id or "").strip()
    resume = bool(resume_id)

    if resume:
        store = EnrichRunStore(resume_id)
        if not store.checkpoint_path.exists():
            raise HTTPException(
                status_code=404,
                detail=f"No enrich checkpoint found for run_id={resume_id}",
            )
        content = b""
        filename = store.load_state().get("source_filename") or "accepted_contacts.csv"
    else:
        if file is None:
            raise HTTPException(status_code=400, detail="CSV file is required")
        filename = file.filename or "accepted_contacts.csv"
        if not filename.lower().endswith(".csv"):
            raise HTTPException(status_code=400, detail="Only .csv uploads are allowed")
        content = await file.read()
        max_bytes = settings.max_upload_size_mb * 1024 * 1024
        if len(content) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds maximum upload size of {settings.max_upload_size_mb} MB",
            )

    if not (settings.apollo_api_key or "").strip():
        raise HTTPException(
            status_code=400,
            detail="APOLLO_API_KEY is not set in .env — required for Apollo Enrichment.",
        )

    if max_people < 1 or max_people > 10000:
        raise HTTPException(
            status_code=400, detail="max_people must be between 1 and 10000"
        )
    if stop_after_valid_emails < 0 or stop_after_valid_emails > 10000:
        raise HTTPException(
            status_code=400,
            detail="stop_after_valid_emails must be between 0 and 10000 (0 = no early stop)",
        )
    if max_pages_per_domain < 1 or max_pages_per_domain > 50:
        raise HTTPException(
            status_code=400,
            detail="max_pages_per_domain must be between 1 and 50",
        )

    only_incomplete_flag = str(only_incomplete).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    only_has_email_flag = str(only_enrich_if_has_email).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    out_name = str(filename).rsplit(".", 1)[0] + "_apollo_enriched.csv"
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def on_progress(payload: dict[str, Any]) -> None:
        await queue.put({"event": "progress", "data": payload})

    async def runner() -> None:
        try:
            result = await ContactEnrichBackfillService(db).backfill_csv(
                content if not resume else None,
                only_incomplete=only_incomplete_flag,
                only_enrich_if_has_email=only_has_email_flag,
                max_people=max_people,
                stop_after_valid_emails=stop_after_valid_emails,
                max_pages_per_domain=max_pages_per_domain,
                run_id=resume_id or None,
                resume=resume,
                source_filename=filename,
                on_progress=on_progress,
            )
            await queue.put(
                {
                    "event": "done",
                    "data": {
                        "filename": out_name,
                        "run_id": result.run_id,
                        "total_rows": result.total_rows,
                        "enrich_targets": result.enrich_targets,
                        "people_done": result.people_done,
                        "skipped_complete": result.skipped_complete,
                        "skipped_no_email": result.skipped_no_email,
                        "skipped_after_stop": result.skipped_after_stop,
                        "skipped_already_processed": result.skipped_already_processed,
                        "skipped_no_apollo_email": result.skipped_no_apollo_email,
                        "filled_from_db": result.filled_from_db,
                        "names_updated": result.names_updated,
                        "emails_updated": result.emails_updated,
                        "valid_emails_found": result.valid_emails_found,
                        "chargeable_matches": result.chargeable_matches,
                        "unresolved": result.unresolved,
                        "already_exact": result.already_exact,
                        "enrich_calls": result.enrich_calls,
                        "search_calls": result.search_calls,
                        "credits_consumed": result.credits_consumed,
                        "stop_after_valid_emails": result.stop_after_valid_emails,
                        "stopped_early": result.stopped_early,
                        "percent": 100,
                        "phase": "completed",
                        "notes": result.notes,
                        "csv_base64": __import__("base64")
                        .b64encode(result.csv_bytes)
                        .decode("ascii"),
                    },
                }
            )
        except ValueError as exc:
            await queue.put({"event": "error", "data": {"detail": str(exc)}})
        except Exception as exc:  # noqa: BLE001
            if resume_id:
                try:
                    EnrichRunStore(resume_id).mark_interrupted()
                except Exception:  # noqa: BLE001
                    pass
            await queue.put(
                {"event": "error", "data": {"detail": f"Contact enrich failed: {exc}"}}
            )
        finally:
            await queue.put(None)

    async def event_gen() -> AsyncIterator[str]:
        task = asyncio.create_task(runner())
        try:
            yield (
                "event: progress\n"
                f"data: {json.dumps({'phase': 'starting', 'percent': 0})}\n\n"
            )
            while True:
                if await request.is_disconnected():
                    task.cancel()
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    break
                yield (
                    f"event: {item['event']}\n"
                    f"data: {json.dumps(item['data'], separators=(',', ':'))}\n\n"
                )
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/ui/jobs/{job_id}/view", dependencies=[Depends(require_api_key)])
def quick_job_view(job_id: str, db: Session = Depends(get_db)) -> dict:
    """Progress + people results for the local UI."""
    try:
        return QuickPipelineService(db).get_view(job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/ui/jobs/{job_id}/stream", dependencies=[Depends(require_api_key)])
async def quick_job_stream(job_id: str, request: Request) -> StreamingResponse:
    """
    Server-Sent Events stream of job views.

    Emits a snapshot whenever progress/people change, then closes on terminal status.
    """
    factory = get_session_factory()
    # Confirm job exists before opening the stream.
    probe = factory()
    try:
        QuickPipelineService(probe).get_view(job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        probe.close()

    async def event_gen() -> AsyncIterator[str]:
        last_fp: str | None = None
        idle_rounds = 0
        while True:
            if await request.is_disconnected():
                break

            session = factory()
            try:
                view = QuickPipelineService(session).get_view(job_id)
            except LookupError:
                yield f"event: error\ndata: {json.dumps({'detail': 'Job not found'})}\n\n"
                break
            finally:
                session.close()

            fp = _view_fingerprint(view)
            if fp != last_fp:
                last_fp = fp
                idle_rounds = 0
                payload = json.dumps(view, separators=(",", ":"))
                yield f"event: view\ndata: {payload}\n\n"
                status_value = ((view.get("progress") or {}).get("job") or {}).get(
                    "status"
                )
                if status_value in _TERMINAL:
                    yield f"event: done\ndata: {payload}\n\n"
                    break
            else:
                idle_rounds += 1
                # Keepalive comments so proxies/browsers don't drop the connection.
                if idle_rounds % 15 == 0:
                    yield ": keepalive\n\n"

            await asyncio.sleep(0.4)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
