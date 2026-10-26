import pandas as pd
import streamlit as st

from app.core import PRODUCT_CATEGORIES, PRODUCT_COLUMNS, load_products, write_csv
from app.ui import empty_state, metric_cards, page_header, section, toast

page_header("Product catalog", "Maintain verified models for Quote Builder suggestions. Confirm every specification against its datasheet.")

products = load_products()
for column in PRODUCT_COLUMNS:
    if column != "typical_cost_eur":
        products[column] = products[column].astype("string")
products["typical_cost_eur"] = pd.to_numeric(products["typical_cost_eur"], errors="coerce")

with section("Catalog overview", "Only verified product facts belong in this internal reference."):
    category_count = int(products["category"].dropna().nunique()) if not products.empty else 0
    datasheet_count = int(products["datasheet_url"].fillna("").astype(str).str.strip().ne("").sum()) \
        if not products.empty else 0
    metric_cards([
        ("Verified models", str(len(products)), None),
        ("Categories", str(category_count), None),
        ("Datasheets linked", str(datasheet_count), "Add a verified datasheet link where available."),
    ])

with section("Find and edit products", "Edit cells directly. Add a row from the table footer, then save your changes."):
    f1, f2 = st.columns([2, 1])
    query = f1.text_input("Search catalog", placeholder="Brand, model, measurement, connection…", key="catalog_search")
    category_filter = f2.selectbox("Category", ["All categories"] + PRODUCT_CATEGORIES)
    view = products.copy()
    if query.strip():
        mask = view.astype(str).apply(lambda col: col.str.contains(query.strip(), case=False, na=False)).any(axis=1)
        view = view[mask]
    if category_filter != "All categories":
        view = view[view["category"].astype(str) == category_filter]

    if products.empty:
        empty_state("No catalog models yet", "Add a verified model in the editable table below. The Quote Builder will use it for suggestions.")
    elif view.empty:
        empty_state("No matching models", "Try another search term or choose a different category.")

    edited = st.data_editor(
        view, num_rows="dynamic", width="stretch", hide_index=True, key="catalog_editor",
        column_config={
            "category": st.column_config.SelectboxColumn("Category", options=PRODUCT_CATEGORIES),
            "datasheet_url": st.column_config.LinkColumn("Datasheet"),
            "typical_cost_eur": st.column_config.NumberColumn("Typical cost (€)", min_value=0, format="€%.2f"),
            "haz_cert": st.column_config.TextColumn("Hazardous-area certification"),
            "process_connection": st.column_config.TextColumn("Process connection"),
        },
    )

    visible_ids = set(view["product_id"].dropna().astype(str).str.strip()) - {""}
    edited_ids = set(edited["product_id"].dropna().astype(str).str.strip()) - {""}
    removed_ids = visible_ids - edited_ids
    confirmed = True
    if removed_ids:
        st.warning(f"{len(removed_ids)} existing product(s) will be removed from the catalog.")
        confirmed = st.checkbox("Confirm removing these product(s)", key="confirm_catalog_removal")

    if st.button("Save catalog", type="primary", disabled=bool(removed_ids) and not confirmed):
        hidden = products.loc[~products.index.isin(view.index)]
        result = pd.concat([hidden, edited], ignore_index=True)
        missing_id = result["product_id"].isna() | (result["product_id"].astype(str).str.strip() == "")
        if missing_id.any():
            used = {str(value) for value in result["product_id"].dropna()}
            next_id = 1
            assigned = []
            for _ in range(int(missing_id.sum())):
                while f"P{next_id:04d}" in used:
                    next_id += 1
                assigned.append(f"P{next_id:04d}")
                used.add(f"P{next_id:04d}")
                next_id += 1
            result.loc[missing_id, "product_id"] = assigned
        blank_model = result["model"].isna() | (result["model"].astype(str).str.strip() == "")
        if blank_model.any():
            st.error("Every product needs a model.")
        elif result["product_id"].astype(str).duplicated().any():
            st.error("Product IDs must be unique.")
        else:
            write_csv("products.csv", result[PRODUCT_COLUMNS])
            toast(f"Saved {len(result)} catalog product(s).", "✅")
            st.rerun()

with st.expander("Catalog data fields"):
    st.write("Identity: product ID, category, brand, model, measurement, range, accuracy, and output protocol.")
    st.write("Fit: process connection, wetted materials, hazardous-area certification, and ingress rating.")
    st.write("Reference: datasheet URL, notes, typical cost, and preferred supplier.")
