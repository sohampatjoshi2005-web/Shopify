# Changes

## Revision 2: recommendations + leads (new)
New package `app/recommend/` (engine, leads, API, tables `rec_leads`), `recommend_ui.py` (Shop shelf + admin **Leads** tab), wired into
`app/main.py` and `streamlit_app.py`. 12 new tests in `tests/test_recommend.py`; full suite **71 passing**. See the README section "Recommendations + leads".
Two bugs the new tests caught while building it: a new lead's status was `None` until flushed (so auto-convert skipped it), and adding one manual
lead triggered a full sync (now scoped to that user). The stale "Not built yet" paragraph in the README was replaced.

---

# Revision 1: corrections

Test suite at the end of revision 1: 47 original + 12 regression tests (`tests/test_fixes.py`) = 59 passing.

## Money
| File | Problem | Fix |
|---|---|---|
| `app/ext/returns.py` | Provider refund was called *before* the return was claimed, so a double click or two admins could refund twice (Razorpay has no idempotency key). | New `refunding` state: claim first, call provider, then finish. Provider failure returns the return to `received` for retry. |
| `app/routers/admin.py` | Admin "refund order" always refunded the full total, even after a partial return had already paid part back. | Refunds only `total - already returned`; refuses when nothing is left; no double restock. |
| `app/routers/payments.py` | Webhook dedup row was committed before the work finished, so a crash made the provider's retry look like a duplicate. | Dedup row is written after the work, in the same commit. |
| `app/ext/ledger.py`, `ext/config.py` | Coupon discounts were silently absorbed by the platform. | Explicit `EXT_COUPON_FUNDED_BY=platform\|seller` (default unchanged). |
| `app/services/orders.py` | Re-using an Idempotency-Key whose order was cancelled returned the dead order as if checkout succeeded. | 409 asking for a new key. |
| `app/ext/gst.py` | Warning text named a setting that does not exist (`TAX_RATE_PCT`). | Now `TAX_PERCENT`. |

## Auth
| File | Problem | Fix |
|---|---|---|
| `app/security.py` | Password reset did not log out existing sessions (known gap #2). | Tokens carry `iat`; tokens older than `password_changed_at` are rejected. |
| `app/routers/auth.py` | Login throttle keyed by email only: anyone could lock a victim out. | Keyed by (email, client IP); dict kept bounded. |

## Support desk
| File | Problem | Fix |
|---|---|---|
| `app/support/chatbot.py` | Substring matching ("bill" in "billion", "api" in "capital"), bare "order" catch-all. | Whole-word matching; a bare `ORD-...` number means order status. |
| `app/support/resolver.py` | "refund"/"cancel"/"return" + an order number autonomously cancelled and refunded a paid order. | Customer must reply `confirm cancel ORD-...` first. |
| `app/support/api.py` | A stale token returned 401 from the chat endpoint, breaking logged-out password reset. | Invalid token = logged out. |
| `app/support/api.py` | Admin ticket for an unknown email built an unsaved `User` just to read `.id`. | Explicit `user_id=None`. |
| `app/support/api.py` | Re-opening a resolved ticket left `resolved_at` set; escalation left `resolution_type` unset. | Cleared / set. |

## Marketplace
| File | Problem | Fix |
|---|---|---|
| `app/ext/models.py`, `shipping.py`, `ext/__init__.py`, `main.py` | Two simultaneous "ship" clicks could book two carriers (check-then-insert race). | Partial unique index `uq_x_shipments_live`; `ensure_schema()` adds it to existing databases; violation becomes a 409. |
| `app/ext/api.py` | Carrier webhook was `async def` doing blocking DB work on the event loop. | Plain `def`. |

## Deployment / UI
- `Dockerfile`: runs as non-root; one worker by default (throttle, RAG and CAG are per-process); `--proxy-headers` so the per-IP throttle sees real clients.
- `ext_ui.py`: shows the new `refunding` state.
- Unused imports removed; `extensions.md` and `README.md` updated.

## Not changed (needs your decision)
- Checkout still charges the flat shipping fee and `TAX_PERCENT` (known gap #1).
- `bank_account_no` is still plain text (gap #4).
- Stripe / Razorpay / Shiprocket / Meilisearch calls are still untested against live accounts.
- `streamlit_app.py` was syntax-checked only, not reviewed line by line.
