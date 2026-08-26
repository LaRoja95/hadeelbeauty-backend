"""HadeelBeauty — beauty tools & skincare store API.

Clean, from-scratch FastAPI backend, modeled on the Basharti/Rawalaps
store codebases. Cash-on-delivery only, targeting Algeria (58 wilayas,
per-wilaya shipping cost). TikTok Pixel + Events API (CAPI) tracking wired
in from day one with the correct event name ("CompletePayment").
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import asyncpg
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from capi import dispatch_capi_event, log_capi
from sheets import schedule_order_sheet_sync, sheets_configured

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("hadeelbeauty.api")

API_BUILD = "hadeelbeauty-api-1.0.0"

# Algerian mobile numbers, local format: 0 + (5|6|7) + 8 digits = 10 digits.
DZ_PHONE_RE = re.compile(r"^0[5-7]\d{8}$")

# --- Product catalog -------------------------------------------------------
# Product catalog — Algerian beauty market trending products. Prices in DZD.
PRODUCTS: dict[str, dict[str, Any]] = {
    "scar-gel-tcm": {
        "name": "مرهم ازالة الندبات",
        "description": (
            "تركيبة TCM بسنتيلا آسياتيكا ونياسيناميد — لتلطيف مظهر الندبات وآثار حب الشباب "
            "وتوحيد لون البشرة. قوام شفاف سريع الامتصاص — 30 جرام."
        ),
        "price": 3500,
        "image": "assets/products/scar-gel/hero-product.png",
        "problem": "ندبات وآثار حب الشباب",
        "images": [
            "assets/products/scar-gel/hero-product.png",
            "assets/products/scar-gel/v05-problems.png",
            "assets/products/scar-gel/v02-scar-types.png",
            "assets/products/scar-gel/v03-benefits.png",
            "assets/products/scar-gel/v09-promo.png",
            "assets/products/scar-gel/v06-features.png",
            "assets/products/scar-gel/v08-ingredients.png",
            "assets/products/scar-gel/v04-texture.png",
            "assets/products/scar-gel/v07-specs.png",
        ],
    },
    "niacinamide-serum": {
        "name": "سيروم نياسيناميد 10%",
        "description": (
            "نياسيناميد 10% + زنك 1% — يضيّق المسام، يُقلّل الإفرازات الزيتية، "
            "ويُساعد على توحيد لون البشرة وتخفيف البقع الداكنة. 30 مل."
        ),
        "price": 1990,
        "image": "assets/assets/products/niacinamide-serum/hero-product.png",
        "problem": "البشرة الدهنية والمسام الواسعة",
    },
    "vitamin-c-serum": {
        "name": "سيروم فيتامين سي 20%",
        "description": (
            "فيتامين سي 20% + حمض الفيروليك + فيتامين E — لتفتيح البشرة، "
            "محاربة آثار الشمس وعلامات التقدم في السن. يُستخدم صباحاً. 30 مل."
        ),
        "price": 2490,
        "image": "assets/assets/products/vitamin-c-serum/hero-product.png",
        "problem": "البشرة الباهتة وآثار الشمس",
    },
    "arbutin-cream": {
        "name": "كريم ألفا أربوتين للتفتيح",
        "description": (
            "ألفا أربوتين 2% + حمض الكوجيك + فيتامين سي — لتوحيد لون البشرة "
            "وتفتيح البقع الداكنة والكلف. مناسب لجميع أنواع البشرة. 50 جرام."
        ),
        "price": 1990,
        "image": "assets/assets/products/arbutin-cream/hero-product.png",
        "problem": "البقع الداكنة والكلف",
    },
    "spf50-sunscreen": {
        "name": "واقي شمس SPF 50+ يومي",
        "description": (
            "SPF 50+ · PA++++ — حماية عالية من أشعة UVA/UVB، قوام خفيف غير دهني "
            "لا يُبيّض البشرة. مناسب للاستخدام اليومي تحت المكياج. 50 مل."
        ),
        "price": 1990,
        "image": "assets/assets/products/spf50-sunscreen/hero-product.png",
        "problem": "حماية البشرة من الشمس يومياً",
    },
    "ceramide-cream": {
        "name": "كريم السيراميد لإصلاح البشرة",
        "description": (
            "سيراميد + هيالورونيك أسيد + بانتينول — يُرمّم الحاجز الجلدي، "
            "يُرطّب البشرة الجافة والحساسة ويُخفّف الاحمرار والتهيّج. 50 جرام."
        ),
        "price": 2490,
        "image": "assets/assets/products/ceramide-cream/hero-product.png",
        "problem": "البشرة الجافة والحساسة",
    },
    "rosemary-hair-oil": {
        "name": "زيت إكليل الجبل لنمو الشعر",
        "description": (
            "زيت إكليل الجبل + زيت الخروع + زيت الأرغان — يُحفّز نمو الشعر، "
            "يُقوّي الجذور ويُقلّل التساقط. مجرّب وفعّال — 60 مل."
        ),
        "price": 2190,
        "image": "assets/assets/products/rosemary-hair-oil/hero-product.png",
        "problem": "تساقط الشعر وضعف النمو",
    },
    "retinol-serum": {
        "name": "سيروم ريتينول ليلي 0.3%",
        "description": (
            "ريتينول 0.3% + هيالورونيك أسيد + توكوفيرول — يُجدّد خلايا البشرة ليلاً، "
            "يُقلّل التجاعيد والخطوط الدقيقة ويُوحّد الملمس. للبشرة العادية والمختلطة. 30 مل."
        ),
        "price": 2990,
        "image": "assets/assets/products/retinol-serum/hero-product.png",
        "problem": "التجاعيد وعلامات التقدم في السن",
    },
}


# --- Shipping regions (Algeria — 58 wilayas) --------------------------------
# Cash-on-delivery shipping cost per wilaya (DZD). Adjust as needed.
REGIONS: dict[str, dict[str, Any]] = {
    "alger": {"name": "الجزائر", "shippingCost": 400},

    "blida": {"name": "البليدة", "shippingCost": 450},
    "bouira": {"name": "البويرة", "shippingCost": 450},
    "tizi_ouzou": {"name": "تيزي وزو", "shippingCost": 450},
    "medea": {"name": "المدية", "shippingCost": 450},
    "boumerdes": {"name": "بومرداس", "shippingCost": 450},
    "tipaza": {"name": "تيبازة", "shippingCost": 450},

    "chlef": {"name": "الشلف", "shippingCost": 550},
    "laghouat": {"name": "الأغواط", "shippingCost": 550},
    "oum_el_bouaghi": {"name": "أم البواقي", "shippingCost": 550},
    "batna": {"name": "باتنة", "shippingCost": 550},
    "bejaia": {"name": "بجاية", "shippingCost": 550},
    "tebessa": {"name": "تبسة", "shippingCost": 550},
    "tlemcen": {"name": "تلمسان", "shippingCost": 550},
    "tiaret": {"name": "تيارت", "shippingCost": 550},
    "djelfa": {"name": "الجلفة", "shippingCost": 550},
    "jijel": {"name": "جيجل", "shippingCost": 550},
    "setif": {"name": "سطيف", "shippingCost": 550},
    "saida": {"name": "سعيدة", "shippingCost": 550},
    "skikda": {"name": "سكيكدة", "shippingCost": 550},
    "sidi_bel_abbes": {"name": "سيدي بلعباس", "shippingCost": 550},
    "annaba": {"name": "عنابة", "shippingCost": 550},
    "guelma": {"name": "قالمة", "shippingCost": 550},
    "constantine": {"name": "قسنطينة", "shippingCost": 550},
    "mostaganem": {"name": "مستغانم", "shippingCost": 550},
    "msila": {"name": "المسيلة", "shippingCost": 550},
    "mascara": {"name": "معسكر", "shippingCost": 550},
    "oran": {"name": "وهران", "shippingCost": 550},
    "bordj_bou_arreridj": {"name": "برج بوعريريج", "shippingCost": 550},
    "el_tarf": {"name": "الطارف", "shippingCost": 550},
    "tissemsilt": {"name": "تيسمسيلت", "shippingCost": 550},
    "khenchela": {"name": "خنشلة", "shippingCost": 550},
    "souk_ahras": {"name": "سوق أهراس", "shippingCost": 550},
    "mila": {"name": "ميلة", "shippingCost": 550},
    "ain_defla": {"name": "عين الدفلى", "shippingCost": 550},
    "ain_temouchent": {"name": "عين تموشنت", "shippingCost": 550},
    "relizane": {"name": "غليزان", "shippingCost": 550},

    "biskra": {"name": "بسكرة", "shippingCost": 700},
    "bechar": {"name": "بشار", "shippingCost": 700},
    "ouargla": {"name": "ورقلة", "shippingCost": 700},
    "el_bayadh": {"name": "البيض", "shippingCost": 700},
    "el_oued": {"name": "الوادي", "shippingCost": 700},
    "naama": {"name": "النعامة", "shippingCost": 700},
    "ghardaia": {"name": "غرداية", "shippingCost": 700},
    "ouled_djellal": {"name": "أولاد جلال", "shippingCost": 700},
    "touggourt": {"name": "تقرت", "shippingCost": 700},
    "el_meghaier": {"name": "المغير", "shippingCost": 700},
    "el_meniaa": {"name": "المنيعة", "shippingCost": 700},

    "adrar": {"name": "أدرار", "shippingCost": 900},
    "tamanrasset": {"name": "تمنراست", "shippingCost": 900},
    "illizi": {"name": "إليزي", "shippingCost": 900},
    "tindouf": {"name": "تندوف", "shippingCost": 900},
    "timimoun": {"name": "تيميمون", "shippingCost": 900},
    "bordj_badji_mokhtar": {"name": "برج باجي مختار", "shippingCost": 900},
    "beni_abbes": {"name": "بني عباس", "shippingCost": 900},
    "in_salah": {"name": "عين صالح", "shippingCost": 900},
    "in_guezzam": {"name": "عين قزام", "shippingCost": 900},
    "djanet": {"name": "جانت", "shippingCost": 900},
}


def get_region(region_id: str) -> dict[str, Any]:
    region = REGIONS.get(region_id)
    if not region:
        raise HTTPException(status_code=422, detail="ولاية غير صالحة")
    return region


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def sha256(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def normalize_phone(raw_phone: str) -> str:
    digits = re.sub(r"\D", "", raw_phone)
    if digits.startswith("213"):
        rest = digits[3:]
        if rest.startswith("0"):
            rest = rest[1:]
        if len(rest) == 9 and rest[0] in "567":
            return "+213" + rest
    if DZ_PHONE_RE.fullmatch(digits):
        return "+213" + digits[1:]
    raise HTTPException(
        status_code=422,
        detail="رقم الهاتف يجب أن يبدأ بـ 05 أو 06 أو 07 (10 أرقام) أو +213",
    )


def client_ip(request: Request) -> str:
    for header in ("cf-connecting-ip", "x-real-ip", "x-forwarded-for"):
        value = request.headers.get(header)
        if value:
            return value.split(",")[0].strip()
    return request.client.host if request.client else ""


def generate_order_id(created: datetime) -> str:
    suffix = secrets.token_hex(3).upper()
    return f"hdb{created.strftime('%m%d%Y')}{suffix}"


# --- Request / response models ---------------------------------------------
class OrderItem(BaseModel):
    productId: str
    quantity: int = Field(ge=1, le=10)


class PrepareOrderRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(min_length=9, max_length=20)
    regionId: str = Field(min_length=1, max_length=40)
    city: str = Field(min_length=2, max_length=60)
    address: str = Field(min_length=5, max_length=240)
    items: list[OrderItem] = Field(min_length=1)
    eventId: str = ""


class CompleteOrderRequest(BaseModel):
    orderId: str
    eventId: str = ""
    fbp: str = ""
    fbc: str = ""
    eventSourceUrl: str = ""


class TrackingEventRequest(BaseModel):
    eventName: str
    eventId: str
    orderId: str | None = None
    payload: dict[str, Any] = {}


def validate_items(items: list[OrderItem]) -> tuple[list[dict[str, Any]], int]:
    clean_items: list[dict[str, Any]] = []
    subtotal = 0
    for item in items:
        product = PRODUCTS.get(item.productId)
        if not product:
            raise HTTPException(status_code=422, detail="منتج غير صالح")
        line_total = product["price"] * item.quantity
        clean_items.append(
            {
                "product_id": item.productId,
                "name": product["name"],
                "price": product["price"],
                "quantity": item.quantity,
            }
        )
        subtotal += line_total
    return clean_items, subtotal


def health_payload() -> dict[str, Any]:
    pixel_id = os.getenv("TIKTOK_PIXEL_ID", "").strip()
    token = os.getenv("TIKTOK_ACCESS_TOKEN", "").strip()
    return {
        "status": "ok",
        "service": "hadeelbeauty-api",
        "build": API_BUILD,
        "productsCount": len(PRODUCTS),
        "tiktokConfigured": bool(pixel_id and token),
        "sheetsConfigured": sheets_configured(),
    }


def database_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    return url.replace("postgres://", "postgresql://", 1)


async def ensure_schema(conn: "asyncpg.Connection") -> None:
    await conn.execute(
        """
        create table if not exists orders (
            id text primary key,
            created_at timestamptz not null,
            status text not null,
            name text not null,
            phone_raw text not null,
            phone_e164 text not null,
            city text not null,
            address text not null,
            items jsonb not null,
            total_sar integer not null,
            event_id text not null
        );
        """
    )
    # Migration-safe: add shipping columns for stores created before regions existed.
    await conn.execute("alter table orders add column if not exists region_id text not null default 'alger'")
    await conn.execute("alter table orders add column if not exists shipping_sar integer not null default 0")
    await conn.execute(
        """
        create table if not exists tracking_events (
            id bigserial primary key,
            created_at timestamptz not null,
            event_name text not null,
            event_id text not null,
            order_id text,
            payload jsonb not null
        );
        """
    )
    await conn.execute("create index if not exists idx_orders_created_at on orders (created_at desc)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    url = database_url()
    logger.info("STARTUP: connecting to Postgres ...")
    app.state.pool = await asyncpg.create_pool(url, min_size=1, max_size=5)
    async with app.state.pool.acquire() as conn:
        await ensure_schema(conn)
    logger.info("STARTUP OK: DB connected, tables ensured")
    yield
    await app.state.pool.close()
    logger.info("SHUTDOWN: Postgres pool closed")


app = FastAPI(title="HadeelBeauty API", version="1.0.0", lifespan=lifespan)

allowed_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/")
async def root() -> dict[str, Any]:
    return health_payload()


@app.get("/health")
async def health() -> dict[str, Any]:
    return health_payload()


@app.get("/api/products")
async def list_products() -> list[dict[str, Any]]:
    return [{"id": pid, **data} for pid, data in PRODUCTS.items()]


@app.get("/api/products/{product_id}")
async def get_product(product_id: str) -> dict[str, Any]:
    product = PRODUCTS.get(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"id": product_id, **product}


@app.get("/api/regions")
async def list_regions() -> list[dict[str, Any]]:
    return [{"id": rid, **data} for rid, data in REGIONS.items()]


@app.post("/api/orders/prepare")
async def prepare_order(payload: PrepareOrderRequest, request: Request) -> dict[str, Any]:
    phone_e164 = normalize_phone(payload.phone)
    region = get_region(payload.regionId)
    clean_items, subtotal = validate_items(payload.items)
    shipping = region["shippingCost"]
    total = subtotal + shipping

    order_id = generate_order_id(now_dt())
    created = now_dt()

    async with request.app.state.pool.acquire() as conn:
        await conn.execute(
            """
            insert into orders
                (id, created_at, status, name, phone_raw, phone_e164, city, address, items, total_sar, event_id, region_id, shipping_sar)
            values ($1, $2, 'pending', $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12)
            """,
            order_id,
            created,
            payload.name.strip(),
            re.sub(r"\D", "", payload.phone),
            phone_e164,
            payload.city.strip(),
            payload.address.strip(),
            json.dumps(clean_items, ensure_ascii=False),
            total,
            payload.eventId,
            payload.regionId,
            shipping,
        )

    return {
        "orderId": order_id,
        "subtotal": subtotal,
        "shipping": shipping,
        "total": total,
        "regionName": region["name"],
    }


@app.post("/api/orders/complete")
async def complete_order(payload: CompleteOrderRequest, request: Request) -> dict[str, Any]:
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow("select * from orders where id = $1", payload.orderId)
        if not row:
            raise HTTPException(status_code=404, detail="Order not found")
        if row["status"] == "completed":
            return {"orderId": payload.orderId, "status": "completed"}

        await conn.execute(
            "update orders set status = 'completed', event_id = $2 where id = $1",
            payload.orderId,
            payload.eventId,
        )

    ip = client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    items = json.loads(row["items"]) if isinstance(row["items"], str) else row["items"]
    order_dict = dict(row)
    region_name = REGIONS.get(row["region_id"], {}).get("name", row["region_id"])

    schedule_order_sheet_sync(order_dict, region_name, status="مؤكد")

    capi_payload: dict[str, Any] = {
        "value": row["total_sar"],
        "currency": "DZD",
        "phone": row["phone_e164"],
        "productIds": [item["product_id"] for item in items],
        "fbp": payload.fbp or "",
        "fbc": payload.fbc or "",
    }
    site_url = os.getenv("SITE_URL", "").strip().rstrip("/")
    page_url = payload.eventSourceUrl or (f"{site_url}/?order={payload.orderId}" if site_url else "")
    if page_url:
        capi_payload["pageUrl"] = page_url

    asyncio.create_task(
        dispatch_capi_event(
            event_name="CompletePayment",
            event_id=payload.eventId or secrets.token_hex(8),
            payload=capi_payload,
            ip=ip,
            user_agent=user_agent,
        )
    )

    return {"orderId": payload.orderId, "status": "completed"}


@app.post("/api/e")
async def tracking_event(payload: TrackingEventRequest, request: Request) -> dict[str, bool]:
    ip = client_ip(request)
    user_agent = request.headers.get("user-agent", "")

    async with request.app.state.pool.acquire() as conn:
        await conn.execute(
            """
            insert into tracking_events (created_at, event_name, event_id, order_id, payload)
            values ($1, $2, $3, $4, $5::jsonb)
            """,
            now_dt(),
            payload.eventName,
            payload.eventId,
            payload.orderId,
            json.dumps(payload.payload, ensure_ascii=False),
        )

    if payload.eventName != "CompletePayment":
        # CompletePayment is dispatched from /api/orders/complete once the
        # order is actually confirmed server-side, so it isn't re-fired here.
        asyncio.create_task(
            dispatch_capi_event(
                event_name=payload.eventName,
                event_id=payload.eventId,
                payload=payload.payload,
                ip=ip,
                user_agent=user_agent,
            )
        )

    return {"ok": True}


@app.get("/api/admin/orders")
async def admin_orders(request: Request, x_admin_token: str = Header(default="")) -> list[dict[str, Any]]:
    expected = os.getenv("ADMIN_TOKEN", "").strip()
    if not expected or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")

    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch("select * from orders order by created_at desc limit 200")

    result = []
    for row in rows:
        order = dict(row)
        order["created_at"] = order["created_at"].isoformat()
        order["items"] = json.loads(order["items"]) if isinstance(order["items"], str) else order["items"]
        result.append(order)
    return result
