from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg.errors import UniqueViolation
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.api import catalogue, stock
from app.api.deps import DbSession
from app.domain.errors import DomainError

app = FastAPI(title="Perishable Stock Manager API")
app.include_router(stock.router)
app.include_router(catalogue.router)


@app.exception_handler(DomainError)
def domain_error(request: Request, exc: DomainError) -> JSONResponse:
    """A request the domain rules refuse is a 400 with the domain's message."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(IntegrityError)
def integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
    """A unique violation is a 409; any other integrity error is a bug, so a 500.

    The domain functions check for a used client_id, a received invoice and a
    reversed request before writing, so a unique violation means another
    request saved the same thing at the same moment. Retrying replays the
    request or gets the domain's 400. Re-raising leaves the error to Starlette,
    which logs it and returns a 500.
    """
    if isinstance(exc.orig, UniqueViolation):
        return JSONResponse(
            status_code=409,
            content={
                "detail": "another request saved conflicting data at the same moment;"
                " retry this request"
            },
        )
    raise exc


@app.get("/health")
def health(db: DbSession) -> dict[str, str]:
    """Report whether the API is up and can reach the database."""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok", "database": "ok"}
