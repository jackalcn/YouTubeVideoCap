from __future__ import annotations

import streamlit as st


LIGHT_PALETTE = {
    "app-background": "linear-gradient(165deg, #f4f7fb 0%, #eef2f7 40%, #edf3f5 100%)",
    "sidebar-background": "linear-gradient(180deg, #f9fbfd 0%, #f2f6fb 100%)",
    "sidebar-border": "#dde6ef",
    "input-background": "#ffffff",
    "input-text": "#0f2238",
    "input-border": "#93abc1",
    "input-border-hover": "#6f8ea9",
    "input-placeholder": "#67809b",
    "focus-ring": "rgba(14, 106, 119, 0.18)",
    "accent": "#0e6a77",
    "title": "#1d2b3f",
    "subtitle": "#5f6f85",
    "muted": "#5f7088",
    "strong": "#17314d",
    "card-background": "#ffffff",
    "card-border": "#deebf3",
    "card-shadow": "rgba(18, 34, 58, 0.06)",
    "panel-background": "linear-gradient(135deg, rgba(255, 255, 255, 0.98), rgba(232, 246, 248, 0.9))",
    "tile-background": "rgba(245, 250, 252, 0.92)",
    "log-background": "#f8fbfd",
    "log-text": "#27415a",
    "dot-off": "#cbd8e4",
}
DARK_PALETTE = {
    "app-background": "linear-gradient(165deg, #10161e 0%, #121a23 40%, #101b20 100%)",
    "sidebar-background": "linear-gradient(180deg, #151d27 0%, #121922 100%)",
    "sidebar-border": "#26323f",
    "input-background": "#1a2430",
    "input-text": "#e6edf5",
    "input-border": "#3d4f62",
    "input-border-hover": "#5c7690",
    "input-placeholder": "#8096ab",
    "focus-ring": "rgba(53, 178, 196, 0.25)",
    "accent": "#35b2c4",
    "title": "#eaf1f8",
    "subtitle": "#a3b4c6",
    "muted": "#9aacbf",
    "strong": "#dfe9f3",
    "card-background": "#18212c",
    "card-border": "#28394a",
    "card-shadow": "rgba(0, 0, 0, 0.35)",
    "panel-background": "linear-gradient(135deg, rgba(26, 36, 48, 0.98), rgba(18, 42, 48, 0.9))",
    "tile-background": "rgba(31, 42, 55, 0.92)",
    "log-background": "#141c25",
    "log-text": "#c3d2e0",
    "dot-off": "#3a4a5a",
}

STYLESHEET = """
<style>
    .stApp {
        background: var(--vc-app-background);
    }
    [data-testid="stSidebar"] {
        background: var(--vc-sidebar-background);
        border-right: 1px solid var(--vc-sidebar-border);
    }
    [data-testid="stSidebar"] [data-testid="stTextInput"] input,
    [data-testid="stSidebar"] [data-testid="stTextArea"] textarea,
    [data-testid="stSidebar"] [data-testid="stNumberInput"] input {
        background: var(--vc-input-background);
        color: var(--vc-input-text);
        border: 1px solid var(--vc-input-border);
        border-radius: 10px;
    }
    [data-testid="stSidebar"] [data-testid="stTextInput"] input::placeholder,
    [data-testid="stSidebar"] [data-testid="stTextArea"] textarea::placeholder {
        color: var(--vc-input-placeholder);
    }
    [data-testid="stSidebar"] [data-testid="stTextInput"] input:focus,
    [data-testid="stSidebar"] [data-testid="stTextArea"] textarea:focus,
    [data-testid="stSidebar"] [data-testid="stNumberInput"] input:focus {
        border-color: var(--vc-accent);
        box-shadow: 0 0 0 2px var(--vc-focus-ring);
    }
    [data-testid="stSidebar"] div[data-baseweb="select"] > div,
    [data-testid="stSidebar"] [data-baseweb="base-input"] > div {
        background: var(--vc-input-background);
        border: 1px solid var(--vc-input-border);
        color: var(--vc-input-text);
    }
    [data-testid="stSidebar"] div[data-baseweb="select"] > div:hover,
    [data-testid="stSidebar"] [data-baseweb="base-input"] > div:hover {
        border-color: var(--vc-input-border-hover);
    }
    .hero-title {
        font-size: 44px;
        font-weight: 800;
        letter-spacing: -0.02em;
        color: var(--vc-title);
        margin-bottom: 4px;
    }
    .hero-subtitle {
        color: var(--vc-subtitle);
        font-size: 17px;
        margin-bottom: 8px;
    }
    .page-title {
        font-size: 30px;
        font-weight: 800;
        letter-spacing: -0.01em;
        color: var(--vc-title);
        margin-bottom: 2px;
    }
    .meta-line {
        color: var(--vc-muted);
        font-size: 14px;
        margin-bottom: 12px;
    }
    .meta-panel {
        margin: 12px 0 22px;
        padding: 18px 20px;
        background: var(--vc-panel-background);
        border: 1px solid var(--vc-card-border);
        border-radius: 18px;
        box-shadow: 0 16px 34px var(--vc-card-shadow);
    }
    .meta-panel-caption {
        margin-bottom: 12px;
        color: var(--vc-muted);
        font-size: 12px;
        font-weight: 700;
        /* The caption is Chinese, so the wide tracking an English small-caps label gets would only pull the characters apart. */
        letter-spacing: 0.06em;
    }
    .badge-row {
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
    }
    .badge-row-primary {
        margin-bottom: 14px;
    }
    .meta-badge {
        display: flex;
        align-items: center;
        gap: 10px;
        flex: 1 1 210px;
        min-width: 0;
        padding: 12px 14px;
        background: var(--vc-card-background);
        border: 1px solid var(--vc-card-border);
        border-radius: 14px;
    }
    .badge-dot {
        width: 10px;
        height: 10px;
        flex: 0 0 10px;
        border-radius: 999px;
        background: var(--vc-accent);
    }
    .meta-badge-label {
        color: var(--vc-muted);
        font-size: 12px;
        font-weight: 700;
        letter-spacing: 0.04em;
        white-space: nowrap;
    }
    .meta-badge-value {
        min-width: 0;
        color: var(--vc-strong);
        font-size: 15px;
        font-weight: 700;
    }
    .meta-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
        gap: 12px;
    }
    .meta-card {
        min-width: 0;
        padding: 14px 16px;
        background: var(--vc-tile-background);
        border: 1px solid var(--vc-card-border);
        border-radius: 14px;
    }
    .meta-card-label {
        margin-bottom: 6px;
        color: var(--vc-muted);
        font-size: 12px;
        font-weight: 700;
        letter-spacing: 0.04em;
    }
    .meta-card-value {
        color: var(--vc-strong);
        font-size: 14px;
        font-weight: 700;
        line-height: 1.45;
        word-break: break-word;
    }
    .meta-card-value.mono {
        font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
        font-size: 13px;
    }
    .video-title {
        color: var(--vc-strong);
        font-size: 20px;
        font-weight: 700;
        line-height: 1.35;
        margin-bottom: 6px;
    }
    .job-title {
        color: var(--vc-strong);
        font-size: 15px;
        font-weight: 700;
        line-height: 1.4;
        word-break: break-word;
    }
    .job-status {
        color: var(--vc-muted);
        font-size: 13px;
        line-height: 1.45;
        margin-bottom: 8px;
    }
    .log-box {
        max-height: 280px;
        overflow-y: auto;
        background: var(--vc-log-background);
        border: 1px solid var(--vc-card-border);
        border-radius: 10px;
        padding: 12px;
        font-family: Consolas, monospace;
        font-size: 13px;
        color: var(--vc-log-text);
        line-height: 1.6;
        word-break: break-all;
    }
    .health-card {
        background: var(--vc-card-background);
        border: 1px solid var(--vc-card-border);
        border-radius: 12px;
        padding: 12px 14px;
        margin-bottom: 10px;
        box-shadow: 0 8px 20px var(--vc-card-shadow);
    }
    .health-title {
        font-size: 14px;
        color: var(--vc-strong);
        margin-bottom: 8px;
        font-weight: 700;
    }
    .health-lights {
        display: flex;
        gap: 16px;
        align-items: center;
        margin-bottom: 8px;
    }
    .light-item {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        color: var(--vc-muted);
    }
    .light-dot {
        width: 12px;
        height: 12px;
        border-radius: 50%;
        background: var(--vc-dot-off);
    }
    .light-dot.on.green {
        background: #22c55e;
        box-shadow: 0 0 0 5px rgba(34, 197, 94, 0.15);
    }
    .light-dot.on.yellow {
        background: #f59e0b;
        box-shadow: 0 0 0 5px rgba(245, 158, 11, 0.18);
    }
    .light-dot.on.red {
        background: #ef4444;
        box-shadow: 0 0 0 5px rgba(239, 68, 68, 0.16);
    }
    .health-message {
        color: var(--vc-strong);
        font-size: 13px;
    }
    @media (max-width: 900px) {
        .hero-title {
            font-size: 32px;
        }
        .hero-subtitle {
            font-size: 15px;
        }
        .meta-panel {
            padding: 16px;
        }
        .meta-badge {
            flex-basis: 100%;
        }
        .meta-grid {
            grid-template-columns: 1fr;
        }
    }
</style>
"""


def current_palette() -> dict[str, str]:
    # Streamlit reruns the app when the theme changes, so this stays in step with the frontend.
    return DARK_PALETTE if st.context.theme.type == "dark" else LIGHT_PALETTE


def build_stylesheet(palette: dict[str, str]) -> str:
    variables = "".join(f"--vc-{name}: {value};" for name, value in palette.items())
    return f"<style>:root {{{variables}}}</style>{STYLESHEET}"


def apply_custom_style() -> None:
    st.markdown(build_stylesheet(current_palette()), unsafe_allow_html=True)
