# AI Growth Command Center

A modular local Streamlit application combining:

1. **AI SDR**
   - ICP input
   - synthetic lead discovery/enrichment
   - BANT qualification
   - personalized 3-step email + LinkedIn drafts
   - human approval before sending

2. **Marketing Automation Engine**
   - Facebook/Instagram, Google Search, email and blog content
   - campaign ROI/CTR/conversion forecasting
   - lifecycle segment messaging

3. **E-commerce Recommendation Engine**
   - collaborative filtering
   - content-based filtering
   - rules-based personalization
   - hybrid recommendation scoring
   - dynamic customer persona/history studio

## Architecture

```text
Streamlit UI (app.py)
   |
   +--> sdr_module.py             lead synthesis, BANT, outreach drafts
   +--> marketing_module.py       copy, claim checks, forecasts
   +--> recommendation_module.py  hybrid recommender
   +--> utils.py                  LLM abstraction (errors surfaced, not swallowed)
   +--> catalogue.py              single source of truth for products/events
   |
   +--> database.py --> SQLite (leads, outreach_drafts, campaigns, products, interactions)
```

The app is intentionally local-first. External providers are optional.

## Setup in VS Code

### 1. Create/activate a virtual environment

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
```

Windows:

```powershell
copy .env.example .env
```

Add an OpenAI API key if you want cloud LLM generation:

```text
OPENAI_API_KEY=your_key_here
OPENAI_MODEL=gpt-4o-mini
```

Or run Ollama locally and configure:

```text
OLLAMA_URL=http://localhost:11434/api/chat
OLLAMA_MODEL=llama3.2
```

The app still works without an LLM by using deterministic templates.

### 4. Run

```bash
streamlit run app.py
```

Run the tests:

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Open the URL shown by Streamlit, normally:

```text
http://localhost:8501
```

## What changed in this version

**Backend**
- Leads, outreach drafts, campaigns and interactions are now actually persisted; leads upsert on email (unique index, auto-migration for older DBs).
- New approval workflow: drafts -> approve/reject, stored with review timestamps. Nothing is ever sent.
- Connections use a commit/rollback/close context manager (no leaks); table access is whitelisted.
- LLM failures are logged and shown to the operator instead of silently falling back; OpenAI client has timeout and retries; LLM replies are parsed into the 4 outreach sections.
- Forecast fixes: Email/Organic CPC was floored at 0.80 (wrong); conversion rate now comes from per-channel benchmarks instead of CTR; added clicks, conversions and CPA; inputs validated.
- Recommender fixes: already-seen products are no longer recommended back to the customer; cold-start popularity fallback; diversity filter backfills to `top_n`; per-signal scores, confidence and clearer explanations; numpy vectorised; invalid product IDs ignored.
- Marketing copy is scanned for risky claims ("guaranteed", "risk-free", "100%" ...).
- Deterministic lead synthesis via SHA-256, unique emails per batch (reserved `.example` domain).
- Product catalogue deduplicated into `catalogue.py`.
- Added 8 pytest tests.

**Frontend**
- Save-to-database buttons, approval queue, editable drafts, CSV downloads, text filter on the Data tab.
- Stale outreach is no longer shown for a different prospect.
- Persona studio (tier, lifecycle, churn, history multiselect) with live results and interaction logging.
- Channel ROI comparison, signal-breakdown chart, richer Overview, guarded database reset, input validation, LLM status in the sidebar.

## Production-hardening notes

This is a production-oriented local foundation, not a deployed multi-tenant SaaS. Before connecting real customer/lead data:

- add authentication and authorization;
- encrypt/secure secrets;
- add audit logs;
- validate and sanitize all external inputs;
- use timezone-aware timestamps;
- replace simulated SDR enrichment with an approved provider/API;
- add real email/CRM providers behind an approval workflow;
- add model/version tracking and evaluation datasets;
- add automated tests and CI;
- move SQLite to PostgreSQL for concurrent production workloads;
- add observability, rate limiting and retry policies;
- implement privacy/consent and data-retention controls;
- keep recommendation and marketing decisions explainable.

The SDR flow deliberately creates drafts rather than silently sending messages.

## Source alignment

The recommendation catalogue, event-weight concept, rule-based lifecycle/tier logic, hybrid scoring, confidence/explanation approach, and diversity filter were adapted into this local app from the supplied `MarketingReco.ipynb`.

The supplied SDR documentation describes a deterministic ICP/validation control path, enrichment, BANT qualification and an approval-first outreach flow. This app keeps that architecture in a smaller local Streamlit form.

The supplied Shopify design describes Shopify as the storefront/order system and the backend as the support brain, with support and recommendation touchpoints such as "frequently bought together". This local app models the recommendation and marketing layers without pretending to be a live Shopify integration.
