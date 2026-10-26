from datetime import date, timedelta

import pandas as pd
import streamlit as st

from app.core import (COST_COLUMNS, CURRENCIES, INCOTERMS, OUTCOMES, QUOTE_LOG_COLUMNS, RFQ_COLUMNS, ROOT,
                      SUPPLIER_REQUEST_COLUMNS, ComplianceHoldError,
                      apply_default_margins, approval_request_email, approve_quote, build_quote_lines,
                      carry_forward, compliance_hold, cover_email, customer_lines, default_deviations,
                      generate_quote, list_parsed_ids, load_parsed, load_products, load_seller, logo_path,
                      mailto_link, mark_quote_sent, next_version, parsed_rfq_is_draft, previous_quote_lines, price_quote,
                      quote_summary, read_csv, reject_quote, set_quote_outcome, suggest_products)
from app.ui import empty_state, page_header, status_badge, workflow_step, workflow_stepper

page_header("Quote builder", "Price each line, review the customer document, and check compliance before generating.")

parsed_ids = list_parsed_ids()
if not parsed_ids:
    empty_state("No quote-ready RFQ", "Add line items on the RFQs page, then record at least one supplier price per line.")
    st.caption("Next: open RFQs from the Deals section in the sidebar.")
    st.stop()

seller = load_seller()
if not seller["seller_company"]:
    st.warning("No company profile yet. Add your company details and logo on the Settings page.")

rfq_id = st.selectbox("RFQ", parsed_ids)
lines, missing = load_parsed(rfq_id)
if parsed_rfq_is_draft(rfq_id):
    st.error("COMPLIANCE HOLD · This tender conversion is an unreviewed draft. Replace the notice summary with verified customer line items and resolve the RFQ checklist before pricing.")
    st.info("Open RFQs → Start from this RFQ → edit the line items and questions → Save RFQ line items. Saving reviewed details clears the draft lock.")
    st.stop()
costs = read_csv("costs.csv", COST_COLUMNS)
rfqs = read_csv("rfqs.csv", RFQ_COLUMNS)
requests = read_csv("supplier_requests.csv", SUPPLIER_REQUEST_COLUMNS)
quotes_for_progress = read_csv("quotes.csv", QUOTE_LOG_COLUMNS)
rfq_match = rfqs[rfqs["rfq_id"].astype(str) == rfq_id] if not rfqs.empty else rfqs
rfq_status = str(rfq_match.iloc[0].get("status") or "") if not rfq_match.empty else ""
rfq_costs = costs[costs["rfq_id"].astype(str) == rfq_id] if not costs.empty else costs
request_count = int((requests["rfq_id"].astype(str) == rfq_id).sum()) if not requests.empty else 0
priced_line_count = int(rfq_costs["line"].astype(str).nunique()) if not rfq_costs.empty else 0
progress_quotes = quotes_for_progress[quotes_for_progress["rfq_id"].astype(str) == rfq_id].to_dict("records") \
    if not quotes_for_progress.empty else []
workflow_stepper(rfq_id, workflow_step(rfq_status, bool(lines), request_count, priced_line_count,
                                       len(lines), progress_quotes))
if missing:
    st.warning(f"{len(missing)} open question(s) on this RFQ. They are listed as deviations unless you edit them.")

base = build_quote_lines(lines, costs, rfq_id)
prev_version, prev_lines = previous_quote_lines(rfq_id)
use_prev = False
if prev_version:
    use_prev = st.checkbox(f"Start from v{prev_version} (keeps your margins, wording and models)", value=True)
if use_prev:
    base = carry_forward(base, prev_lines)
base = apply_default_margins(base)
st.caption(f"This will be v{next_version(rfq_id)}. Costs in EUR. Margin is a share of the sell price. "
           "Default tiers: 35% (< €1k), 25% standard, 18% (> €20k). Floor 12%.")

with st.container(border=True):
    st.subheader("Customer and commercial details")
    a, b, c = st.columns(3)
    customer_company = a.text_input("Customer company")
    contact_name = b.text_input("Contact name")
    customer_ref = c.text_input("Customer RFQ ref", value=rfq_id)
    incoterms_term = a.selectbox("Incoterms", INCOTERMS, index=1)
    incoterms_place = b.text_input("Incoterms place", placeholder="e.g. supplier warehouse, Hamburg")
    payment = c.text_input("Payment terms", value=seller["default_payment_terms"])
    customer_email = a.text_input("Customer email (for follow-ups)")
    documents = b.text_input("Documents included", value="Datasheet, certificate of conformity")
    include_terms = c.checkbox("Print terms and conditions", value=True)
    deviations = st.text_area("Notes / deviations", value=default_deviations(lines), height=100)

left, right = st.columns([1.05, 1], gap="large")
with left:
    with st.container(border=True):
        st.subheader("Internal · never sent")
        edited = st.data_editor(
            base, key=f"qb_{rfq_id}_{use_prev}", width="stretch", hide_index=True, num_rows="fixed",
            disabled=["line", "supplier_id", "unit_cost_eur", "freight_eur", "duty_eur"],
            column_config={
                "margin": st.column_config.NumberColumn("Margin", min_value=0.0, max_value=0.9, step=0.01, format="%.2f"),
                "qty": st.column_config.NumberColumn("Qty", min_value=0.0),
                "unit_cost_eur": st.column_config.NumberColumn("Unit cost (€)", format="€%.2f"),
                "freight_eur": st.column_config.NumberColumn("Freight (€)", format="€%.2f"),
                "duty_eur": st.column_config.NumberColumn("Duty (€)", format="€%.2f"),
            },
        )
        products = load_products()
        if not products.empty:
            with st.expander("Catalog suggestions per line"):
                for line in lines:
                    picks = suggest_products(line, products)
                    title = f"Line {line.get('line')}: {line.get('description') or line.get('item') or ''}"
                    if not picks:
                        st.caption(f"{title} — no catalog match")
                        continue
                    st.markdown(f"**{title}**")
                    st.dataframe(pd.DataFrame(picks)[["brand", "model", "score", "why", "typical_cost_eur",
                                                      "preferred_supplier_id"]], hide_index=True, width="stretch")
                st.caption("Suggestions are keyword matches. Confirm models and specifications against the datasheet.")

        approved = st.checkbox("Approved by Shayan (allows margin below 12%)")
        c1, c2 = st.columns(2)
        currencies = list(CURRENCIES)
        currency = c1.selectbox("Quote currency", currencies, index=currencies.index(seller["default_currency"]))
        fx_rate = 1.0
        if currency != "EUR":
            fx_rate = c2.number_input(f"1 EUR = ? {currency}", min_value=0.0001,
                                      value=0.87 if currency == "GBP" else 1.08, step=0.01, format="%.4f",
                                      help="Use today's rate plus a small buffer (1-2%) to protect your margin.")
        validity = st.number_input("Validity (days)", 1, 365, int(seller["default_validity_days"]))
        priced, error = None, None
        try:
            priced = price_quote(edited, approved_by_shayan=approved)
        except ValueError as exc:
            error = str(exc)

        symbol = CURRENCIES[currency]
        if error:
            st.error(error)
        else:
            summary = quote_summary(priced)
            m = st.columns(3)
            m[0].metric("Revenue", f"€{summary['subtotal']:,.2f}")
            m[1].metric("Gross profit", f"€{summary['gross_profit']:,.2f}")
            m[2].metric("Gross margin", f"{summary['margin']:.1%}")
            low_margin = priced[priced["margin"] < 0.12]
            if not low_margin.empty:
                st.warning(f"{len(low_margin)} line(s) are below 12%. Shayan's approval is required before generation.")
            st.dataframe(priced[["line", "supplier_id", "qty", "landed_unit_eur", "margin", "unit_price_eur",
                                 "total_eur"]], hide_index=True, width="stretch")
        st.caption("Supplier, landed cost and margin stay on this side of the screen and are never included in customer files.")
with right:
    with st.container(border=True):
        st.subheader(f"Customer document preview · {currency}")
        st.markdown(f"#### QUOTATION · Q-{rfq_id}-v{next_version(rfq_id)}")
        if logo_path(seller):
            st.image(str(logo_path(seller)), width=180)
        seller_name = seller.get("seller_company") or seller.get("seller_name") or "Company profile not set"
        seller_address = seller.get("seller_address") or ""
        seller_contacts = " · ".join(str(seller.get(k)) for k in ("seller_email", "seller_phone", "seller_website")
                                     if seller.get(k))
        st.write(f"**From:** {seller_name}" + (f" · {seller_address}" if seller_address else "")
                 + (f" · {seller_contacts}" if seller_contacts else ""))
        st.write(f"**To:** {customer_company or 'Customer company'} · {contact_name or 'Contact name'}")
        st.caption(f"Your ref: {customer_ref} · Date: {date.today().isoformat()} · "
                   f"Valid until: {(date.today() + timedelta(days=validity)).isoformat()} · Currency: {currency} · "
                   f"Incoterms: {incoterms_term} {incoterms_place}".strip())
        if priced is not None:
            view = pd.DataFrame(customer_lines(priced, fx_rate))
            st.dataframe(view, hide_index=True, width="stretch",
                         column_config={"unit_price": st.column_config.NumberColumn(f"Unit price ({symbol})", format=f"{symbol}%.2f"),
                                         "total": st.column_config.NumberColumn(f"Total ({symbol})", format=f"{symbol}%.2f")})
            st.metric("Subtotal (excl. VAT)", f"{symbol}{view['total'].sum():,.2f}")
        st.write(f"**Payment terms:** {payment or '—'}  ·  **Documents:** {documents or '—'}")
        st.write(f"**Notes / deviations:** {deviations or '—'}")
        st.caption("Prices exclude VAT unless stated. Subject to final confirmation of specifications and export compliance.")
        st.write(f"{seller.get('seller_name') or ''}  ·  {seller_name}")
        if include_terms and seller.get("terms_text"):
            with st.expander("Terms and conditions"):
                st.write(seller["terms_text"])

with st.container(border=True):
    st.subheader("Compliance check")
    d1, d2 = st.columns(2)
    destination = d1.text_input("Country of destination *")
    end_user = d2.text_input("End user *")
    held, message = compliance_hold(destination, end_user)
    status_badge("hold" if held else "clear", "compliance", "COMPLIANCE HOLD" if held else "Review before sending")
    if held:
        st.error(message)
    else:
        st.info(message)

needs_approval = seller.get("require_approval") == "yes"
if needs_approval:
    st.info(f"Approval required: this creates a watermarked DRAFT for {seller.get('approver_name') or 'the approver'}. "
            "The final PDF is produced after approval.")
label = "Create draft for approval" if needs_approval else "Generate quote (Markdown + PDF)"
if st.button(label, type="primary", disabled=held or priced is None):
    header = {"customer_company": customer_company, "contact_name": contact_name, "customer_email": customer_email,
              "customer_rfq_ref": customer_ref, "incoterms": f"{incoterms_term} {incoterms_place}".strip(),
              "payment_terms": payment, "documents": documents, "deviations": deviations,
              "terms": seller["terms_text"] if include_terms else "",
              **{k: seller[k] for k in ("seller_name", "seller_company", "seller_email", "seller_phone",
                                        "seller_address", "seller_website", "seller_vat")}}
    try:
        result = generate_quote(rfq_id, priced, header, destination=destination, end_user=end_user,
                                approved_by_shayan=approved, currency=currency, fx_rate=fx_rate,
                                validity_days=validity, logo=logo_path(seller), require_approval=needs_approval)
    except (ComplianceHoldError, ValueError, FileNotFoundError) as exc:
        st.error(str(exc))
    else:
        kind = "draft" if needs_approval else "quote"
        st.success(f"Created {kind} {result['quote_no']} ({currency} {result['subtotal_quote_ccy']:,.2f}). "
                   "Find it in the quote log below.")

st.subheader("Quote lifecycle")
st.caption("Draft → approval → cover email → sent → outcome. You remain in control of every external email.")
log = read_csv("quotes.csv", QUOTE_LOG_COLUMNS)
mine = log[log["rfq_id"].astype(str) == rfq_id] if not log.empty else log
if mine.empty:
    st.caption("No quotes generated for this RFQ yet.")
else:
    st.dataframe(mine[["quote_no", "generated_date", "approval_status", "currency", "value_quote_ccy", "margin",
                       "sent_date", "followup_count", "outcome", "lost_reason"]], hide_index=True, width="stretch")
    quote_nos = mine["quote_no"].astype(str).tolist()
    pick = st.selectbox("Selected quote", quote_nos, index=len(quote_nos) - 1)
    row = mine[mine["quote_no"].astype(str) == pick].iloc[0]
    status = str(row.get("approval_status") or "not_required").lower()
    status = "not_required" if status in {"nan", "none", ""} else status
    if status in {"pending", "approved", "rejected"}:
        status_badge(status, "approval")
    if not pd.isna(row.get("sent_date")):
        status_badge("quoted", "rfq", f"Sent · {row.get('sent_date')}")
    outcome_value = str(row.get("outcome") or "").lower()
    if outcome_value in {"won", "lost"}:
        status_badge(outcome_value, "rfq", f"Outcome · {outcome_value.title()}")
    pdf_path = ROOT / str(row["pdf_file"])
    if pdf_path.exists():
        st.download_button(f"Download {pdf_path.name}", pdf_path.read_bytes(), file_name=pdf_path.name,
                           mime="application/pdf", key=f"dl_{pick}")

    if status == "pending":
        st.warning(f"{pick} is waiting for approval.")
        with st.expander("Approval request email (internal: includes costs and margins)", expanded=True):
            try:
                req = approval_request_email(pick, seller)
                st.text_input("Subject", req["subject"], key=f"ar_s_{pick}")
                st.text_area("Body", req["body"], height=260, key=f"ar_b_{pick}")
                if req["to"]:
                    st.link_button("Open in Mail (attach the draft PDF)", mailto_link(req["to"], req["subject"],
                                                                                      req["body"]))
            except FileNotFoundError as exc:
                st.error(str(exc))
        a1, a2 = st.columns(2)
        approver = a1.text_input("Approved / rejected by", value=seller.get("approver_name", ""), key=f"apr_{pick}")
        note = a2.text_input("Note (required to reject)", key=f"apn_{pick}")
        b1, b2 = st.columns(2)
        if b1.button("Approve → create final PDF", type="primary", key=f"approve_{pick}"):
            try:
                approve_quote(pick, approver, note)
                st.rerun()
            except (ValueError, FileNotFoundError, KeyError) as exc:
                st.error(str(exc))
        if b2.button("Reject", key=f"reject_{pick}"):
            try:
                reject_quote(pick, approver, note)
                st.rerun()
            except (ValueError, KeyError) as exc:
                st.error(str(exc))
    elif status == "rejected":
        st.error(f"{pick} was rejected by {row.get('approver') or 'the approver'}: {row.get('approval_note')}. "
                 "Adjust the table above and create a new version.")
    else:
        if status == "approved":
            st.success(f"Approved by {row.get('approver')} on {row.get('approval_date')}.")
        with st.expander("Cover email to the customer", expanded=pd.isna(row.get("sent_date"))):
            try:
                cover = cover_email(pick, seller)
                st.text_input("Subject", cover["subject"], key=f"cv_s_{pick}")
                st.text_area("Body", cover["body"], height=240, key=f"cv_b_{pick}")
                if cover["to"]:
                    st.link_button("Open in Mail (attach the PDF)", mailto_link(cover["to"], cover["subject"],
                                                                               cover["body"]))
                else:
                    st.caption("No customer email on this quote. Copy the text above.")
            except (ValueError, FileNotFoundError) as exc:
                st.caption(f"Cover email unavailable: {exc}")
        q1, q2, q3 = st.columns([1, 1, 1])
        if pd.isna(row.get("sent_date")) and q1.button("Mark sent", help="After you emailed it yourself"):
            mark_quote_sent(pick)
            st.rerun()
        outcome = q2.selectbox("Outcome", OUTCOMES, key=f"oc_{pick}")
        reason = st.text_input("Lost reason (price, lead time, spec, no decision...)",
                               key=f"lr_{pick}") if outcome == "lost" else ""
        if q3.button("Save outcome", key=f"so_{pick}"):
            set_quote_outcome(pick, outcome, reason)
            st.success(f"{pick} marked {outcome}.")
            st.rerun()
