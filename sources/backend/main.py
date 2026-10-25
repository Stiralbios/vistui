import asyncio
import traceback
from contextlib import asynccontextmanager

from backend.admin import apis as admin
from backend.auth import apis as auth
from backend.batch import start_scheduler, stop_scheduler
from backend.chat import apis as chat
from backend.chatgroup import apis as chatgroup
from backend.database import create_db_and_tables
from backend.event import apis as event
from backend.exceptions import BaseProblem
from backend.fact import apis as fact
from backend.health import apis as health
from backend.logconfig import configure_loggers
from backend.message import apis as message
from backend.seeders import create_default_user, seed_demo_chat
from backend.settings import AppSettings
from backend.topic import apis as topic
from backend.user import apis as user
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

settings = AppSettings()
configure_loggers()


def custom_exception_handler(loop, context):
    """Log exceptions from asyncio tasks."""
    exception = context.get("exception")
    if exception:
        tb_str = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))
        logger.error(f"Exception in asyncio task: {tb_str}")
    else:
        logger.error(f"Exception in asyncio task: {context['message']}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_db_and_tables()
    await create_default_user()
    if AppSettings().APP_ENVIRONMENT != "TEST":
        start_scheduler()
    if AppSettings().VISTUI_SEED_DEMO:
        await seed_demo_chat()
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(custom_exception_handler)
    yield
    if AppSettings().APP_ENVIRONMENT != "TEST":
        stop_scheduler()


app = FastAPI(debug=True, lifespan=lifespan)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(user.router)
app.include_router(chatgroup.router)
app.include_router(chat.router)
app.include_router(message.router)
app.include_router(event.router)
app.include_router(topic.router)
app.include_router(fact.router)
app.include_router(admin.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(BaseProblem)
async def problem_exception_handler(request: Request, exc: BaseProblem):
    return JSONResponse(
        status_code=exc.status,
        content={"type": exc.kind, "on": exc.entity, "title": exc.title, "detail": exc.detail, "status": exc.status},
    )
