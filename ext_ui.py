"""Streamlit screens for the store extensions. streamlit_app.py passes a small context `c` (st, api, act, api_bytes, money, is_admin)
so this module never imports the app and has no circular dependency."""
from types import SimpleNamespace
import pandas as pd

Ctx = SimpleNamespace


def _df(c, rows, cols=None):
    if not rows:
        c.st.caption("Nothing here yet.")
        return
    c.st.dataframe(pd.DataFrame(rows)[cols] if cols else pd.DataFrame(rows), hide_index=True, width="stretch")


# ------------------------------------------------------------------ sidebar: verification + password reset
def sidebar_account(c):
    st = c.st
    qp = st.query_params
    if qp.get("verify_token"):
        ok, _ = c.act("POST", "/auth/email/verify", json={"token": qp["verify_token"]}, auth=False)
        st.sidebar.success("Email verified. Thank you!") if ok else None
        qp.pop("verify_token", None)
    if st.session_state.get("token"):
        ok, s = c.act("GET", "/auth/email/status")
        if ok and not s["verified"]:
            with st.sidebar.expander("⚠️ Verify your email"):
                if st.button("Send verification email", key="xv_send") and c.act("POST", "/auth/email/send-verification")[0]:
                    st.toast("Verification email sent")
                code = st.text_input("Paste the code from the email", key="xv_code")
                if st.button("Verify", key="xv_go") and code and c.act("POST", "/auth/email/verify", json={"token": code.strip()})[0]:
                    st.rerun()
        return
    with st.sidebar.expander("Forgot password?", expanded=bool(qp.get("reset_token"))):
        em = st.text_input("Your account email", key="xf_em")
        if st.button("Email me a reset code", key="xf_go"):
            ok, r = c.act("POST", "/auth/password/forgot", json={"email": em}, auth=False)
            if ok:
                st.info(r["message"])
        code = st.text_input("Reset code", value=qp.get("reset_token", ""), key="xf_code")
        new = st.text_input("New password (8+ chars)", type="password", key="xf_new")
        if st.button("Set new password", key="xf_set") and c.act("POST", "/auth/password/reset", json={"token": code.strip(), "password": new}, auth=False)[0]:
            qp.pop("reset_token", None)
            st.success("Password changed. You can log in now.")


# ------------------------------------------------------------------ buyer extras
def wishlist_button(c, pid):
    if c.st.button("♡ Wishlist", key=f"wl{pid}"):
        if not c.st.session_state.get("token"):
            c.st.info("Log in to save items.")
        elif c.act("PUT", f"/wishlist/{pid}")[0]:
            c.st.toast("Saved to your wishlist")


def page_wishlist(c):
    st = c.st
    st.header("Wishlist")
    if not st.session_state.get("token"):
        st.info("Please log in first.")
        return
    items = c.api("GET", "/wishlist")["items"]
    if not items:
        st.info("Nothing saved yet. Tap ♡ on a product in the Shop.")
    for p in items:
        a, b, d = st.columns([5, 2, 1])
        a.markdown(f"**{p['title']}**  \n{p['brand'] or ''}")
        b.write(c.money(p["price_from_cents"]))
        if d.button("✕", key=f"wr{p['id']}") and c.act("DELETE", f"/wishlist/{p['id']}")[0]:
            st.rerun()


def order_extras(c, o):
    """Rendered inside each order expander on 'My orders': tracking, invoices, return request."""
    st = c.st
    if o["status"] in ("shipped", "delivered"):
        sh = c.api("GET", f"/orders/{o['number']}/shipments")
        for s in sh:
            st.caption(f"📦 {s['carrier']} {s['awb']} · **{s['status'].replace('_', ' ')}**")
    if o["status"] in ("paid", "shipped", "delivered", "refunded"):
        ok, invs = c.act("GET", f"/orders/{o['number']}/invoices")
        for i in (invs or []):
            data = c.api_bytes("GET", f"/invoices/{i['id']}/pdf")
            if data:
                st.download_button(f"⬇ {'Credit note' if i['kind'] == 'credit_note' else 'Tax invoice'} {i['number']}", data, file_name=i["number"].replace("/", "-") + ".pdf",
                                   mime="application/pdf", key=f"inv{i['id']}")
    if o["status"] == "delivered":
        items = [i for i in c.api("GET", f"/orders/{o['number']}/items") if i["returnable_qty"] > 0]
        with st.expander("Return items", expanded=False):
            if not items:
                st.caption("Everything in this order has already been returned or is being returned.")
            picks = st.multiselect("Items", items, format_func=lambda i: f"{i['title']} (up to {i['returnable_qty']})", key=f"rp{o['number']}")
            lines = [{"order_item_id": p["id"], "qty": int(st.number_input(f"Qty of {p['title']}", 1, p["returnable_qty"], 1, key=f"rq{o['number']}{p['id']}"))} for p in picks]
            reason = st.selectbox("Reason", ["Defective / damaged", "Wrong item", "Doesn't fit", "Changed my mind"], key=f"rr{o['number']}")
            if st.button("Request return", key=f"rb{o['number']}", disabled=not lines) and c.act("POST", f"/orders/{o['number']}/returns", json={"lines": lines, "reason": reason})[0]:
                st.success("Return requested. We'll review it shortly.")
                st.rerun()


def my_returns(c):
    st = c.st
    ok, rs = c.act("GET", "/returns")
    if not rs:
        return
    st.subheader("My returns")
    for r in rs:
        a, b = st.columns([5, 1])
        a.write(f"#{r['id']} · order {r['order']} · **{r['status']}**" + (f" · refunded {c.money(r['refund_cents'])}" if r["refund_cents"] else ""))
        if r["status"] == "requested" and b.button("Cancel", key=f"rc{r['id']}") and c.act("POST", f"/returns/{r['id']}/cancel")[0]:
            st.rerun()


# ------------------------------------------------------------------ seller dashboard
def page_seller_dashboard(c):
    st, m = c.st, c.money
    st.header("Seller dashboard")
    if not st.session_state.get("token"):
        st.info("Please log in first.")
        return
    ok, led = c.act("GET", "/sellers/me/ledger")
    if not ok:
        st.info("Apply under **Sell on the store** first.")
        return
    t1, t2, t3, t4 = st.tabs(["Earnings", "Orders to ship", "Payouts", "Business & payout details"])
    with t1:
        b = led["balance"]
        x, y, z = st.columns(3)
        x.metric("Available to pay out", m(b["available_cents"]))
        y.metric(f"Pending ({led['hold_days']}-day hold)", m(b["pending_cents"]))
        z.metric("Paid out so far", m(b["paid_out_cents"]))
        st.caption(f"Platform commission: {led['commission_pct']:g}% per sale. Returns are deducted automatically.")
        _df(c, [{"When": e["created_at"][:16], "Type": e["kind"], "Order": e["order"], "Amount": m(e["amount_cents"]), "Note": e["note"],
                 "Status": "paid out" if e["paid_out"] else "open"} for e in led["entries"]])
    with t2:
        sales = c.api("GET", "/sellers/me/orders")
        mine = {s["order"]: s for s in c.api("GET", "/sellers/me/shipments")}
        shipped = {s["order"] for s in mine.values()}
        todo = sorted({s["order"] for s in sales if s["status"] == "paid"} - shipped)
        if todo:
            pick = st.selectbox("Paid orders waiting for you", todo)
            if st.button("Book carrier + create shipment") and (r := c.act("POST", f"/sellers/me/orders/{pick}/ship"))[0]:
                st.success(f"AWB {r[1]['awb']} ({r[1]['carrier']})")
                st.rerun()
        else:
            st.caption("No paid orders waiting to be shipped.")
        st.subheader("Shipments")
        _df(c, [{"Order": s["order"], "Carrier": s["carrier"], "AWB": s["awb"], "Status": s["status"], "Label": s["label_url"]} for s in mine.values()])
        st.subheader("Sales lines")
        _df(c, [{"Order": s["order"], "Status": s["status"], "Qty": s["qty"], "Gross": m(s["gross_cents"]), "Commission": m(s["commission_cents"]), "Net": m(s["net_cents"])} for s in sales])
    with t3:
        _df(c, [{"#": p["id"], "Amount": m(p["amount_cents"]), "Status": p["status"], "Reference": p["reference"], "Created": p["created_at"][:10]} for p in c.api("GET", "/sellers/me/payouts")])
    with t4:
        p = c.api("GET", "/sellers/me/profile")
        with st.form("sprofile"):
            a, b = st.columns(2)
            f = {"legal_name": a.text_input("Legal / trade name", p.get("legal_name", "")), "gstin": b.text_input("GSTIN", p.get("gstin", "")),
                 "pan": a.text_input("PAN", p.get("pan", "")), "pickup_pincode": b.text_input("Pickup PIN code", p.get("pickup_pincode", "")),
                 "address": st.text_input("Registered address", p.get("address", "")), "upi_id": a.text_input("UPI ID", p.get("upi_id", "")),
                 "bank_account_name": b.text_input("Bank account holder", p.get("bank_account_name", "")),
                 "bank_account_no": a.text_input(f"Bank account number {('(saved: ' + p['bank_account_masked'] + ', leave blank to keep)') if p.get('bank_account_masked') else ''}", type="password"),
                 "bank_ifsc": b.text_input("IFSC", p.get("bank_ifsc", ""))}
            if st.form_submit_button("Save") and c.act("PUT", "/sellers/me/profile", json=f)[0]:
                st.success("Saved.")
                st.rerun()
        st.caption("A valid GSTIN is needed for GST invoices; UPI or bank details are needed to receive payouts.")


# ------------------------------------------------------------------ admin tabs
def admin_tabs(c, tabs):
    """`tabs` = the 5 extra st.tabs created by the caller, in this order."""
    t_mk, t_ret, t_ship, t_inv, t_srch = tabs
    st, m = c.st, c.money
    with t_mk:
        d = c.api("GET", "/admin/ext/dashboard")
        a, b, x, y = st.columns(4)
        a.metric("Marketplace GMV", m(d["marketplace_gmv_cents"]))
        b.metric("Commission earned", m(d["commission_earned_cents"]))
        x.metric("Owed to sellers", m(d["owed_to_sellers_cents"]))
        y.metric("Open returns", sum(v for k, v in d["returns_by_status"].items() if k in ("requested", "approved", "received")))
        s1, s2 = st.columns(2)
        if s1.button("Sync ledger now") and c.act("POST", "/admin/ext/ledger/sync")[0]:
            st.rerun()
        if s2.button("Run payouts (creates a payout per eligible seller)", type="primary"):
            ok, r = c.act("POST", "/admin/ext/payouts/run")
            if ok:
                st.success(f"Created {len(r['created'])} payout(s).")
                for sk in r["skipped"]:
                    st.warning(f"Seller {sk['seller_id']} skipped: {sk['reason']}")
        sellers = c.api("GET", "/admin/ext/ledger/sellers")
        _df(c, [{"ID": s["seller_id"], "Seller": s["name"], "Status": s["status"], "Commission %": s["commission_pct"], "Available": m(s["available_cents"]),
                 "Pending": m(s["pending_cents"]), "Paid out": m(s["paid_out_cents"]), "Payout details": "✅" if s["payout_details"] else "❌"} for s in sellers])
        if sellers:
            sel = st.selectbox("Change commission for", sellers, format_func=lambda s: f"{s['name']} ({s['commission_pct']:g}%)")
            pct = st.number_input("New commission % (applies to future orders)", 0.0, 100.0, float(sel["commission_pct"]))
            if st.button("Save commission") and c.act("PUT", f"/admin/ext/sellers/{sel['seller_id']}/commission", json={"commission_pct": pct})[0]:
                st.rerun()
        st.subheader("Payouts")
        pays = c.api("GET", "/admin/ext/payouts")
        _df(c, [{"#": p["id"], "Seller": p["seller_id"], "Amount": m(p["amount_cents"]), "Status": p["status"], "Reference": p["reference"], "Created": p["created_at"][:10]} for p in pays])
        open_ = [p for p in pays if p["status"] == "created"]
        if open_:
            p = st.selectbox("Settle a payout", open_, format_func=lambda p: f"#{p['id']} · seller {p['seller_id']} · {m(p['amount_cents'])}")
            utr = st.text_input("Bank / UPI reference (UTR)")
            k1, k2 = st.columns(2)
            if k1.button("Mark paid") and c.act("POST", f"/admin/ext/payouts/{p['id']}/paid", json={"reference": utr})[0]:
                st.rerun()
            if k2.button("Mark failed (money becomes payable again)") and c.act("POST", f"/admin/ext/payouts/{p['id']}/fail")[0]:
                st.rerun()
    with t_ret:
        flt = st.selectbox("Status", [None, "requested", "approved", "received", "refunded", "rejected", "cancelled"], format_func=lambda s: "All" if s is None else s, key="xrf")
        rs = c.api("GET", "/admin/ext/returns", params={"status": flt})
        _df(c, [{"#": r["id"], "Order": r["order"], "Status": r["status"], "Reason": r["reason"], "Items": sum(l["qty"] for l in r["lines"]), "Refund": m(r["refund_cents"])} for r in rs])
        if rs:
            r = st.selectbox("Manage return", rs, format_func=lambda r: f"#{r['id']} · {r['order']} · {r['status']}")
            note = st.text_input("Note to customer (optional)", key="xrn")
            nxt = {"requested": [("Approve", "approve"), ("Reject", "reject")], "approved": [("Mark received", "receive"), ("Reject", "reject")], "received": [("Refund now", "refund")]}.get(r["status"], [])
            for col, (label, step) in zip(st.columns(max(len(nxt), 1)), nxt):
                if col.button(label, key=f"xr{step}{r['id']}", type="primary" if step in ("approve", "refund") else "secondary"):
                    body = {"admin_note": note} if step in ("approve", "reject") else None
                    if c.act("POST", f"/admin/ext/returns/{r['id']}/{step}", json=body)[0]:
                        st.rerun()
            if r["status"] == "received":
                st.caption("Refund goes to the original payment method for the returned items (their share of discount and tax included; shipping is not refunded). Stock is returned and the seller's earnings are reversed.")
    with t_ship:
        sh = c.api("GET", "/admin/ext/shipments")
        _df(c, [{"#": s["id"], "Order": s["order"], "Seller": s["seller_id"], "Carrier": s["carrier"], "AWB": s["awb"], "Status": s["status"], "Cost": m(s["cost_cents"])} for s in sh])
        if sh:
            s = st.selectbox("Shipment", sh, format_func=lambda s: f"#{s['id']} · {s['order']} · {s['awb']} · {s['status']}")
            a, b, d = st.columns(3)
            if a.button("Refresh from carrier") and c.act("POST", f"/admin/ext/shipments/{s['id']}/refresh")[0]:
                st.rerun()
            new = b.selectbox("Set status manually", ["in_transit", "delivered", "rto", "cancelled"], key="xss")
            if d.button("Apply status") and c.act("POST", f"/admin/ext/shipments/{s['id']}/status", json={"status": new})[0]:
                st.rerun()
            st.json(s["events"], expanded=False)
        with st.form("xnewship"):
            st.caption("Create a shipment for a paid order (blank seller = house products)")
            a, b = st.columns(2)
            num, sid = a.text_input("Order number"), b.number_input("Seller ID (0 = house)", 0, step=1)
            if st.form_submit_button("Book shipment") and (r := c.act("POST", f"/admin/ext/orders/{num.strip()}/shipments", params={"seller_id": int(sid) or None}))[0]:
                st.success(f"AWB {r[1]['awb']}")
                st.rerun()
    with t_inv:
        num = st.text_input("Order number", key="xinvn", placeholder="ORD-1A2B3C4D")
        if num:
            ok, invs = c.act("GET", f"/orders/{num.strip()}/invoices")
            for i in (invs or []):
                data = c.api_bytes("GET", f"/invoices/{i['id']}/pdf")
                a, b = st.columns([4, 1])
                a.write(f"**{i['number']}** · {i['kind'].replace('_', ' ')} · seller {i['seller_id'] or 'house'} · {m(i['total_cents'])}")
                if data:
                    b.download_button("PDF", data, file_name=i["number"].replace("/", "-") + ".pdf", mime="application/pdf", key=f"xdl{i['id']}")
        st.subheader("Tax & shipping data")
        ps = c.api("GET", "/products", params={"page_size": 50}, auth=False)["items"]
        if ps:
            p = st.selectbox("Product", ps, format_func=lambda p: p["title"], key="xtp")
            a, b, d = st.columns(3)
            hsn, rate = a.text_input("HSN code (digits)", key="xhsn"), b.number_input("GST rate %", 0.0, 40.0, 18.0, key="xrate")
            if d.button("Save tax") and c.act("PUT", f"/admin/ext/products/{p['id']}/tax", json={"hsn": hsn, "gst_rate_pct": rate})[0]:
                st.toast("Saved")
            v = st.selectbox("Variant", p["variants"], format_func=lambda v: f"{v['sku']} · {v['title']}", key="xtv")
            w = st.number_input("Weight (grams)", 1, 100000, 500, key="xw")
            if st.button("Save weight") and c.act("PUT", f"/admin/ext/variants/{v['id']}/shipping", json={"weight_g": int(w)})[0]:
                st.toast("Saved")
    with t_srch:
        d = c.api("GET", "/admin/ext/dashboard")
        st.write(f"Search engine: **{d['search_provider']}** · Carrier: **{d['shipping_provider']}**")
        st.caption("Set SEARCH_PROVIDER=meilisearch (+ MEILI_URL, MEILI_KEY) to switch engines. Without it, search uses your database.")
        if st.button("Rebuild search index"):
            ok, r = c.act("POST", "/admin/ext/search/reindex")
            if ok:
                st.success(f"{r['provider']}: indexed {r['indexed']} products")
