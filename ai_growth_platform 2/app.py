import pandas as pd
import plotly.express as px
import streamlit as st

from catalogue import EVENT_WEIGHTS, LIFECYCLES, PRODUCT_IDS, PRODUCTS_BY_ID, SEGMENTS
from database import (
    ALLOWED_TABLES, init_db, query_df, record_interaction, reset_db, save_campaign,
    save_drafts, save_lead, seed_demo_data, set_draft_status, table_df,
)
from marketing_module import (
    CONTENT_CHANNELS, FORECAST_CHANNELS, INTENT_LEVELS, SEGMENTS as MKT_SEGMENTS,
    compare_channels, generate_content, predict_campaign_performance, segment_message,
)
from recommendation_module import get_personalized_recommendations, normalise_profile
from sdr_module import (
    BANT_MAX, generate_outreach, qualification_label, score_bant, synthesize_leads,
)
from utils import PROVIDERS, get_llm_client, llm_enabled

st.set_page_config(page_title="AI Growth Command Center", page_icon="🤖", layout="wide",
                   initial_sidebar_state="expanded")


@st.cache_resource
def bootstrap() -> bool:
    init_db()
    seed_demo_data()
    return True


@st.cache_resource
def llm_client():
    return get_llm_client()


bootstrap()

st.markdown(
    """
    <style>
    .block-container {padding-top: 1.4rem; padding-bottom: 2rem;}
    div[data-testid="stMetric"] {border:1px solid rgba(128,128,128,.25); border-radius:12px;
                                 padding:12px 14px; background:rgba(128,128,128,.05);}
    </style>
    """,
    unsafe_allow_html=True,
)


def show_generation_status(result: dict) -> None:
    """Tell the operator whether copy came from an LLM or the template fallback."""
    if result.get("error"):
        st.warning(result["error"])
    if result.get("risky_claims"):
        st.warning("Claims to substantiate or remove before publishing: " + ", ".join(result["risky_claims"]))


# ---------- Sidebar ----------
with st.sidebar:
    st.header("Configuration")
    provider = st.selectbox("LLM provider", PROVIDERS)
    if provider == "OpenAI" and not llm_enabled():
        st.warning("OPENAI_API_KEY not found in .env. Templates will be used.")
    elif provider == "OpenAI":
        st.success("OpenAI key detected.")
    elif provider == "Ollama":
        st.info("Requires Ollama running locally (see OLLAMA_URL / OLLAMA_MODEL).")
    client = llm_client() if provider == "OpenAI" else None
    sender_name = st.text_input("Sender name for outreach", "[Your name]")
    st.divider()
    st.caption("Outreach is saved as drafts. This app never sends messages; approval marks a draft "
               "ready for you to send from your own email/CRM.")
    confirm_reset = st.checkbox("I understand reset deletes all local data")
    if st.button("Reset demo database", disabled=not confirm_reset):
        reset_db()
        init_db()
        seed_demo_data()
        for k in ("sdr_leads", "outreach", "marketing", "segment_copy"):
            st.session_state.pop(k, None)
        st.success("Demo database reset.")
        st.rerun()

st.title("🤖 AI Growth Command Center")
st.caption("AI SDR + Marketing Automation + Product Recommendations (local-first)")

tabs = st.tabs(["🏠 Overview", "🎯 AI SDR", "📣 Marketing Engine", "🛍️ Recommendation Engine", "🗄️ Data"])

# ---------- Overview ----------
with tabs[0]:
    leads = query_df("SELECT * FROM leads")
    campaigns = query_df("SELECT * FROM campaigns")
    drafts = query_df("SELECT status FROM outreach_drafts")
    interactions = query_df("SELECT * FROM interactions")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Leads", len(leads))
    c2.metric("SQL leads", int((leads["qualification"] == "SQL").sum()))
    c3.metric("Drafts awaiting approval", int((drafts["status"] == "draft").sum()))
    c4.metric("Campaigns saved", len(campaigns))
    c5.metric("Interactions", len(interactions))

    left, right = st.columns(2)
    with left:
        scored = leads[leads["bant_score"] > 0]
        if scored.empty:
            st.info("No BANT-scored leads yet. Score and save a lead in the AI SDR tab.")
        else:
            counts = scored["qualification"].value_counts().reset_index()
            counts.columns = ["Qualification", "Leads"]
            st.plotly_chart(px.bar(counts, x="Qualification", y="Leads", title="Qualified pipeline"))
    with right:
        if campaigns.empty:
            st.info("No saved campaigns yet. Save a forecast in the Marketing tab.")
        else:
            st.plotly_chart(px.bar(campaigns, x="channel", y="roi", color="status", title="Saved campaign ROI (x)"))

    st.subheader("How it fits together")
    st.markdown(
        "**UI → module services → SQLite → optional LLM**\n\n"
        "* **AI SDR:** ICP → simulated enrichment → BANT → drafts → human approval queue.\n"
        "* **Marketing:** channel copy (with claim checks) → forecast → saved campaigns → lifecycle messaging.\n"
        "* **Recommendations:** collaborative + content + rules, explained per product, with live interaction logging."
    )

# ---------- SDR ----------
with tabs[1]:
    st.header("AI SDR")
    st.write("Define an ICP, generate synthetic lead profiles, score with BANT, draft outreach and approve it.")

    with st.form("icp_form"):
        a, b = st.columns(2)
        target_company = a.text_input("Target company / market", "B2B SaaS companies")
        industry = b.text_input("Industry", "Software / SaaS")
        c, d = st.columns(2)
        persona = c.text_input("Target persona", "VP Sales / Head of Revenue")
        geography = d.text_input("Geography", "US / Canada")
        pain_points = st.text_area("Known ICP pain points (comma or semicolon separated)",
                                   "manual prospecting, low pipeline visibility, fragmented CRM data")
        num_leads = st.slider("Synthetic leads", 3, 15, 6)
        submitted = st.form_submit_button("Discover & enrich leads")

    if submitted:
        if not target_company.strip() or not persona.strip():
            st.error("Target company/market and persona are required.")
        else:
            st.session_state.sdr_leads = synthesize_leads(
                target_company.strip(), industry.strip(), persona.strip(), geography.strip(), pain_points, num_leads)
            st.session_state.pop("outreach", None)
            st.success(f"Generated {len(st.session_state.sdr_leads)} synthetic profiles (simulated, not real people).")

    sdr_leads = st.session_state.get("sdr_leads", [])
    if sdr_leads:
        st.dataframe(pd.DataFrame(sdr_leads), hide_index=True)
        idx = st.selectbox("Select a prospect", range(len(sdr_leads)),
                           format_func=lambda i: f"{sdr_leads[i]['name']} — {sdr_leads[i]['title']}")
        lead = sdr_leads[idx]

        st.subheader("BANT qualification")
        b1, b2, b3, b4 = st.columns(4)
        budget = b1.slider("Budget evidence", 0, BANT_MAX, 16)
        authority = b2.slider("Authority evidence", 0, BANT_MAX, 20)
        need = b3.slider("Need evidence", 0, BANT_MAX, 21)
        timing = b4.slider("Timing evidence", 0, BANT_MAX, 15)
        score = score_bant(budget, authority, need, timing)
        label = qualification_label(score)
        m1, m2 = st.columns(2)
        m1.metric("BANT score", score)
        m2.metric("Qualification", label)

        bant = {"budget": budget, "authority": authority, "need": need, "timing": timing,
                "score": score, "label": label}
        if st.button("💾 Save lead with score"):
            lead_id = save_lead(lead, bant)
            st.success(f"Saved lead #{lead_id} ({label}, {score}).")

        if st.button("Generate personalized outreach"):
            with st.spinner("Drafting..."):
                result = generate_outreach(lead, lead["pain_points"], client=client, provider=provider,
                                           sender_name=sender_name)
            st.session_state.outreach = {"email": lead["email"], "data": result}

        stored = st.session_state.get("outreach")
        if stored and stored["email"] == lead["email"]:  # never show another prospect's draft
            out = stored["data"]
            st.subheader("Review and edit drafts")
            if out["error"]:
                st.warning(out["error"])
            st.caption(f"Source: {'LLM' if out['source'] == 'llm' else 'template'}")
            edited = {
                "email_step_1": st.text_area("Email — Step 1", out["email_step_1"], height=180, key="e1"),
                "email_step_2": st.text_area("Email — Step 2", out["email_step_2"], height=150, key="e2"),
                "email_step_3": st.text_area("Email — Step 3", out["email_step_3"], height=150, key="e3"),
                "linkedin": st.text_area("LinkedIn connection", out["linkedin"], height=100, key="li"),
            }
            if st.button("📥 Submit drafts for approval"):
                lead_id = save_lead(lead, bant)
                n = save_drafts(lead_id, edited)
                st.success(f"{n} drafts added to the approval queue below.")

    st.divider()
    st.subheader("Approval queue")
    queue = query_df(
        """SELECT d.id, l.name, l.company, d.step, d.status, d.body, d.created_at
           FROM outreach_drafts d JOIN leads l ON l.id = d.lead_id ORDER BY d.id DESC""")
    if queue.empty:
        st.info("No drafts yet.")
    else:
        status_filter = st.multiselect("Show statuses", ["draft", "approved", "rejected"], default=["draft"])
        view = queue[queue["status"].isin(status_filter)]
        st.dataframe(view, hide_index=True)
        if not view.empty:
            q1, q2, q3 = st.columns([2, 1, 1])
            draft_id = q1.selectbox("Draft", view["id"].tolist(),
                                    format_func=lambda i: f"#{i} — {view.set_index('id').loc[i, 'name']} / "
                                                          f"{view.set_index('id').loc[i, 'step']}")
            st.text_area("Selected draft", view.set_index("id").loc[draft_id, "body"], height=140, disabled=True)
            if q2.button("✅ Approve"):
                set_draft_status(int(draft_id), "approved")
                st.rerun()
            if q3.button("🚫 Reject"):
                set_draft_status(int(draft_id), "rejected")
                st.rerun()
        approved = queue[queue["status"] == "approved"]
        if not approved.empty:
            st.download_button("Download approved drafts (CSV)", approved.to_csv(index=False),
                               "approved_drafts.csv", "text/csv")

# ---------- Marketing ----------
with tabs[2]:
    st.header("E-Commerce Marketing Automation Engine")
    left, right = st.columns(2)

    with left:
        st.subheader("Content generator")
        channel = st.selectbox("Content channel", CONTENT_CHANNELS)
        product = st.text_input("Product / offer", "Premium Analytics Subscription")
        audience = st.text_input("Target audience", "Growth-stage e-commerce teams")
        objective = st.selectbox("Campaign objective", ["Acquisition", "Conversion", "Retention", "Upsell"])
        tone = st.selectbox("Tone", ["Professional", "Friendly", "Urgent", "Premium", "Educational"])
        if st.button("Generate marketing content"):
            if not product.strip() or not audience.strip():
                st.error("Product and audience are required.")
            else:
                with st.spinner("Writing..."):
                    st.session_state.marketing = generate_content(
                        channel, product.strip(), audience.strip(), objective, tone,
                        client=client, provider=provider)

    with right:
        st.subheader("Campaign performance predictor")
        st.caption("Heuristic planning model using generic benchmarks, not your measured data.")
        budget = st.number_input("Budget (USD)", 100.0, 1_000_000.0, 5000.0, step=500.0)
        demographic = st.selectbox("Audience intent", INTENT_LEVELS, index=1)
        campaign_channel = st.selectbox("Prediction channel", FORECAST_CHANNELS)
        creative_quality = st.slider("Creative quality", 1, 10, 7)
        offer_strength = st.slider("Offer strength", 1, 10, 7)
        f = predict_campaign_performance(budget, demographic, campaign_channel, creative_quality, offer_strength)

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Est. ROI", f"{f['roi']:.2f}x")
        k2.metric("CTR", f"{f['ctr']:.2f}%")
        k3.metric("Conversion", f"{f['conversion_rate']:.2f}%")
        k4.metric("CPA", f"${f['cpa']:,.0f}")
        st.caption(f"≈ {f['clicks']:,.0f} clicks → {f['conversions']:,.0f} conversions → ${f['revenue']:,.0f} revenue")

        comp = pd.DataFrame(compare_channels(budget, demographic, creative_quality, offer_strength))
        st.plotly_chart(px.bar(comp, x="channel", y="roi", title="ROI by channel (same inputs)",
                               labels={"roi": "ROI (x)", "channel": "Channel"}))
        if st.button("💾 Save as campaign draft"):
            cid = save_campaign(campaign_channel, objective, audience, budget, f["ctr"], f["conversion_rate"], f["roi"])
            st.success(f"Saved campaign #{cid}.")

    if st.session_state.get("marketing"):
        st.subheader("Generated content")
        show_generation_status(st.session_state.marketing)
        st.text_area("Copy", st.session_state.marketing["text"], height=300)

    st.subheader("Customer segment messaging")
    s1, s2 = st.columns(2)
    segment = s1.selectbox("Segment", MKT_SEGMENTS)
    context = s2.text_input("Customer context", "Recently purchased two premium products")
    if st.button("Create segment message"):
        st.session_state.segment_copy = segment_message(segment, context, client=client, provider=provider)
    if st.session_state.get("segment_copy"):
        show_generation_status(st.session_state.segment_copy)
        st.info(st.session_state.segment_copy["text"])

    saved = query_df("SELECT * FROM campaigns ORDER BY id DESC")
    if not saved.empty:
        st.subheader("Saved campaigns")
        st.dataframe(saved, hide_index=True)

# ---------- Recommendations ----------
with tabs[3]:
    st.header("AI Recommendation Engine")
    st.write("Hybrid personalization: behavioural affinity + product metadata + business rules.")

    presets = {
        "New buyer": {"segment": "silver", "lifecycle": "new", "churn_risk": 0.10, "history": ["P001"]},
        "Active premium": {"segment": "gold", "lifecycle": "active", "churn_risk": 0.20, "history": ["P003", "P005", "P006"]},
        "VIP": {"segment": "platinum", "lifecycle": "vip", "churn_risk": 0.15, "history": ["P007", "P009"]},
        "Churn risk": {"segment": "silver", "lifecycle": "at_risk", "churn_risk": 0.78, "history": ["P002", "P010"]},
    }
    preset_name = st.selectbox("Start from a persona", list(presets))
    preset = presets[preset_name]

    st.subheader("Persona studio")
    p1, p2, p3 = st.columns(3)
    # Keys include the preset name so changing the preset resets the controls to its defaults.
    seg = p1.selectbox("Loyalty tier", SEGMENTS, index=SEGMENTS.index(preset["segment"]), key=f"seg_{preset_name}")
    life = p2.selectbox("Lifecycle", LIFECYCLES, index=LIFECYCLES.index(preset["lifecycle"]), key=f"life_{preset_name}")
    churn = p3.slider("Churn risk", 0.0, 1.0, float(preset["churn_risk"]), 0.01, key=f"churn_{preset_name}")
    history = st.multiselect("Browse / purchase history", PRODUCT_IDS, default=preset["history"],
                             format_func=lambda pid: f"{pid} — {PRODUCTS_BY_ID[pid]['name']}",
                             key=f"hist_{preset_name}")
    top_n = st.slider("Products to show", 3, 8, 5)

    profile = normalise_profile({"segment": seg, "lifecycle": life, "churn_risk": churn, "history": history})
    recs = get_personalized_recommendations(profile, top_n=top_n)

    if recs:
        rec_df = pd.DataFrame(recs)
        st.dataframe(rec_df[["rank", "product_id", "name", "category", "match_pct", "confidence",
                             "collab_score", "content_score", "rule_score", "reason"]], hide_index=True)
        cols = st.columns(min(4, len(recs)))
        for col, rec in zip(cols, recs[:4]):
            with col:
                st.markdown(f"### {rec['name']}")
                st.metric("Match", f"{rec['match_pct']}%")
                st.caption(f"{rec['confidence']} confidence · {rec['reason']}")

        parts = rec_df.melt(id_vars=["name"], value_vars=["collab_score", "content_score", "rule_score"],
                            var_name="signal", value_name="score")
        st.plotly_chart(px.bar(parts, x="score", y="name", color="signal", orientation="h",
                               title="Signal breakdown per recommendation"))

        st.subheader("Log an interaction")
        l1, l2, l3, l4 = st.columns([1, 2, 2, 1])
        user_id = l1.text_input("User ID", "U100")
        log_pid = l2.selectbox("Product", [r["product_id"] for r in recs],
                               format_func=lambda pid: f"{pid} — {PRODUCTS_BY_ID[pid]['name']}")
        event = l3.selectbox("Event", list(EVENT_WEIGHTS))
        l4.write("")
        if l4.button("Record") and user_id.strip():
            record_interaction(user_id, log_pid, event)
            st.success("Recorded. Future recommendations will reflect it.")
    else:
        st.info("No products to recommend for this profile.")

# ---------- Data ----------
with tabs[4]:
    st.header("Local SQLite data")
    table = st.selectbox("Table", ALLOWED_TABLES)
    df = table_df(table)
    needle = st.text_input("Filter rows containing text")
    if needle:
        mask = df.astype(str).apply(lambda col: col.str.contains(needle, case=False, regex=False)).any(axis=1)
        df = df[mask]
    st.caption(f"{len(df)} rows")
    st.dataframe(df, hide_index=True)
    st.download_button("Download CSV", df.to_csv(index=False), f"{table}.csv", "text/csv")
