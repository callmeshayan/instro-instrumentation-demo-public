import json
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from app.core import (TENDER_COLUMNS, TENDER_COUNTRY_OPTIONS, TENDER_CPV_DEFAULT,
                      build_tender_query, compliance_hold, fetch_all_tenders, fetch_contracts_finder,
                      fetch_find_a_tender, fetch_public_contracts_scotland, merge_tenders,
                      parse_find_a_tender, parse_tenders, parse_uk_portal_tenders,
                      MANUAL_LINE_FIELDS, RFQ_COLUMNS, read_csv, safe_rfq_id, save_parsed_rfq,
                      tender_draft_line, tender_is_held, update_tender_status, upsert_rfq, write_csv)
from app.ui import empty_state, page_header, section, toast

page_header("Tender monitor", "Search public TED notices across buyer countries for instrumentation opportunities. Searches run only when you select Check now.")
st.info("Search TED, Find a Tender, Contracts Finder and Public Contracts Scotland. Searches run only when you select Check now.")

with section("Search settings", "TED search uses CPV codes and buyer countries. Results are further filtered by title keywords and future deadlines."):
    cpv_text = st.text_input("CPV codes", value=", ".join(TENDER_CPV_DEFAULT),
                             help="Comma-separated CPV codes. TED may include their more specific subcodes.")
    source_columns = st.columns(2)
    search_ted = source_columns[0].checkbox("Search TED", value=True)
    search_uk = source_columns[1].checkbox("Search UK Find a Tender", value=True)
    local_columns = st.columns(2)
    search_contracts_finder = local_columns[0].checkbox("Search Contracts Finder", value=True)
    search_scotland = local_columns[1].checkbox("Search Public Contracts Scotland", value=True)
    all_countries = st.checkbox("Search all buyer countries represented in TED", value=True)
    countries = []
    if not all_countries:
        countries = st.multiselect("Limit TED to buyer countries", options=list(TENDER_COUNTRY_OPTIONS),
                                   default=["CYP", "GRC"],
                                   format_func=lambda code: f"{TENDER_COUNTRY_OPTIONS[code]} ({code})")
    cpv_codes = [code.strip() for code in cpv_text.split(",") if code.strip()]
    any_source = search_ted or search_uk or search_contracts_finder or search_scotland
    if st.button("Check now", type="primary", disabled=(not cpv_codes and any_source)
                 or not any_source
                 or (search_ted and not all_countries and not countries)):
        today = date.today()
        start_date = today - timedelta(days=29)
        source_results = []
        parsed_sources = []
        source_configs = [
            (search_ted, "TED", lambda: fetch_all_tenders(build_tender_query(cpv_codes, countries, start_date, today)),
             lambda response: parse_tenders(response, today=today, countries=countries), "notices"),
            (search_uk, "UK Find a Tender", lambda: fetch_find_a_tender(start_date, today),
             lambda response: parse_find_a_tender(response, today=today, cpv_codes=cpv_codes), "releases"),
            (search_contracts_finder, "Contracts Finder", lambda: fetch_contracts_finder(start_date, today),
             lambda response: parse_uk_portal_tenders(response, "Contracts Finder", today=today, cpv_codes=cpv_codes), "releases"),
            (search_scotland, "Public Contracts Scotland", lambda: fetch_public_contracts_scotland(start_date, today),
             lambda response: parse_uk_portal_tenders(response, "Public Contracts Scotland", today=today, cpv_codes=cpv_codes), "releases"),
        ]
        failures = []
        for selected, name, fetch, parse, list_key in source_configs:
            if not selected:
                continue
            try:
                with st.spinner(f"Checking {name}…"):
                    response = fetch()
                matched = parse(response)
                parsed_sources.append(matched)
                source_results.append({
                    "name": name, "count": len(response.get(list_key, [])),
                    "pages": response.get("pagesFetched", 1), "total": response.get("totalNoticeCount"),
                    "relevant": len(matched), "truncated": response.get("truncated", False), "error": "",
                })
            except (RuntimeError, ValueError, OSError) as exc:
                failures.append(f"{name}: {exc}")
                source_results.append({"name": name, "count": 0, "pages": 0, "total": None,
                                       "relevant": 0, "truncated": False, "error": str(exc)})
        if parsed_sources:
            found = pd.concat(parsed_sources, ignore_index=True) if parsed_sources else pd.DataFrame(columns=TENDER_COLUMNS)
            existing = read_csv("tenders.csv", TENDER_COLUMNS)
            merged, added = merge_tenders(existing, found)
            write_csv("tenders.csv", merged[TENDER_COLUMNS])
            normalized, _ = merge_tenders(pd.DataFrame(columns=TENDER_COLUMNS), found)
            st.session_state["ted_last_check"] = {
                "sources": source_results, "relevant": len(normalized), "added": added, "failures": failures,
            }
            st.rerun()
        else:
            st.error("No source was selected. Choose at least one source and try again.")

last_check = st.session_state.get("ted_last_check")
if last_check:
    for source in last_check["sources"]:
        if source.get("error"):
            st.error(f"{source['name']} search failed: {source['error']}")
            continue
        total = f" of {source['total']}" if source["total"] is not None else ""
        truncated = "; source pagination limit reached, results may be incomplete" if source["truncated"] else ""
        st.caption(f"{source['name']}: fetched {source['count']}{total} records across {source['pages']} page(s); "
                   f"{source['relevant']} matched local filters{truncated}.")
    st.caption(f"Across selected sources: {last_check['relevant']} matched; {last_check['added']} were new to your cache.")

with section("Other UK portals", "Open these official portals for Welsh and Northern Irish opportunities."):
    st.caption("Sell2Wales publishes OCDS data and documents an API, but its public data page returned HTTP 502 during this integration, so automatic retrieval is not enabled yet. eTendersNI's public opportunity portal is available, but no documented open search feed was available to verify.")
    portal_links = st.columns(2)
    portal_links[0].link_button("Open Sell2Wales opportunities", "https://www.sell2wales.gov.wales/Search/search_mainpage.aspx", width="stretch")
    portal_links[1].link_button("Open eTendersNI opportunities", "https://etendersni.gov.uk/epps/prepareCurrentOpportunities.do?currentType=cft", width="stretch")

tenders = read_csv("tenders.csv", TENDER_COLUMNS)
if not tenders.empty:
    tenders["source"] = tenders["source"].astype("string").fillna("TED")
    # Never show tenders whose destination/buyer triggers the repo's compliance hold.
    safe = tenders.loc[~tenders.apply(tender_is_held, axis=1)].copy()
else:
    safe = tenders.copy()

new_tenders = safe[safe["status"].fillna("new").astype(str).str.lower() == "new"].copy()
if not new_tenders.empty:
    new_tenders["_deadline_sort"] = pd.to_datetime(new_tenders["deadline"], errors="coerce")
    new_tenders = new_tenders.sort_values("_deadline_sort", na_position="last")

with section("New tenders", "Review notices before converting a relevant opportunity into an RFQ."):
    if new_tenders.empty:
        empty_state("No new tenders", "Select Check now to fetch current notices from TED. Previous decisions remain in the pipeline below.")
    for _, tender in new_tenders.iterrows():
        notice_id = str(tender["notice_id"])
        title = str(tender.get("title") or "Untitled TED notice")
        with st.container(border=True):
            st.markdown(f"**{title}**")
            st.caption(f"{tender.get('source') or 'TED'} · {tender.get('buyer') or 'Buyer unavailable'} · {tender.get('country') or 'Country unavailable'} · "
                       f"Published {tender.get('published') or 'date unavailable'} · Deadline {tender.get('deadline') or 'not stated'}")
            st.markdown(f"[Open {tender.get('source') or 'tender'} notice]({tender.get('url')}) · CPV: {tender.get('cpv') or 'not stated'}")
            held, reason = compliance_hold(tender.get("country", ""), tender.get("buyer", ""))
            if held:
                st.error(reason)
                continue
            buttons = st.columns(3)
            if buttons[0].button("Mark relevant", key=f"rel_{notice_id}"):
                write_csv("tenders.csv", update_tender_status(tenders, notice_id, "relevant")[TENDER_COLUMNS])
                toast("Tender marked relevant.", "✅")
                st.rerun()
            if buttons[1].button("Ignore", key=f"ignore_{notice_id}"):
                write_csv("tenders.csv", update_tender_status(tenders, notice_id, "ignored")[TENDER_COLUMNS])
                st.rerun()
            if buttons[2].button("Review → draft RFQ", key=f"rfq_{notice_id}"):
                st.session_state["tender_rfq_review"] = notice_id
                st.rerun()

review_id = st.session_state.get("tender_rfq_review")
if review_id:
    selected_rows = safe[safe["notice_id"].astype(str) == str(review_id)]
    if selected_rows.empty:
        st.session_state.pop("tender_rfq_review", None)
    else:
        tender = selected_rows.iloc[0]
        notice_id = str(tender.get("notice_id") or "")
        title = str(tender.get("title") or "Tender notice")
        description = str(tender.get("description") or "")
        draft_lines, default_questions = tender_draft_line(title, description)
        draft_lines[0]["source_reference"] = (
            f"{tender.get('url')} — page / clause TBC" if tender.get("url") else "Official notice; page / clause TBC")
        draft_table_seed = pd.DataFrame(draft_lines, columns=MANUAL_LINE_FIELDS).astype({"line": "string"})
        try:
            documents = json.loads(str(tender.get("documents_json") or "[]"))
        except (TypeError, ValueError):
            documents = []
        with section("Review tender and prepare RFQ draft", "Nothing is priced or sent. Unknown fields remain TBC until checked against the full notice."):
            st.markdown(f"**{title}**  ·  {tender.get('buyer') or 'Buyer unavailable'} · {tender.get('country') or 'Country unavailable'}")
            if description:
                st.write(description)
            if documents:
                st.markdown("**Source documents**")
                for document in documents:
                    if isinstance(document, dict) and str(document.get("url", "")).startswith(("https://", "http://")):
                        st.markdown(f"- [{document.get('title') or 'Tender document'}]({document['url']})")
            else:
                st.caption("No linked documents were provided in the cached notice. Open the official notice and add its attachments manually.")
            st.caption("After creating the draft, download any notice files yourself and attach them under RFQs → Tender attachments. Add a document/page/clause reference to each extracted line.")
            st.warning("This creates a locked draft. Replace the summary with the actual requested line items and resolve the open questions on the RFQs page before pricing.")
            with st.form(f"tender_draft_form_{notice_id}"):
                a, b = st.columns(2)
                draft_id = a.text_input("RFQ ID", value=safe_rfq_id(notice_id))
                customer = b.text_input("Buyer / customer", value=str(tender.get("buyer") or ""))
                draft_table = st.data_editor(
                    draft_table_seed, key=f"tender_lines_{notice_id}",
                    num_rows="dynamic", width="stretch", hide_index=True,
                    column_config={"line": st.column_config.TextColumn("Line"),
                                   "quantity": st.column_config.TextColumn("Quantity (TBC until confirmed)"),
                                   "description": st.column_config.TextColumn("Description / summary", width="large")})
                questions = st.text_area("Open questions / review checklist", value="\n".join(default_questions), height=110)
                save_draft = st.form_submit_button("Create locked RFQ draft", type="primary")
            if save_draft:
                try:
                    rid = safe_rfq_id(draft_id)
                    path = save_parsed_rfq(rid, draft_table.to_dict("records"),
                                            [q.strip() for q in questions.splitlines() if q.strip()],
                                            allow_incomplete=True, draft=True)
                    notes = f"Tender draft — human review required. Source: {tender.get('url') or ''}"
                    rfqs = upsert_rfq(read_csv("rfqs.csv", RFQ_COLUMNS), rid,
                                      customer=customer.strip(), received_date=date.today().isoformat(),
                                      status="new", value_eur=pd.NA, notes=notes)
                    write_csv("rfqs.csv", rfqs)
                    write_csv("tenders.csv", update_tender_status(tenders, notice_id, "rfq_created")[TENDER_COLUMNS])
                    st.session_state.pop("tender_rfq_review", None)
                    toast(f"Locked RFQ draft {rid} saved to {path.name}.", "✅")
                    st.rerun()
                except (ValueError, FileExistsError) as exc:
                    st.error(str(exc))

with section("Tender pipeline", "Relevant, ignored and RFQ-created notices."):
    history = safe[safe["status"].fillna("new").astype(str).str.lower() != "new"]
    if history.empty:
        empty_state("No reviewed tenders yet", "Mark a notice relevant or create an RFQ to track it here.")
    else:
        view = history[[c for c in ("notice_id", "source", "title", "buyer", "country", "published", "deadline", "status", "url", "notes") if c in history.columns]].copy()
        st.dataframe(view, hide_index=True, width="stretch", column_config={"url": st.column_config.LinkColumn("TED notice")})

