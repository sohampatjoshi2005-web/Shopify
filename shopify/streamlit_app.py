"""Streamlit storefront + admin.  Streamlit Cloud main file: streamlit_app.py

Two modes, chosen automatically:
  * API_BASE_URL unset -> the backend runs INSIDE this app (in-process, no separate server).
  * API_BASE_URL set   -> this app is only a UI for a separately deployed backend (needed for real payments).
"""
import os
import uuid
import streamlit as st

# Secrets -> env vars BEFORE the backend is imported (its settings read the environment at import time).
try:
    for _k in st.secrets:
        _v = st.secrets[_k]
        if isinstance(_v, (str, int, float, bool)):
            os.environ.setdefault(str(_k), str(_v))
except Exception:
    pass  # no secrets file locally; real environment variables / .env are used instead

st.set_page_config(page_title="Store", page_icon="🛍️", layout="wide")
API_BASE = os.getenv("API_BASE_URL", "").rstrip("/")


# ---------------------------------------------------------------- API layer
class ApiError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


@st.cache_resource
def _embedded_app():
    from app.db import Base, engine
    from app.main import app, bootstrap_admin
    Base.metadata.create_all(engine)
    bootstrap_admin()
    return app


@st.cache_data(ttl=300)
def _expire_unpaid():
    """No background loop on Streamlit, so release stock of abandoned checkouts every ~5 min on page loads."""
    if API_BASE:
        return 0
    _embedded_app()
    from app.db import SessionLocal
    from app.services.orders import expire_unpaid
    with SessionLocal() as db:
        return expire_unpaid(db)


def api(method, path, *, json=None, params=None, headers=None, auth=True):
    h = dict(headers or {})
    tok = st.session_state.get("token")
    if auth and tok:
        h["Authorization"] = f"Bearer {tok}"
    if API_BASE:
        import requests
        r = requests.request(method, API_BASE + path, json=json, params=params, headers=h, timeout=30)
    else:
        from fastapi.testclient import TestClient
        r = TestClient(_embedded_app(), raise_server_exceptions=False).request(method, path, json=json, params=params, headers=h)
    if r.status_code == 401 and tok and auth:
        st.session_state.clear()
        st.warning("Your session expired. Please log in again.")
        st.stop()
    if r.status_code >= 400:
        try:
            d = r.json().get("detail", r.text)
        except Exception:
            d = r.text
        if isinstance(d, list):  # pydantic validation errors
            d = "; ".join(f"{'.'.join(map(str, e.get('loc', [])[1:]))}: {e.get('msg')}" for e in d)
        raise ApiError(r.status_code, d)
    return r.json() if r.content else None


def act(*a, **k):
    """Call the API; on failure show the message and return (False, None)."""
    try:
        return True, api(*a, **k)
    except ApiError as e:
        st.error(e.detail)
        return False, None


@st.cache_data(ttl=3600)
def _currency():
    return api("GET", "/health", auth=False)["currency"].upper()


def money(cents):
    return f"{_currency()} {cents / 100:,.2f}"


def is_admin():
    return (st.session_state.get("user") or {}).get("role") == "admin"


def need_login() -> bool:
    if not st.session_state.get("token"):
        st.info("Please log in or sign up from the sidebar first.")
        return True
    return False


# ---------------------------------------------------------------- sidebar auth
def sidebar_auth():
    sb = st.sidebar
    sb.title("🛍️ Store")
    if st.session_state.get("token"):
        u = st.session_state["user"]
        sb.caption(f"Signed in as **{u['email']}**" + (" · admin" if u["role"] == "admin" else ""))
        if sb.button("Log out"):
            st.session_state.clear()
            st.rerun()
        return

    def _signed_in(d):
        st.session_state["token"], st.session_state["user"] = d["access_token"], d["user"]
        st.rerun()

    t_in, t_up = sb.tabs(["Log in", "Sign up"])
    with t_in.form("login"):
        email, pw = st.text_input("Email"), st.text_input("Password", type="password")
        if st.form_submit_button("Log in"):
            ok, d = act("POST", "/auth/login", json={"email": email, "password": pw}, auth=False)
            if ok:
                _signed_in(d)
    with t_up.form("signup"):
        name, email, pw = st.text_input("Full name"), st.text_input("Email "), st.text_input("Password (8+ chars)", type="password")
        if st.form_submit_button("Create account"):
            ok, d = act("POST", "/auth/register", json={"email": email, "password": pw, "full_name": name}, auth=False)
            if ok:
                _signed_in(d)


# ---------------------------------------------------------------- shop
def page_shop():
    st.header("Shop")
    cats = api("GET", "/categories", auth=False)
    c1, c2, c3, c4 = st.columns([3, 2, 2, 1])
    q = c1.text_input("Search", placeholder="hoodie, mug...")
    slug = c2.selectbox("Category", [None] + [c["slug"] for c in cats],
                        format_func=lambda s: "All" if s is None else next(c["name"] for c in cats if c["slug"] == s))
    sort = c3.selectbox("Sort by", ["new", "price_asc", "price_desc", "rating"],
                        format_func={"new": "Newest", "price_asc": "Price: low to high", "price_desc": "Price: high to low", "rating": "Top rated"}.get)
    page = c4.number_input("Page", min_value=1, value=1)
    res = api("GET", "/products", params={"q": q or None, "category": slug, "sort": sort, "page": page, "page_size": 12}, auth=False)
    if not res["items"]:
        st.info("No products found." + (" Admins: open Admin -> Products to load sample data." if not q and not slug else ""))
        return
    st.caption(f"{res['total']} products")
    cols = st.columns(3)
    for i, p in enumerate(res["items"]):
        with cols[i % 3].container(border=True):
            if p["images"]:
                st.image(p["images"][0]["url"])
            st.subheader(p["title"])
            st.caption((p["brand"] or "") + (f"  ·  ⭐ {p['rating_avg']:.1f} ({p['rating_count']})" if p["rating_count"] else ""))
            st.markdown(f"**from {money(p['price_from_cents'])}**")
            v = st.selectbox("Option", p["variants"], key=f"v{p['id']}", label_visibility="collapsed",
                             format_func=lambda v: f"{v['title']} · {money(v['price_cents'])}" + ("" if v["stock"] > 0 else " · sold out"))
            qty = st.number_input("Qty", 1, max(v["stock"], 1), 1, key=f"q{p['id']}")
            if st.button("Add to cart", key=f"add{p['id']}", disabled=v["stock"] <= 0):
                if need_login():
                    continue
                ok, _ = act("POST", "/cart/items", json={"variant_id": v["id"], "qty": int(qty)})
                if ok:
                    st.toast(f"Added {p['title']} to cart")
            with st.expander("Details"):
                st.write(p["description"] or "No description.")


# ---------------------------------------------------------------- cart + checkout
def pay_panel(order, key):
    p = order.get("payment") or {}
    if p.get("provider") == "mock":
        if st.button("💳 Pay now (demo, no real charge)", key=f"pay{key}", type="primary"):
            ok, _ = act("POST", p["confirm_url"])
            if ok:
                st.session_state.pop("last_order", None)
                st.toast("Payment received. Thank you!", icon="✅")
                st.rerun()
    else:
        st.info(f"This order uses **{p.get('provider')}**. Card entry needs that provider's hosted checkout page, which this Streamlit "
                "UI doesn't render. Use the `payment` details from the API in a custom checkout page.")


def page_cart():
    st.header("Your cart")
    if need_login():
        return
    last = st.session_state.get("last_order")
    if last:
        st.success(f"Order **{last['number']}** created. Total **{money(last['total_cents'])}**. Stock is held for 30 minutes.")
        pay_panel(last, "cart")
        if st.button("Cancel this order"):
            ok, _ = act("POST", f"/orders/{last['number']}/cancel")
            if ok:
                st.session_state.pop("last_order")
                st.rerun()
        return

    coupon = st.session_state.get("coupon", "")
    try:
        cart = api("GET", "/cart", params={"coupon": coupon} if coupon else None)
    except ApiError as e:
        st.error(f"Coupon removed: {e.detail}")
        st.session_state.pop("coupon", None)
        coupon, cart = "", api("GET", "/cart")
    if not cart["items"]:
        st.info("Your cart is empty. Head to the Shop.")
        return

    for it in cart["items"]:
        c = st.columns([1, 4, 2, 2, 2, 1])
        if it["image"]:
            c[0].image(it["image"], width=60)
        c[1].markdown(f"**{it['title']}**  \n{it['variant_title']}" + ("" if it["ok"] else "  \n:red[Not enough stock]"))
        c[2].write(money(it["unit_price_cents"]))
        new = c[3].number_input("Qty", 0, max(it["available"], it["qty"]), it["qty"], key=f"cq{it['variant_id']}", label_visibility="collapsed")
        c[4].write(money(it["line_total_cents"]))
        if c[5].button("✕", key=f"rm{it['variant_id']}"):
            new = 0
        if new != it["qty"]:
            ok, _ = act("PATCH", f"/cart/items/{it['variant_id']}", json={"qty": int(new)})
            if ok:
                st.rerun()

    pr = cart["pricing"]
    left, right = st.columns(2)
    with right:
        st.write(f"Subtotal: **{money(pr['subtotal_cents'])}**")
        if pr["discount_cents"]:
            st.write(f"Discount: **-{money(pr['discount_cents'])}**")
        st.write(f"Shipping: **{money(pr['shipping_cents']) if pr['shipping_cents'] else 'Free'}**")
        if pr["tax_cents"]:
            st.write(f"Tax: **{money(pr['tax_cents'])}**")
        st.subheader(f"Total: {money(pr['total_cents'])}")
    with left:
        with st.form("coupon_form", border=False):
            code = st.text_input("Coupon code", value=coupon)
            if st.form_submit_button("Apply"):
                st.session_state["coupon"] = code.strip()
                st.rerun()
        addrs = api("GET", "/addresses")
        with st.expander("Add a new address", expanded=not addrs):
            with st.form("addr"):
                a1, a2 = st.columns(2)
                f = {"full_name": a1.text_input("Full name"), "phone": a2.text_input("Phone"), "line1": st.text_input("Address line 1"),
                     "line2": st.text_input("Address line 2 (optional)") or None, "city": a1.text_input("City"),
                     "state": a2.text_input("State"), "postal_code": a1.text_input("PIN / postal code"), "country": "IN"}
                if st.form_submit_button("Save address"):
                    ok, _ = act("POST", "/addresses", json=f)
                    if ok:
                        st.rerun()
        if addrs:
            addr = st.selectbox("Deliver to", addrs, format_func=lambda a: f"{a['full_name']}, {a['line1']}, {a['city']} {a['postal_code']}")
            if st.button("Place order", type="primary", disabled=not all(i["ok"] for i in cart["items"])):
                idem = st.session_state.setdefault("idem", uuid.uuid4().hex)  # same click twice => same order
                ok, order = act("POST", "/checkout", json={"address_id": addr["id"], "coupon_code": coupon or None},
                                headers={"Idempotency-Key": idem})
                if ok:
                    st.session_state.pop("idem", None)
                    st.session_state.pop("coupon", None)
                    st.session_state["last_order"] = order
                    st.rerun()


# ---------------------------------------------------------------- my orders
def page_orders():
    st.header("My orders")
    if need_login():
        return
    orders = api("GET", "/orders")
    if not orders:
        st.info("No orders yet.")
        return
    icon = {"pending_payment": "🟡", "paid": "🟢", "shipped": "📦", "delivered": "✅", "cancelled": "⚪", "refunded": "↩️"}
    for o in orders:
        with st.expander(f"{icon.get(o['status'], '')} {o['number']} · {o['status'].replace('_', ' ')} · {money(o['total_cents'])} · {o['created_at'][:10]}"):
            st.dataframe([{"Item": i["title"], "Qty": i["qty"], "Price": money(i["unit_price_cents"]), "Total": money(i["line_total_cents"])}
                          for i in o["items"]], hide_index=True)
            a = o["shipping_address"]
            st.caption(f"Ship to: {a['full_name']}, {a['line1']}, {a['city']} {a['postal_code']}")
            if o["tracking_number"]:
                st.write(f"Tracking: **{o['carrier']} {o['tracking_number']}**")
            if o["status"] == "pending_payment":
                pay_panel(o, o["number"])
            if o["status"] in ("pending_payment", "paid") and st.button("Cancel order", key=f"c{o['number']}"):
                ok, _ = act("POST", f"/orders/{o['number']}/cancel")
                if ok:
                    st.rerun()


# ---------------------------------------------------------------- seller
def page_sell():
    st.header("Sell on the store")
    if need_login():
        return
    try:
        me = api("GET", "/sellers/me")
    except ApiError as e:
        if e.status != 404:
            st.error(e.detail)
            return
        me = None
    if not me:
        with st.form("apply"):
            name = st.text_input("Store / brand name")
            if st.form_submit_button("Apply to sell") and act("POST", "/sellers", json={"name": name})[0]:
                st.rerun()
        return
    st.write(f"**{me['name']}**  ·  status: `{me['status']}`  ·  storefront slug: `{me['slug']}`")
    if me["status"] != "approved":
        st.info("An admin must approve your seller account before you can connect Shopify.")
        return
    sh = me.get("shopify")
    if sh:
        st.success(f"Connected: {sh['shop_domain']}  (last import: {sh['last_import_at'] or 'never'})")
    with st.form("shopify"):
        dom = st.text_input("Shopify domain", value=sh["shop_domain"] if sh else "", placeholder="your-store.myshopify.com")
        tok = st.text_input("Admin API access token", type="password")
        loc = st.text_input("Location ID (optional; enables stock push back to Shopify)")
        if st.form_submit_button("Save connection"):
            if act("PUT", "/sellers/me/shopify", json={"shop_domain": dom, "access_token": tok, "location_id": loc or None})[0]:
                st.success("Saved.")
    if sh and st.button("Import products from Shopify"):
        ok, res = act("POST", "/sellers/me/shopify/import")
        if ok:
            st.success(f"Synced {res['variants_synced']} variants from {res['products']} products.")


# ---------------------------------------------------------------- AI SDR
def page_sdr():
    st.header("AI SDR")
    if not is_admin():
        st.error("Admins only.")
        return
    stt = api("GET", "/sdr/status")
    st.caption(f"Discovery: **{stt['discovery']}** | Email: **{stt['outreach']}** | Calendar: {stt['meeting']} | CRM: {stt['crm']}. {stt['note']}")
    tabs = st.tabs(["1. ICP", "2. Pipeline", "3. Prospects", "4. Outreach", "5. Inbox", "6. Meetings & CRM", "7. Analytics"])
    icps = api("GET", "/icp")
    pick = lambda key: st.selectbox("ICP", icps, format_func=lambda i: f"#{i['id']} {i['name']} (v{i['version']})", key=key) if icps else None

    with tabs[0]:
        with st.form("icp"):
            name = st.text_input("Name", "My ICP")
            prompt = st.text_area("Describe your ideal customer", "We sell a Revenue Operations platform to SaaS companies in North America with 200-5000 employees. Target RevOps and CRO.")
            pains = st.text_input("Pain points (comma separated)", "Manual prospect research")
            a, b = st.columns(2)
            check, save = a.form_submit_button("Validate"), b.form_submit_button("Save ICP")
            body = {"name": name, "prompt": prompt, "pain_points": [p.strip() for p in pains.split(",") if p.strip()]}
            if check:
                st.json(api("POST", "/icp/validate", json=body))
            if save and act("POST", "/icp", json=body)[0]:
                st.rerun()
        for i in icps:
            st.write(f"#{i['id']} **{i['name']}** v{i['version']}: {', '.join(i['data']['industries'])} | {', '.join(i['data']['geographies'])} | personas: {', '.join(i['data']['personas'])}")

    with tabs[1]:
        icp = pick("pi")
        n = st.slider("Accounts", 1, 25, 5)
        if icp and st.button("Run pipeline (discover, enrich, score, qualify, draft)"):
            ok, r = act("POST", "/sdr/pipeline/run", json={"icp_id": icp["id"], "limit": n})
            if ok:
                st.success(f"Provider: {r['provider']}. {r['summary']}")
                st.dataframe(r["qualification_results"], width="stretch")
                for w in r["warnings"]:
                    st.warning(w)

    with tabs[2]:
        rows = api("GET", "/prospect-intelligence")
        st.dataframe([{k: x[k] for k in ("id", "name", "title", "account", "fit_score", "priority", "decision", "intent", "reply", "meeting", "qualification")} for x in rows], width="stretch")
        if rows:
            cid = st.selectbox("Details", [x["id"] for x in rows], format_func=lambda i: next(f"{x['name']} @ {x['account']}" for x in rows if x["id"] == i))
            st.json({"intelligence": api("GET", f"/prospect-intelligence/{cid}"), "qualification": api("GET", f"/qualification/{cid}")})

    with tabs[3]:
        camps = api("GET", "/outreach/campaigns")
        st.dataframe([{k: x[k] for k in ("id", "contact", "account", "status", "provider")} for x in camps], width="stretch")
        if camps:
            c = st.selectbox("Campaign", camps, format_func=lambda x: f"#{x['id']} {x['contact']} ({x['status']})")
            for m in c["messages"]:
                st.markdown(f"**{m['channel']}** ({m['type']})" + (f" | {m['subject']}" if m.get("subject") else ""))
                st.text(m["body"])
            a, b, d, e = st.columns(4)
            if a.button("Approve") and act("POST", f"/outreach/campaigns/{c['id']}/approve")[0]:
                st.rerun()
            if b.button("Send (dry run unless Brevo)") and act("POST", f"/outreach/campaigns/{c['id']}/send")[0]:
                st.rerun()
            if d.button("Plan follow-ups (0, 3, 7 days)") and act("POST", "/follow-up/plans", json={"campaign_id": c["id"], "delays_days": [0, 3, 7]})[0]:
                act("POST", f"/follow-up/plans/{c['id']}/approve")
                st.rerun()
            if e.button("Run due follow-ups"):
                st.write(api("POST", "/follow-up/scheduler/run-due"))
            st.json(api("GET", f"/follow-up/plans/{c['id']}"))

    with tabs[4]:
        sent = [x for x in api("GET", "/outreach/campaigns") if x["sent_at"]]
        if not sent:
            st.info("Send a campaign first, then simulate the prospect's reply here (or POST /conversations/inbound/brevo from Brevo).")
        else:
            c = st.selectbox("Campaign", sent, format_func=lambda x: f"#{x['id']} {x['contact']}", key="inb")
            txt = st.text_area("Simulated reply", "Sounds interesting, can we schedule a call next week?")
            if st.button("Receive reply") and act("POST", "/conversations/inbound", json={"campaign_id": c["id"], "body": txt})[0]:
                st.rerun()
        for t in api("GET", "/conversations"):
            with st.expander(f"Thread #{t['id']} {t['contact']}: {t['intent']} ({t['reply_status']})"):
                st.text(t["reply_draft"] or "(no reply drafted)")
                a, b, d = st.columns(3)
                if t["reply_status"] == "draft" and a.button("Approve reply", key=f"ar{t['id']}") and act("POST", f"/conversations/{t['id']}/approve-reply")[0]:
                    st.rerun()
                if t["reply_status"] == "approved" and b.button("Send reply", key=f"sr{t['id']}") and act("POST", f"/conversations/{t['id']}/send-reply")[0]:
                    st.rerun()
                if t["intent"] in ("meeting_request", "interested"):
                    day = d.date_input("Meeting date", key=f"d{t['id']}")
                    if d.button("Request meeting (10:00 UTC)", key=f"m{t['id']}") and act("POST", "/meetings", json={"thread_id": t["id"], "start_at": f"{day}T10:00:00Z"})[0]:
                        st.rerun()

    with tabs[5]:
        for m in api("GET", "/meetings"):
            a, b, c2 = st.columns([4, 1, 1])
            a.write(f"#{m['id']} **{m['contact']}** | {m['start_at']} | **{m['status']}** | CRM: {m['crm']} {m['meet_url'] or ''}")
            if m["status"] == "requested" and b.button("Approve", key=f"ma{m['id']}") and act("POST", f"/meetings/{m['id']}/approve")[0]:
                st.rerun()
            if m["status"] == "approved" and c2.button("Book", key=f"mb{m['id']}") and act("POST", f"/meetings/{m['id']}/book")[0]:
                st.rerun()
        st.subheader("CRM syncs")
        st.dataframe(api("GET", "/crm/syncs"), width="stretch")

    with tabs[6]:
        a = api("GET", "/sdr/analytics")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Prospects", a["contacts"]); c2.metric("Sent", a["sent"]); c3.metric("Reply rate", f"{a['reply_rate']:.0%}"); c4.metric("Meetings booked", a["meetings_booked"])
        st.write("Qualification", a["qualification"]); st.write("Campaigns", a["campaigns"]); st.write("Reply intents", a["reply_intents"])


# ---------------------------------------------------------------- admin
def page_admin():
    st.header("Admin")
    if not is_admin():
        st.error("Admins only.")
        return
    t1, t2, t3, t4, t5 = st.tabs(["Dashboard", "Orders", "Products", "Coupons", "Sellers"])

    with t1:
        s = api("GET", "/admin/stats")
        a, b = st.columns(2)
        a.metric("Revenue", money(s["revenue_cents"]))
        b.metric("Orders", sum(s["orders_by_status"].values()))
        if s["orders_by_status"]:
            st.bar_chart(s["orders_by_status"])
        st.subheader("Low stock (5 or fewer)")
        st.dataframe(s["low_stock"], hide_index=True) if s["low_stock"] else st.caption("Nothing low. 🎉")

    with t2:
        status = st.selectbox("Filter", [None, "pending_payment", "paid", "shipped", "delivered", "cancelled", "refunded"],
                              format_func=lambda x: "All" if x is None else x)
        rows = api("GET", "/admin/orders", params={"status": status})
        if not rows:
            st.caption("No orders.")
        else:
            st.dataframe([{"Order": o["number"], "Customer": o["customer_email"], "Status": o["status"], "Total": money(o["total_cents"]),
                           "Placed": o["created_at"][:16]} for o in rows], hide_index=True)
            o = st.selectbox("Manage order", rows, format_func=lambda o: f"{o['number']} · {o['status']}")
            if o["status"] == "paid":
                with st.form("ship"):
                    c1, c2 = st.columns(2)
                    trk, car = c1.text_input("Tracking number"), c2.text_input("Carrier")
                    if st.form_submit_button("Mark shipped"):
                        ok, _ = act("POST", f"/admin/orders/{o['number']}/ship", json={"tracking_number": trk, "carrier": car})
                        if ok:
                            st.rerun()
            if o["status"] == "shipped" and st.button("Mark delivered"):
                ok, _ = act("POST", f"/admin/orders/{o['number']}/deliver")
                if ok:
                    st.rerun()
            if o["status"] in ("paid", "shipped", "delivered"):
                restock = st.checkbox("Return items to stock", key="restock")
                if st.button("Refund in full", type="secondary"):
                    ok, _ = act("POST", f"/admin/orders/{o['number']}/refund", params={"restock": restock})
                    if ok:
                        st.rerun()

    with t3:
        res = api("GET", "/products", params={"page_size": 50}, auth=False)
        if not res["items"] and not API_BASE and st.button("Load sample products"):
            from app.seed import seed
            st.success(seed())
            st.rerun()
        with st.expander("➕ New product", expanded=not res["items"]):
            cats = api("GET", "/categories", auth=False)
            with st.form("newprod"):
                c1, c2 = st.columns(2)
                title, brand = c1.text_input("Title"), c2.text_input("Brand")
                cat = c1.selectbox("Category", [None] + [c["slug"] for c in cats], format_func=lambda s: "None" if s is None else s)
                img = c2.text_input("Image URL")
                desc = st.text_area("Description")
                st.caption("Variants (price in major units, e.g. 799.00)")
                import pandas as pd
                vdf = st.data_editor(pd.DataFrame([{"sku": "", "title": "Default", "price": 0.0, "stock": 0}]), num_rows="dynamic", hide_index=True)
                if st.form_submit_button("Create product"):
                    variants = [{"sku": str(r.sku).strip(), "title": r.title or "Default", "price_cents": round(float(r.price) * 100), "stock": int(r.stock)}
                                for r in vdf.itertuples() if str(r.sku).strip()]
                    ok, _ = act("POST", "/admin/products", json={"title": title, "brand": brand or None, "category_slug": cat, "description": desc,
                                                                 "variants": variants, "image_urls": [img] if img else []})
                    if ok:
                        st.rerun()
            with st.form("newcat", border=False):
                nc = st.text_input("Add a category")
                if st.form_submit_button("Add category") and nc:
                    ok, _ = act("POST", "/admin/categories", json={"name": nc})
                    if ok:
                        st.rerun()
        if res["items"]:
            p = st.selectbox("Edit price & stock", res["items"], format_func=lambda p: p["title"])
            import pandas as pd
            orig = pd.DataFrame([{"id": v["id"], "sku": v["sku"], "title": v["title"], "price": v["price_cents"] / 100, "stock": v["stock"]} for v in p["variants"]])
            edited = st.data_editor(orig, hide_index=True, disabled=["id", "sku"], key=f"ed{p['id']}")
            c1, c2 = st.columns([1, 1])
            if c1.button("Save changes"):
                for (_, a), (_, b) in zip(orig.iterrows(), edited.iterrows()):
                    if (a.title, a.price, a.stock) != (b.title, b.price, b.stock):
                        act("PATCH", f"/admin/variants/{int(a.id)}", json={"title": b.title, "price_cents": round(float(b.price) * 100), "stock": int(b.stock)})
                st.rerun()
            if c2.button("Archive product (hide from shop)"):
                ok, _ = act("DELETE", f"/admin/products/{p['id']}")
                if ok:
                    st.rerun()

    with t4:
        cps = api("GET", "/admin/coupons")
        if cps:
            st.dataframe([{"Code": c["code"], "% off": c["percent_off"], "Amount off": c["amount_off_cents"] and money(c["amount_off_cents"]),
                           "Min order": money(c["min_subtotal_cents"]), "Used": f"{c['used_count']}/{c['max_uses'] or '∞'}"} for c in cps],
                         hide_index=True)
        with st.form("newcoupon"):
            c1, c2, c3 = st.columns(3)
            code, kind = c1.text_input("Code"), c2.radio("Type", ["Percent", "Fixed amount"], horizontal=True)
            val = c3.number_input("Value (% or amount)", min_value=1.0, value=10.0)
            mn, mx = c1.number_input("Minimum order (0 = none)", min_value=0.0), c2.number_input("Max uses (0 = unlimited)", min_value=0, step=1)
            if st.form_submit_button("Create coupon"):
                body = {"code": code, "min_subtotal_cents": round(mn * 100), "max_uses": int(mx) or None}
                body.update({"percent_off": int(val)} if kind == "Percent" else {"amount_off_cents": round(val * 100)})
                ok, _ = act("POST", "/admin/coupons", json=body)
                if ok:
                    st.rerun()

    with t5:
        for sl in api("GET", "/admin/sellers"):
            a, b, c = st.columns([3, 1, 1])
            a.write(f"**{sl['name']}** · {sl['owner_email']}")
            b.write(sl["status"])
            new = "suspended" if sl["status"] == "approved" else "approved"
            if c.button("Suspend" if new == "suspended" else "Approve", key=f"sl{sl['id']}") and \
                    act("POST", f"/admin/sellers/{sl['id']}/status", params={"status": new})[0]:
                st.rerun()


# ---------------------------------------------------------------- main
def main():
    if not API_BASE:
        _embedded_app()
        _expire_unpaid()
    sidebar_auth()
    pages = {"Shop": page_shop, "Cart": page_cart, "My orders": page_orders, "Sell on the store": page_sell}
    if is_admin():
        pages["Admin"] = page_admin
        pages["AI SDR"] = page_sdr
    choice = st.sidebar.radio("Navigate", list(pages), key="nav")
    pages[choice]()


main()
