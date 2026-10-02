# Store Backend

FastAPI + SQLAlchemy 2 backend for a merchandise store: accounts, catalog with variants, cart, checkout,
payments (Stripe / Razorpay / mock), orders, coupons, reviews, and an admin API.

## Run
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # set ADMIN_EMAIL / ADMIN_PASSWORD
python -m app.seed              # optional sample products + coupon WELCOME10
uvicorn app.main:app --reload   # docs at http://localhost:8000/docs
pytest -q
```

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
