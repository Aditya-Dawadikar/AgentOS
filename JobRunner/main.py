from __future__ import annotations

from fastapi import FastAPI
from fastapi import Request
from fastapi.staticfiles import StaticFiles
import time
import uvicorn

from api import router as jobs_router
from app_logging import get_logger
from core.runner import data_root


logger = get_logger(__name__)


def create_app() -> FastAPI:
    application = FastAPI(title='AgentOS Job Server')
    data_root.mkdir(parents=True, exist_ok=True)
    application.mount('/artifacts', StaticFiles(directory=data_root, check_dir=False), name='artifacts')
    application.include_router(jobs_router)

    @application.middleware('http')
    async def log_requests(request: Request, call_next):
        started_at = time.perf_counter()
        logger.info('request_started method=%s path=%s client=%s',
                    request.method,
                    request.url.path,
                    request.client.host if request.client else 'unknown')
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            logger.exception('request_failed method=%s path=%s duration_ms=%.2f',
                             request.method,
                             request.url.path,
                             elapsed_ms)
            raise

        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.info('request_completed method=%s path=%s status_code=%s duration_ms=%.2f',
                    request.method,
                    request.url.path,
                    response.status_code,
                    elapsed_ms)
        return response

    @application.get('/health', tags=['system'])
    def health() -> dict[str, str]:
        logger.info('health_check_requested')
        return {'status': 'ok'}

    return application


app = create_app()


if __name__ == '__main__':
    logger.info('server_starting host=0.0.0.0 port=8000')
    uvicorn.run('main:app', host='0.0.0.0', port=8000, reload=False)