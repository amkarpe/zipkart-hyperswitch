import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, field_validator

from app import fulfillment
from app import hyperswitch as hs
from app import payments
from app import refunds
from app import sweep
from app.config import settings
from app.db import close_pool, open_pool, pool
from app.fulfillment import FulfillmentError
from app.payments import CheckoutError
from app.pricing import PricingError
from app.refunds import RefundError
from app.remedies import RemedyError, create_remedy
from app.store import router as store_router
from app.webhooks import router as webhook_router
from app.views import quote_breakdown

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("zipkart")

_SECRET_PARAM = re.compile(r"((?:payment_intent_)?client_secret=)[^&\s\"]*")


class RedactClientSecret(logging.Filter):
    """Hyperswitch redirects to the return page with ?payment_intent_client_secret=...;
    uvicorn's access log would otherwise write it to Render's logs in plain text."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_SECRET_PARAM.sub(r"\1[REDACTED]", a) if isinstance(a, str) else a
                                for a in record.args)
        if isinstance(record.msg, str):
            record.msg = _SECRET_PARAM.sub(r"\1[REDACTED]", record.msg)
        return True


logging.getLogger("uvicorn.access").addFilter(RedactClientSecret())
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
def _check_base_url() -> None:
    """APP_BASE_URL becomes the payment return_url: if it's wrong, customers who just paid
    are redirected to someone else's site, carrying their client_secret in the URL."""
    base = settings.app_base_url
    if settings.app_env == "production" and not base.startswith("https://"):
        log.warning("APP_BASE_URL=%r is not https in production; payment redirects would be insecure", base)
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")   # set by Render automatically
    if render_url and render_url != base:
        log.warning("APP_BASE_URL=%r does not match this service's Render URL %r; customers would be "
                    "redirected to the wrong site after paying", base, render_url)


async def lifespan(_app: FastAPI):
    _check_base_url()
    await open_pool()
    sweeper = asyncio.create_task(sweep.sweep_forever())
    yield
    sweeper.cancel()
    await hs.close()
    await close_pool()


app = FastAPI(title="ZipKart", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(store_router)
app.include_router(webhook_router)


@app.exception_handler(CheckoutError)
async def _checkout_error(_req, exc: CheckoutError):
    body = {"error": exc.message}
    if exc.order_id:
        body["order_id"] = exc.order_id
    return JSONResponse(body, status_code=exc.http_status)


@app.exception_handler(FulfillmentError)
async def _fulfillment_error(_req, exc: FulfillmentError):
    return JSONResponse({"error": exc.message}, status_code=exc.http_status)


@app.exception_handler(RemedyError)
@app.exception_handler(RefundError)
async def _remedy_error(_req, exc):
    return JSONResponse({"error": exc.message}, status_code=exc.http_status)


@app.exception_handler(PricingError)
async def _pricing_error(_req, exc: PricingError):
    return JSONResponse({"error": str(exc)}, status_code=400)


# ── Request models (the browser sends SKUs and amounts of goods, never prices) ──
class CartLine(BaseModel):
    sku: str = Field(max_length=40)
    quantity: int | None = None
    weight_lb: Decimal | None = None


class Customer(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: EmailStr
    phone: str = Field(pattern=r"^\d{10}$")
    line1: str = Field(min_length=3, max_length=200)
    line2: str | None = Field(default=None, max_length=200)
    city: str = Field(min_length=2, max_length=100)
    state: str = Field(pattern=r"^[A-Z]{2}$")
    zip: str = Field(pattern=r"^\d{5}$")

    @field_validator("name", "line1", "city")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class QuoteRequest(BaseModel):
    items: list[CartLine] = Field(min_length=1, max_length=50)


class OrderRequest(QuoteRequest):
    customer: Customer
    substitution_preference: Literal["best_match", "remove"]


def _cart(items: list[CartLine]) -> list[dict]:
    return [line.model_dump(exclude_none=True) for line in items]


# ── Pages ──────────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def index_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/orders/{order_id}", include_in_schema=False)
@app.get("/orders/{order_id}/return", include_in_schema=False)
async def order_page(order_id: str):
    # /return is where Hyperswitch redirects after confirm (appending payment_id/status,
    # which we ignore: the page asks our server, and our server asks Hyperswitch).
    return FileResponse(STATIC_DIR / "order.html")


# ── API ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    try:
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) AS n FROM schema_migrations")
            migrations = (await cur.fetchone())["n"]
    except Exception as exc:  # report, don't crash: Render uses this for health checks
        return JSONResponse({"status": "error", "database": repr(exc)}, status_code=503)
    return {"status": "ok", "database": "ok", "migrations_applied": migrations}


@app.get("/api/products")
async def list_products():
    products = await payments.load_products()
    return [
        {k: p[k] for k in ("sku", "name", "category", "pricing_type", "unit_price_cents",
                           "unit_label", "taxable", "in_stock", "emoji")}
        for p in products.values()
    ]


@app.post("/api/quote")
async def quote(req: QuoteRequest):
    q = await payments.quote_cart(_cart(req.items))
    return {
        "lines": [
            {"sku": l.sku, "name": l.name, "pricing_type": l.pricing_type, "quantity": l.quantity,
             "estimated_weight_lb": str(l.estimated_weight_lb) if l.estimated_weight_lb is not None else None,
             "estimated_line_cents": l.estimated_line_cents, "hold_line_cents": l.hold_line_cents}
            for l in q.lines
        ],
        "subtotal_cents": q.subtotal_cents,
        "tax_cents": q.tax_cents,
        "delivery_fee_cents": q.delivery_fee_cents,
        "estimated_total_cents": q.estimated_total_cents,
        "hold_amount_cents": q.hold_amount_cents,
        "hold_breakdown": quote_breakdown(q, settings.weight_buffer_percent),
    }


@app.post("/api/orders")
async def create_order(req: OrderRequest):
    return await payments.create_order(_cart(req.items), req.customer.model_dump(), req.substitution_preference)


@app.post("/api/orders/{order_id}/payment-session")
async def payment_session(order_id: str):
    return await payments.payment_session(order_id)


@app.post("/api/orders/{order_id}/sync")
async def sync(order_id: str):
    return await payments.sync_payment(order_id)


@app.get("/api/orders/{order_id}")
async def get_order(order_id: str):
    # Unanswered substitutions past their deadline get the order's preference here,
    # on read: no background job needed, and the answer is the same whoever looks first.
    await fulfillment.apply_expired_substitutions(order_id)
    return await payments.order_view(order_id)


class Decision(BaseModel):
    decision: Literal["accept", "decline"]


@app.post("/api/orders/{order_id}/substitutions/{sub_id}")
async def decide_substitution(order_id: str, sub_id: int, body: Decision):
    await fulfillment.decide_substitution(order_id, sub_id, body.decision == "accept")
    return await payments.order_view(order_id)


class ProblemReport(BaseModel):
    item_id: int
    reason: Literal["missing", "damaged", "expired"]
    resolution: Literal["refund", "replacement"]
    quantity: int | None = Field(default=None, ge=1, le=24)
    note: str | None = Field(default=None, max_length=280)


@app.post("/api/orders/{order_id}/remedies")
async def report_problem(order_id: str, body: ProblemReport):
    # Tx 1 (remedies.insert_remedy): validate limits under the order lock and record it.
    remedy = await create_remedy(order_id, body.item_id, body.reason, body.resolution, body.quantity,
                                 (body.note or "").strip() or None)
    # Tx 2 + Hyperswitch call: open refund attempt 1 and send it.
    if remedy["resolution"] == "refund":
        await refunds.start_refund(remedy["id"])
    return await payments.order_view(order_id)


@app.post("/api/orders/{order_id}/cancel")
async def cancel_order(order_id: str):
    await fulfillment.cancel_order(order_id, "customer")
    return await payments.order_view(order_id)
