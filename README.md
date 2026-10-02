# Store Backend

FastAPI + SQLAlchemy 2 backend for a merchandise store: accounts, catalog with variants, cart, checkout,
payments (Stripe / Razorpay / mock), orders, coupons, reviews, and an admin API.

## Run locally
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt streamlit
cp .env.example .env            # set ADMIN_EMAIL / ADMIN_PASSWORD
streamlit run streamlit_app.py  # storefront + admin UI (backend runs inside it)
# or API only:
python -m app.seed              # optional sample products + coupon WELCOME10
uvicorn app.main:app --reload   # docs at http://localhost:8000/docs
pytest -q
```

## Deploy on streamlit.io (Community Cloud)
1. Create a free Postgres on Neon or Supabase and copy its connection string (SQLite is wiped on every restart).
2. Push this folder to GitHub with `streamlit_app.py` at the repo root.
3. share.streamlit.io -> New app -> main file `streamlit_app.py` -> Advanced settings: Python 3.12 and paste
   `.streamlit/secrets.toml.example` (edited) into Secrets.
4. Open the app, log in with `ADMIN_EMAIL` / `ADMIN_PASSWORD`, go to Admin -> Products -> "Load sample products".

**Limits on Streamlit Cloud:** it cannot receive Stripe/Razorpay webhooks, so use it with mock payments (demo,
prototypes, internal tools). It is not suited to a public high-traffic storefront (app sleeps when idle, one
Python process, no custom domain). For real sales, deploy the API (`Dockerfile`, e.g. on Render) with a real
`PAYMENT_PROVIDER`, then set `API_BASE_URL` in the Streamlit secrets so Streamlit is only the UI.

## Buyer flow
`POST /auth/register` -> `GET /products` -> `POST /cart/items` -> `POST /addresses` ->
`POST /checkout` (send an `Idempotency-Key` header) -> pay using the `payment` block in the response ->
provider webhook marks the order `paid` -> `GET /orders/{number}`.
In dev (`PAYMENT_PROVIDER=mock`) call `POST /payments/mock/{number}/confirm` instead of paying.

## Order states
`pending_payment -> paid -> shipped -> delivered`, plus `cancelled` and `refunded`.
Unpaid orders expire after `UNPAID_EXPIRY_MINUTES` and their stock is released.

## Design decisions worth knowing
- Money is integer minor units (paise/cents). `4900` = 49.00.
- Prices are always read from the DB at checkout; the client never sends a price.
- Stock is reserved atomically at checkout (`UPDATE ... WHERE stock >= qty`), so it can't oversell under load.
- Webhooks are signature-verified, de-duplicated, and the paid amount must equal the order total.
- A payment arriving after an order expired is auto-refunded.
- Admin role is read from the DB on every request, not trusted from the JWT.

## Going live checklist
1. `APP_ENV=production`, strong `JWT_SECRET`, Postgres `DATABASE_URL`, real `PAYMENT_PROVIDER` (the app refuses to boot otherwise).
2. Register the webhook in your provider dashboard:
   Stripe -> `https://<host>/payments/webhook/stripe` (event `payment_intent.succeeded`);
   Razorpay -> `https://<host>/payments/webhook/razorpay` (events `payment.captured`).
3. Replace `create_all` in `app/main.py` with Alembic migrations.
4. Move the login throttle to Redis if you run multiple workers; put the API behind HTTPS.
5. Product images: store URLs only; host files on S3/Cloudinary.

## Not built yet (natural next steps)
Multi-vendor sellers/payouts, product search engine (Meilisearch/Elastic), wishlist, password reset + email
verification, GST invoices, shipping-rate/carrier integration, returns workflow, admin dashboard UI.
