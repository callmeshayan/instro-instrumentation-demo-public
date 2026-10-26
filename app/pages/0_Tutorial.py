import streamlit as st

from app.ui import page_header, section

page_header("Demo tutorial", "A five-minute tour of the fictional instrumentation sales workflow.")

st.warning(
    "This is a public demo. Anyone can view the app, and changes may be visible to other visitors or reset. "
    "Use fictional information only—never enter real customer, supplier, pricing, or tender data."
)
st.info(
    "Recommended path: Today → Leads → RFQs → Costs → Quote Builder → Weekly review. "
    "DEMO-001 is preloaded with two fictional product lines and example supplier costs."
)

with section("1 · Start on Today", "See what needs attention and get a quick view of the pipeline."):
    st.write(
        "The cards show open leads, open RFQs, quotes sent this week, and estimated pipeline value. "
        "The action queue brings due follow-ups, quote approvals, supplier chasers, and backup reminders together."
    )
    st.caption("Use Today in the sidebar to return to the home page.")

with section("2 · Add a lead", "Create a fictional prospect and keep its next step visible."):
    st.write(
        "Use the lead form for a new company, or edit rows in the table. Company and country are required; "
        "an email address is optional but must be valid. Save changes after editing the table."
    )
    st.page_link("pages/1_Leads.py", label="Open Leads", icon=":material/group:")

with section("3 · Review or create an RFQ", "Turn a customer request into structured line items."):
    st.write(
        "Open DEMO-001 to inspect the parsed example, including its line items and review notes. "
        "For a new example, enter lines manually or import a CSV/Excel line list. You can also upload a PDF, "
        "email, or supported document for text extraction and review. Check every extracted field before quoting."
    )
    st.caption(
        "Scanned-PDF OCR uses macOS Vision and is unavailable on this hosted Linux app. "
        "PDFs with selectable text can still be extracted."
    )
    st.page_link("pages/2_RFQs.py", label="Open RFQs", icon=":material/inbox:")

with section("4 · Record supplier costs", "Compare offers by line and calculate landed unit cost."):
    st.write(
        "DEMO-001 already has fictional costs for both lines. Select an RFQ and line to add or update an offer. "
        "Supplier emails can be drafted and opened in your mail app; the dashboard never sends them automatically."
    )
    st.page_link("pages/3_Costs.py", label="Open Costs", icon=":material/payments:")

with section("5 · Build a quote", "Review internal pricing separately from the customer-facing quote."):
    st.write(
        "Select DEMO-001, enter a fictional customer, destination, and end user, then review the internal cost and "
        "margin table beside the customer prices. The default margin tiers are 35% below €1,000, 25% standard, "
        "and 18% above €20,000; the 12% floor blocks generation unless the approval control is enabled. "
        "Restricted or blank compliance details place the quote on hold."
    )
    st.write(
        "Generate creates a Markdown and PDF quote. The customer PDF omits internal costs and margins. "
        "You can draft a cover email, then mark the quote sent only after you send it yourself."
    )
    st.page_link("pages/4_Quote_Builder.py", label="Open Quote Builder", icon=":material/receipt_long:")

with section("6 · Track activity", "Use the review page to understand movement through the pipeline."):
    st.write(
        "Choose a date range to view recorded lead contacts, RFQs, supplier requests, quotes, outcomes, win rate, "
        "margin, and weekly activity. The reports only become useful after the demo workflow has been used."
    )
    st.page_link("pages/6_Review.py", label="Open Weekly review", icon=":material/insights:")

with section("7 · Explore tenders", "Search public procurement notices when you choose to run a search."):
    st.write(
        "The tender page searches TED and selected UK portals on demand. Results are public notices, not verified "
        "sales opportunities. Review the original notice and attachments before creating an RFQ."
    )
    st.page_link("pages/8_Tenders.py", label="Open Tenders", icon=":material/travel_explore:")

with st.expander("Catalog, settings, and demo limits"):
    st.write(
        "Catalog stores verified product references used for suggestions. Settings contains the quote header, "
        "terms, approval options, and downloadable backups. The hosted demo uses temporary local files, so edits "
        "may reset when the app restarts; backups are available under Settings. This is a walkthrough, not a "
        "production CRM or secure data store."
    )
    left, right = st.columns(2)
    left.page_link("pages/7_Catalog.py", label="Open Catalog", icon=":material/inventory_2:")
    right.page_link("pages/5_Settings.py", label="Open Settings", icon=":material/settings:")
