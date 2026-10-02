"""streamlit.io entry point. Main file path on Streamlit Community Cloud: streamlit_app.py
Copies Streamlit secrets into environment variables (read by the backend settings), then runs the dashboard.
If API_BASE_URL is set the dashboard talks to a remote backend; otherwise the backend runs inside this app."""
import os, runpy
import streamlit as st

try:
    for k in st.secrets:
        v = st.secrets[k]
        if isinstance(v, (str, int, float, bool)):
            os.environ.setdefault(str(k), str(v))
except Exception:
    pass  # no secrets file locally; rely on real environment variables / .env

runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "app.py"), run_name="__main__")
