from datetime import date

import pandas as pd
import streamlit as st

from app.core import (IMPORT_SYNONYMS, MANUAL_LINE_FIELDS, QUOTE_LOG_COLUMNS, RFQ_COLUMNS, RFQ_STATUSES,
                      SUPPLIER_REQUEST_COLUMNS, apply_mapping,
                      excel_sheets, extract_document_text, guess_mapping, list_inbox, list_parsed_ids, load_parsed, read_csv,
                      list_rfq_attachments, parsed_rfq_is_draft, read_import_table, rfq_overview, safe_rfq_id,
                      save_parsed_rfq, save_rfq_attachment, save_upload,
                      tender_review_complete, tbc_fields,
                      upsert_rfq, write_csv)
from app.ui import empty_state, page_header, workflow_step, workflow_stepper

page_header("RFQs", "Register customer requests, review line items, and track each deal through quoting.")

rfqs = read_csv("rfqs.csv", RFQ_COLUMNS)
costs = read_csv("costs.csv", ["rfq_id", "line"])
overview = rfq_overview(rfqs, costs)

st.subheader("Pipeline")
if overview.empty:
    empty_state("No RFQs yet", "Start from a customer line list or enter line items manually in the sections below.")
else:
    counts = overview["status"].value_counts()
    cols = st.columns(len(RFQ_STATUSES))
    for col, status in zip(cols, RFQ_STATUSES):
        col.metric(status.title(), int(counts.get(status, 0)))
    status_colors = {"new": ("#667085", "#f2f4f7"), "parsed": ("#175cd3", "#eff8ff"),
                     "priced": ("#3538cd", "#eef4ff"), "quoted": ("#1f3a5f", "#eaf0f6"),
                     "won": ("#067647", "#ecfdf3"), "lost": ("#667085", "#f2f4f7")}
    styled_overview = overview[RFQ_COLUMNS].style.map(
        lambda value: (f"color: {status_colors.get(str(value).lower(), ('#475467', '#f2f4f7'))[0]}; "
                       f"background-color: {status_colors.get(str(value).lower(), ('#475467', '#f2f4f7'))[1]}; "
                       "font-weight: 650") if value is not None else "", subset=["status"])
    st.dataframe(styled_overview, width="stretch", hide_index=True)

    chosen_rfq = st.selectbox("Track RFQ", overview["rfq_id"].astype(str).tolist(), key="rfq_track")
    selected = overview[overview["rfq_id"].astype(str) == chosen_rfq].iloc[0]
    parsed = list_parsed_ids()
    parsed_lines = []
    if chosen_rfq in parsed:
        try:
            parsed_lines = load_parsed(chosen_rfq)[0]
        except (ValueError, FileNotFoundError) as exc:
            st.warning(f"Could not read parsed line items for {chosen_rfq}: {exc}")
    all_costs = read_csv("costs.csv", ["rfq_id", "line", "supplier_id"])
    all_requests = read_csv("supplier_requests.csv", SUPPLIER_REQUEST_COLUMNS)
    all_quotes = read_csv("quotes.csv", QUOTE_LOG_COLUMNS)
    request_count = int((all_requests["rfq_id"].astype(str) == chosen_rfq).sum()) if not all_requests.empty else 0
    rfq_costs = all_costs[all_costs["rfq_id"].astype(str) == chosen_rfq] if not all_costs.empty else all_costs
    priced_lines = int(rfq_costs["line"].astype(str).nunique()) if not rfq_costs.empty else 0
    quote_rows = all_quotes[all_quotes["rfq_id"].astype(str) == chosen_rfq].to_dict("records") \
        if not all_quotes.empty else []
    workflow_stepper(chosen_rfq, workflow_step(str(selected["status"]), bool(parsed_lines), request_count,
                                              priced_lines, len(parsed_lines), quote_rows))

st.subheader("New RFQ · Import Excel / CSV")
st.caption("Upload an itemized customer spreadsheet. Keep unknown specifications marked as TBC.")
upload_x = st.file_uploader("Customer line list (.xlsx or .csv)", type=["xlsx", "xlsm", "csv"], key="import_file")
if upload_x is not None:
    raw = upload_x.getvalue()
    i1, i2, i3 = st.columns(3)
    sheet, readable = 0, True
    if upload_x.name.lower().endswith((".xlsx", ".xlsm")):
        try:
            sheets = excel_sheets(raw)
            sheet = i1.selectbox("Sheet", sheets, key="import_sheet")
        except Exception as exc:  # corrupt or password-protected workbook
            st.error(f"Cannot open workbook: {exc}")
            readable = False
    header_row = i2.number_input("Header row (as numbered in Excel)", min_value=1, max_value=50, value=1,
                                 key="import_header", help="The row that contains column names like Tag, Qty.")
    import_id = i3.text_input("RFQ ID *", key="import_id", placeholder="e.g. EAC-2026-014")
    table = None
    if readable:
        try:
            table = read_import_table(raw, upload_x.name, sheet, int(header_row))
        except Exception as exc:
            st.error(f"Cannot read the file with header row {header_row}: {exc}")
    if table is not None:
        st.caption(f"{len(table)} row(s) found. First rows:")
        st.dataframe(table.head(8), width="stretch", hide_index=True)
        columns = list(table.columns)
        guess = guess_mapping(columns)
        st.markdown("**Map columns** (auto-guessed — check them)")
        mapping = {}
        grid = st.columns(4)
        for i, field in enumerate(IMPORT_SYNONYMS):
            options = ["(none)"] + columns
            default = options.index(guess[field]) if guess.get(field) in options else 0
            label = field.replace("_", " ").capitalize() + (" *" if field in {"description", "quantity"} else "")
            picked = grid[i % 4].selectbox(label, options, index=default, key=f"map_{field}")
            mapping[field] = None if picked == "(none)" else picked
        import_customer = st.text_input("Customer", key="import_customer")
        import_overwrite = st.checkbox("Overwrite if this RFQ ID already exists", key="import_overwrite")
        try:
            preview_lines, skipped = apply_mapping(table, mapping, int(header_row))
        except ValueError as exc:
            preview_lines, skipped = [], []
            st.warning(str(exc))
        if preview_lines:
            st.caption(f"{len(preview_lines)} line(s) will be imported.")
            st.dataframe(pd.DataFrame(preview_lines), width="stretch", hide_index=True)
        if skipped:
            st.warning("Skipped rows:\n\n" + "\n".join(f"- {m}" for m in skipped))
        if st.button("Import as RFQ", type="primary", disabled=not preview_lines, key="import_save"):
            try:
                rid = safe_rfq_id(import_id)
                path = save_parsed_rfq(rid, preview_lines, overwrite=import_overwrite)
                saved_copy = save_upload(upload_x.name, raw)
                fields = {"received_date": date.today().isoformat(), "notes": f"Imported from {saved_copy.name}"}
                if import_customer.strip():
                    fields["customer"] = import_customer.strip()
                write_csv("rfqs.csv", upsert_rfq(read_csv("rfqs.csv", RFQ_COLUMNS), rid, **fields))
                st.success(f"Imported {len(preview_lines)} lines into {path.name}. Original saved to the inbox. "
                           "Review or add specs below, then request supplier prices on the Costs page.")
            except (ValueError, FileExistsError) as exc:
                st.error(str(exc))

st.subheader("New RFQ · Enter manually")
st.caption("Type the customer's items here instead of parsing with Claude. Mark unknown specs as TBC.")
existing = list_parsed_ids()
source = st.selectbox("Start from", ["New RFQ"] + existing, key="manual_source")
if source == "New RFQ":
    seed = pd.DataFrame([{field: None for field in MANUAL_LINE_FIELDS} | {"line": i} for i in (1, 2, 3)])
    seed_missing = ""
else:
    seed_lines, seed_missing_list = load_parsed(source)
    seed = pd.DataFrame([{f: line.get(f) for f in MANUAL_LINE_FIELDS} for line in seed_lines])
    seed_missing = "\n".join(m for m in seed_missing_list if not m.startswith("Line "))
for column in MANUAL_LINE_FIELDS:
    seed[column] = seed[column].map(lambda v: None if v is None or pd.isna(v) else str(v)).astype("string")
m1, m2, m3 = st.columns(3)
manual_id = m1.text_input("RFQ ID *", value="" if source == "New RFQ" else source, key="manual_id",
                          placeholder="e.g. EAC-2026-014")
manual_customer = m2.text_input("Customer", key="manual_customer")
manual_received = m3.date_input("Received", value=date.today(), key="manual_received")
manual_lines = st.data_editor(
    seed, key=f"manual_editor_{source}", num_rows="dynamic", width="stretch", hide_index=True,
    column_config={
        "line": st.column_config.TextColumn("Line", width="small"),
        "description": st.column_config.TextColumn("Description *", width="medium"),
        "quantity": st.column_config.TextColumn("Qty * (TBC allowed during review)"),
        "hazardous_area_cert": st.column_config.TextColumn("Haz. area cert"),
    },
)
manual_missing = st.text_area("Open questions for the customer (one per line)", value=seed_missing, key="manual_missing")
overwrite = st.checkbox("Overwrite if this RFQ ID already exists (old version is backed up)",
                        value=source != "New RFQ", key="manual_overwrite")

def save_manual_rfq(*, keep_locked: bool) -> None:
    try:
        rid = safe_rfq_id(manual_id)
        line_records = manual_lines.to_dict("records")
        questions = [m.strip() for m in manual_missing.splitlines() if m.strip()]
        is_locked_tender = source != "New RFQ" and parsed_rfq_is_draft(source)
        if is_locked_tender and not keep_locked and not tender_review_complete(line_records, questions):
            raise ValueError("Tender draft is still locked: replace the summary with verified line items, complete every review field and source reference, resolve all TBC values, and clear the open questions before confirming.")
        path = save_parsed_rfq(rid, line_records, questions, overwrite=overwrite,
                               allow_incomplete=keep_locked, draft=keep_locked)
        fields = {"received_date": manual_received.isoformat()}
        if manual_customer.strip():
            fields["customer"] = manual_customer.strip()
        write_csv("rfqs.csv", upsert_rfq(read_csv("rfqs.csv", RFQ_COLUMNS), rid, **fields))
        if keep_locked:
            st.success(f"Saved review progress to {path.name}. The quote lock remains in place.")
            st.rerun()
        st.success(f"Saved {path.name}. Tender review is confirmed; next: enter supplier costs on the Costs page.")
        st.rerun()
    except (ValueError, FileExistsError) as exc:
        st.error(str(exc))

if source != "New RFQ" and parsed_rfq_is_draft(source):
    review_cols = st.columns(2)
    if review_cols[0].button("Save review progress · keep quote lock", key="manual_save_progress"):
        save_manual_rfq(keep_locked=True)
    review_candidate = manual_lines.to_dict("records")
    question_candidate = [m.strip() for m in manual_missing.splitlines() if m.strip()]
    can_confirm = tender_review_complete(review_candidate, question_candidate)
    if review_cols[1].button("Confirm reviewed lines and unlock", type="primary", disabled=not can_confirm,
                             key="manual_confirm_review"):
        save_manual_rfq(keep_locked=False)
    st.caption("Unlock requires actual line descriptions, all checklist fields, a source document/page/clause for every line, numeric quantities, and no open questions.")
else:
    if st.button("Save RFQ line items", type="primary", key="manual_save"):
        save_manual_rfq(keep_locked=False)

st.subheader("Update RFQ status (won / lost / notes)")
with st.form("rfq_form", clear_on_submit=False):
    a, b, c = st.columns(3)
    rfq_id = a.text_input("RFQ ID *", help="Must match the parsed file name, e.g. RFQ-2026-001")
    customer = b.text_input("Customer")
    received = c.date_input("Received", value=date.today())
    status = a.selectbox("Status", ["(auto)"] + RFQ_STATUSES,
                         help="Leave on auto unless the RFQ is won or lost.")
    value = b.number_input("Estimated value (€)", min_value=0.0, step=100.0)
    notes = c.text_input("Notes")
    save = st.form_submit_button("Save RFQ", type="primary")
if save:
    try:
        rid = safe_rfq_id(rfq_id)
        fields = {"customer": customer.strip() or pd.NA, "received_date": received.isoformat(),
                  "notes": notes.strip() or pd.NA}
        if value:
            fields["value_eur"] = value
        if status != "(auto)":
            fields["status"] = status
        write_csv("rfqs.csv", upsert_rfq(rfqs, rid, **fields))
        st.success(f"Saved {rid}.")
        st.rerun()
    except ValueError as exc:
        st.error(str(exc))

st.subheader("Inbox")
uploads = st.file_uploader("Drop RFQ files (PDF, email, Excel, text)", accept_multiple_files=True,
                           type=["pdf", "eml", "msg", "txt", "docx", "xlsx", "xls", "csv"])
if uploads and st.button("Save to rfq/inbox"):
    for item in uploads:
        path = save_upload(item.name, item.getvalue())
        st.success(f"Saved {path.name}")
inbox = list_inbox()
if inbox:
    st.dataframe(pd.DataFrame([{"file": p.name, "size_kb": round(p.stat().st_size / 1024, 1)} for p in inbox]),
                 hide_index=True, width="stretch")
else:
    st.caption("The document inbox is empty. Upload a customer file above if you need to keep the original here.")

st.subheader("Parsed RFQ viewer")
parsed_ids = list_parsed_ids()
if not parsed_ids:
    empty_state("No quote-ready line items", "Import a line list or save line items manually above to prepare supplier requests and quotes.")
else:
    chosen = st.selectbox("Parsed RFQ", parsed_ids)
    try:
        lines, missing = load_parsed(chosen)
    except (ValueError, FileNotFoundError) as exc:
        st.error(f"Cannot read {chosen}.json: {exc}")
    else:
        if any(str(m).lower().startswith("expand the notice") for m in missing):
            st.warning("Tender-origin RFQ: confirm the full notice, expand the summary into actual line items, and resolve the checklist before requesting costs.")
        if missing:
            st.error("Missing / questions for the customer:\n\n" + "\n".join(f"- {m}" for m in missing))
        frame = pd.DataFrame([{k: (", ".join(map(str, v)) if isinstance(v, list) else v)
                               for k, v in line.items()} for line in lines])
        if frame.empty:
            st.warning("No line items found in this file.")
        else:
            def highlight(value):
                return "background-color: #f8d7da; color: #842029" if (
                    isinstance(value, str) and value.strip().upper() == "TBC") else ""
            styler = frame.style.map(highlight) if hasattr(frame.style, "map") else frame.style.applymap(highlight)
            st.dataframe(styler, width="stretch", hide_index=True)
            tbc_total = sum(len(tbc_fields(line)) for line in lines)
            st.caption(f"{len(lines)} line(s), {tbc_total} TBC field(s) highlighted in red.")
            st.page_link("pages/9_Technical_Review.py", label="Open technical review for these requirements",
                         icon=":material/fact_check:")

st.subheader("Tender attachments · local review")
st.caption("Upload documents you downloaded from the official notice. Files stay in this repo and are never sent automatically.")
attachment_ids = list_parsed_ids()
if attachment_ids:
    attachment_rfq = st.selectbox("RFQ for these source documents", attachment_ids, key="source_attachment_rfq")
    attachment_upload = st.file_uploader("Add a notice attachment", type=["pdf", "docx", "xlsx", "xlsm", "csv", "txt", "eml"],
                                         key="source_attachment_upload")
    if attachment_upload and st.button("Save attachment to this RFQ", key="source_attachment_save"):
        try:
            saved_path = save_rfq_attachment(attachment_rfq, attachment_upload.name, attachment_upload.getvalue())
            st.success(f"Saved {saved_path.name} under rfq/sources/{safe_rfq_id(attachment_rfq)}/")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    attachments = list_rfq_attachments(attachment_rfq)
    if attachments:
        for item in attachments:
            size_kb = round(item.stat().st_size / 1024, 1)
            st.markdown(f"**{item.name}** · {size_kb} KB")
            extract_key = f"extracted_text_{attachment_rfq}_{item.name}"
            action_col, download_col = st.columns([1, 1])
            if action_col.button("Extract text locally", key=f"extract_attachment_{attachment_rfq}_{item.name}"):
                try:
                    st.session_state[extract_key] = extract_document_text(item.name, item.read_bytes())
                except ValueError as exc:
                    st.error(str(exc))
            download_col.download_button(f"Open / download original · {item.name}", item.read_bytes(),
                                         file_name=item.name, mime="application/octet-stream",
                                         key=f"source_attachment_{attachment_rfq}_{item.name}")
            extracted = st.session_state.get(extract_key)
            if extracted:
                st.caption(f"{extracted['method']} · local only. Check every value against the original before using it.")
                if extracted.get("ocr_error"):
                    st.warning(f"macOS OCR could not run; pages without selectable text remain unread: {extracted['ocr_error']}")
                if extracted["has_text"]:
                    st.text_area(f"Extracted text · {item.name}", value=extracted["text"], height=260,
                                 disabled=True, key=f"text_preview_{attachment_rfq}_{item.name}")
                else:
                    st.warning("No text was recognized. The file may be image-only or too low-quality for OCR; check the original and enter verified details manually.")
    else:
        st.caption("No local source documents saved for this RFQ yet.")
else:
    st.caption("Save a parsed RFQ first, then attach its source documents here.")
