# Ecommerce AI Platform (scaffold)

FastAPI backend (support, marketing, SDR, recommender, Shopify seam) + Streamlit admin dashboard.

## Run locally (VS Code terminal, from this folder)
```bash
python -m venv venv && source venv/bin/activate      # Windows: .\venv\Scripts\activate
pip install -r backend/requirements.txt -r frontend/requirements.txt pytest httpx
cp .env.example .env                                  # then edit ADMIN_EMAIL / ADMIN_PASSWORD / keys
python -m backend.seed                                # sample orders (dev only)
uvicorn backend.main:app --reload --port 8000         # terminal 1 -> http://localhost:8000/docs
streamlit run frontend/app.py                         # terminal 2 -> http://localhost:8501
pytest -q                                             # smoke tests
```
For the Streamlit "Try the bot" box, set `WIDGET_API_KEY` (same value as backend) in your environment or
`.streamlit/secrets.toml` along with `API_BASE_URL`.

## Go live
1. GitHub: push this folder (`.env` is git-ignored).
2. Database: create Postgres (Neon/Supabase/Render) -> `DATABASE_URL`.
3. Backend: Render "Blueprint" from `render.yaml`; fill the env vars. Check `/health`.
4. Frontend: share.streamlit.io -> pick `frontend/app.py`; Secrets: `API_BASE_URL`, `WIDGET_API_KEY`.
5. Shopify: custom app (read_orders, write_orders, read_customers) -> set the SHOPIFY_* vars; register webhooks
   orders/create, orders/updated, refunds/create at `https://<backend>/webhooks/shopify`.

## Still to plug in (marked TODO in code)
- Live Shopify refunds (`issue_refund`), your real resolver/RAG/CAG, AI SDR pipeline, ProductRecommender, LLM client.
- Alembic migrations (tables are auto-created for now). Verify Shopify API version in `SHOPIFY_API_VERSION`.
