from pathlib import Path

import streamlit as st

from app.core import (CURRENCIES, DEFAULT_TERMS, backup_age_days, create_backup, latest_backup, load_seller,
                      logo_path, save_logo, save_seller)
from app.ui import page_header, section, toast

page_header("Settings", "Company identity and quote defaults used in customer documents.")
seller = load_seller()

profile_column, preview_column = st.columns([1.45, 1])
with profile_column:
    with st.form("seller_form"):
        st.subheader("Company identity")
        a, b = st.columns(2)
        values = {
            "seller_name": a.text_input("Your name (signature)", seller["seller_name"]),
            "seller_company": b.text_input("Company name", seller["seller_company"]),
            "seller_email": a.text_input("Email", seller["seller_email"]),
            "seller_phone": b.text_input("Phone", seller["seller_phone"]),
            "seller_address": a.text_input("Address", seller["seller_address"]),
            "seller_website": b.text_input("Website", seller["seller_website"]),
            "seller_vat": a.text_input("VAT / company number", seller["seller_vat"]),
        }

        st.divider()
        st.subheader("Quote defaults")
        c, d = st.columns(2)
        currencies = list(CURRENCIES)
        values["default_currency"] = c.selectbox("Default currency", currencies,
                                                 index=currencies.index(seller["default_currency"]))
        values["default_payment_terms"] = d.text_input("Payment terms", seller["default_payment_terms"])
        values["default_validity_days"] = str(c.number_input("Validity (days)", 1, 365,
                                                              int(seller["default_validity_days"])))
        values["terms_text"] = st.text_area("Terms and conditions", seller["terms_text"], height=130,
                                             help="Printed at the end of each quote.")

        st.divider()
        st.subheader("Approval policy")
        c1, c2 = st.columns(2)
        values["require_approval"] = "yes" if c1.checkbox(
            "Require approval for every quote", value=seller.get("require_approval") == "yes",
            help="Quotes are created as watermarked drafts. The final PDF is produced after approval.") else "no"
        values["approver_name"] = c1.text_input("Approver name", seller.get("approver_name", ""))
        values["approver_email"] = c2.text_input("Approver email", seller.get("approver_email", ""))
        values["logo_file"] = seller["logo_file"]
        saved = st.form_submit_button("Save company profile", type="primary", width="stretch")

    if saved:
        try:
            save_seller(values)
            toast("Company profile saved.", "✅")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

with preview_column:
    with section("Live quote header", "Preview of the saved company profile. Save edits to refresh this preview."):
        current_logo = logo_path(seller)
        if current_logo:
            st.image(str(current_logo), width=220)
        company = seller.get("seller_company") or "Company name"
        st.markdown(f"### {company}")
        details = [seller.get(key) for key in ("seller_address", "seller_email", "seller_phone", "seller_website",
                                                "seller_vat") if seller.get(key)]
        st.write("  \n".join(str(value) for value in details) if details else "Add your address and contact details.")
        st.divider()
        st.markdown("**QUOTATION · Q-EXAMPLE-v1**")
        st.caption(f"Currency: {seller['default_currency']} · Validity: {seller['default_validity_days']} days")
        st.caption(f"Payment: {seller['default_payment_terms'] or 'To be confirmed'}")

with section("Company logo", "Use a clear PNG or JPG that works on a white quote page."):
    current_logo = logo_path(seller)
    if current_logo:
        st.image(str(current_logo), width=220)
    upload = st.file_uploader("Upload logo", type=["png", "jpg", "jpeg"], key="seller_logo_upload")
    if upload is not None and st.button("Save logo", type="primary"):
        try:
            seller = load_seller()
            seller["logo_file"] = save_logo(upload.getvalue(), Path(upload.name).suffix)
            save_seller(seller)
            toast("Company logo saved.", "✅")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    if current_logo:
        confirmed_remove = st.checkbox("Confirm removing the logo from future quotes", key="confirm_logo_removal")
        if st.button("Remove logo", disabled=not confirmed_remove):
            seller = load_seller()
            seller["logo_file"] = ""
            save_seller(seller)
            toast("Company logo removed.", "✅")
            st.rerun()

with section("Data backup", "Create a local archive of your data, quotes, RFQs, and templates."):
    age = backup_age_days()
    last = latest_backup()
    if last is None:
        st.info("No backup has been created yet.")
    else:
        st.write(f"**Latest backup:** {last.name} · {age} day(s) ago")
    st.caption("Download the archive and keep a separate copy somewhere safe.")
    if st.button("Create backup", type="primary"):
        path = create_backup()
        st.session_state["backup_path"] = str(path)
        toast("Backup created.", "✅")
    if st.session_state.get("backup_path"):
        path = Path(st.session_state["backup_path"])
        if path.exists():
            st.download_button("Download backup (.zip)", path.read_bytes(), file_name=path.name,
                               mime="application/zip")

with st.expander("Restore default quote terms"):
    st.write("This replaces the saved terms and conditions with the standard terms text.")
    confirmed_reset = st.checkbox("Confirm resetting the terms", key="confirm_terms_reset")
    if st.button("Reset terms", disabled=not confirmed_reset):
        seller = load_seller()
        seller["terms_text"] = DEFAULT_TERMS
        save_seller(seller)
        toast("Default quote terms restored.", "✅")
        st.rerun()
