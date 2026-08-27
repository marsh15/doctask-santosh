import base64
import binascii
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import SpooledTemporaryFile

from fastapi import APIRouter, FastAPI, File, HTTPException, UploadFile, status
from fastapi.staticfiles import StaticFiles
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from doctask.application import DecisionInput
from doctask.durable import DurableProjectDeliveryService
from doctask.ingestion import UnsupportedSourceError
from doctask.interfaces.contracts import (
    CreateCorpusRequest,
    DecisionRequest,
    SubmitDecisionsRequest,
)
from doctask.interfaces.presenters import corpus, evidence, proposal, register, run, source_receipt
from doctask.model_adapters import ClaimExtractionAdapter

MAX_SOURCE_BYTES = 10_485_760


class ServiceContainer:
    def __init__(self) -> None:
        self._service: DurableProjectDeliveryService | None = None

    def set(self, service: DurableProjectDeliveryService) -> None:
        self._service = service

    def clear(self) -> None:
        self._service = None

    def get(self) -> DurableProjectDeliveryService:
        if self._service is None:
            raise RuntimeError("Application service is not available")
        return self._service


def _decisions(values: list[DecisionRequest]) -> list[DecisionInput]:
    return [
        DecisionInput(
            value.review_item_id, value.is_approved, value.reason, value.resolution
        )
        for value in values
    ]


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": str(exc)})
    if isinstance(exc, UnsupportedSourceError):
        return HTTPException(
            status_code=422,
            detail={"code": "INVALID_SOURCE", "message": str(exc)},
        )
    if isinstance(exc, ValueError):
        message = str(exc)
        if any(
            marker in message.lower()
            for marker in ("unsupported source format", "invalid docx", "invalid pdf")
        ):
            return HTTPException(
                status_code=422,
                detail={"code": "INVALID_SOURCE", "message": message},
            )
        return HTTPException(status_code=409, detail={"code": "INVALID_STATE", "message": str(exc)})
    if isinstance(exc, RuntimeError) and "version conflict" in str(exc):
        return HTTPException(status_code=409, detail={"code": "VERSION_CONFLICT", "message": str(exc)})
    return HTTPException(status_code=500, detail={"code": "INTERNAL_ERROR", "message": "Unexpected error"})


def create_application(
    *,
    database_url: str,
    rules_path: str | Path | None = None,
    mcp_allowed_hosts: list[str] | None = None,
    web_dist: str | Path | None = None,
    model_adapter: ClaimExtractionAdapter | None = None,
) -> tuple[FastAPI, MCPServer]:
    container = ServiceContainer()
    mcp = MCPServer("doctask", description="Governed project-delivery control register")

    @mcp.tool()
    def create_corpus(name: str) -> dict[str, object]:
        request = CreateCorpusRequest(name=name)
        return corpus(container.get().create_corpus(name=request.name))

    @mcp.tool()
    def add_source(corpus_id: str, filename: str, content_base64: str) -> dict[str, object]:
        if len(content_base64) > ((MAX_SOURCE_BYTES + 2) // 3) * 4:
            raise ValueError("Source exceeds the 10 MiB limit")
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("content_base64 must be strict base64") from exc
        return source_receipt(
            container.get().add_source(
                corpus_id=corpus_id, filename=filename, content=content
            )
        )

    @mcp.tool()
    def start_analysis(corpus_id: str) -> dict[str, object]:
        return run(container.get().start_analysis(corpus_id=corpus_id))

    @mcp.tool()
    def get_run_status(run_id: str) -> dict[str, object]:
        return run(container.get().get_run(run_id=run_id))

    @mcp.tool()
    def list_proposed_mutations(run_id: str) -> dict[str, object]:
        return {
            "proposals": [
                proposal(value)
                for value in container.get().list_proposed_mutations(run_id=run_id)
            ]
        }

    @mcp.tool()
    def list_review_items(run_id: str) -> dict[str, object]:
        return run(container.get().get_run(run_id=run_id))

    @mcp.tool()
    def submit_decisions(run_id: str, decisions: list[DecisionRequest]) -> dict[str, object]:
        request = SubmitDecisionsRequest(decisions=decisions)
        return run(
            container.get().submit_decisions(
                run_id=run_id, decisions=_decisions(request.decisions)
            )
        )

    @mcp.tool()
    def get_project_register(corpus_id: str) -> dict[str, object]:
        return register(container.get().get_register(corpus_id=corpus_id))

    @mcp.tool()
    def list_conflicts(corpus_id: str) -> dict[str, object]:
        return {"conflicts": container.get().list_conflicts(corpus_id=corpus_id)}

    @mcp.tool()
    def get_record_evidence(corpus_id: str, record_id: str) -> dict[str, object]:
        return {
            "evidence": [
                evidence(value)
                for value in container.get().get_record_evidence(
                    corpus_id=corpus_id, record_id=record_id
                )
            ]
        }

    @mcp.tool()
    def resume_run(run_id: str) -> dict[str, object]:
        return run(container.get().resume_run(run_id=run_id))

    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=mcp_allowed_hosts
            or ["127.0.0.1:*", "localhost:*", "testserver"]
        ),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Iterator[None]:
        with DurableProjectDeliveryService.connect(
            database_url, rules_path=rules_path, model_adapter=model_adapter
        ) as service:
            container.set(service)
            async with mcp.session_manager.run():
                yield
            container.clear()

    app = FastAPI(title="doctask", version="0.1.0", lifespan=lifespan)
    router = APIRouter(prefix="/v1")

    @app.get("/healthz", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.post("/corpora", status_code=status.HTTP_201_CREATED)
    def rest_create_corpus(request: CreateCorpusRequest) -> dict[str, object]:
        return corpus(container.get().create_corpus(name=request.name))

    @router.post("/corpora/{corpus_id}/sources", status_code=status.HTTP_201_CREATED)
    async def rest_add_source(
        corpus_id: str, source: UploadFile = File(...)  # noqa: B008 - FastAPI declaration
    ) -> dict[str, object]:
        total = 0
        with SpooledTemporaryFile(max_size=1_048_576, mode="w+b") as spool:
            while chunk := await source.read(64 * 1024):
                total += len(chunk)
                if total > MAX_SOURCE_BYTES:
                    raise HTTPException(
                        status_code=413, detail={"code": "SOURCE_TOO_LARGE"}
                    )
                spool.write(chunk)
            spool.seek(0)
            content = spool.read()
        try:
            receipt = container.get().add_source(
                corpus_id=corpus_id,
                filename=source.filename or "source",
                content=content,
            )
        except Exception as exc:
            raise _translate_error(exc) from exc
        return source_receipt(receipt)

    @router.post("/corpora/{corpus_id}/runs", status_code=status.HTTP_202_ACCEPTED)
    def rest_start_analysis(corpus_id: str) -> dict[str, object]:
        try:
            return run(container.get().start_analysis(corpus_id=corpus_id))
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/runs/{run_id}")
    def rest_get_run(run_id: str) -> dict[str, object]:
        try:
            return run(container.get().get_run(run_id=run_id))
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/runs/{run_id}/timeline")
    def rest_get_timeline(run_id: str) -> dict[str, object]:
        try:
            container.get().get_run(run_id=run_id)
            return {"stages": container.get().get_run_timeline(run_id=run_id)}
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/runs/{run_id}/proposals")
    def rest_get_proposals(run_id: str) -> dict[str, object]:
        try:
            return {
                "proposals": [
                    proposal(value)
                    for value in container.get().list_proposed_mutations(run_id=run_id)
                ]
            }
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.post("/runs/{run_id}/decisions")
    def rest_submit_decisions(
        run_id: str, request: SubmitDecisionsRequest
    ) -> dict[str, object]:
        try:
            return run(
                container.get().submit_decisions(
                    run_id=run_id, decisions=_decisions(request.decisions)
                )
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/corpora/{corpus_id}/register")
    def rest_get_register(corpus_id: str) -> dict[str, object]:
        try:
            return register(container.get().get_register(corpus_id=corpus_id))
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/corpora/{corpus_id}/conflicts")
    def rest_list_conflicts(corpus_id: str) -> dict[str, object]:
        try:
            return {"conflicts": container.get().list_conflicts(corpus_id=corpus_id)}
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/corpora/{corpus_id}/evidence/{record_id:path}")
    def rest_get_evidence(corpus_id: str, record_id: str) -> dict[str, object]:
        try:
            return {
                "evidence": [
                    evidence(value)
                    for value in container.get().get_record_evidence(
                        corpus_id=corpus_id, record_id=record_id
                    )
                ]
            }
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.post("/runs/{run_id}/resume")
    def rest_resume_run(run_id: str) -> dict[str, object]:
        try:
            return run(container.get().resume_run(run_id=run_id))
        except Exception as exc:
            raise _translate_error(exc) from exc

    app.include_router(router)
    app.mount("/mcp", mcp_app)
    static_root = (
        Path(web_dist)
        if web_dist is not None
        else Path(__file__).resolve().parents[2] / "apps" / "web" / "dist"
    )
    if static_root.is_dir():
        app.mount("/", StaticFiles(directory=static_root, html=True), name="web")
    return app, mcp
