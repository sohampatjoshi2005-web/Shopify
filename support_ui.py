"""Streamlit screens for the support desk. Same pattern as ext_ui.py: streamlit_app.py passes a context `c` (st, api, act, is_admin)."""
import pandas as pd

STATUSES = ["open", "in_progress", "escalated", "resolved", "closed"]
PRIORITIES = ["low", "medium", "high", "critical"]


def _tickets_table(c, rows):
    if not rows:
        c.st.caption("No tickets.")
        return
    c.st.dataframe(pd.DataFrame(rows)[["id", "subject", "status", "priority", "order_number", "sla_due_at", "sla_breached", "created_at"]],
                   hide_index=True, width="stretch")


def page_support(c):
    st = c.st
    st.header("Support")
    if not st.session_state.get("token"):
        st.info("Please log in or sign up from the sidebar first. (Forgot your password? Use the sidebar reset form.)")
        return
    chat_tab, tix_tab = st.tabs(["Chat with support", "My tickets"])

    with chat_tab:
        st.caption("Ask about an order status, invoice, tracking or a refund. Include your order number (looks like ORD-1A2B3C4D).")
        hist = st.session_state.setdefault("sup_chat", [{"role": "assistant", "text": "Hi! How can I help you today?"}])
        for m in hist:
            st.chat_message(m["role"]).write(m["text"])
        if text := st.chat_input("Type your message"):
            hist.append({"role": "user", "text": text})
            ok, r = c.act("POST", "/chat/message", json={"message": text})
            if ok:
                note = f"\n\n_Ticket `{r['ticket']['id'][:8]}` · {r['ticket']['status']}_" if r.get("ticket") else ""
                hist.append({"role": "assistant", "text": r["reply"] + note})
            st.rerun()

    with tix_tab:
        with st.form("sup_new"):
            subj = st.text_input("Subject")
            desc = st.text_area("What happened?")
            order = st.text_input("Order number (optional)")
            if st.form_submit_button("Open a ticket") and c.act("POST", "/tickets", json={"subject": subj, "description": desc, "order_number": order or None})[0]:
                st.success("Ticket created. We'll get back to you.")
                st.rerun()
        ok, rows = c.act("GET", "/tickets")
        if ok:
            _tickets_table(c, rows)


def admin_tab(c, tab):
    st = c.st
    with tab:
        a, b, d = st.columns(3)
        status = a.selectbox("Status", ["(any)"] + STATUSES, key="sd_status")
        prio = b.selectbox("Priority", ["(any)"] + PRIORITIES, key="sd_prio")
        email = d.text_input("Customer email", key="sd_email")
        params = {k: v for k, v in {"status": status if status != "(any)" else None, "priority": prio if prio != "(any)" else None,
                                    "customer_email": email or None}.items() if v}
        rows = c.api("GET", "/tickets", params=params)
        _tickets_table(c, rows)
        if rows:
            t = st.selectbox("Open ticket", rows, format_func=lambda r: f"{r['id'][:8]} · {r['subject']} ({r['status']}, {r['priority']})", key="sd_pick")
            st.write(f"**{t['subject']}**  ·  {t['customer_email']}  ·  order: `{t['order_number'] or '-'}`  ·  SLA due: {t['sla_due_at']}"
                     + ("  ·  :red[SLA breached]" if t["sla_breached"] else ""))
            if t.get("description"):
                st.write(t["description"])
            with st.expander("Worklog (audit trail)"):
                for w in t["worklogs"]:
                    st.write(f"`{w['created_at'][:16]}` **{w['actor']}** · {w['action']} · {w['note'] or ''}")
            with st.form("sd_update"):
                x, y, z = st.columns(3)
                ns = x.selectbox("Status", STATUSES, index=STATUSES.index(t["status"]))
                np_ = y.selectbox("Priority", PRIORITIES, index=PRIORITIES.index(t["priority"]))
                who = z.text_input("Assign to", value=t["assigned_to"] or "")
                note = st.text_area("Internal note")
                if st.form_submit_button("Save changes"):
                    body = {"status": ns if ns != t["status"] else None, "priority": np_ if np_ != t["priority"] else None,
                            "assigned_to": who if who and who != t["assigned_to"] else None, "note": note or None}
                    if c.act("PATCH", f"/tickets/{t['id']}", json={k: v for k, v in body.items() if v})[0]:
                        st.rerun()
            e1, e2 = st.columns([1, 3])
            reason = e2.text_input("Escalation reason", value="Needs engineering review", key="sd_reason")
            if e1.button("Escalate") and c.act("POST", f"/tickets/{t['id']}/escalate", params={"reason": reason})[0]:
                st.toast("Escalated and engineering emailed")
                st.rerun()
        with st.expander("Search & context tools (RAG / CAG)"):
            r1, r2 = st.columns(2)
            if r1.button("Rebuild search index (RAG)"):
                ok, s = c.act("POST", "/rag/collect")
                st.success(f"Indexed {s['documents_indexed']} documents {s['by_type']}") if ok else None
            if r2.button("Refresh cached context (CAG)"):
                ok, s = c.act("POST", "/cag/refresh")
                st.success(f"Cached {s['document_count']} documents, {s['context_size_chars']} chars") if ok else None
            q = st.text_input("Search orders, tickets and worklogs", key="sd_rag_q")
            if q:
                ok, hits = c.act("GET", "/rag/search", params={"q": q, "top_k": 8})
                for h in hits or []:
                    st.write(f"**{h['title']}** · {h['doc_type']} · score {h['score']}")
                    st.caption(h["snippet"])
                if ok and not hits:
                    st.caption("No matches (rebuild the index first?).")
