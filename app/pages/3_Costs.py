from datetime import date, timedelta

import pandas as pd
import streamlit as st

from app.core import (COST_COLUMNS, RFQ_COLUMNS, SUPPLIER_COLUMNS, ComplianceHoldError, landed_unit,
                      QUOTE_LOG_COLUMNS, SUPPLIER_REQUEST_COLUMNS, list_parsed_ids, load_parsed, load_seller,
                      log_supplier_requests, mailto_link, read_csv,
                      supplier_request_email, upsert_cost, write_csv)
from app.ui import empty_state, page_header, workflow_step, workflow_stepper

page_header("Supplier costs", "Draft supplier requests, record offers, and compare landed cost per line in EUR.")

costs = read_csv("costs.csv", COST_COLUMNS)
suppliers = read_csv("suppliers.csv", SUPPLIER_COLUMNS)
rfqs = read_csv("rfqs.csv", RFQ_COLUMNS)

rfq_ids = sorted(set(rfqs["rfq_id"].dropna().astype(str)) | set(list_parsed_ids()))
supplier_ids = suppliers["supplier_id"].dropna().astype(str).tolist() if not suppliers.empty else []
labels = {sid: f"{sid} — {name}" for sid, name in
          zip(supplier_ids, suppliers["company"].fillna("").astype(str))} if supplier_ids else {}

if not rfq_ids:
    empty_state("Add an RFQ before pricing", "Register a customer request to see line items and capture supplier offers here.")
    st.caption("Next: open RFQs from the Deals section in the sidebar.")
    st.stop()
if not supplier_ids:
    st.warning("No suppliers in data/suppliers.csv yet. Add one below.")

rfq_id = st.selectbox("RFQ", rfq_ids)
line_options: list[str] = []
line_labels: dict[str, str] = {}
if rfq_id in list_parsed_ids():
    try:
        lines, _ = load_parsed(rfq_id)
        for line in lines:
            key = str(line.get("line"))
            line_options.append(key)
            line_labels[key] = f"{key} — {line.get('tag') or ''} {line.get('description') or line.get('item') or ''}".strip()
    except (ValueError, FileNotFoundError) as exc:
        st.error(str(exc))

parsed_lines = []
if rfq_id in list_parsed_ids():
    try:
        parsed_lines, _ = load_parsed(rfq_id)
    except (ValueError, FileNotFoundError) as exc:
        st.error(f"Could not load line items for {rfq_id}: {exc}")

current = costs[costs["rfq_id"].astype(str) == rfq_id].copy() if not costs.empty else costs.copy()
requests = read_csv("supplier_requests.csv", SUPPLIER_REQUEST_COLUMNS)
quotes = read_csv("quotes.csv", QUOTE_LOG_COLUMNS)
request_count = int((requests["rfq_id"].astype(str) == rfq_id).sum()) if not requests.empty else 0
priced_lines = int(current["line"].astype(str).nunique()) if not current.empty else 0
quote_rows = quotes[quotes["rfq_id"].astype(str) == rfq_id].to_dict("records") if not quotes.empty else []
stored_status = ""
if not rfqs.empty:
    match = rfqs[rfqs["rfq_id"].astype(str) == rfq_id]
    stored_status = str(match.iloc[0].get("status") or "") if not match.empty else ""
workflow_stepper(rfq_id, workflow_step(stored_status, bool(parsed_lines), request_count,
                                       priced_lines, len(parsed_lines), quote_rows))

coverage = []
for parsed_line in parsed_lines:
    line_no = str(parsed_line.get("line"))
    offers = current[current["line"].astype(str) == line_no] if not current.empty else current
    if offers.empty:
        coverage.append({"Line": line_no, "Item": parsed_line.get("description") or parsed_line.get("item") or "",
                         "Offers": 0, "Best supplier": "—", "Landed / unit (€)": None, "Status": "Needs price"})
        continue
    landed = offers.apply(landed_unit, axis=1)
    best_idx = landed.idxmin()
    best = offers.loc[best_idx]
    coverage.append({"Line": line_no, "Item": parsed_line.get("description") or parsed_line.get("item") or "",
                     "Offers": len(offers), "Best supplier": str(best.get("supplier_id") or "—"),
                     "Landed / unit (€)": float(landed.loc[best_idx]), "Status": "Priced"})
if parsed_lines:
    with st.container(border=True):
        st.subheader("Line price coverage")
        missing_lines = [row["Line"] for row in coverage if row["Status"] == "Needs price"]
        if missing_lines:
            st.warning(f"Prices still needed for line(s): {', '.join(missing_lines)}")
        else:
            st.success("Every RFQ line has at least one supplier price.")
        coverage_frame = pd.DataFrame(coverage)
        def highlight_best(row):
            color = "background-color: #e8f5ee" if row["Status"] == "Priced" else "background-color: #fff3f0"
            return [color] * len(row)
        styled = coverage_frame.style.apply(highlight_best, axis=1)
        st.dataframe(styled, width="stretch", hide_index=True,
                     column_config={"Landed / unit (€)": st.column_config.NumberColumn(format="€%.2f")})

    st.subheader("Supplier comparison")
    st.caption("Compare landed cost, lead time, validity, and supplier qualification for each line. Lowest price is a reference, not an automatic award decision.")
    supplier_names = suppliers.set_index(suppliers["supplier_id"].astype(str))["company"].fillna("").to_dict() \
        if not suppliers.empty else {}
    supplier_qualified = suppliers.set_index(suppliers["supplier_id"].astype(str))["authorized"].fillna("unknown").to_dict() \
        if not suppliers.empty else {}
    for parsed_line in parsed_lines:
        line_no = str(parsed_line.get("line"))
        offers = current[current["line"].astype(str) == line_no].copy() if not current.empty else current.copy()
        title = f"Line {line_no} · {parsed_line.get('tag') or ''} {parsed_line.get('description') or parsed_line.get('item') or ''}".strip()
        with st.expander(title, expanded=len(parsed_lines) == 1):
            if offers.empty:
                st.info("No supplier offers saved for this line yet.")
                continue
            offers["landed_unit_eur"] = offers.apply(landed_unit, axis=1)
            qty = pd.to_numeric(pd.Series([parsed_line.get("quantity")]), errors="coerce").fillna(0).iloc[0]
            offers["extended_landed_eur"] = offers["landed_unit_eur"] * float(qty)
            offers["supplier"] = offers["supplier_id"].astype(str).map(supplier_names).fillna("")
            offers["authorized status"] = offers["supplier_id"].astype(str).map(supplier_qualified).fillna("unknown")
            validity = pd.to_datetime(offers["valid_until"], errors="coerce")
            offers["price validity"] = offers["valid_until"].fillna("").astype(str).replace("nan", "")
            offers.loc[validity.notna() & (validity.dt.date < date.today()), "price validity"] += " · EXPIRED"
            offers = offers.sort_values("landed_unit_eur")
            view = offers.rename(columns={"supplier_id": "Supplier ID", "supplier": "Supplier",
                                          "model": "Model offered", "unit_cost_eur": "Unit cost (€)",
                                          "freight_eur": "Freight (€)", "duty_eur": "Duty (€)",
                                          "landed_unit_eur": "Landed / unit (€)",
                                          "extended_landed_eur": "Extended landed (€)",
                                          "lead_time": "Lead time", "authorized status": "Authorized",
                                          "price validity": "Price validity", "quote_ref": "Supplier ref"})
            columns = ["Supplier", "Supplier ID", "Model offered", "Unit cost (€)", "Freight (€)", "Duty (€)",
                       "Landed / unit (€)", "Extended landed (€)", "Lead time", "Authorized", "Price validity", "Supplier ref"]
            st.dataframe(view[columns], width="stretch", hide_index=True,
                         column_config={column: st.column_config.NumberColumn(format="€%.2f") for column in
                                        ["Unit cost (€)", "Freight (€)", "Duty (€)", "Landed / unit (€)", "Extended landed (€)"]})
            st.caption("Authorized status is supplier-record information. Verify the scope and validity of authorization for the proposed brand and country.")

st.subheader("1. Request prices from suppliers")
if not line_options:
    st.caption("Supplier requests need the RFQ's line items (enter them on the RFQs page).")
elif not supplier_ids:
    st.caption("Add suppliers first (bottom of this page).")
else:
    parsed_lines, _ = load_parsed(rfq_id)
    r1, r2 = st.columns(2)
    req_suppliers = r1.multiselect("Suppliers to ask", supplier_ids, format_func=lambda k: labels.get(k, k),
                                   key=f"req_sup_{rfq_id}")
    req_lines = r2.multiselect("Lines", line_options, default=line_options,
                               format_func=lambda k: line_labels.get(k, k), key=f"req_lines_{rfq_id}")
    r3, r4, r5 = st.columns(3)
    reply_by = r3.date_input("Reply by", value=date.today() + timedelta(days=3), key=f"req_by_{rfq_id}")
    req_destination = r4.text_input("Destination country *", key=f"req_dest_{rfq_id}")
    req_end_use = r5.text_input("End use", placeholder="e.g. power plant cooling water", key=f"req_use_{rfq_id}")
    req_documents = r3.text_input("Documents required", value="Datasheet, certificate of conformity",
                                  key=f"req_docs_{rfq_id}")
    include_end_user = r4.checkbox("Name the end user", key=f"req_inc_{rfq_id}",
                                   help="Off by default to protect your customer. Turn on if the supplier "
                                        "requires it for export approval.")
    req_end_user = r5.text_input("End user", key=f"req_eu_{rfq_id}") if include_end_user else ""
    if st.button("Draft supplier emails", key=f"req_draft_{rfq_id}"):
        chosen = [l for l in parsed_lines if str(l.get("line")) in req_lines]
        seller = load_seller()
        drafts = {}
        try:
            for sid in req_suppliers:
                supplier = suppliers[suppliers["supplier_id"].astype(str) == sid].iloc[0].to_dict()
                drafts[sid] = supplier_request_email(rfq_id, chosen, supplier, seller, reply_by=reply_by,
                                                     destination=req_destination, end_use=req_end_use,
                                                     documents=req_documents, end_user=req_end_user)
                drafts[sid]["email"] = "" if pd.isna(supplier.get("email")) else str(supplier.get("email"))
            if not req_suppliers:
                st.warning("Pick at least one supplier.")
            st.session_state[f"req_drafts_{rfq_id}"] = drafts
        except (ComplianceHoldError, ValueError) as exc:
            st.session_state.pop(f"req_drafts_{rfq_id}", None)
            st.error(str(exc))
    drafts = st.session_state.get(f"req_drafts_{rfq_id}", {})
    for sid, draft in drafts.items():
        with st.expander(f"{labels.get(sid, sid)}", expanded=len(drafts) == 1):
            st.text_input("Subject", draft["subject"], key=f"subj_{rfq_id}_{sid}")
            st.text_area("Body", draft["body"], height=260, key=f"body_{rfq_id}_{sid}")
            if draft["email"]:
                st.link_button("Open in Mail", mailto_link(draft["email"], draft["subject"], draft["body"]))
            else:
                st.caption("No email saved for this supplier. Copy the text above.")
    if drafts and st.button("I've sent these — log the requests", type="primary", key=f"req_log_{rfq_id}"):
        log_supplier_requests(rfq_id, list(drafts), req_lines, reply_by=reply_by, destination=req_destination,
                              end_use=req_end_use, include_end_user=include_end_user)
        st.session_state.pop(f"req_drafts_{rfq_id}", None)
        st.success(f"Logged {len(drafts)} request(s). Overdue replies will show on the Today page.")

st.subheader("2. Enter supplier prices")
with st.form("cost_form", clear_on_submit=True):
    a, b, c = st.columns(3)
    if line_options:
        line = a.selectbox("Line *", line_options, format_func=lambda k: line_labels.get(k, k))
    else:
        line = a.text_input("Line *", help="No parsed file for this RFQ; type the line number.")
    supplier = b.selectbox("Supplier *", supplier_ids, format_func=lambda k: labels.get(k, k)) if supplier_ids \
        else b.text_input("Supplier ID *")
    model = c.text_input("Brand / model offered")
    unit_cost = a.number_input("Unit cost € *", min_value=0.0, step=10.0, format="%.2f")
    freight = b.number_input("Freight € per unit", min_value=0.0, step=1.0, format="%.2f")
    duty = c.number_input("Duty € per unit", min_value=0.0, step=1.0, format="%.2f")
    lead = a.text_input("Lead time", placeholder="e.g. 4-6 weeks")
    ref = b.text_input("Supplier quote ref")
    valid = c.date_input("Supplier price valid until", value=date.today() + timedelta(days=30))
    submitted = st.form_submit_button("Save cost", type="primary")
if submitted:
    try:
        updated = upsert_cost(costs, {"rfq_id": rfq_id, "line": line, "supplier_id": supplier,
                                      "model": model.strip() or pd.NA, "unit_cost_eur": unit_cost,
                                      "freight_eur": freight, "duty_eur": duty, "lead_time": lead.strip() or pd.NA,
                                      "quote_ref": ref.strip() or pd.NA, "valid_until": valid.isoformat()})
        write_csv("costs.csv", updated)
        st.success(f"Saved cost for {rfq_id} line {line} from {supplier}.")
        st.rerun()
    except ValueError as exc:
        st.error(str(exc))

st.subheader(f"Costs for {rfq_id}")
if current.empty:
    st.caption("No costs entered yet.")
else:
    current["landed_unit_eur"] = [landed_unit(r) for _, r in current.iterrows()]
    valid_until = pd.to_datetime(current["valid_until"], errors="coerce")
    expired = valid_until.notna() & (valid_until < pd.Timestamp(date.today()))
    if expired.any():
        st.warning(f"{int(expired.sum())} supplier price(s) have expired. Re-confirm before quoting.")
    st.dataframe(current.sort_values(["line", "landed_unit_eur"]), width="stretch", hide_index=True)

with st.expander("Add a supplier"):
    with st.form("supplier_form", clear_on_submit=True):
        a, b = st.columns(2)
        sid = a.text_input("Supplier ID *", placeholder="S002")
        company = b.text_input("Company *")
        country = a.text_input("Country")
        email = b.text_input("Email")
        contact = a.text_input("Contact name")
        brands = b.text_input("Brands")
        authorized = a.selectbox("Authorized distributor?", ["yes", "no", "unknown"])
        add = st.form_submit_button("Add supplier")
    if add:
        if not sid.strip() or not company.strip():
            st.error("Supplier ID and company are required.")
        elif sid.strip() in supplier_ids:
            st.error(f"{sid.strip()} already exists.")
        else:
            row = {c: pd.NA for c in SUPPLIER_COLUMNS}
            row.update({"supplier_id": sid.strip(), "company": company.strip(), "country": country.strip(),
                        "email": email.strip(), "contact": contact.strip(), "brands": brands.strip(),
                        "authorized": authorized})
            write_csv("suppliers.csv", pd.concat([suppliers, pd.DataFrame([row])], ignore_index=True))
            st.success(f"Added {company.strip()}.")
            st.rerun()
