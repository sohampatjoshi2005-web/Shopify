"""Streamlit 'Recommended for you' strip. Same pattern as ext_ui.py: streamlit_app.py passes the context `c`. UNTESTED (no Streamlit in my sandbox)."""


def recommended(c, title="Recommended for you", limit=4, product_id=None):
    st = c.st
    logged_in = bool(st.session_state.get("token"))
    path = f"/recommend/product/{product_id}" if product_id else ("/recommend/me" if logged_in else "/recommend/popular")
    try:
        items = c.api("GET", path, params={"limit": limit}, auth=logged_in and not product_id)["items"]
    except Exception:
        return                                   # recommendations must never break the shop page
    if not items:
        return
    st.subheader(title)
    cols = st.columns(min(len(items), 4))
    for col, p in zip(cols, items):
        with col.container(border=True):
            if p["image"]:
                st.image(p["image"])
            st.markdown(f"**{p['title']}**")
            st.caption(f"from {c.money(p['price_from_cents'])}")
            st.caption(p["reason"])
    st.divider()
