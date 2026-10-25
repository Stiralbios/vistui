from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from backend.batch.worker import BatchWorker
from backend.settings import AppSettings
from loguru import logger
from sqlalchemy import create_engine

_job_store_tablename = "apscheduler_jobs"
_scheduler: AsyncIOScheduler | None = None


def _sync_database_url() -> str:
    """Build a synchronous database URL for APScheduler's job store."""
    async_url = AppSettings().VISTUI_DATABASE_URL.get_secret_value()
    return async_url.replace("+psycopg_async", "+psycopg")


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        job_store_url = _sync_database_url()
        sync_engine = create_engine(job_store_url)
        _scheduler = AsyncIOScheduler(
            jobstores={
                "default": SQLAlchemyJobStore(
                    engine=sync_engine,
                    tablename=_job_store_tablename,
                )
            }
        )
    return _scheduler


def start_scheduler() -> None:
    scheduler = get_scheduler()
    interval = AppSettings().VISTUI_BATCH_INTERVAL_SECONDS
    if scheduler.get_job("batch_round") is None:
        scheduler.add_job(
            _run_worker_round,
            "interval",
            seconds=interval,
            id="batch_round",
            replace_existing=True,
        )
        logger.info(f"Scheduled batch worker every {interval} seconds")
    scheduler.start()


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


async def _run_worker_round() -> None:
    worker = BatchWorker()
    try:
        await worker.run_round()
    finally:
        worker.should_stop = True
