# Recommendations: "Recommended for you"

Built on your real tables (orders, x_wishlist, reviews, carts); tested against your core files (6 tests, all pass).
Signals: purchases (x3), wishlist (x2), 4-5 star reviews (x2), cart (x1). Refunded/cancelled orders are ignored. Only products
that can be bought today are shown (active, in stock, seller approved). New customers get popular items.

## Install
1. Copy `app/ext/recommend.py`, `app/ext/recommend_api.py`, `tests/test_ext_recommend.py`, and `reco_ui.py` (next to `streamlit_app.py`).
2. `app/main.py`, right after `install(app)`:
   ```python
   from .ext.recommend_api import router as reco_router  # noqa: E402
   app.include_router(reco_router)
   ```
3. `streamlit_app.py`: add `import reco_ui` next to `import ext_ui`, and at the top of `page_shop()` after `st.header("Shop")`:
   ```python
   reco_ui.recommended(_ctx())
   ```
   (optional: `reco_ui.recommended(_ctx(), "Customers also liked", product_id=p["id"])` inside a product's Details expander)
4. `pytest -q tests/test_ext_recommend.py`

Endpoints: `GET /recommend/me`, `GET /recommend/product/{id}`, `GET /recommend/popular`, `POST /recommend/refresh` (admin).
Env: `EXT_RECO_CACHE_SECONDS` (default 600). Computed in-process and cached; fine for thousands of products.

## Separate fix: the support bot cancels and refunds from a casual question (NOT tested: needs app/ext/api.py)
Any message with "return" or "cancel" plus an order number cancels and refunds a paid, unshipped order. In `app/support/resolver.py`,
in the `refund_request` branch, before `d = shop.initiate_refund(...)`:
```python
            elif intent == "refund_request":
                if "confirm" not in (message or "").lower():
                    return intent, False, (f"To cancel and refund order {order_number} (only possible before it ships), reply: "
                                           f"confirm refund {order_number}. If you already received it and want to send it back, say so "
                                           "and I'll open a request for our team."), None
                d = shop.initiate_refund(db, user, order_number, message)
```
Then update `tests/test_support.py`: use `f"confirm refund {o.number}"` in `test_refund_on_paid_order_cancels_and_restocks` and
`test_refund_on_shipped_order_goes_to_a_human`, and add a test that "refund {o.number}" alone leaves the order `paid`.
