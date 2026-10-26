"""Application shell: shared page configuration and workflow navigation."""

import sys
from pathlib import Path

import streamlit as st

# Streamlit Cloud executes this entrypoint from app/, so add the repo root
# before importing the app package and resolving page files.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.ui import apply_global_styles

st.set_page_config(
    page_title="Instro Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)
apply_global_styles()
st.info('COMPANY DEMO · Fictional training records only. Do not enter or upload real customer, supplier, or tender data. Demo changes may reset.')

with st.sidebar:
    st.markdown("### INSTRO")
    st.caption("SALES OPERATIONS")
    st.divider()

today = st.Page("Home.py", title="Today", icon=":material/today:", default=True)
guide = st.Page("pages/0_Tutorial.py", title="Tutorial", icon=":material/menu_book:")
leads = st.Page("pages/1_Leads.py", title="Leads", icon=":material/group:")
rfqs = st.Page("pages/2_RFQs.py", title="RFQs", icon=":material/inbox:")
costs = st.Page("pages/3_Costs.py", title="Costs", icon=":material/payments:")
technical_review = st.Page("pages/9_Technical_Review.py", title="Technical review", icon=":material/fact_check:")
quote_builder = st.Page("pages/4_Quote_Builder.py", title="Quote Builder", icon=":material/receipt_long:")
review = st.Page("pages/6_Review.py", title="Weekly review", icon=":material/insights:")
catalog = st.Page("pages/7_Catalog.py", title="Catalog", icon=":material/inventory_2:")
settings = st.Page("pages/5_Settings.py", title="Settings", icon=":material/settings:")
tenders = st.Page("pages/8_Tenders.py", title="Tenders", icon=":material/travel_explore:")

page = st.navigation(
    {
        "Start here": [guide],
        "": [today],
        "Sales": [leads],
        "Deals": [rfqs, costs, technical_review, quote_builder],
        "Insights": [review, tenders],
        "Setup": [catalog, settings],
    },
    position="sidebar",
    expanded=True,
)
page.run()
