# Instro Dashboard · Public Company Demo

**Live demo:** https://instro-demo.streamlit.app/

This is a public, fictional demonstration of the instrumentation sales workflow. Anyone can open the site; visitors may be able to discover it through Streamlit as well as by URL. The source repository is public. Do not enter or upload real customer, supplier, employee, pricing, or tender data. Demo edits may be visible to other visitors and may reset when the hosted app restarts.

## Try the demo

Open the **Tutorial** page in the sidebar, or follow the sample RFQ `DEMO-001` through Today → RFQs → Costs → Technical review → Quote Builder → Weekly review. The tutorial explains each step and the app's limits.

The **Costs** page compares supplier offers by landed cost, lead time, offer validity, and recorded supplier qualification. The **Technical review** page records requested versus offered specifications, review decisions, evidence references, and notes per RFQ line. Quote Builder lets you select the supplier offer used for each line and shows whether technical checks remain open or have deviations.

## Run locally

Python 3.11+ is required. Install dependencies from `requirements.txt`, then run `streamlit run app/main.py` from this folder.

## Demo boundaries

- All customer, supplier, and product information is fictional and labeled DEMO.
- Supplier costs and quote margins are visible in the app. Customer quote files omit internal costs and margins.
- Supplier and customer emails are drafts; the app does not send them.
- Tender searches run only when you choose **Check now** and can retrieve real public notices.
- macOS Vision OCR is unavailable on the hosted Linux app. PDFs with selectable text can be extracted; scanned-PDF OCR requires macOS.
- Community Cloud local-file storage is temporary. This demo is not a production CRM or secure data store.
