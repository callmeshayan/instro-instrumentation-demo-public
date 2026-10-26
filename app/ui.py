"""Small presentation helpers shared by Streamlit pages."""

from __future__ import annotations

from contextlib import contextmanager
from html import escape
from typing import Iterator

import streamlit as st

_STATUS_STYLES = {
    ("rfq", "new"): ("#667085", "#f2f4f7"),
    ("rfq", "parsed"): ("#175cd3", "#eff8ff"),
    ("rfq", "priced"): ("#3538cd", "#eef4ff"),
    ("rfq", "quoted"): ("#1f3a5f", "#eaf0f6"),
    ("rfq", "won"): ("#067647", "#ecfdf3"),
    ("rfq", "lost"): ("#667085", "#f2f4f7"),
    ("approval", "pending"): ("#b54708", "#fffaeb"),
    ("approval", "approved"): ("#067647", "#ecfdf3"),
    ("approval", "rejected"): ("#b42318", "#fef3f2"),
    ("compliance", "clear"): ("#067647", "#ecfdf3"),
    ("compliance", "hold"): ("#b42318", "#fef3f2"),
    ("followup", "due"): ("#b54708", "#fffaeb"),
    ("followup", "overdue"): ("#b42318", "#fef3f2"),
    ("action", "backup"): ("#475467", "#f2f4f7"),
}

_CURRENCY_SYMBOLS = {"EUR": "€", "GBP": "£", "USD": "$"}


def apply_global_styles() -> None:
    """Apply the shared calm, presentation-ready visual system to the dashboard shell."""
    st.markdown(
        """
        <style>
        :root { --ui-navy: #1f3a5f; --ui-ink: #182b3d; --ui-muted: #657487;
            --ui-line: #e2e8ef; --ui-canvas: #f4f7fa; --ui-surface: #ffffff; }
        .stApp { background: var(--ui-canvas); }
        .block-container { max-width: 1360px; padding-top: 2rem; padding-bottom: 3rem; }
        [data-testid="stSidebar"] { background: #edf2f7; border-right: 1px solid var(--ui-line); }
        [data-testid="stSidebarNav"] { padding-top: .5rem; }
        [data-testid="stSidebarNav"] a { border-radius: 8px; margin: 2px 6px; }
        [data-testid="stSidebarNav"] a[aria-current="page"] { background: #dce7f2; color: var(--ui-navy); font-weight: 650; }
        h1, h2, h3 { color: var(--ui-ink); letter-spacing: -.025em; }
        h1 { font-size: 2rem !important; font-weight: 720 !important; margin-bottom: .1rem !important; }
        h2 { font-size: 1.35rem !important; font-weight: 680 !important; }
        h3 { font-size: 1.08rem !important; font-weight: 650 !important; }
        [data-testid="stCaptionContainer"] { color: var(--ui-muted); }
        [data-testid="stMetric"] { font-variant-numeric: tabular-nums; }
        [data-testid="stMetricValue"] { font-variant-numeric: tabular-nums; color: var(--ui-ink); font-weight: 680; }
        [data-testid="stMetricLabel"] { color: var(--ui-muted); font-size: .82rem; font-weight: 600; }
        [data-testid="stMetric"] [data-testid="stMetricDelta"] { font-size: .78rem; }
        [data-testid="stVerticalBlockBorderWrapper"] { border-color: var(--ui-line); border-radius: 12px; }
        [data-testid="stDataFrame"] { border: 1px solid var(--ui-line); border-radius: 9px; overflow: hidden; }
        [data-testid="stForm"] { border-color: var(--ui-line); border-radius: 12px; background: var(--ui-surface); }
        div[data-testid="stTextInput"] input, div[data-testid="stNumberInput"] input,
        div[data-testid="stDateInput"] input, div[data-testid="stTextArea"] textarea,
        div[data-testid="stSelectbox"] [data-baseweb="select"],
        div[data-testid="stMultiSelect"] [data-baseweb="select"] { border-radius: 8px; }
        div[data-testid="stButton"] > button, div[data-testid="stDownloadButton"] > button,
        div[data-testid="stLinkButton"] > a { border-radius: 8px; font-weight: 620; min-height: 2.55rem; }
        div[data-testid="stButton"] > button[kind="primary"],
        div[data-testid="stFormSubmitButton"] > button[kind="primary"] { background: var(--ui-navy); border-color: var(--ui-navy); }
        div[data-testid="stButton"] > button[kind="primary"]:hover { background: #294c78; border-color: #294c78; }
        div[data-testid="stAlert"] { border-radius: 10px; }
        hr { border-color: var(--ui-line); }
        .ui-eyebrow { color: var(--ui-muted); font-size: .68rem; font-weight: 750;
            letter-spacing: .12em; text-transform: uppercase; margin-bottom: .35rem; }
        .ui-badge { display: inline-flex; align-items: center; border: 1px solid currentColor;
            color: var(--ui-badge-color); background: var(--ui-badge-bg);
            border-radius: 999px; padding: .15rem .55rem; font-size: .75rem; font-weight: 650;
            line-height: 1.35; white-space: nowrap; }
        .ui-money { font-variant-numeric: tabular-nums; white-space: nowrap; }
        @media (prefers-color-scheme: dark) {
            .ui-badge { color: color-mix(in srgb, var(--ui-badge-color) 68%, white);
                background: color-mix(in srgb, var(--ui-badge-color) 15%, transparent); }
        }
        @media (max-width: 760px) {
            .block-container { padding: 1.1rem 1rem 2rem; }
            h1 { font-size: 1.65rem !important; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def page_header(title: str, description: str | None = None) -> None:
    st.markdown('<div class="ui-eyebrow">INSTRUMENTATION · SALES OPERATIONS</div>', unsafe_allow_html=True)
    st.title(title)
    if description:
        st.caption(description)


def metric_cards(items: list[tuple[str, str, str | None]]) -> None:
    """Render a responsive row of consistent bordered KPI cards."""
    columns = st.columns(len(items))
    for column, (label, value, detail) in zip(columns, items):
        with column.container(border=True):
            st.metric(label, value, help=detail)


@contextmanager
def section(title: str, description: str | None = None) -> Iterator[None]:
    """Render a bordered section with a consistent heading."""
    with st.container(border=True):
        st.subheader(title)
        if description:
            st.caption(description)
        yield


def empty_state(title: str, description: str) -> None:
    with st.container(border=True):
        st.markdown(f"### {title}")
        st.write(description)


def status_badge(status: str, family: str = "rfq", label: str | None = None) -> None:
    """Render a text-labelled status badge; colour is supplementary."""
    text = str(label if label is not None else status).strip()
    foreground, background = _STATUS_STYLES.get((family.lower(), status.lower()), ("#475467", "#f2f4f7"))
    st.markdown(
        f'<span class="ui-badge" style="--ui-badge-color:{foreground};--ui-badge-bg:{background};">'
        f'{escape(text)}</span>',
        unsafe_allow_html=True,
    )


WORKFLOW_STEPS = ("Lines", "Supplier requests", "Costs", "Quote", "Approval", "Sent", "Outcome")


def workflow_step(rfq_status: str, has_lines: bool, request_count: int, priced_line_count: int,
                  line_count: int, quote_rows: list[dict]) -> int:
    """Return the current deal step using already-loaded page data (presentation only)."""
    outcome = str(rfq_status or "").lower()
    if outcome in {"won", "lost"}:
        return 6
    if not has_lines or line_count == 0:
        return 0
    if quote_rows:
        latest = quote_rows[-1]
        approval = str(latest.get("approval_status", "")).strip().lower()
        sent = str(latest.get("sent_date", "")).strip().lower()
        if approval in {"<na>", "nan", "none", "nat"}:
            approval = ""
        if approval == "pending":
            return 4
        if sent not in {"", "<na>", "nan", "none", "nat"}:
            return 6 if outcome in {"won", "lost"} else 5
        if approval == "rejected":
            return 3
        return 5
    if priced_line_count >= line_count:
        return 3
    if request_count or priced_line_count:
        return 2
    return 1


def workflow_stepper(rfq_id: str, current_step: int) -> None:
    """Show the same compact, labelled progress indicator on each deal page."""
    st.caption(f"Deal progress · {rfq_id}")
    columns = st.columns(len(WORKFLOW_STEPS))
    for index, (column, label) in enumerate(zip(columns, WORKFLOW_STEPS)):
        if index < current_step:
            text, family, state = f"✓ {label}", "approval", "approved"
        elif index == current_step:
            text, family, state = f"{index + 1} · {label}", "rfq", "priced"
        else:
            text, family, state = label, "action", "backup"
        with column:
            status_badge(state, family, text)


def money(value: object, currency: str = "EUR") -> str:
    amount = float(value or 0)
    symbol = _CURRENCY_SYMBOLS.get(currency.upper(), currency.upper() + " ")
    return f"{symbol}{amount:,.2f}"


def toast(message: str, icon: str | None = None) -> None:
    st.toast(message, icon=icon)
