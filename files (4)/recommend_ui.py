"""Streamlit pieces for recommendations + leads. Same pattern as support_ui.py: streamlit_app.py passes a context `c` (st, act, ...).
  for_you_strip(c)      -> a "Recommended for you" row for the shop home / product page
  admin_tab(c, tab)     -> an admin "Leads" tab"""
import pandas as pd

SEGMENTS = ["new", "cart_waiting", "first_time", "repeat", "high_value", "lapsed", "manual"]
STATUSES = ["new", "contacted", "qualified", "converted", "lost"]


def _cards(c, items):
    st = c.st
    cols = st.columns(min(len(items), 4) or 1)
    for i, p in enumerate(items):
        with cols[i % len(cols)]:
            if p.get("images"):
                st.image(p["images"][0]["url"], width="stretch")
            st.write(f"**{p['title']}**")
            st.caption(f"{c.money(p.get('price_from_cents') or 0)} · {p['reason']}")


def _has(c, path: str) -> bool:
    return getattr(c, "has_route", lambda p: True)(path)


def for_you_strip(c, limit: int = 8, slug: str | None = None):
    """Logged in -> personalised; logged out -> popular; slug given -> 'customers also bought' for that product.
    Never shows an error: recommendations are a nicety and must not get in the way of shopping."""
    st = c.st
    if not _has(c, "/recommend/popular"):
        return
    if slug:
        path, title = f"/recommend/similar/{slug}", "Customers also bought"
    elif st.session_state.get("token"):
        path, title = "/recommend/for-you", "Recommended for you"
    else:
        path, title = "/recommend/popular", "Popular right now"
    try:
        r = c.api("GET", path, params={"limit": limit})
    except Exception:
        return
    if r and r.get("items"):
        st.subheader(title)
        _cards(c, r["items"])
        st.divider()


def admin_tab(c, tab):
    st = c.st
    with tab:
        if not _has(c, "/admin/recommend/leads"):
            st.info("This backend does not serve the recommendation / leads routes yet.")
            return
        top = st.columns([1, 4])
        if top[0].button("Sync leads from store"):
            ok, s = c.act("POST", "/admin/recommend/leads/sync")
            if ok:
                st.success(f"Created {s['created']}, refreshed {s['refreshed']}, converted {s['converted']}")
        ok, summ = c.act("GET", "/admin/recommend/leads/summary")
        if ok:
            top[1].caption(f"{summ['total']} leads · " + " · ".join(f"{k}: {v}" for k, v in summ["by_segment"].items()))
        a, b, d, e = st.columns(4)
        seg = a.selectbox("Segment", ["(any)"] + SEGMENTS, key="ld_seg")
        stat = b.selectbox("Status", ["(any)"] + STATUSES, key="ld_stat")
        q = d.text_input("Search email / name", key="ld_q")
        min_score = e.slider("Min score", 0, 100, 0, key="ld_score")
        params = {k: v for k, v in {"segment": seg if seg != "(any)" else None, "status": stat if stat != "(any)" else None,
                                    "q": q or None, "min_score": min_score or None}.items() if v}
        ok, res = c.act("GET", "/admin/recommend/leads", params=params)
        rows = res["items"] if ok else []
        if not rows:
            st.caption("No leads. Click 'Sync leads from store' first.")
            return
        st.dataframe(pd.DataFrame(rows)[["id", "email", "name", "segment", "score", "status", "order_count", "lifetime_cents",
                                         "has_cart", "interests", "owner", "opted_out"]], hide_index=True, width="stretch")
        lead = st.selectbox("Open lead", rows, format_func=lambda r: f"{r['email']} · {r['segment']} · score {r['score']}", key="ld_pick")
        st.write(f"**{lead['name'] or lead['email']}** · spent {c.money(lead['lifetime_cents'])} over {lead['order_count']} orders"
                 + ("  ·  :orange[opted out: do not contact]" if lead["opted_out"] else ""))
        with st.form("ld_update"):
            x, y, z = st.columns(3)
            ns = x.selectbox("Status", STATUSES, index=STATUSES.index(lead["status"]))
            who = y.text_input("Owner", value=lead["owner"])
            out = z.checkbox("Opted out of marketing", value=lead["opted_out"])
            notes = st.text_area("Notes", value=lead["notes"])
            if st.form_submit_button("Save") and c.act("PATCH", f"/admin/recommend/leads/{lead['id']}",
                                                       json={"status": ns, "owner": who, "notes": notes, "opted_out": out})[0]:
                st.rerun()
        with st.expander("What to pitch this lead"):
            ok, r = c.act("GET", f"/admin/recommend/leads/{lead['id']}/recommendations", params={"limit": 4})
            if ok and r["items"]:
                _cards(c, r["items"])
        with st.expander("Add a lead manually"):
            with st.form("ld_add"):
                em, nm = st.text_input("Email"), st.text_input("Name")
                if st.form_submit_button("Add") and c.act("POST", "/admin/recommend/leads", json={"email": em, "name": nm})[0]:
                    st.rerun()
