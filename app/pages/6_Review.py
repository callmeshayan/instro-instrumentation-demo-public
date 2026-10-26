from datetime import date, timedelta

import pandas as pd
import streamlit as st

from app.core import review_metrics, weekly_activity
from app.ui import empty_state, metric_cards, page_header, section

page_header("Weekly review", "A concise view of pipeline activity, quote outcomes, and the next improvement to make.")

with section("Reporting period", "Choose the dates used for the results below."):
    period = st.radio("Period", ["This week", "Last 7 days", "Last 30 days", "Last 90 days", "Custom"],
                      horizontal=True, label_visibility="collapsed")
    today = date.today()
    if period == "This week":
        start, end = today - timedelta(days=today.weekday()), today
    elif period == "Custom":
        c1, c2 = st.columns(2)
        start = c1.date_input("From", today - timedelta(days=30))
        end = c2.date_input("To", today)
    else:
        days = {"Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90}[period]
        start, end = today - timedelta(days=days - 1), today

if start > end:
    st.error("The start date must be on or before the end date.")
    st.stop()

m = review_metrics(start, end)
st.caption(f"Reporting {start.isoformat()} to {end.isoformat()}")

with section("Activity", "Pipeline work recorded during this period."):
    metric_cards([
        ("Leads contacted", str(m["lead_contacts"]), None),
        ("RFQs received", str(m["rfqs_received"]), None),
        ("Supplier requests", str(m["supplier_requests"]), None),
        ("Quotes sent", str(m["quotes_sent"]), None),
    ])

with section("Commercial results", "Sent quotes and recorded decisions for this period."):
    metric_cards([
        ("Won / lost", f"{m['won']} / {m['lost']}", None),
        ("Win rate", "—" if m["win_rate"] is None else f"{m['win_rate']:.0%}", "Won divided by won plus lost."),
        ("Won value", f"€{m['won_value_eur']:,.2f}", "Recorded quote value in EUR."),
        ("Won gross profit", f"€{m['won_profit_eur']:,.2f}", "Value multiplied by recorded quote margin."),
    ])
    metric_cards([
        ("Value quoted", f"€{m['value_sent_eur']:,.2f}", "Value of quotes sent during this period."),
        ("Average margin", "—" if m["avg_margin_sent"] is None else f"{m['avg_margin_sent']:.1%}",
         "Average recorded margin on sent quotes."),
        ("Open quote pipeline", f"€{m['open_pipeline_eur']:,.2f}", "All sent quotes without a recorded outcome."),
    ])

charts = st.columns(2)
with charts[0]:
    with section("Activity trend", "Weekly counts for the last eight weeks."):
        weekly = weekly_activity(8).reset_index()
        weekly = weekly.rename(columns={"week": "Week starting"})
        if weekly[["RFQs", "Quotes sent", "Won"]].to_numpy().sum() == 0:
            empty_state("No recorded activity", "Log RFQs and quote outcomes to build the weekly trend.")
        else:
            st.bar_chart(weekly, x="Week starting", y=["RFQs", "Quotes sent", "Won"],
                         x_label="Week starting", y_label="Count", sort=False,
                         alt="RFQs received, quotes sent, and quotes won by week.")
with charts[1]:
    with section("Lost quote reasons", "Recorded reasons help identify where to improve."):
        if m["lost_reasons"]:
            losses = pd.DataFrame([{"Reason": reason, "Quotes": count}
                                   for reason, count in m["lost_reasons"].items()])
            losses = losses.sort_values("Quotes", ascending=True)
            st.bar_chart(losses, x="Reason", y="Quotes", x_label="Reason", y_label="Quotes lost",
                         horizontal=True, sort=False, alt="Lost quote count by recorded reason.")
        else:
            empty_state("No lost quotes in this period", "Record an outcome and reason in Quote Builder to see patterns here.")

with section("Next action", "A short prompt based on the numbers above."):
    hints = []
    if m["quotes_sent"] == 0 and m["rfqs_received"] == 0:
        hints.append(("Build the top of the funnel", "There were no RFQs or quotes in this period. Prioritize lead outreach."))
    elif m["rfqs_received"] > 0 and m["quotes_sent"] < m["rfqs_received"]:
        hints.append(("Speed up quote preparation", "Some RFQs have not become sent quotes. Check supplier price coverage on Today."))
    if m["win_rate"] is not None and m["win_rate"] < 0.2 and (m["won"] + m["lost"]) >= 5:
        hints.append(("Review lost quote reasons", "Win rate is below 20% across at least five decisions. Compare price, lead time, and specification feedback."))
    if m["avg_margin_sent"] is not None and m["avg_margin_sent"] < 0.15:
        hints.append(("Review low-margin quotes", "Average sent margin is below 15%. Confirm these quotes were deliberate."))
    if hints:
        for title, detail in hints:
            st.warning(f"**{title}.** {detail}")
    else:
        st.success("No immediate issue surfaced. Keep the pipeline current and review again next week.")
