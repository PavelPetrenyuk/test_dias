from __future__ import annotations

import logging
import os
import signal
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from article_masks.core import MaskGeneratorError, Settings, ValidationError, generate
from article_masks.database import Job, create_tables, get_session

logger = logging.getLogger(__name__)
stop_event = threading.Event()


def main() -> int:
    _configure_logging()
    _configure_signals()
    create_tables()
    poll_seconds = float(os.getenv("WORKER_POLL_SECONDS", "2"))
    logger.info("Воркер запущен")

    while not stop_event.is_set():
        job_id = _claim_next_job()
        if job_id is None:
            stop_event.wait(poll_seconds)
            continue
        _process_job(job_id)

    logger.info("Воркер остановлен")
    return 0


def _claim_next_job() -> uuid.UUID | None:
    with get_session() as session, session.begin():
        job = session.scalar(
            select(Job)
            .where(Job.status == "queued")
            .order_by(Job.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        job.status = "running"
        job.started_at = datetime.now(UTC)
        job.error_message = None
        job_id = job.id

    logger.info("Получено задание %s", job_id)
    return job_id


def _process_job(job_id: uuid.UUID) -> None:
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            logger.error("Задание %s исчезло из базы данных", job_id)
            return
        input_path = job.input_path
        output_path = job.output_path
        settings_data = job.settings

    try:
        settings = Settings(**settings_data)
        manifest = generate(
            input_path=Path(input_path),
            output_directory=Path(output_path),
            settings=settings,
        )
        generated_value = manifest["generated_articles"]
        if not isinstance(generated_value, int):
            raise MaskGeneratorError("Манифест содержит некорректное число артикулов")
        generated_articles = generated_value
    except ValidationError as exc:
        _mark_failed(job_id, " | ".join(exc.messages))
        logger.error("Задание %s отклонено: %s", job_id, " | ".join(exc.messages))
        return
    except MaskGeneratorError as exc:
        _mark_failed(job_id, str(exc))
        logger.error("Задание %s завершилось ошибкой: %s", job_id, exc)
        return
    except Exception:
        _mark_failed(job_id, "Внутренняя ошибка обработки")
        logger.exception("Непредвиденная ошибка при обработке задания %s", job_id)
        return

    with get_session() as session, session.begin():
        job = session.get(Job, job_id)
        if job is None:
            return
        job.status = "completed"
        job.generated_articles = generated_articles
        job.finished_at = datetime.now(UTC)

    logger.info(
        "Задание %s завершено, создано %s артикулов",
        job_id,
        f"{generated_articles:,}",
    )


def _mark_failed(job_id: uuid.UUID, message: str) -> None:
    with get_session() as session, session.begin():
        job = session.get(Job, job_id)
        if job is None:
            return
        job.status = "failed"
        job.error_message = message[:4000]
        job.finished_at = datetime.now(UTC)


def _configure_signals() -> None:
    def stop_worker(_: int, __: object) -> None:
        logger.info("Получен сигнал остановки")
        stop_event.set()

    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)


def _configure_logging() -> None:
    logging.addLevelName(logging.INFO, "ИНФО")
    logging.addLevelName(logging.ERROR, "ОШИБКА")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )


if __name__ == "__main__":
    raise SystemExit(main())
