"""Streamlit screens for recommendations and the lead list. Same pattern as ext_ui.py / support_ui.py: streamlit_app.py passes a context `c`."""
import pandas as pd

STATUSES = ["new", "contacted", "converted", "lost"]
SEGMENTS = ["prospect", "new_customer", "repeat", "lapsed"]


def _shelf(c, items, key):
    st = c.st
    cols = st.columns(3)
    for i, p in enumerate(items):
        with cols[i % 3].container(border=True):
            if p["images"]:
                st.image(p["images"][0]["url"])
            st.markdown(f"**{p['title']}**")
            st.caption(f"from {c.money(p['price_from_cents'])}")
            for r in p["reasons"][:2]:
                st.caption(f"• {r}")
            v = next((v for v in p["variants"] if v["stock"] > 0), None)
            if v and st.button("Add to cart", key=f"{key}{p['id']}"):
                if not st.session_state.get("token"):
                    st.info("Log in to add items to your cart.")
                elif c.act("POST", "/cart/items", json={"variant_id": v["id"], "qty": 1})[0]:
                    st.toast(f"Added {p['title']} to cart")


def for_you(c):
    """Shelf at the top of the shop: personalised when logged in, popular items otherwise. Never breaks the page."""
    st = c.st
    logged_in = bool(st.session_state.get("token"))
    try:
        res = c.api("GET", "/recommend", params={"limit": 6}) if logged_in else c.api("GET", "/recommend/popular", params={"limit": 6}, auth=False)
    except Exception:
        return
    if not res.get("items"):
        return
    title = "Recommended for you" if res["strategy"] == "personalised" else "Popular right now"
    with st.expander(title, expanded=True):
        _shelf(c, res["items"], "rec")


def admin_tab(c, tab):
    st = c.st
    with tab:
        s = c.api("GET", "/admin/recommend/summary")
        m = st.columns(5)
        m[0].metric("Leads", s["total"])
        m[1].metric("Avg score", s["avg_score"])
        for col, seg in zip(m[2:], ("prospect", "repeat", "lapsed")):
            col.metric(seg.replace("_", " ").title(), s["by_segment"].get(seg, 0))
        if st.button("Sync leads from store data"):
            ok, r = c.act("POST", "/admin/recommend/leads/sync")
            if ok:
                st.success(f"{r['created']} new, {r['updated']} refreshed, {r['converted']} converted")
                st.rerun()
        st.caption("Score = purchase intent / engagement from real store activity (wishlist, cart, orders, verified email). Prospects are registered customers who have never bought.")

        a, b, d = st.columns(3)
        status = a.selectbox("Status", ["(any)"] + STATUSES, key="rl_status")
        seg = b.selectbox("Segment", ["(any)"] + SEGMENTS, key="rl_seg")
        q = d.text_input("Search email or name", key="rl_q")
        params = {k: v for k, v in {"status": None if status == "(any)" else status, "segment": None if seg == "(any)" else seg, "q": q or None}.items() if v}
        rows = c.api("GET", "/admin/recommend/leads", params=params)
        if rows:
            st.dataframe(pd.DataFrame(rows)[["id", "email", "name", "segment", "status", "score", "orders_count", "wishlist_count", "cart_items", "source"]], hide_index=True, width="stretch")
            lead = st.selectbox("Open lead", rows, format_func=lambda r: f"{r['email']} · {r['segment']} · score {r['score']}", key="rl_pick")
            with st.form("rl_update"):
                x, y = st.columns([1, 3])
                ns = x.selectbox("Status", STATUSES, index=STATUSES.index(lead["status"]))
                notes = y.text_input("Notes", value=lead["notes"])
                if st.form_submit_button("Save") and c.act("PATCH", f"/admin/recommend/leads/{lead['id']}", json={"status": ns, "notes": notes})[0]:
                    st.rerun()
            res = c.api("GET", f"/admin/recommend/leads/{lead['id']}/products", params={"limit": 6})
            st.markdown(f"**What to show {lead['email']}** ({res['strategy']})")
            for p in res["items"]:
                st.write(f"{p['title']} · {c.money(p['price_from_cents'])} · {'; '.join(p['reasons'])}")
            if not res["items"]:
                st.caption("Nothing to recommend yet (no sellable products).")
        else:
            st.caption("No leads yet. Click “Sync leads from store data”.")

        with st.expander("Add a lead by hand / preview a customer's shelf"):
            with st.form("rl_add"):
                email, name = st.text_input("Email"), st.text_input("Name")
                if st.form_submit_button("Add lead") and email and c.act("POST", "/admin/recommend/leads", json={"email": email, "name": name})[0]:
                    st.rerun()
            pv = st.text_input("Preview recommendations for customer email", key="rl_preview")
            if pv:
                ok, r = c.act("GET", "/admin/recommend/preview", params={"email": pv})
                for p in (r or {}).get("items", []):
                    st.write(f"{p['title']} · {'; '.join(p['reasons'])}")
