from __future__ import annotations

import hashlib
import logging
import os
import secrets
import shutil
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from article_masks.database import Job, create_tables, get_session

logger = logging.getLogger(__name__)

DATA_DIRECTORY = Path(os.getenv("DATA_DIRECTORY", "./data")).resolve()
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
UPLOAD_CHUNK_SIZE = 1024 * 1024
ALLOWED_CONTENT_TYPES = {
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
    "application/octet-stream",
}


class JobResponse(BaseModel):
    id: uuid.UUID
    status: str
    generated_articles: int | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    status_url: str
    manifest_url: str | None


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not os.getenv("API_TOKEN"):
        raise RuntimeError("Не задана обязательная переменная API_TOKEN")
    DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
    create_tables()
    logger.info("Веб-сервис готов принимать задания")
    yield


app = FastAPI(
    title="Сервис генерации артикулов",
    version="0.1.0",
    lifespan=lifespan,
)


def require_api_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected_token = os.getenv("API_TOKEN")
    if not expected_token:
        raise RuntimeError("Не задана обязательная переменная API_TOKEN")

    scheme, _, provided_token = (authorization or "").partition(" ")
    token_is_valid = scheme.lower() == "bearer" and secrets.compare_digest(
        provided_token,
        expected_token,
    )
    if not token_is_valid:
        raise HTTPException(status_code=401, detail="Неверный или отсутствующий токен")


def database_session() -> Iterator[Session]:
    with get_session() as session:
        yield session


@app.get("/health")
def health(session: Annotated[Session, Depends(database_session)]) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.post(
    "/v1/jobs",
    response_model=JobResponse,
    status_code=202,
    dependencies=[Depends(require_api_token)],
)
async def create_job(
    request: Request,
    session: Annotated[Session, Depends(database_session)],
    file: Annotated[UploadFile, File()],
    shard_size: Annotated[int, Form(ge=1)] = 5_000_000,
    max_mask_combinations: Annotated[int, Form(ge=1)] = 100_000_000,
    max_combinations: Annotated[int, Form(ge=1)] = 500_000_000,
    compression: Annotated[str, Form(pattern="^(gzip|none)$")] = "gzip",
    buffer_records: Annotated[int, Form(ge=1)] = 10_000,
) -> JobResponse:
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail="Поддерживаются только CSV-файлы",
        )

    job_id = uuid.uuid4()
    job_directory = DATA_DIRECTORY / "jobs" / str(job_id)
    input_path = job_directory / "input.csv"
    output_path = job_directory / "result"
    job_directory.mkdir(parents=True)

    try:
        input_sha256 = await _save_upload(file, input_path)
        job = Job(
            id=job_id,
            status="queued",
            input_path=str(input_path),
            output_path=str(output_path),
            input_sha256=input_sha256,
            settings={
                "shard_size": shard_size,
                "max_mask_combinations": max_mask_combinations,
                "max_combinations": max_combinations,
                "compression": compression,
                "buffer_records": buffer_records,
            },
        )
        session.add(job)
        session.commit()
    except HTTPException:
        shutil.rmtree(job_directory, ignore_errors=True)
        raise
    except SQLAlchemyError as exc:
        session.rollback()
        shutil.rmtree(job_directory, ignore_errors=True)
        logger.exception("Не удалось создать задание в базе данных")
        raise HTTPException(
            status_code=503,
            detail="Не удалось создать задание",
        ) from exc
    finally:
        await file.close()

    logger.info("Создано задание %s", job.id)
    return _job_response(job, request)


@app.get(
    "/v1/jobs/{job_id}",
    response_model=JobResponse,
    dependencies=[Depends(require_api_token)],
)
def get_job(
    job_id: uuid.UUID,
    request: Request,
    session: Annotated[Session, Depends(database_session)],
) -> JobResponse:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задание не найдено")
    return _job_response(job, request)


@app.get(
    "/v1/jobs",
    response_model=list[JobResponse],
    dependencies=[Depends(require_api_token)],
)
def list_jobs(
    request: Request,
    session: Annotated[Session, Depends(database_session)],
    limit: int = 20,
) -> list[JobResponse]:
    safe_limit = min(max(limit, 1), 100)
    jobs = session.scalars(
        select(Job).order_by(Job.created_at.desc()).limit(safe_limit)
    ).all()
    return [_job_response(job, request) for job in jobs]


@app.get(
    "/v1/jobs/{job_id}/manifest",
    dependencies=[Depends(require_api_token)],
)
def download_manifest(
    job_id: uuid.UUID,
    session: Annotated[Session, Depends(database_session)],
) -> FileResponse:
    job = _completed_job(session, job_id)
    manifest_path = Path(job.output_path) / "manifest.json"
    if not manifest_path.is_file():
        raise HTTPException(status_code=404, detail="Манифест не найден")
    return FileResponse(manifest_path, media_type="application/json")


@app.get(
    "/v1/jobs/{job_id}/files/{file_path:path}",
    dependencies=[Depends(require_api_token)],
)
def download_result_file(
    job_id: uuid.UUID,
    file_path: str,
    session: Annotated[Session, Depends(database_session)],
) -> FileResponse:
    job = _completed_job(session, job_id)
    result_directory = Path(job.output_path).resolve()
    requested_path = (result_directory / file_path).resolve()
    if not requested_path.is_relative_to(result_directory):
        raise HTTPException(status_code=400, detail="Недопустимый путь к файлу")
    if not requested_path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(requested_path, filename=requested_path.name)


async def _save_upload(file: UploadFile, destination: Path) -> str:
    digest = hashlib.sha256()
    total_size = 0

    try:
        with destination.open("xb") as output:
            while chunk := await file.read(UPLOAD_CHUNK_SIZE):
                total_size += len(chunk)
                if total_size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Размер файла превышает лимит {MAX_UPLOAD_BYTES} байт",
                    )
                output.write(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail="Не удалось сохранить входной файл",
        ) from exc

    if total_size == 0:
        raise HTTPException(status_code=400, detail="Загружен пустой файл")
    return digest.hexdigest()


def _completed_job(session: Session, job_id: uuid.UUID) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задание не найдено")
    if job.status != "completed":
        raise HTTPException(status_code=409, detail="Задание ещё не завершено")
    return job


def _job_response(job: Job, request: Request) -> JobResponse:
    manifest_url = None
    if job.status == "completed":
        manifest_url = str(request.url_for("download_manifest", job_id=job.id))

    return JobResponse(
        id=job.id,
        status=job.status,
        generated_articles=job.generated_articles,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        status_url=str(request.url_for("get_job", job_id=job.id)),
        manifest_url=manifest_url,
    )
