import uuid

import pandas as pd
import streamlit as st

from app.core import LEAD_COLUMNS, read_csv, valid_email, write_csv
from app.ui import empty_state, page_header

page_header("Leads", "Build your pipeline and keep the next customer action visible.")
frame = read_csv("leads.csv", LEAD_COLUMNS)
if frame.empty:
    empty_state("Your lead list is ready", "Add the first company below. Lead status and next-action dates will appear here after saving.")

with st.expander("Filters", expanded=True):
    cols = st.columns(4)
    filters = {}
    for col, name in zip(cols, ["status", "stage", "country", "sector"]):
        options = sorted(str(x) for x in frame[name].dropna().unique()) if name in frame else []
        filters[name] = col.multiselect(name.title(), options)
filtered = frame.copy()
for name, selected in filters.items():
    if selected and name in filtered:
        filtered = filtered[filtered[name].astype(str).isin(selected)]

edited = st.data_editor(
    filtered, num_rows="dynamic", width="stretch", hide_index=True,
    key="lead_editor", column_config={
        "estimated_value_eur": st.column_config.NumberColumn("Estimated value (€)", min_value=0, format="%.2f"),
        "last_contact": st.column_config.DateColumn("Last contact"),
        "next_action_date": st.column_config.DateColumn("Next action date"),
    },
)
if st.button("Save changes", type="primary"):
    # Preserve rows hidden by filters while replacing the visible slice.
    if filters and any(filters.values()):
        hidden = frame.loc[~frame.index.isin(filtered.index)].copy()
        edited = pd.concat([hidden, edited], ignore_index=True)
    for column in LEAD_COLUMNS:
        if column not in edited:
            edited[column] = pd.NA
    edited = edited[LEAD_COLUMNS + [c for c in edited.columns if c not in LEAD_COLUMNS]]
    write_csv("leads.csv", edited)
    st.success("Leads saved.")

st.subheader("Add lead")
with st.form("add_lead", clear_on_submit=True):
    a, b = st.columns(2)
    company = a.text_input("Company *")
    country = b.text_input("Country *")
    contact = a.text_input("Contact name")
    email = b.text_input("Email")
    sector = a.text_input("Sector")
    stage = b.selectbox("Stage", ["Prospecting", "Qualified", "RFQ", "Quote sent", "Negotiation"])
    value = a.number_input("Estimated value (€)", min_value=0.0, step=100.0)
    notes = b.text_input("Notes")
    submitted = st.form_submit_button("Add lead")
if submitted:
    errors = []
    if not company.strip():
        errors.append("Company is required.")
    if not country.strip():
        errors.append("Country is required.")
    if email.strip() and not valid_email(email):
        errors.append("Enter a valid email address.")
    if errors:
        for error in errors:
            st.error(error)
    else:
        added = {column: pd.NA for column in LEAD_COLUMNS}
        added.update({"lead_id": str(uuid.uuid4()), "company": company.strip(), "contact_name": contact.strip(),
                      "email": email.strip(), "country": country.strip(), "sector": sector.strip(),
                      "status": "open", "stage": stage, "estimated_value_eur": value, "notes": notes.strip()})
        result = pd.concat([frame, pd.DataFrame([added])], ignore_index=True)
        write_csv("leads.csv", result)
        st.success(f"Added {company.strip()}.")
        st.rerun()
