import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, field_validator

from app import hyperswitch as hs
from app import payments
from app.db import close_pool, open_pool, pool
from app.payments import CheckoutError
from app.pricing import PricingError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await open_pool()
    yield
    await hs.close()
    await close_pool()


app = FastAPI(title="ZipKart", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(CheckoutError)
async def _checkout_error(_req, exc: CheckoutError):
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


def _cart(items: list[CartLine]) -> list[dict]:
    return [line.model_dump(exclude_none=True) for line in items]


# ── Pages ──────────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def index_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/orders/{order_id}/return", include_in_schema=False)
async def return_page(order_id: str):
    # Hyperswitch redirects here after confirm (appending payment_id/status, which we
    # ignore: the page asks our server, and our server asks Hyperswitch).
    return FileResponse(STATIC_DIR / "return.html")


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
    }


@app.post("/api/orders")
async def create_order(req: OrderRequest):
    return await payments.create_order(_cart(req.items), req.customer.model_dump())


@app.post("/api/orders/{order_id}/payment-session")
async def payment_session(order_id: str):
    return await payments.payment_session(order_id)


@app.post("/api/orders/{order_id}/sync")
async def sync(order_id: str):
    return await payments.sync_payment(order_id)


@app.get("/api/orders/{order_id}")
async def get_order(order_id: str):
    return await payments.order_view(order_id)
