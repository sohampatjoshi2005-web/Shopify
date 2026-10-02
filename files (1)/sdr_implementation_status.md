# AI SDR: documentation vs. what is built

All SDR endpoints are admin-only (except provider webhooks, which use shared secrets). The defaults run with no external accounts,
so the whole flow works on Streamlit Community Cloud: mock discovery, dry-run email, mock calendar, dry-run CRM.

| Agent (doc) | Built in `app/sdr` + `app/routers/sdr.py` | Not built / differs from the docs |
|---|---|---|
| ICP | Prompt + filters, normalization, persona alias expansion, validation, deterministic suggestions, versioned history (`/icp...`). Structured filters are hard constraints | LLM-assisted suggestions |
| Discovery | Mock fixture (explicit, `.example` domains), Apollo org + people search with 422 (credits) and 403 (plan) mapped to clear 502 errors, optional fallback to the fixture | Apollo calls follow Apollo's public docs but are **untested live** (no key/credits). Public registries (GLEIF, SEC, OpenCorporates, Companies House) and SearXNG/OpenSearch are not built |
| Enrichment | Deterministic brief + signals + pain hypotheses from the discovery record, stored per account | Web search, OpenSearch, LLM synthesis, citations beyond the source record |
| Prospect Intelligence | Canonical signals, intent/reply/meeting/qualification probabilities, priority score, personalization (email, LinkedIn, call). Always `mode=heuristic`, `calibrated=false`; outcome labels are stored | LightGBM/SHAP models, training endpoint, MetaRank (local ordering only), MLflow/Evidently |
| Qualification | BANT + MEDDIC evidence scoring, SQL/MQL/Nurture/Disqualified, explicit missing information; only SQL/MQL reach Outreach | Learned probability (heuristic stand-in) |
| Outreach | Draft campaigns in `pending_approval`; approve/send/pause/resume/cancel; send is idempotent and blocked for suppressed contacts; LinkedIn/phone are human tasks; no duplicate active campaigns; dry-run and Brevo providers; rate limit; Brevo event webhook | `/outreach/operator/command` (OpenClaw); Brevo delivery path is untested live |
| Conversation | Rule-based classifier (9 intents), follow-ups stopped before drafting, reviewable draft, explicit approval before send, unsubscribe suppression, Brevo inbound webhook | LLM classifier/drafts; Brevo payload shape is untested live |
| Follow-Up | Approval-ready touches with due times, stop on reply/unsubscribe/meeting/cancel, `run-due` endpoint | Temporal timers (a local scheduler endpoint only; on Streamlit Cloud call it from the UI) |
| Meeting | Request needs a meeting-type reply, approval, free/busy check, idempotent event ID; `booked` only with event ID + URL; reminder times computed | Real Google Calendar / Cal.com; reminder worker |
| CRM | Company/person/opportunity created only after booking; idempotent per meeting; retry endpoint | Real Twenty API |
| Analytics | Funnel, reply/meeting rates, intents, outcome labels | MetaRank feedback loop |

## Run
`streamlit run streamlit_app.py` -> log in as the admin -> **AI SDR** page: ICP -> Pipeline -> Prospects -> Outreach -> Inbox (simulate a reply) -> Meetings & CRM -> Analytics.
API only: `uvicorn app.main:app` (docs at `/docs`). Tests: `pytest -q` (`tests/test_sdr.py`, `tests/test_marketplace.py`, existing `tests/test_flow.py`).

## Streamlit Community Cloud
Use `.streamlit/secrets.toml.example`. Needs a Postgres `DATABASE_URL`. Real email, calendar and webhooks need a deployed API (Dockerfile) with `API_BASE_URL` set.
