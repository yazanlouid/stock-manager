"""GET /alerts: what needs attention now, and what did (D65).

Stock writes keep the alerts in step with the stock, but a batch also becomes
expiring or expired just because a day passes. So this read first runs the
alerts sweep when the business's local date has moved past its alerts date
(D62), and the list is current whenever someone looks at it. get_db commits
the sweep with the request (D42).
"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import and_, select

from app.api.deps import CurrentBusiness, DbSession, Now
from app.api.schemas import AlertBatchOut, AlertOut
from app.domain.alerts import sweep_alerts_if_due
from app.models import Alert, Batch, Item, Location, StockBalance

router = APIRouter(tags=["alerts"])

AlertStatus = Literal["open", "resolved", "all"]
AlertType = Literal["low_stock", "expiring", "expired", "negative_stock"]


@router.get("/alerts")
def get_alerts(
    db: DbSession,
    business: CurrentBusiness,
    now: Now,
    status: AlertStatus = "open",
    # "type" in the URL; alert_type here, so the builtin isn't shadowed.
    alert_type: Annotated[AlertType | None, Query(alias="type")] = None,
    location_id: UUID | None = None,
    item_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[AlertOut]:
    """The business's alerts, newest first (raised_at, then id, descending).

    Open alerts by default; status=resolved gives the history and status=all
    both. Each alert carries its item, its location's name and, unless it is
    a low_stock alert, its batch with what that batch holds now. A
    location_id or item_id that isn't the business's matches nothing, so the
    list is empty.
    """
    business_id = business.id
    sweep_alerts_if_due(db, business_id, now)

    alerts = (
        select(
            Alert,
            Item.sku,
            Item.name.label("item_name"),
            Item.base_unit,
            Location.name.label("location_name"),
            Batch.is_default,
            Batch.expires_on,
            Batch.lot_code,
            StockBalance.qty_on_hand,
        )
        .join(Item, and_(Item.business_id == Alert.business_id, Item.id == Alert.item_id))
        .join(
            Location,
            and_(Location.business_id == Alert.business_id, Location.id == Alert.location_id),
        )
        # A low_stock alert has no batch, so both of these are outer joins.
        .outerjoin(Batch, and_(Batch.business_id == Alert.business_id, Batch.id == Alert.batch_id))
        .outerjoin(
            StockBalance,
            and_(
                StockBalance.business_id == Alert.business_id,
                StockBalance.batch_id == Alert.batch_id,
            ),
        )
        .where(Alert.business_id == business_id)
        .order_by(Alert.raised_at.desc(), Alert.id.desc())
        .limit(limit)
    )
    if status != "all":
        alerts = alerts.where(Alert.status == status)
    if alert_type is not None:
        alerts = alerts.where(Alert.type == alert_type)
    if location_id is not None:
        alerts = alerts.where(Alert.location_id == location_id)
    if item_id is not None:
        alerts = alerts.where(Alert.item_id == item_id)

    result = []
    for row in db.execute(alerts):
        alert = row.Alert
        batch = None
        if alert.batch_id is not None:
            batch = AlertBatchOut(
                id=alert.batch_id,
                is_default=row.is_default,
                expires_on=row.expires_on,
                lot_code=row.lot_code,
                on_hand=row.qty_on_hand,
            )
        result.append(
            AlertOut(
                id=alert.id,
                type=alert.type,
                status=alert.status,
                raised_at=alert.raised_at,
                resolved_at=alert.resolved_at,
                location_id=alert.location_id,
                location_name=row.location_name,
                item_id=alert.item_id,
                sku=row.sku,
                item_name=row.item_name,
                base_unit=row.base_unit,
                batch=batch,
            )
        )
    return result
