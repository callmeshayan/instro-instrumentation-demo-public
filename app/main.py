"""Temporary privacy bootstrap page for the Streamlit Cloud deployment."""

import streamlit as st

st.set_page_config(
    page_title="Instro Demo Setup",
    page_icon="🔒",
    layout="centered",
)
st.title("Instro Dashboard")
st.info("The private company demo is being prepared. Access will be enabled by invitation.")
st.caption("No dashboard records are available on this setup page.")
