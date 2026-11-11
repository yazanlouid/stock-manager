from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import DbSession

app = FastAPI(title="Perishable Stock Manager API")


@app.get("/health")
def health(db: DbSession) -> dict[str, str]:
    """Report whether the API is up and can reach the database."""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok", "database": "ok"}
