# Store extensions: install and reference

Adds multi-vendor payouts, returns, GST invoices, shipping, search, wishlist, email verification and password reset **without
editing any existing router or service**. All new tables are prefixed `x_` and no existing column changes, so
`Base.metadata.create_all` creates them (Alembic will also pick them up).

## Install (3 steps)
1. Copy `app/ext/` into your `app/`, `ext_ui.py` next to `streamlit_app.py`, and replace `streamlit_app.py` with the patched one
   (diff it first; it only adds `ext_ui` calls, a `/search` call in the shop, and extra tabs/pages).
2. In `app/main.py`, after `app = FastAPI(...)` and **before** `Base.metadata.create_all(...)`, add:
   ```python
   from .ext import install; install(app)
   ```
3. `pip install -r requirements.txt` (adds `reportlab`). Then `pytest -q`.

`app/main.py` also calls `ensure_schema(engine)` after `create_all`: it adds the unique index `uq_x_shipments_live` (one live parcel per
order and seller) to databases whose `x_shipments` table already exists. If it logs an error, you have duplicate live shipments to clean up first.

Optional but recommended (run the money ledger on a schedule, next to `expire_unpaid`):
```python
from app.ext import ledger
with SessionLocal() as db: ledger.sync(db)      # accrues paid orders, reverses refunded ones; idempotent
```
`sync` also runs automatically when a seller opens their ledger and when an admin runs payouts.

## Environment variables (all optional)
| Variable | Default | Meaning |
|---|---|---|
| `EXT_PAYOUT_HOLD_DAYS` | 7 | days before a sale becomes payable |
| `EXT_PAYOUT_MIN_CENTS` | 0 | minimum payout |
| `EXT_DEFAULT_COMMISSION_PCT` | 10 | used if a seller has no commission set |
| `EXT_COUPON_FUNDED_BY` | platform | `platform`: sellers are paid on list price and the platform absorbs coupons. `seller`: the order discount is shared across lines, so sellers are paid on what the buyer paid |
| `EXT_RETURN_WINDOW_DAYS` | 10 | days after delivery a return can be requested |
| `GST_MODE` | inclusive | `inclusive` (prices contain GST) or `exclusive` |
| `GST_DEFAULT_RATE` | 18 | used when a product has no rate set |
| `GST_PLATFORM_NAME/GSTIN/STATE_CODE/ADDRESS` | | supplier details for house-sold products |
| `SHIPPING_PROVIDER` | mock | `mock` or `shiprocket` (`SHIPROCKET_EMAIL/PASSWORD`) |
| `SHIPPING_DEFAULT_PICKUP_PINCODE` / `_WEIGHT_G` | 110001 / 500 | fallbacks |
| `SHIPPING_WEBHOOK_TOKEN` | | required to accept `POST /shipping/webhook` |
| `SEARCH_PROVIDER` | sql | `sql` or `meilisearch` (`MEILI_URL`, `MEILI_KEY`, `MEILI_INDEX`) |
| `APP_BASE_URL` | http://localhost:8501 | base of the links in verification / reset emails |
| `AUTH_TOKENS_PER_HOUR` | 3 | verify/reset emails per account per hour |

## How the pieces work
- **Ledger.** Every paid order line gets a `LineSplit` (seller, commission % snapshot, net). Sellers get an append-only
  ledger credit (available after the hold). Refunds and returns post negative entries. Houses sales (no seller) earn no ledger row.
- **Payouts.** `Run payouts` bundles each approved seller's released balance into one payout (skips sellers without UPI/bank
  details). You pay them (bank/UPI) and record the UTR; "failed" releases the money back. No automatic transfer yet.
- **Returns.** requested → approved → received → refunding → refunded (or rejected/cancelled). `refunding` is held while the provider is called, so a double click cannot refund twice; if the provider does not confirm, the return goes back to `received` and can be retried. Partial returns supported. Refund = items
  + their share of tax − their share of discount (shipping not refunded), sent to the provider as a partial refund; stock is
  restocked, the seller's earnings reversed, and a GST credit note issued. Returning every unit marks the order `refunded`.
- **GST.** One invoice per supplier (each seller / the house) per order, numbered `S12/2627/000001` (FY, sequential, ≤16 chars),
  CGST+SGST when supplier and buyer state match, IGST otherwise. HSN + rate per product (`PUT /admin/ext/products/{id}/tax`).
  Invoices are snapshots: later price/rate edits never change an issued invoice.
- **Shipping.** Rate quotes per seller parcel (`POST /shipping/quote`), one shipment per seller per order, order becomes
  `shipped` when all parcels are booked and `delivered` when all are delivered. Tracking by webhook or the admin refresh button.
- **Search.** `GET /search` (+ `/search/suggest`). With Meilisearch the index is updated by SQLAlchemy hooks after commit;
  if the engine is down, requests fall back to SQL.
- **Accounts.** Single-use hashed tokens, per-account rate limit, no account enumeration. `require_verified_email` is an
  optional dependency (e.g. add it to checkout).

## Known gaps (read before going live)
1. **Checkout still charges your flat shipping fee and flat `TAX_PERCENT`.** Quotes and invoices exist, but changing what
   checkout charges means editing `pricing.quote` / `create_order`. With `GST_MODE=inclusive` set `TAX_PERCENT=0`
   (invoices warn if tax was added on top).
2. ~~Reset does not log out existing sessions.~~ Fixed: tokens carry `iat` and `security.current_user` rejects tokens issued before
   `UserFlag.password_changed_at`. Tokens minted before this release have no `iat` and are treated as issued at time 0, so they stop
   working after the owner's next password reset (and are otherwise valid until they expire).
3. Payouts are recorded manually; Stripe/Razorpay/Shiprocket calls (incl. partial refunds) are written from their docs and
   not run against live accounts; Meilisearch was tested against a fake server only.
4. `SellerProfile.bank_account_no` is stored as plain text; encrypt before production.
5. Streamlit Cloud cannot receive webhooks (carrier tracking): use the Refresh button, or deploy the API.
6. Cross-table references are plain integers (no ForeignKeys) so the package works with your existing table names.
7. A return stuck in `refunding` means the process died after claiming it. Check the payment provider's dashboard: if the refund is
   there, set the return to `refunded` by hand; if not, set it back to `received` and refund again.
8. An admin "refund order" after partial returns refunds only the remainder (total minus what returns already paid back) and does not
   restock; returned units were restocked by the returns flow.
9. The login throttle, RAG index and CAG cache are per-process memory. The Dockerfile therefore runs one worker by default.
