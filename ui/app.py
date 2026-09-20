"""Streamlit workbench UI for CodeReview-Agent."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from base64 import b64decode, b64encode
from datetime import datetime
from typing import Any

import httpx
import streamlit as st
import streamlit.components.v1 as components

API_BASE = os.getenv("API_BASE", "http://localhost:8000")
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "admin")
SECRET_KEY = os.getenv("SECRET_KEY", "codereview-secret-key-change-me")
TOKEN_TTL = 60 * 60 * 24 * 30  # 30 days

SEVERITY_COLOR = {
    "CRITICAL": "🔴",
    "HIGH": "🟠",
    "MEDIUM": "🟡",
    "LOW": "🟢",
}
SEVERITY_BG = {
    "CRITICAL": "#fde8e8",
    "HIGH": "#fef0e0",
    "MEDIUM": "#fefce0",
    "LOW": "#e8f5ee",
}
SEVERITY_FG = {
    "CRITICAL": "#b91c1c",
    "HIGH": "#c2580a",
    "MEDIUM": "#92680a",
    "LOW": "#166534",
}
SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
STATUS_LABELS = {
    "pending": "Queued",
    "running": "Running",
    "completed": "Completed",
    "failed": "Failed",
}
STATUS_ICON = {
    "pending": "⏳",
    "running": "⚙️",
    "completed": "✅",
    "failed": "❌",
}
TIME_WINDOW_OPTIONS = {
    "24h": 1,
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "All": None,
}
AGENT_TABS = [
    ("All Findings", None),
    ("Security", "SecurityAgent"),
    ("Logic", "LogicAgent"),
    ("Performance", "PerformanceAgent"),
    ("Style", "StyleAgent"),
]
FINDING_PAGE_SIZE_OPTIONS = [25, 50, 100, 250, "All"]
VERSION = "1.0.0"

st.set_page_config(
    page_title="CodeReview-Agent",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ── Auth ─────────────────────────────────────────────────────────────────────

def _generate_token(username: str) -> str:
    payload = f"{username}:{int(time.time()) + TOKEN_TTL}"
    sig = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return b64encode(f"{payload}:{sig}".encode()).decode()


def _verify_token(token: str) -> bool:
    try:
        decoded = b64decode(token.encode()).decode()
        username, expiry, sig = decoded.rsplit(":", 2)
        if int(expiry) < int(time.time()):
            return False
        payload = f"{username}:{expiry}"
        expected = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:  # noqa: BLE001
        return False


def _check_login() -> bool:
    token = st.session_state.get("_auth_token", "")
    if token and _verify_token(token):
        return True
    # fall back to query param (set after login redirect)
    return False


def _do_login(username: str, password: str) -> bool:
    if username == DASHBOARD_USER and password == DASHBOARD_PASSWORD:
        st.session_state["_auth_token"] = _generate_token(username)
        st.session_state["_auth_user"] = username
        return True
    return False


def _do_logout() -> None:
    st.session_state.pop("_auth_token", None)
    st.session_state.pop("_auth_user", None)
    st.rerun()


# ── CSS ───────────────────────────────────────────────────────────────────────

def _inject_css() -> None:
    st.markdown(
        """
        <style>
          :root {
            --panel: rgba(255, 250, 242, 0.88);
            --panel-strong: #fff5e8;
            --line: rgba(124, 89, 58, 0.18);
            --ink: #1b1915;
            --muted: #685f54;
            --accent: #9f4f31;
            --accent-soft: #f4dfd0;
            --accent-2: #1d5c4a;
            --accent-3: #c98b2b;
            --shadow: 0 20px 40px rgba(35, 26, 20, 0.08);
            --radius: 18px;
          }
          html, body, [class*="css"] { font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; }
          .stApp {
            background:
              radial-gradient(circle at top left, rgba(255, 210, 179, 0.65) 0%, transparent 28%),
              radial-gradient(circle at bottom right, rgba(177, 214, 197, 0.42) 0%, transparent 24%),
              linear-gradient(180deg, #fbf7f0 0%, #f3ecdf 100%);
            color: var(--ink);
          }
          /* hide default chrome */
          #MainMenu, header[data-testid="stHeader"], footer { display: none !important; }
          /* sidebar */
          [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #17372e 0%, #244a3d 100%) !important;
            border-right: none !important;
          }
          [data-testid="stSidebar"] * { color: #fff8ef !important; }
          [data-testid="stSidebar"] .stRadio label { padding: 6px 0; }
          [data-testid="stSidebar"] hr { border-color: rgba(255,255,255,0.12) !important; }
          /* panels */
          .panel {
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: var(--radius);
            padding: 22px 24px;
            box-shadow: var(--shadow);
            margin-bottom: 18px;
          }
          .panel:hover { box-shadow: 0 24px 48px rgba(35,26,20,0.12); }
          /* hero */
          .hero {
            padding: 28px 0 18px;
            margin-bottom: 4px;
          }
          .hero h1 {
            font-size: 2rem;
            font-weight: 700;
            margin: 0 0 6px;
            color: var(--ink);
          }
          .hero p { color: var(--muted); margin: 0; font-size: 1rem; }
          /* kpi */
          .kpi {
            background: linear-gradient(180deg, rgba(255,255,255,0.96) 0%, rgba(250,243,234,0.9) 100%);
            border: 1px solid var(--line);
            border-radius: var(--radius);
            padding: 20px 22px;
            min-height: 110px;
            box-shadow: var(--shadow);
            transition: box-shadow 0.2s;
          }
          .kpi:hover { box-shadow: 0 24px 48px rgba(35,26,20,0.14); }
          .kpi-label {
            color: var(--muted);
            font-size: 0.78rem;
            text-transform: uppercase;
            letter-spacing: 0.09em;
            font-weight: 600;
          }
          .kpi-value {
            font-size: 2rem;
            font-weight: 700;
            margin-top: 10px;
            color: var(--ink);
          }
          .kpi-sub { font-size: 0.82rem; color: var(--muted); margin-top: 4px; }
          /* task/mini cards */
          .mini-card {
            background: linear-gradient(180deg, rgba(255,255,255,0.92) 0%, rgba(248,241,232,0.9) 100%);
            border: 1px solid var(--line);
            border-radius: 14px;
            padding: 14px 16px;
            margin-bottom: 10px;
            cursor: pointer;
            transition: box-shadow 0.2s, border-color 0.2s;
          }
          .mini-card:hover {
            box-shadow: 0 8px 24px rgba(159,79,49,0.13);
            border-color: rgba(159,79,49,0.35);
          }
          .mini-card-active {
            border-color: rgba(159,79,49,0.55) !important;
            box-shadow: 0 10px 32px rgba(159,79,49,0.16) !important;
            background: linear-gradient(180deg, rgba(255,249,241,0.98) 0%, rgba(249,236,224,0.96) 100%) !important;
          }
          .mini-title { font-weight: 700; color: var(--ink); margin-bottom: 4px; font-size: 0.95rem; }
          .mini-meta { color: var(--muted); font-size: 0.85rem; margin-top: 2px; }
          .mini-url { color: var(--muted); font-size: 0.78rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }
          /* section headers */
          .section-title {
            font-size: 1.15rem;
            font-weight: 700;
            margin-bottom: 12px;
            color: var(--ink);
          }
          .section-copy { color: var(--muted); margin-bottom: 14px; font-size: 0.92rem; }
          /* severity chips */
          .chip {
            display: inline-block;
            padding: 3px 10px;
            border-radius: 999px;
            font-size: 0.78rem;
            font-weight: 700;
            letter-spacing: 0.04em;
            margin-right: 4px;
          }
          /* status chips */
          .status-chip {
            display: inline-block;
            padding: 3px 10px;
            border-radius: 999px;
            background: var(--accent-soft);
            color: var(--accent);
            font-size: 0.82rem;
            font-weight: 600;
          }
          /* finding card */
          .finding-card {
            border-radius: 14px;
            padding: 16px 18px;
            margin-bottom: 12px;
            border: 1px solid var(--line);
            background: rgba(255,255,255,0.75);
          }
          .finding-title { font-weight: 700; font-size: 0.97rem; margin-bottom: 6px; }
          .finding-meta { font-size: 0.82rem; color: var(--muted); margin-bottom: 8px; }
          .finding-suggestion {
            background: rgba(29,92,74,0.07);
            border-left: 3px solid var(--accent-2);
            border-radius: 6px;
            padding: 8px 12px;
            font-size: 0.88rem;
            color: var(--ink);
            margin-top: 8px;
          }
          /* login */
          .login-box {
            background: rgba(255,255,255,0.92);
            border: 1px solid var(--line);
            border-radius: 24px;
            padding: 40px 36px;
            box-shadow: 0 32px 64px rgba(35,26,20,0.12);
            max-width: 400px;
            margin: 60px auto 0;
          }
          .login-title { font-size: 1.6rem; font-weight: 700; margin-bottom: 6px; }
          .login-sub { color: var(--muted); margin-bottom: 24px; font-size: 0.92rem; }
          /* footer */
          .app-footer {
            text-align: center;
            color: var(--muted);
            font-size: 0.78rem;
            padding: 24px 0 8px;
            border-top: 1px solid var(--line);
            margin-top: 32px;
          }
          /* buttons */
          .stButton > button {
            border-radius: 10px !important;
            font-weight: 600 !important;
            transition: all 0.18s !important;
          }
          .stButton > button[kind="primary"] {
            background: linear-gradient(135deg, #9f4f31 0%, #c06a44 100%) !important;
            border: none !important;
            color: white !important;
          }
          .stButton > button[kind="primary"]:hover {
            transform: translateY(-1px);
            box-shadow: 0 8px 20px rgba(159,79,49,0.35) !important;
          }
          /* inputs */
          .stTextInput > div > div > input {
            border-radius: 10px !important;
            border: 1px solid var(--line) !important;
            background: rgba(255,255,255,0.85) !important;
          }
          .stTextInput > div > div > input:focus {
            border-color: rgba(159,79,49,0.45) !important;
            box-shadow: 0 0 0 3px rgba(159,79,49,0.1) !important;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ── HTTP helpers ─────────────────────────────────────────────────────────────

@st.cache_resource
def _http_client() -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE,
        timeout=httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=2.0),
        limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        headers={"Connection": "keep-alive"},
    )


def _safe_get(path: str, **kwargs: Any) -> Any | None:
    try:
        resp = _http_client().get(path, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Request failed: {exc}")
        return None


def _safe_post(path: str, payload: dict[str, Any]) -> Any | None:
    try:
        resp = _http_client().post(path, json=payload)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Request failed: {exc}")
        return None


@st.cache_data(ttl=3, show_spinner=False)
def _cached_status(task_id: int, include_results: bool) -> dict[str, Any] | None:
    return _safe_get(f"/review/{task_id}", params={"include_results": str(include_results).lower()})


@st.cache_data(ttl=300, show_spinner=False)
def _cached_completed_report(task_id: int) -> dict[str, Any] | None:
    """Long-lived cache for completed/failed tasks whose results never change."""
    return _safe_get(f"/review/{task_id}", params={"include_results": "true"})


@st.cache_data(ttl=30, show_spinner=False)
def _cached_recent_tasks(limit: int) -> list[dict[str, Any]]:
    data = _safe_get("/reviews/recent", params={"limit": limit})
    return data or []


@st.cache_data(ttl=60, show_spinner=False)
def _cached_dashboard(days: int) -> dict[str, Any] | None:
    return _safe_get("/stats/dashboard", params={"days": days})


def _clear_ui_caches() -> None:
    _cached_status.clear()
    _cached_completed_report.clear()
    _cached_recent_tasks.clear()
    _cached_dashboard.clear()


def _post_review(pr_url: str) -> int | None:
    data = _safe_post("/review", {"pr_url": pr_url})
    if data:
        _clear_ui_caches()
    return data.get("task_id") if data else None


def _get_status(task_id: int) -> dict[str, Any] | None:
    return _cached_status(task_id, False)


def _get_recent_tasks(limit: int = 50) -> list[dict[str, Any]]:
    return _cached_recent_tasks(limit)


def _get_dashboard(days: int = 30) -> dict[str, Any] | None:
    return _cached_dashboard(days)


# ── Formatters ────────────────────────────────────────────────────────────────

def _format_ts(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return value


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _severity_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    return {
        sev: sum(1 for f in findings if f.get("severity") == sev)
        for sev in SEVERITY_ORDER
    }


def _severity_chips(counts: dict[str, int]) -> str:
    parts = []
    for sev in SEVERITY_ORDER:
        n = counts.get(sev, 0)
        if n:
            bg = SEVERITY_BG[sev]
            fg = SEVERITY_FG[sev]
            parts.append(
                f'<span class="chip" style="background:{bg};color:{fg};">'
                f'{SEVERITY_COLOR[sev]} {sev[0]} {n}</span>'
            )
    return "".join(parts) if parts else '<span class="chip" style="background:#f0f0f0;color:#999;">no findings</span>'


# ── Shared render helpers ─────────────────────────────────────────────────────

def _render_kpis(items: list[tuple[str, str, str]]) -> None:
    """Render KPI cards. items = list of (label, value, sub_text)."""
    cols = st.columns(len(items))
    for col, (label, value, sub) in zip(cols, items):
        with col:
            st.markdown(
                f"""
                <div class="kpi">
                  <div class="kpi-label">{label}</div>
                  <div class="kpi-value">{value}</div>
                  <div class="kpi-sub">{sub}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def _render_finding_card(finding: dict[str, Any]) -> None:
    severity = finding.get("severity", "LOW")
    icon = SEVERITY_COLOR.get(severity, "")
    bg = SEVERITY_BG.get(severity, "#f9f9f9")
    fg = SEVERITY_FG.get(severity, "#333")
    file_ = finding.get("file", "")
    line_start = finding.get("line_start", "?")
    line_end = finding.get("line_end", "?")
    category = finding.get("category", "")
    description = finding.get("description", "")
    suggestion = finding.get("suggestion", "")
    confidence = finding.get("confidence", 0.0)
    sources = ", ".join(finding.get("source_agents", []))

    st.markdown(
        f"""
        <div class="finding-card" style="border-left: 4px solid {fg};">
          <div class="finding-title">
            <span class="chip" style="background:{bg};color:{fg};">{icon} {severity}</span>
            &nbsp;<code style="font-size:0.88rem;">{file_}</code>
            <span style="color:var(--muted);font-size:0.82rem;"> L{line_start}–{line_end}</span>
            {f'&nbsp;<span class="status-chip">{category}</span>' if category else ''}
          </div>
          <div class="finding-meta">
            Confidence: {confidence:.0%}{f' &nbsp;·&nbsp; Sources: {sources}' if sources else ''}
          </div>
          <div style="font-size:0.92rem;color:var(--ink);margin-bottom:4px;">{description}</div>
          {f'<div class="finding-suggestion"><b>Suggested fix:</b> {suggestion}</div>' if suggestion else ''}
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_findings(
    findings: list[dict[str, Any]],
    agent_name: str | None,
    *,
    max_items: int | None = None,
) -> None:
    subset = findings if agent_name is None else [
        f for f in findings if agent_name in f.get("source_agents", [])
    ]
    if not subset:
        st.info("No findings in this view.")
        return
    visible = subset if max_items is None else subset[:max_items]
    if max_items is not None and len(subset) > max_items:
        st.caption(f"Showing {len(visible)} of {len(subset)} findings.")
    for finding in visible:
        _render_finding_card(finding)


def _filter_tasks(
    tasks: list[dict[str, Any]],
    *,
    query: str,
    statuses: list[str],
    days: int | None,
    only_with_report: bool,
) -> list[dict[str, Any]]:
    filtered = tasks
    if query:
        q = query.lower().strip()
        filtered = [
            item for item in filtered
            if q in item["pr_url"].lower() or q in str(item["task_id"])
        ]
    if statuses:
        filtered = [item for item in filtered if item["status"] in statuses]
    if only_with_report:
        filtered = [item for item in filtered if item.get("has_report")]
    if days is not None:
        now = datetime.now().astimezone()
        recent_filtered: list[dict[str, Any]] = []
        for item in filtered:
            ts = _parse_ts(item["created_at"])
            if ts is None:
                continue
            if (now - ts.astimezone()).days <= days:
                recent_filtered.append(item)
        filtered = recent_filtered
    return filtered


# ── Report renderer ──────────────────────────────────────────────────────────

def _render_report(data: dict[str, Any]) -> None:
    findings: list[dict[str, Any]] = data.get("findings") or []
    markdown_report: str = data.get("markdown_report") or ""
    executive_summary: str = data.get("executive_summary") or ""
    sev_counts = _severity_counts(findings)

    _render_kpis([
        ("Total Findings", str(len(findings)), _severity_chips(sev_counts)),
        ("Critical", str(sev_counts.get("CRITICAL", 0)), "severity: critical"),
        ("High", str(sev_counts.get("HIGH", 0)), "severity: high"),
        ("Status", STATUS_LABELS.get(data.get("status", ""), "—"), _format_ts(data.get("updated_at", ""))),
    ])

    left, right = st.columns([1.6, 1], gap="large")
    with left:
        st.markdown('<div class="panel">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Executive Summary</div>', unsafe_allow_html=True)
        st.markdown(executive_summary or "No summary available.")
        st.divider()
        st.caption(f"PR: {data.get('pr_url', '')}")
        st.caption(f"Created: {_format_ts(data.get('created_at', ''))}  ·  Updated: {_format_ts(data.get('updated_at', ''))}")
        st.markdown("</div>", unsafe_allow_html=True)

    with right:
        st.markdown('<div class="panel">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Severity Breakdown</div>', unsafe_allow_html=True)
        for sev in SEVERITY_ORDER:
            count = sev_counts.get(sev, 0)
            bg = SEVERITY_BG[sev]
            fg = SEVERITY_FG[sev]
            icon = SEVERITY_COLOR[sev]
            st.markdown(
                f'<div style="display:flex;align-items:center;justify-content:space-between;'
                f'padding:8px 12px;border-radius:10px;background:{bg};margin-bottom:6px;">'
                f'<span style="font-weight:600;color:{fg};">{icon} {sev}</span>'
                f'<span style="font-weight:700;color:{fg};font-size:1.1rem;">{count}</span></div>',
                unsafe_allow_html=True,
            )
        st.markdown("</div>", unsafe_allow_html=True)

    view_map = {label: agent_name for label, agent_name in AGENT_TABS}
    view_options = list(view_map.keys()) + ["Raw Report"]
    selected_view = st.radio(
        "Report View",
        options=view_options,
        horizontal=True,
        key=f"report_view_{data.get('task_id', 'report')}",
    )
    if selected_view == "Raw Report":
        st.code(markdown_report or "No markdown report generated.", language="markdown")
    else:
        page_size = st.selectbox(
            "Findings To Render",
            options=FINDING_PAGE_SIZE_OPTIONS,
            index=1,
            key=f"report_page_size_{data.get('task_id', 'report')}_{selected_view}",
        )
        max_items = None if page_size == "All" else int(page_size)
        _render_findings(findings, view_map[selected_view], max_items=max_items)

    if markdown_report:
        st.download_button(
            label="Download Markdown Report",
            data=markdown_report,
            file_name=f"code_review_task_{data.get('task_id', 'report')}.md",
            mime="text/markdown",
            use_container_width=True,
        )


# ── Login page ────────────────────────────────────────────────────────────────

def _login_page() -> None:
    _inject_css()
    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        if DASHBOARD_USER == "admin" and DASHBOARD_PASSWORD == "admin":
            st.warning("Default credentials in use. Set DASHBOARD_USER and DASHBOARD_PASSWORD environment variables.")
        st.markdown(
            """
            <div class="login-box">
              <div class="login-title">🧭 CodeReview-Agent</div>
              <div class="login-sub">Sign in to access the AI review control center.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        with st.form("login_form"):
            username = st.text_input("Username", placeholder="Enter username")
            password = st.text_input("Password", type="password", placeholder="Enter password")
            submitted = st.form_submit_button("Sign In", use_container_width=True, type="primary")
        if submitted:
            if _do_login(username, password):
                st.success("Signed in successfully.")
                st.rerun()
            else:
                st.error("Invalid username or password.")


# ── Tasks page ───────────────────────────────────────────────────────────────

def _render_tasks_page() -> None:
    st.markdown(
        """
        <div class="hero">
          <h1>Review Operations</h1>
          <p>Submit new reviews, filter recent runs, and inspect completed reports side by side.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Top: submit + filters ──
    submit_col, filter_col = st.columns([1.2, 1], gap="large")
    with submit_col:
        st.markdown('<div class="panel">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Submit New Review</div>', unsafe_allow_html=True)
        pr_url = st.text_input(
            "GitHub PR URL",
            placeholder="https://github.com/owner/repo/pull/123",
            key="tasks_page_pr_url",
            label_visibility="collapsed",
        )
        submit_col2, clear_col2 = st.columns(2)
        with submit_col2:
            submit_now = st.button("Queue Review", type="primary", use_container_width=True, key="tasks_submit")
        with clear_col2:
            clear_detail = st.button("Clear Detail", use_container_width=True, key="tasks_clear_detail")
        if submit_now and pr_url:
            task_id = _post_review(pr_url)
            if task_id is not None:
                st.session_state["selected_task_id"] = task_id
                st.rerun()
        if clear_detail:
            st.session_state.pop("selected_task_id", None)
            _clear_ui_caches()
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    with filter_col:
        st.markdown('<div class="panel">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Filters</div>', unsafe_allow_html=True)
        search_query = st.text_input("Search by PR URL or Task ID", key="tasks_search", label_visibility="collapsed", placeholder="Search…")
        status_filter = st.multiselect(
            "Status",
            options=list(STATUS_LABELS.keys()),
            format_func=lambda s: f"{STATUS_ICON.get(s, '')} {STATUS_LABELS.get(s, s)}",
            key="tasks_status_filter",
            label_visibility="collapsed",
        )
        col_tw, col_rep = st.columns(2)
        with col_tw:
            time_window = st.selectbox("Time window", list(TIME_WINDOW_OPTIONS.keys()), index=2, key="tasks_time_window", label_visibility="collapsed")
        with col_rep:
            only_report = st.checkbox("Has report", key="tasks_only_report")
        st.markdown("</div>", unsafe_allow_html=True)

    # ── Bottom: task list + detail ──
    all_tasks = _get_recent_tasks(limit=100)
    days_filter = TIME_WINDOW_OPTIONS[time_window]
    filtered = _filter_tasks(
        all_tasks,
        query=search_query,
        statuses=status_filter,
        days=days_filter,
        only_with_report=only_report,
    )

    list_col, detail_col = st.columns([1, 2], gap="large")
    with list_col:
        st.markdown('<div class="panel">', unsafe_allow_html=True)
        top_l, top_r = st.columns([1, 1])
        with top_l:
            st.markdown(f'<div class="section-title">Tasks <span style="font-size:0.85rem;font-weight:400;color:var(--muted);">({len(filtered)})</span></div>', unsafe_allow_html=True)
        with top_r:
            if st.button("↻ Refresh", use_container_width=True, key="tasks_refresh"):
                _clear_ui_caches()
                st.rerun()

        selected_id = st.session_state.get("selected_task_id")
        if not filtered:
            st.caption("No tasks match the current filters.")
        for item in filtered:
            tid = item["task_id"]
            status = item["status"]
            icon = STATUS_ICON.get(status, "")
            label = STATUS_LABELS.get(status, status)
            findings_count = item.get("findings_count", 0)
            is_active = tid == selected_id
            active_cls = " mini-card-active" if is_active else ""
            st.markdown(
                f"""
                <div class="mini-card{active_cls}">
                  <div class="mini-title">{icon} #{tid} &nbsp;<span class="status-chip">{label}</span></div>
                  <div class="mini-meta">{findings_count} findings &nbsp;·&nbsp; {_format_ts(item['created_at'])}</div>
                  <div class="mini-url">{item['pr_url']}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if st.button(f"Load #{tid}", key=f"load_task_{tid}", use_container_width=True):
                st.session_state["selected_task_id"] = tid
                st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    with detail_col:
        if selected_id is None:
            st.markdown(
                '<div class="panel"><div class="section-copy">Select a task on the left to view the full report.</div></div>',
                unsafe_allow_html=True,
            )
        else:
            # Use short-TTL cache for in-progress tasks; long-TTL for completed/failed
            _peek = _cached_status(selected_id, False)
            _status = (_peek or {}).get("status", "")
            if _status in {"completed", "failed"}:
                data = _cached_completed_report(selected_id)
            else:
                data = _cached_status(selected_id, True)
            if data is None:
                st.info(f"Could not load task #{selected_id}.")
            else:
                current_status = data.get("status", "")
                if current_status in {"pending", "running"}:
                    st.info(f"{STATUS_ICON.get(current_status, '')} Task #{selected_id} is {STATUS_LABELS.get(current_status, current_status).lower()}…")
                    auto_refresh = st.toggle("Auto-refresh (4s)", key="tasks_auto_refresh")
                    if auto_refresh:
                        components.html(
                            """<script>setTimeout(function(){window.parent.location.reload();},4000);</script>""",
                            height=0,
                        )
                else:
                    _render_report(data)


# ── Review page ───────────────────────────────────────────────────────────────

def _render_review_page() -> None:
    st.markdown(
        """
        <div class="hero">
          <h1>Code Review Workbench</h1>
          <p>Submit a GitHub pull request, monitor execution, and inspect multi-agent findings in one place.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    left, right = st.columns([1.4, 1], gap="large")
    with left:
        st.markdown('<div class="panel">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Start A Review</div>', unsafe_allow_html=True)
        st.markdown(
            '<p class="section-copy">Paste a GitHub PR URL and the service will queue a new multi-agent review task.</p>',
            unsafe_allow_html=True,
        )
        pr_url = st.text_input(
            "GitHub PR URL",
            placeholder="https://github.com/owner/repo/pull/123",
            label_visibility="collapsed",
        )
        col_a, col_b = st.columns([1, 1])
        with col_a:
            start = st.button("Start Review", use_container_width=True, type="primary")
        with col_b:
            if st.button("Clear", use_container_width=True):
                st.session_state.pop("task_id", None)
                st.session_state.pop("data", None)
                _clear_ui_caches()
                st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

        if start and pr_url:
            task_id = _post_review(pr_url)
            if task_id is not None:
                st.session_state["task_id"] = task_id
                st.session_state["data"] = None
                st.rerun()

    with right:
        recent = _get_recent_tasks(limit=5)
        st.markdown('<div class="panel">', unsafe_allow_html=True)
        top_left, top_right = st.columns([1, 1])
        with top_left:
            st.markdown('<div class="section-title">Recent Queue</div>', unsafe_allow_html=True)
        with top_right:
            if st.button("↻ Refresh", use_container_width=True, key="review_refresh_queue"):
                _cached_recent_tasks.clear()
                st.rerun()
        if not recent:
            st.caption("No tasks yet.")
        for item in recent:
            status = item["status"]
            label = STATUS_LABELS.get(status, status)
            icon = STATUS_ICON.get(status, "")
            st.markdown(
                f"""
                <div class="mini-card">
                  <div class="mini-title">{icon} #{item['task_id']} &nbsp;<span class="status-chip">{label}</span></div>
                  <div class="mini-meta">{item['findings_count']} findings &nbsp;·&nbsp; {_format_ts(item['created_at'])}</div>
                  <div class="mini-url">{item['pr_url']}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        st.markdown("</div>", unsafe_allow_html=True)

    if "task_id" in st.session_state and st.session_state.get("data") is None:
        task_id = st.session_state["task_id"]
        ctrl_l, ctrl_r, ctrl_auto = st.columns([1, 1, 1.2])
        with ctrl_l:
            refresh_now = st.button("Refresh Status", use_container_width=True, key="review_refresh_active")
        with ctrl_r:
            if st.button("Stop Tracking", use_container_width=True, key="review_stop"):
                st.session_state.pop("task_id", None)
                st.session_state.pop("data", None)
                _clear_ui_caches()
                st.rerun()
        with ctrl_auto:
            auto_refresh = st.toggle("Auto-refresh (4s)", value=False, key="review_auto_refresh")

        if refresh_now:
            _cached_status.clear()

        data = _get_status(task_id)
        if data is None:
            return
        current_status = STATUS_LABELS.get(data.get("status", ""), data.get("status", "running")).lower()
        st.info(f"{STATUS_ICON.get(data.get('status',''), '')} Task #{task_id} is {current_status}.")
        if data.get("status") in {"completed", "failed"}:
            st.session_state["data"] = data
            st.rerun()
        if auto_refresh:
            st.caption("Auto-refresh enabled — reloading in 4 s.")
            components.html(
                """<script>setTimeout(function(){window.parent.location.reload();},4000);</script>""",
                height=0,
            )

    if st.session_state.get("data") is not None:
        st.markdown('<div class="section-title" style="margin-top:24px;">Current Task Report</div>', unsafe_allow_html=True)
        _render_report(st.session_state["data"])


# ── Dashboard page ───────────────────────────────────────────────────────────

def _render_dashboard() -> None:
    st.markdown(
        """
        <div class="hero">
          <h1>Analytics Dashboard</h1>
          <p>Aggregate statistics across all review tasks and findings.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    time_window = st.sidebar.selectbox(
        "Time Window",
        list(TIME_WINDOW_OPTIONS.keys()),
        index=2,
        key="dashboard_time_window",
    )
    days = TIME_WINDOW_OPTIONS[time_window]
    if days is None:
        days = 3650  # "All" — large enough

    if st.sidebar.button("↻ Refresh Stats", use_container_width=True, key="dashboard_refresh"):
        _cached_dashboard.clear()
        st.rerun()

    dashboard = _get_dashboard(days=days)
    if not dashboard:
        st.info("No dashboard data available. Make sure the API is running.")
        return

    summary = dashboard.get("summary") or {}
    total_tasks = summary.get("total_tasks", 0)
    completed = summary.get("completed", 0)
    failed = summary.get("failed", 0)
    total_findings = summary.get("total_findings", 0)
    success_rate = f"{completed / total_tasks:.0%}" if total_tasks else "—"

    _render_kpis([
        ("Total Tasks", str(total_tasks), f"last {time_window}"),
        ("Completed", str(completed), f"success rate: {success_rate}"),
        ("Failed", str(failed), "review errors"),
        ("Total Findings", str(total_findings), "across all tasks"),
    ])

    # ── Severity + categories ──
    left, right = st.columns([1, 1], gap="large")
    with left:
        st.markdown('<div class="panel">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Findings by Severity</div>', unsafe_allow_html=True)
        sev_data = {row["severity"]: row["count"] for row in summary.get("by_severity", [])}
        for sev in SEVERITY_ORDER:
            count = sev_data.get(sev, 0)
            pct = count / max(total_findings, 1)
            bg = SEVERITY_BG[sev]
            fg = SEVERITY_FG[sev]
            icon = SEVERITY_COLOR[sev]
            st.markdown(
                f'<div style="margin-bottom:8px;">'
                f'<div style="display:flex;justify-content:space-between;margin-bottom:3px;">'
                f'<span style="font-weight:600;color:{fg};">{icon} {sev}</span>'
                f'<span style="font-weight:700;color:{fg};">{count}</span></div>'
                f'<div style="background:#f0ece6;border-radius:999px;height:8px;">'
                f'<div style="background:{fg};width:{pct:.0%};height:8px;border-radius:999px;"></div>'
                f'</div></div>',
                unsafe_allow_html=True,
            )
        st.markdown("</div>", unsafe_allow_html=True)

    with right:
        top_cats = dashboard.get("top_categories") or []
        st.markdown('<div class="panel">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Top Categories</div>', unsafe_allow_html=True)
        if top_cats:
            max_cat = max((r["count"] for r in top_cats), default=1)
            for row in top_cats:
                pct = row["count"] / max(max_cat, 1)
                st.markdown(
                    f'<div style="margin-bottom:8px;">'
                    f'<div style="display:flex;justify-content:space-between;margin-bottom:3px;">'
                    f'<span style="font-weight:500;color:var(--ink);">{row["category"]}</span>'
                    f'<span style="font-weight:700;color:var(--accent);">{row["count"]}</span></div>'
                    f'<div style="background:#f0ece6;border-radius:999px;height:7px;">'
                    f'<div style="background:var(--accent);width:{pct:.0%};height:7px;border-radius:999px;"></div>'
                    f'</div></div>',
                    unsafe_allow_html=True,
                )
        else:
            st.caption("No category data yet.")
        st.markdown("</div>", unsafe_allow_html=True)

    # ── Trends ──
    trends = (dashboard or {}).get("trends")
    if trends:
        col_a, col_b = st.columns(2, gap="large")
        with col_a:
            st.markdown('<div class="panel">', unsafe_allow_html=True)
            st.markdown(f'<div class="section-title">Tasks — Last {time_window}</div>', unsafe_allow_html=True)
            task_trend = {row["date"]: row["count"] for row in trends.get("tasks", [])}
            if task_trend:
                st.line_chart(task_trend)
            else:
                st.caption("No trend data.")
            st.markdown("</div>", unsafe_allow_html=True)
        with col_b:
            st.markdown('<div class="panel">', unsafe_allow_html=True)
            st.markdown(f'<div class="section-title">Findings — Last {time_window}</div>', unsafe_allow_html=True)
            finding_trend = {row["date"]: row["count"] for row in trends.get("findings", [])}
            if finding_trend:
                st.line_chart(finding_trend)
            else:
                st.caption("No trend data.")
            st.markdown("</div>", unsafe_allow_html=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _inject_css()

    if not _check_login():
        _login_page()
        return

    # ── Sidebar ──
    with st.sidebar:
        st.markdown(
            """
            <div style="padding:16px 0 8px;">
              <div style="font-size:1.35rem;font-weight:800;letter-spacing:-0.01em;">🧭 CodeReview</div>
              <div style="font-size:0.78rem;opacity:0.65;margin-top:2px;">AI review control center</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.divider()
        page = st.radio(
            "Navigate",
            ["📋  Tasks", "🔍  Review", "📊  Dashboard"],
            index=0,
            label_visibility="collapsed",
        )
        st.divider()
        st.caption(f"API: `{API_BASE}`")
        user = st.session_state.get("_auth_user", "")
        if user:
            st.caption(f"Signed in as **{user}**")
        if st.button("Sign Out", use_container_width=True, key="sidebar_logout"):
            _do_logout()
        st.markdown(
            f'<div style="position:fixed;bottom:16px;left:16px;font-size:0.72rem;opacity:0.45;">v{VERSION}</div>',
            unsafe_allow_html=True,
        )

    page_key = page.split()[-1]  # strip icon prefix
    if page_key == "Review":
        _render_review_page()
    elif page_key == "Tasks":
        _render_tasks_page()
    else:
        _render_dashboard()

    st.markdown(
        f'<div class="app-footer">CodeReview-Agent v{VERSION} &nbsp;·&nbsp; Powered by multi-agent AI</div>',
        unsafe_allow_html=True,
    )


main()





