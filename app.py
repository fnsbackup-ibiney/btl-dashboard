# app.py
# BTL Email Classifier Dashboard(Streamlit 版,測試階段:手動上傳模式)
# 啟動指令:streamlit run app.py

from datetime import datetime
import pandas as pd
import streamlit as st

from filters import load_from_upload, process_emails


# ── 頁面設定 ─────────────────────────────────────────────────
st.set_page_config(
    page_title="BTL Email Classifier",
    page_icon="📧",
    layout="wide",
)

st.title("📧 BTL Email Classifier Dashboard")
st.caption("測試階段:手動上傳 Excel / CSV — 未來將切換為 IMAP 自動拉取")


# ── 側邊欄:設定與上傳 ──────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 參數")

    today_input = st.date_input(
        "今日日期(用於計算等待時長)",
        value=datetime.now().date(),
    )

    st.divider()
    st.header("📥 資料來源")
    uploaded = st.file_uploader(
        "拖放 Excel / CSV 檔案到此",
        type=["xlsx", "xlsm", "csv"],
        help="檔案需包含三個欄位:Sender、Subject、Date",
    )

    st.divider()
    st.caption(
        "**過濾規則(filters.py)**\n\n"
        "- 排除網域:blot.new、bolt.new、cloudhq.net、bolt.eu\n"
        "- 排除行銷話術:save you hours、available now…\n"
        "- 業務關鍵字:SKY / FNS / BTL / WH / FCL / Sendung / Parcel / Order"
    )


# ── 主畫面 ───────────────────────────────────────────────────
if uploaded is None:
    st.info("👈 請從左側上傳郵件匯出檔(Excel 或 CSV)以開始分析。")
    st.stop()

# 讀取 + 處理
try:
    raw_df = load_from_upload(uploaded)
except Exception as e:
    st.error(f"讀檔失敗:{e}")
    st.stop()

try:
    today_dt = datetime.combine(today_input, datetime.min.time())
    result_df = process_emails(raw_df, today=today_dt)
except ValueError as e:
    st.error(f"資料格式錯誤:{e}")
    st.write("原始欄位:", list(raw_df.columns))
    st.stop()

# ── KPI 卡片 ─────────────────────────────────────────────────
total = len(result_df)
critical = int((result_df["Priority"] == "🔴 CRITICAL").sum())
new_cnt = int((result_df["Priority"] == "🟡 NEW").sum())
pending = int((result_df["Priority"] == "⚪️ PENDING").sum())

c1, c2, c3, c4 = st.columns(4)
c1.metric("📨 待處理總數", total)
c2.metric("🔴 緊急 Critical", critical, delta=None)
c3.metric("🟡 今日新進 NEW", new_cnt)
c4.metric("⚪️ 一般 PENDING", pending)

st.divider()

# ── 篩選器 ───────────────────────────────────────────────────
filter_cols = st.columns([1, 1, 2])
with filter_cols[0]:
    show_priorities = st.multiselect(
        "顯示優先級",
        options=["🔴 CRITICAL", "🟡 NEW", "⚪️ PENDING"],
        default=["🔴 CRITICAL", "🟡 NEW", "⚪️ PENDING"],
    )
with filter_cols[1]:
    min_wait = st.number_input("最少等待天數", min_value=0, value=0, step=1)
with filter_cols[2]:
    keyword = st.text_input("主旨/寄件者搜尋(選填)", value="")

view_df = result_df[result_df["Priority"].isin(show_priorities)].copy()
view_df = view_df[view_df["WaitDays"].fillna(-1) >= min_wait]
if keyword:
    kw = keyword.lower()
    view_df = view_df[
        view_df["Subject"].astype(str).str.lower().str.contains(kw, na=False)
        | view_df["Sender"].astype(str).str.lower().str.contains(kw, na=False)
    ]

# ── 表格(可排序) ───────────────────────────────────────────
st.subheader(f"📋 過濾後清單  ({len(view_df)} 筆)")

display_df = view_df[["Priority", "Sender", "Subject", "Date", "WaitDays"]].rename(
    columns={
        "Priority": "優先級",
        "Sender": "寄件者",
        "Subject": "主旨",
        "Date": "收信日",
        "WaitDays": "等待天數",
    }
)

st.dataframe(
    display_df,
    width="stretch",
    hide_index=True,
    column_config={
        "收信日": st.column_config.DatetimeColumn(format="YYYY-MM-DD"),
        "等待天數": st.column_config.NumberColumn(format="%d 天"),
    },
)

# ── 下載結果 ─────────────────────────────────────────────────
csv_bytes = display_df.to_csv(index=False).encode("utf-8-sig")
st.download_button(
    "⬇️ 下載過濾後 CSV",
    data=csv_bytes,
    file_name=f"btl_email_filtered_{today_input}.csv",
    mime="text/csv",
)
