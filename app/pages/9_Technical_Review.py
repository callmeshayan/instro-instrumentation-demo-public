from datetime import datetime

import pandas as pd
import streamlit as st

from app.core import (RFQ_COLUMNS, TECHNICAL_REVIEW_COLUMNS, TECHNICAL_REVIEW_FIELDS,
                      TECHNICAL_REVIEW_STATUSES, list_parsed_ids, load_parsed, read_csv, write_csv)
from app.ui import empty_state, page_header

page_header("Technical review", "Compare requested instrument requirements with the proposed offer, line by line.")
st.caption("Record compliance decisions and evidence before a quote is finalized. Confirm every value against the customer request and supplier datasheet.")


def text_value(value):
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()

rfq_ids = list_parsed_ids()
if not rfq_ids:
    empty_state("No parsed RFQs yet", "Add a customer request and line items on the RFQs page to start a technical review.")
    st.stop()

rfq_id = st.selectbox("RFQ", rfq_ids, key="technical_review_rfq")
try:
    lines, _ = load_parsed(rfq_id)
except (ValueError, FileNotFoundError) as exc:
    st.error(f"Could not load {rfq_id}: {exc}")
    st.stop()

saved = read_csv("technical_reviews.csv", TECHNICAL_REVIEW_COLUMNS)
saved = saved[saved["rfq_id"].astype(str) == str(rfq_id)] if not saved.empty else saved
saved_by_key = {(str(row.get("line")), str(row.get("field"))): row.to_dict()
                for _, row in saved.iterrows()}
records = []
for line in lines:
    line_no = str(line.get("line", ""))
    for field, label in TECHNICAL_REVIEW_FIELDS:
        key = (line_no, field)
        prior = saved_by_key.get(key, {})
        requested_text = text_value(line.get(field, ""))
        initial_status = "Clarification needed" if not requested_text or requested_text.upper() == "TBC" else "Not reviewed"
        records.append({
            "line": line_no,
            "tag": text_value(line.get("tag")),
            "description": text_value(line.get("description") or line.get("item")),
            "requirement": label,
            "requested_value": requested_text,
            "offered_value": text_value(prior.get("offered_value")) or "",
            "status": text_value(prior.get("status")) or initial_status,
            "evidence_ref": text_value(prior.get("evidence_ref")) or "",
            "notes": text_value(prior.get("notes")) or "",
            "_field": field,
        })

if not records:
    empty_state("This RFQ has no line items", "Add or import line items on the RFQs page first.")
    st.stop()

frame = pd.DataFrame(records)
summary = st.columns(4)
summary[0].metric("Checks", len(frame))
summary[1].metric("Compliant", int((frame["status"] == "Compliant").sum()))
summary[2].metric("Deviations", int((frame["status"] == "Deviation").sum()))
summary[3].metric("Need review", int(frame["status"].isin(["Not reviewed", "Clarification needed"]).sum()))

st.info("Mark each proposed value against the source requirement. Use an exact datasheet page, clause, or document reference in Evidence.")
editable = frame[["line", "tag", "description", "requirement", "requested_value", "offered_value", "status", "evidence_ref", "notes"]].copy()
edited = st.data_editor(
    editable,
    key=f"technical_review_editor_{rfq_id}",
    width="stretch",
    hide_index=True,
    num_rows="fixed",
    disabled=["line", "tag", "description", "requirement", "requested_value"],
    column_config={
        "line": st.column_config.TextColumn("Line", width="small"),
        "tag": st.column_config.TextColumn("Tag", width="small"),
        "description": st.column_config.TextColumn("Customer item", width="medium"),
        "requirement": st.column_config.TextColumn("Requirement", width="medium"),
        "requested_value": st.column_config.TextColumn("Customer requirement", width="medium"),
        "offered_value": st.column_config.TextColumn("Proposed offer", width="medium", help="Enter the offered model/specification value."),
        "status": st.column_config.SelectboxColumn("Review result", options=TECHNICAL_REVIEW_STATUSES, required=True),
        "evidence_ref": st.column_config.TextColumn("Evidence reference", width="medium", help="Datasheet page, certificate, drawing, or supplier quote reference."),
        "notes": st.column_config.TextColumn("Reviewer notes", width="medium"),
    },
)

unfinished = edited[edited["status"].isin(["Not reviewed", "Clarification needed"])]
if len(unfinished):
    st.warning(f"{len(unfinished)} check(s) still need review or customer clarification.")
elif (edited["status"] == "Deviation").any():
    st.warning("Review deviations with engineering and record customer acceptance before treating the offer as compliant.")
else:
    st.success("All checks have a recorded disposition. Confirm evidence and approvals before issuing a quote.")

if st.button("Save technical review", type="primary", key=f"save_technical_review_{rfq_id}"):
    other_rfqs = read_csv("technical_reviews.csv", TECHNICAL_REVIEW_COLUMNS)
    if not other_rfqs.empty:
        other_rfqs = other_rfqs[other_rfqs["rfq_id"].astype(str) != str(rfq_id)]
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    saved_rows = []
    for index, row in edited.iterrows():
        saved_rows.append({
            "rfq_id": rfq_id,
            "line": str(row["line"]),
            "field": str(frame.iloc[index]["_field"]),
            "requested_value": row["requested_value"],
            "offered_value": row["offered_value"],
            "status": row["status"],
            "evidence_ref": row["evidence_ref"],
            "notes": row["notes"],
            "updated_at": stamp,
        })
    result = pd.concat([other_rfqs, pd.DataFrame(saved_rows, columns=TECHNICAL_REVIEW_COLUMNS)], ignore_index=True)
    write_csv("technical_reviews.csv", result[TECHNICAL_REVIEW_COLUMNS])
    st.success(f"Saved {len(saved_rows)} technical checks for {rfq_id}. A prior CSV version is backed up automatically.")
    st.rerun()
