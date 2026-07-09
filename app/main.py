import logging
import logging.handlers

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.core.config import settings
from app.models.models import init_db

app = FastAPI(title="ProjectAgent", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _setup_logging() -> None:
    """配置 app 命名空间日志：控制台 + 按天滚动文件。

    只动 "app" logger，uvicorn 自己的 access log 不受影响。
    """
    log_dir = settings.storage_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    app_logger = logging.getLogger("app")
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False
    if app_logger.handlers:
        return
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    app_logger.addHandler(sh)
    fh = logging.handlers.TimedRotatingFileHandler(
        log_dir / "app.log", when="midnight", backupCount=7, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    app_logger.addHandler(fh)


@app.on_event("startup")
def _startup() -> None:
    _setup_logging()
    settings.ensure_dirs()
    init_db()


app.include_router(router)
