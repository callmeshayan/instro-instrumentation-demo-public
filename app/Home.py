from datetime import date

import pandas as pd
import streamlit as st

from app.core import (COST_COLUMNS, LEAD_COLUMNS, QUOTE_LOG_COLUMNS, RFQ_COLUMNS, SUPPLIER_COLUMNS,
                      SUPPLIER_REQUEST_COLUMNS, TENDER_COLUMNS, awaiting_supplier_replies, backup_age_days, due_follow_ups,
                      followup_email, load_seller, mailto_link, mark_follow_up_done, mark_followup_done,
                      mark_supplier_chased, open_count, quote_followups_due, read_csv, tender_is_held, write_csv)
from app.ui import empty_state, metric_cards, page_header, section, status_badge, toast

page_header("Today", "Your next actions, ordered by urgency.")
today = date.today()
leads = read_csv("leads.csv", LEAD_COLUMNS)
rfqs = read_csv("rfqs.csv", RFQ_COLUMNS)
quotes = read_csv("quotes.csv", QUOTE_LOG_COLUMNS)
costs = read_csv("costs.csv", COST_COLUMNS)
requests = read_csv("supplier_requests.csv", SUPPLIER_REQUEST_COLUMNS)
suppliers = read_csv("suppliers.csv", SUPPLIER_COLUMNS)
tenders = read_csv("tenders.csv", TENDER_COLUMNS)
seller = load_seller()

week_start = today - pd.Timedelta(days=today.weekday())
sent_count = 0
if not quotes.empty and "sent_date" in quotes:
    sent_dates = pd.to_datetime(quotes["sent_date"], errors="coerce")
    sent_count = int((sent_dates.dt.normalize() >= pd.Timestamp(week_start)).sum())

pipeline_value = 0.0
if not leads.empty and "estimated_value_eur" in leads:
    values = pd.to_numeric(leads["estimated_value_eur"], errors="coerce").fillna(0)
    statuses = leads.get("status", pd.Series("", index=leads.index)).fillna("").astype(str).str.lower()
    pipeline_value = float(values[~statuses.isin({"won", "lost"})].sum())

metric_cards([
    ("Open leads", str(open_count(leads)), None),
    ("Open RFQs", str(open_count(rfqs)), None),
    ("Quotes sent this week", str(sent_count), None),
    ("Pipeline value", f"€{pipeline_value:,.2f}", "Open lead estimates, in EUR"),
])

if not tenders.empty:
    fresh = tenders[tenders["status"].fillna("new").astype(str).str.lower() == "new"]
    fresh = fresh.loc[~fresh.apply(tender_is_held, axis=1)] if not fresh.empty else fresh
    if not fresh.empty:
        deadlines = pd.to_datetime(fresh["deadline"], errors="coerce").dropna()
        nearest = deadlines.min().date().isoformat() if not deadlines.empty else "date unavailable"
        st.caption(f"{len(fresh)} new tenders, nearest deadline {nearest}")

queue: list[dict] = []

lead_due = due_follow_ups(leads, today)
for _, lead in lead_due.iterrows():
    due_date = pd.to_datetime(lead.get("next_action_date"), errors="coerce")
    due_day = due_date.date() if pd.notna(due_date) else today
    overdue_days = max(0, (today - due_day).days)
    queue.append({"kind": "lead", "data": lead.to_dict(),
                  "order": (0, 0 if overdue_days else 1, -overdue_days, due_day.isoformat())})

quote_due = quote_followups_due(quotes, today)
for _, quote in quote_due.iterrows():
    due_day = date.fromisoformat(str(quote["due_date"])[:10])
    overdue_days = max(0, (today - due_day).days)
    queue.append({"kind": "quote", "data": quote.to_dict(),
                  "order": (0, 0 if overdue_days else 1, -overdue_days, due_day.isoformat())})

waiting = awaiting_supplier_replies(requests, costs, today)
overdue_suppliers = waiting[waiting["days_overdue"] > 0] if not waiting.empty else waiting
for _, request in overdue_suppliers.iterrows():
    overdue_days = int(request["days_overdue"])
    queue.append({"kind": "supplier", "data": request.to_dict(),
                  "order": (0, 0, -overdue_days, str(request.get("reply_by", "")))})

if not quotes.empty:
    approvals = quotes[quotes["approval_status"].fillna("").astype(str).str.lower() == "pending"]
    for _, quote in approvals.iterrows():
        queue.append({"kind": "approval", "data": quote.to_dict(),
                      "order": (1, 0, 0, str(quote.get("generated_date", "")))})

backup_age = backup_age_days(today)
backup_due = backup_age is None or backup_age >= 7
if backup_due:
    queue.append({"kind": "backup", "data": {"age": backup_age},
                  "order": (2, 0, -(backup_age or 0), "")})

queue.sort(key=lambda item: item["order"])

with section("Action queue", "Handle overdue items first, then pending approvals and backup reminders."):
    has_sales_actions = any(item["kind"] != "backup" for item in queue)
    if not has_sales_actions:
        empty_state("Inbox zero", "Your sales action queue is clear: no due follow-ups, overdue supplier replies or pending approvals.")
        next_action, second_action = st.columns(2)
        next_action.page_link("pages/1_Leads.py", label="Add 5 leads", icon=":material/group_add:")
        second_action.page_link("pages/2_RFQs.py", label="Create an RFQ", icon=":material/add_box:")
    if queue:
        for item in queue:
            kind, data = item["kind"], item["data"]
            if kind == "lead":
                lead_id = str(data.get("lead_id", ""))
                company = str(data.get("company") or "Unnamed company")
                due_value = str(data.get("next_action_date") or "")
                due_date = pd.to_datetime(due_value, errors="coerce")
                overdue_days = max(0, (today - due_date.date()).days) if pd.notna(due_date) else 0
                label = "Overdue" if overdue_days else "Due today"
                with st.container(border=True):
                    left, middle, action = st.columns([1.1, 4.5, 1.5])
                    with left:
                        status_badge("overdue" if overdue_days else "due", "followup", label)
                    middle.markdown(f"**Lead follow-up · {company}**")
                    contact = str(data.get("contact_name") or "").strip()
                    middle.caption(f"{contact + ' · ' if contact else ''}Due {due_value}"
                                   + (f" · {overdue_days} day(s) overdue" if overdue_days else ""))
                    if action.button("Mark done", key=f"done_{lead_id}"):
                        try:
                            updated = mark_follow_up_done(leads, lead_id, today)
                            write_csv("leads.csv", updated)
                            toast(f"Follow-up with {company} marked done.", "✅")
                            st.rerun()
                        except (KeyError, ValueError) as exc:
                            st.error(str(exc))

            elif kind == "quote":
                quote = data
                draft = followup_email(quote, seller)
                quote_no = str(quote.get("quote_no", ""))
                title = (f"{quote_no} · {quote.get('customer_company') or 'customer'} · "
                         f"follow-up {draft['stage']} of 3 (due {quote['due_date']})")
                with st.container(border=True):
                    status_badge("overdue", "followup", "Quote follow-up")
                    with st.expander(title):
                        st.text_input("Subject", draft["subject"], key=f"fu_subj_{quote_no}")
                        st.text_area("Body", draft["body"], height=170, key=f"fu_body_{quote_no}")
                        c1, c2 = st.columns(2)
                        email = "" if pd.isna(quote.get("customer_email")) else str(quote.get("customer_email") or "")
                        if email:
                            c1.link_button("Open in Mail", mailto_link(email, draft["subject"], draft["body"]))
                        else:
                            c1.caption("No customer email on this quote. Copy the text.")
                        if c2.button("Follow-up sent", key=f"fu_done_{quote_no}"):
                            mark_followup_done(quote_no, today)
                            toast(f"Follow-up for {quote_no} logged.", "✅")
                            st.rerun()
                        if draft["stage"] == 3:
                            st.caption("Last scheduled follow-up. Afterwards, mark the quote won or lost in the Quote Builder.")

            elif kind == "supplier":
                request = data
                match = suppliers[suppliers["supplier_id"].astype(str) == str(request["supplier_id"])]
                supplier = match.iloc[0].to_dict() if not match.empty else {}
                supplier_name = str(supplier.get("company") or request["supplier_id"])
                request_id = str(request["request_id"])
                subject = f"Chaser: request for quotation {request['rfq_id']}"
                body = (f"Dear {supplier.get('contact') or 'Sales Team'},\n\nFollowing up on our request for quotation "
                        f"{request['rfq_id']} sent on {request['requested_date']}. Could you please send your offer, "
                        "or let us know when to expect it?\n\nBest regards,\n"
                        f"{seller.get('seller_name', '')}\n")
                overdue_days = int(request["days_overdue"])
                title = (f"{supplier_name} · {request['rfq_id']} · {overdue_days} day(s) overdue"
                         + (f" · chased {request['chased_date']}" if pd.notna(request.get("chased_date")) else ""))
                with st.container(border=True):
                    status_badge("overdue", "followup", "Supplier reply overdue")
                    with st.expander(title):
                        st.text_area("Chaser", body, height=140, key=f"ch_{request_id}")
                        c1, c2 = st.columns(2)
                        email = supplier.get("email")
                        if email and not pd.isna(email):
                            c1.link_button("Open in Mail", mailto_link(str(email), subject, body))
                        else:
                            c1.caption("No supplier email saved. Copy the text above.")
                        if c2.button("Chaser sent", key=f"ch_done_{request_id}"):
                            mark_supplier_chased(request_id, today)
                            toast(f"Chaser for {supplier_name} logged.", "✅")
                            st.rerun()

            elif kind == "approval":
                quote_no = str(data.get("quote_no") or "Quote")
                company = str(data.get("customer_company") or "customer")
                with st.container(border=True):
                    left, middle, action = st.columns([1.1, 4.5, 1.5])
                    with left:
                        status_badge("pending", "approval")
                    middle.markdown(f"**Approval needed · {quote_no}**")
                    middle.caption(f"{company} · Created {data.get('generated_date') or 'date unavailable'}")
                    action.page_link("pages/4_Quote_Builder.py", label="Review quote", icon=":material/rate_review:")

            elif kind == "backup":
                age = data["age"]
                age_text = "No backup yet" if age is None else f"Last backup was {age} days ago"
                with st.container(border=True):
                    left, middle, action = st.columns([1.1, 4.5, 1.5])
                    with left:
                        status_badge("backup", "action", "Backup")
                    middle.markdown("**Create a data backup**")
                    middle.caption(age_text + ". Keep a downloaded copy somewhere separate too.")
                    action.page_link("pages/5_Settings.py", label="Open Settings", icon=":material/backup:")
