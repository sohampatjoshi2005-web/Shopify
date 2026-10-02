import os
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Ecommerce AI Platform", layout="wide")

def cfg(key, default=""):
    try:
        return st.secrets[key]
    except Exception:
        return os.getenv(key, default)

API = cfg("API_BASE_URL", "http://localhost:8000").rstrip("/")
WIDGET_KEY = cfg("WIDGET_API_KEY", "")

def call(method, path, auth=True, **kw):
    headers = kw.pop("headers", {})
    if auth:
        headers["Authorization"] = f"Bearer {st.session_state.get('token', '')}"
    try:
        r = requests.request(method, f"{API}{path}", headers=headers, timeout=20, **kw)
    except requests.RequestException as e:
        st.error(f"Cannot reach backend at {API}: {e}")
        st.stop()
    if r.status_code == 401 and auth:
        st.session_state.pop("token", None)
        st.rerun()
    return r

if "token" not in st.session_state:
    st.title("Sign in")
    with st.form("login"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in"):
            r = call("POST", "/auth/login", auth=False, json={"email": email, "password": password})
            if r.ok:
                st.session_state["token"] = r.json()["access_token"]
                st.rerun()
            else:
                st.error(r.json().get("detail", "Login failed"))
    st.stop()

page = st.sidebar.radio("Module", ["Dashboard", "Support", "Orders", "Leads & Marketing", "Recommendations"])
if st.sidebar.button("Sign out"):
    st.session_state.clear()
    st.rerun()

if page == "Dashboard":
    st.title("Dashboard")
    d = call("GET", "/dashboard").json()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Orders", d["orders"])
    c2.metric("Leads", d["leads"])
    c3.metric("Escalated tickets", d["tickets"].get("escalated", 0))
    c4.metric("Auto-resolved", d["tickets"].get("closed", 0))

elif page == "Support":
    st.title("Support")
    with st.expander("Try the support bot"):
        e = st.text_input("Customer email")
        m = st.text_input("Message", "Where is my order #1001?")
        if st.button("Send") and e:
            r = call("POST", "/chat/message", auth=False, json={"email": e, "message": m},
                     headers={"X-API-Key": WIDGET_KEY})
            st.json(r.json())
    t = call("GET", "/tickets").json()
    if t:
        st.dataframe(pd.DataFrame(t), use_container_width=True)
        tid = st.number_input("Close ticket id", min_value=0, step=1)
        if st.button("Close ticket") and tid:
            call("POST", f"/tickets/{int(tid)}/close")
            st.rerun()
    else:
        st.info("No tickets yet.")

elif page == "Orders":
    st.title("Orders")
    o = call("GET", "/orders").json()
    st.dataframe(pd.DataFrame(o), use_container_width=True) if o else st.info("No orders yet.")

elif page == "Leads & Marketing":
    st.title("Leads & Marketing")
    with st.expander("Record an event"):
        e = st.text_input("Lead email")
        ev = st.selectbox("Event", ["page_view", "cta_click", "form_submit", "purchase", "bounce"])
        if st.button("Track") and e:
            st.json(call("POST", "/marketing/events", auth=False, json={"email": e, "type": ev},
                         headers={"X-API-Key": WIDGET_KEY}).json())
    l = call("GET", "/marketing/leads").json()
    st.dataframe(pd.DataFrame(l), use_container_width=True) if l else st.info("No leads yet.")

elif page == "Recommendations":
    st.title("Recommendations")
    e = st.text_input("Customer email")
    if st.button("Recommend") and e:
        st.json(call("GET", f"/recommend/{e}").json())
