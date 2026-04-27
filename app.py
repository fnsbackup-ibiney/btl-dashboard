# app.py
# BTL Email Monitor Dashboard(Streamlit 版,讀取 Google Sheet)
# 資料流:fnsbackup@ibiney.io → GAS(每小時)→ Google Sheet → 此頁面
# 啟動指令(本機測試用):streamlit run app.py

from datetime import datetime
import pandas as pd
import streamlit as st

# ── 設定 ─────────────────────────────────────────────────
SHEET_ID = "1N6cTXNPIQlmKrOzQqB22WoZ6qkvdh-u4ATl1WDmc_A0"
SHEET_NAME = "Sheet1"
CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
    f"/gviz/tq?tqx=out:csv&sheet={SHEET_NAME}"
)

# 快取秒數(避免每次重新整理都打 Google,5 分鐘夠用)
CACHE_TTL = 300


# ── 頁面設定 ─────────────────────────────────────────────
st.set_page_config(
    page_title="BTL Email Monitor",
    page_icon="📧",
    layout="wide",
)


# ── 資料讀取(快取) ─────────────────────────────────────
@st.cache_data(ttl=CACHE_TTL, show_spinner="正在從 Google Sheet 讀取最新資料...")
def load_sheet():
    """從 Google Sheet 抓取最新待處理清單。"""
    df = pd.read_csv(CSV_URL)
    # GAS 寫入的欄位順序:優先級、寄件者、主旨、收信日期、等待時長、郵件連結
    # CSV 可能多帶到 H 欄(最後更新)和空白 G 欄,只取前 6 欄
    df = df.iloc[:, :6]
    df.columns = ["優先級", "寄件者", "主旨", "收信日期", "等待時長", "郵件連結"]
    df = df.dropna(subset=["優先級"]).reset_index(drop=True)
    return df


# ── 標題列 ───────────────────────────────────────────────
title_col, btn_col = st.columns([4, 1])
with title_col:
    st.title("📧 BTL Email Monitor Dashboard")
    st.caption(
        f"資料來源:GAS 每小時自動從 fnsbackup@ibiney.io 抓取 → Google Sheet → 此頁面"
        f" / 本頁快取 {CACHE_TTL // 60} 分鐘"
    )
with btn_col:
    st.write("")  # 對齊用
    if st.button("🔄 立即重新整理", use_container_width=True):
        st.cache_data.clear()
        st.rerun()


# ── 載入資料 ─────────────────────────────────────────────
try:
    df = load_sheet()
except Exception as e:
    st.error(f"無法讀取 Google Sheet:{e}")
    st.info(
        "請確認 Sheet 已設為「任何取得連結的人都能檢視」,"
        "或是 GAS 是否正常產出資料。"
    )
    st.stop()


# ── KPI 卡片 ────────────────────────────────────────────
total = len(df)
critical = int(df["優先級"].str.contains("CRITICAL", na=False).sum())
new_count = int(df["優先級"].str.contains("NEW", na=False).sum())

c1, c2, c3 = st.columns(3)
c1.metric("📨 待處理總數", total)
c2.metric("🔴 緊急 CRITICAL", critical)
c3.metric("🟡 新進 NEW", new_count)

if critical > 0:
    st.warning(f"⚠️ 有 **{critical}** 封等待超過 24 小時,請優先處理")
elif total == 0:
    st.success("🎉 目前沒有待處理郵件,辛苦了")

st.divider()


# ── 篩選器 ─────────────────────────────────────────────
fc1, fc2 = st.columns([1, 2])
with fc1:
    show_pri = st.multiselect(
        "顯示優先級",
        options=["🔴 CRITICAL", "🟡 NEW"],
        default=["🔴 CRITICAL", "🟡 NEW"],
    )
with fc2:
    keyword = st.text_input("主旨 / 寄件者搜尋(選填)", value="")

view_df = df[df["優先級"].isin(show_pri)].copy()
if keyword:
    kw = keyword.lower()
    view_df = view_df[
        view_df["主旨"].astype(str).str.lower().str.contains(kw, na=False)
        | view_df["寄件者"].astype(str).str.lower().str.contains(kw, na=False)
    ]


# ── 表格 ───────────────────────────────────────────────
st.subheader(f"📋 待處理清單  ({len(view_df)} 筆)")

st.dataframe(
    view_df,
    width="stretch",
    hide_index=True,
    column_config={
        "優先級": st.column_config.TextColumn(width="small"),
        "寄件者": st.column_config.TextColumn(width="medium"),
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


# ── 頁尾 ───────────────────────────────────────────────
st.caption(
    f"頁面載入時間:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} / "
    f"如需查看最新狀態請按右上「🔄 立即重新整理」"
)
