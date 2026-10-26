# Instro Dashboard · Company Demo

This standalone demo bundle contains fictional data only. It excludes the Mac app's local data, secrets, customer files, supplier records, generated quotes, and Git history.

## Run locally

Python 3.11+ is required. Install dependencies from requirements.txt, then run streamlit run app/main.py from this folder.

## Deploy as a private web demo

1. Create a new private GitHub repository for this bundle and upload these files. Do not use the Mac app repository.
2. In Streamlit Community Cloud, create an app from that repository and select app/main.py as the entry point. Choose Python 3.11.
3. Keep the app private and invite the intended company viewers by email.
4. Check the deployed site before sharing its link.

Community Cloud supports private apps with invited viewers. Local file storage is not guaranteed to persist, so demo edits can reset after restart. Do not use this bundle for live sales work or upload real RFQs, quotes, supplier information, or customer data.

## Demo boundaries

- All customer, supplier, and product information is fictional and labeled DEMO.
- Supplier costs and quote margins are visible to invited viewers.
- TED searches run only after clicking Check now and can return real public notices.
- macOS Vision OCR is unavailable on Linux-hosted web services. Selectable PDF text extraction remains available; scanned-PDF OCR requires macOS.
- For operational use, deploy a separately reviewed private system with durable storage, backups, authentication, and a deliberate data migration plan.
