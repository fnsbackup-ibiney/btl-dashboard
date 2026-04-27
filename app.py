# app.py
# BTL Email Monitor Dashboard(Streamlit 版,讀取 Google Sheet)

import re
from datetime import datetime
import pandas as pd
import streamlit as st


def clean_sender(s: str) -> str:
    """把 'Name <email@domain>' 簡化成 'Name'。"""
    if not s:
        return s
    s = re.sub(r"\s*<[^>]+>", "", str(s))
    s = s.strip().strip('"').strip()
    return s


# ── 設定 ─────────────────────────────────────────────────
SHEET_ID = "1N6cTXNPIQlmKrOzQqB22WoZ6qkvdh-u4ATl1WDmc_A0"
SHEET_NAME = "Sheet1"
CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
    f"/gviz/tq?tqx=out:csv&sheet={SHEET_NAME}"
)
CACHE_TTL = 300

st.set_page_config(
    page_title="BTL Email Monitor",
    page_icon="📧",
    layout="wide",
)


@st.cache_data(ttl=CACHE_TTL, show_spinner="正在從 Google Sheet 讀取最新資料...")
def load_sheet():
    df = pd.read_csv(CSV_URL)
    df = df.iloc[:, :6]
    df.columns = ["優先級", "寄件者", "主旨", "收信日期", "等待時長", "郵件連結"]
    df = df.dropna(subset=["優先級"]).reset_index(drop=True)
    return df


title_col, btn_col = st.columns([4, 1])
with title_col:
    st.title("📧 BTL Email Monitor Dashboard")
    st.caption(
        f"資料來源:GAS 每小時自動從 fnsbackup@ibiney.io 抓取 → Google Sheet → 此頁面"
        f" / 本頁快取 {CACHE_TTL // 60} 分鐘"
    )
with btn_col:
    st.write("")
    if st.button("🔄 立即重新整理", use_container_width=True):
        st.cache_data.clear()
        st.rerun()


try:
    df = load_sheet()
except Exception as e:
    st.error(f"無法讀取 Google Sheet:{e}")
    st.info("請確認 Sheet 已設為「任何取得連結的人都能檢視」,或是 GAS 是否正常產出資料。")
    st.stop()


# ── KPI 卡片 ──
total = len(df)
unread_cnt = int(df["優先級"].str.contains("未讀未回", na=False).sum())
read_cnt = int(df["優先級"].str.contains("已讀未回", na=False).sum())
today_cnt = int(df["優先級"].str.contains("當日新進", na=False).sum())

c1, c2, c3, c4 = st.columns(4)
c1.metric("📨 待處理總數", total)
c2.metric("🔴 未讀未回", unread_cnt, help="客戶寄來但你還沒打開過")
c3.metric("🟡 已讀未回", read_cnt, help="你看過但還沒回覆")
c4.metric("🔵 當日新進", today_cnt, help="今日才到的新郵件(可能同時也是紅或黃)")

if unread_cnt > 0:
    st.warning(f"⚠️ 有 **{unread_cnt}** 封還沒打開過,建議優先處理")
elif total == 0:
    st.success("🎉 目前沒有待處理郵件,辛苦了")

st.divider()


# ── 篩選器 ──
fc1, fc2 = st.columns([1, 2])
with fc1:
    show_tags = st.multiselect(
        "顯示包含以下狀態的郵件",
        options=["🔴 未讀未回", "🟡 已讀未回", "🔵 當日新進"],
        default=["🔴 未讀未回", "🟡 已讀未回", "🔵 當日新進"],
        help="勾選的狀態任一符合即顯示(OR 邏輯)",
    )
with fc2:
    keyword = st.text_input("主旨 / 寄件者搜尋(選填)", value="")

if show_tags:
    pattern = "|".join([t.split(" ")[1] for t in show_tags])
    view_df = df[df["優先級"].str.contains(pattern, na=False)].copy()
else:
    view_df = df.iloc[0:0].copy()

if keyword:
    kw = keyword.lower()
    view_df = view_df[
        view_df["主旨"].astype(str).str.lower().str.contains(kw, na=False)
        | view_df["寄件者"].astype(str).str.lower().str.contains(kw, na=False)
    ]


# ── 依寄件者分組 + 視覺去重複 + 換欄位順序 + 清理寄件者 ──
display_df = view_df.copy().reset_index(drop=True)

if not display_df.empty:
    sender_first_pos = {}
    for i, s in enumerate(display_df["寄件者"]):
        if s not in sender_first_pos:
            sender_first_pos[s] = i
    display_df["_group_rank"] = display_df["寄件者"].map(sender_first_pos)
    display_df = (
        display_df
        .sort_values("_group_rank", kind="stable")
        .drop(columns=["_group_rank"])
        .reset_index(drop=True)
    )

sender_counts = display_df["寄件者"].value_counts().to_dict()
deduped_senders = []
prev_sender = None
for s in display_df["寄件者"]:
    if s == prev_sender:
        deduped_senders.append("")
    else:
        n = sender_counts.get(s, 1)
        cleaned = clean_sender(s)
        deduped_senders.append(f"{cleaned}  ({n})" if n > 1 else cleaned)
    prev_sender = s
display_df["寄件者"] = deduped_senders

display_df = display_df[["寄件者", "優先級", "主旨", "收信日期", "等待時長", "郵件連結"]]


# ── 表格 ──
st.subheader(f"📋 待處理清單  ({len(view_df)} 筆)")

st.dataframe(
    display_df,
    width="stretch",
    hide_index=True,
    column_config={
        "寄件者": st.column_config.TextColumn(width="medium"),
        "優先級": st.column_config.TextColumn(width="medium"),
        "主旨": st.column_config.TextColumn(width="large"),
        "收信日期": st.column_config.TextColumn(width="small"),
        "等待時長": st.column_config.TextColumn(width="small"),
        "郵件連結": st.column_config.LinkColumn(
            "📧 開啟",
            display_text="🔗 點此打開",
            width="small",
            help="點擊跳轉至 Gmail(需以 fnsbackup@ibiney.io 登入)",
        ),
    },
)


st.caption(
    f"頁面載入時間:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} / "
    f"如需查看最新狀態請按右上「🔄 立即重新整理」"
)
