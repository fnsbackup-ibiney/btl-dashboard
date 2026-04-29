# app.py
# BTL Email Monitor Dashboard

import re
from datetime import datetime
import pandas as pd
import requests
import streamlit as st


# GAS Web App 端點(用來把編輯後的標題/摘要寫回 Google Sheet)
GAS_WEBAPP_URL = "https://script.google.com/macros/s/AKfycbxYxacAovh3YB5d4ReRjYJ_UGgFyJzD6aHRNmvxv0vqCWd5faeqkwd5D2YJjMk11zmO/exec"


DEPARTMENT_MAP = {
    "a.krumme@fuchsschmitt.de": "🔥 EXECUTIVE",
    "m.haberkorn@fuchsschmitt.de": "🔥 EXECUTIVE",
    "vollkauf_ccc@fuchsschmitt.de": "🔧 TECHNICAL",
    "b.monjau@fuchsschmitt.de": "🔧 TECHNICAL",
    "v.nykrake@fuchsschmitt.de": "🔧 TECHNICAL",
    "d.litty@fuchsschmitt.de": "🔧 TECHNICAL",
    "j.ernst@fuchsschmitt.de": "🔧 TECHNICAL",
    "l.bickert@fuchsschmitt.de": "📦 LOGISTICS & PURCHASING",
    "vollkauf@fuchsschmitt.de": "📦 LOGISTICS & PURCHASING",
    "h.schnack@fuchsschmitt.de": "📦 LOGISTICS & PURCHASING",
    "fareast@fuchsschmitt.de": "📦 LOGISTICS & PURCHASING",
    "accounting@fuchsschmitt.de": "📦 LOGISTICS & PURCHASING",
    "o.kerber@fuchsschmitt.de": "🎨 DESIGN & STYLING",
    "m.zellner@fuchsschmitt.de": "🎨 DESIGN & STYLING",
    "a.sieger@fuchsschmitt.de": "🎨 DESIGN & STYLING",
    "k.weintz@fuchsschmitt.de": "🎨 DESIGN & STYLING",
    "n.bachmann@fuchsschmitt.de": "🎨 DESIGN & STYLING",
    "p.brueck@fuchsschmitt.de": "🎨 DESIGN & STYLING",
    "n.loessl@fuchsschmitt.de": "🎨 DESIGN & STYLING",
    "m.lehrmann@fuchsschmitt.de": "📋 ORDER MGMT & ADMIN",
    "a.meinhard@fuchsschmitt.de": "📋 ORDER MGMT & ADMIN",
    "e.ohlenmacher@fuchsschmitt.de": "📋 ORDER MGMT & ADMIN",
    "c.dietz@fuchsschmitt.de": "📋 ORDER MGMT & ADMIN",
    "m.schlett@fuchsschmitt.de": "📋 ORDER MGMT & ADMIN",
    "m.zengel@fuchsschmitt.de": "📋 ORDER MGMT & ADMIN",
    "sohaib.irshad@brax.com": "📋 ORDER MGMT & ADMIN",
}
DEFAULT_DEPARTMENT = "❓ OTHER"
ALL_DEPARTMENTS = [
    "🔥 EXECUTIVE", "🔧 TECHNICAL", "📦 LOGISTICS & PURCHASING",
    "🎨 DESIGN & STYLING", "📋 ORDER MGMT & ADMIN", "❓ OTHER",
]

CLIENT_DISPLAY_MAP = {
    "skyfashion": "Skyfashion",
    "fuchsschmitt": "Fuchsschmitt",
    "brax": "Brax",
    "wanshisheng": "Wanshisheng",
    "ytxinzhong": "Ytxinzhong",
    "goldenbridgetextile": "Goldenbridge",
}


def extract_email(s: str) -> str:
    if not s:
        return ""
    txt = str(s)
    m = re.search(r"<([^>]+)>", txt)
    if m:
        return m.group(1).strip().lower()
    return txt.strip().strip('"').lower()


def get_department(sender_raw) -> str:
    email = extract_email(sender_raw)
    if not email or "@" not in email:
        return DEFAULT_DEPARTMENT
    return DEPARTMENT_MAP.get(email, DEFAULT_DEPARTMENT)


def get_client(sender_raw) -> str:
    email = extract_email(sender_raw)
    if not email or "@" not in email:
        return "Unknown"
    domain = email.split("@")[1]
    base = domain.split(".")[0].split("-")[0].lower()
    return CLIENT_DISPLAY_MAP.get(base, base.capitalize())


def clean_sender(s: str) -> str:
    if not s:
        return s
    s = re.sub(r"\s*<[^>]+>", "", str(s))
    s = s.strip().strip('"').strip()
    return s


def save_edit_to_gas(msg_id: str, title: str, summary: str):
    """把編輯後的標題/摘要 POST 到 GAS Web App,寫進 _UserEdits 工作表。"""
    try:
        resp = requests.post(
            GAS_WEBAPP_URL,
            json={"msgId": msg_id, "title": title, "summary": summary},
            timeout=15,
        )
        if resp.ok:
            return True, ""
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)


SHEET_ID = "1N6cTXNPIQlmKrOzQqB22WoZ6qkvdh-u4ATl1WDmc_A0"
SHEET_NAME = "Sheet1"
CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
    f"/gviz/tq?tqx=out:csv&sheet={SHEET_NAME}"
)
CACHE_TTL = 300

st.set_page_config(page_title="BTL Email Monitor", page_icon="📧", layout="wide")


@st.cache_data(ttl=CACHE_TTL, show_spinner="正在從 Google Sheet 讀取最新資料...")
def load_sheet():
    df = pd.read_csv(CSV_URL)
    expected = ["msg_id", "優先級", "寄件者", "主旨", "收信日期", "等待時長", "郵件連結", "摘要", "信件內容"]
    actual_cols = min(df.shape[1], len(expected))
    df = df.iloc[:, :actual_cols].copy()
    df.columns = expected[:actual_cols]
    for col in expected:
        if col not in df.columns:
            df[col] = ""
    df = df.dropna(subset=["優先級"]).reset_index(drop=True)
    df["部門"] = df["寄件者"].apply(get_department)
    df["客戶"] = df["寄件者"].apply(get_client)
    return df


title_col, btn_col = st.columns([4, 1])
with title_col:
    st.title("📧 BTL Email Monitor Dashboard")
    st.caption(
        f"資料來源:GAS 每小時自動從 fnsbackup@ibiney.io 抓取 + Gemini 摘要 → Google Sheet → 此頁面"
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


st.markdown("##### 🏢 快速依客戶篩選")
client_counts = df["客戶"].value_counts().to_dict()
client_options = [f"全部 ({total})"] + [
    f"{c} ({n})" for c, n in sorted(client_counts.items(), key=lambda x: -x[1])
]
selected_client_label = st.pills(
    "客戶",
    client_options,
    default=client_options[0],
    label_visibility="collapsed",
)

selected_client_name = None
if selected_client_label and not selected_client_label.startswith("全部"):
    selected_client_name = selected_client_label.rsplit(" (", 1)[0]


st.divider()


tag_count_map = {
    "🔴 未讀未回": unread_cnt,
    "🟡 已讀未回": read_cnt,
    "🔵 當日新進": today_cnt,
}
tag_options_with_count = [
    f"{t} ({tag_count_map[t]})" for t in ["🔴 未讀未回", "🟡 已讀未回", "🔵 當日新進"]
]
dept_count_map = df["部門"].value_counts().to_dict()
dept_options_with_count = [
    f"{d} ({dept_count_map.get(d, 0)})" for d in ALL_DEPARTMENTS
]

fc1, fc2, fc3 = st.columns([1, 1, 2])
with fc1:
    show_tags_labeled = st.multiselect(
        "顯示包含以下狀態的郵件(選填)",
        options=tag_options_with_count,
        placeholder="不勾 = 顯示全部",
    )
with fc2:
    show_depts_labeled = st.multiselect(
        "部門(選填)",
        options=dept_options_with_count,
        placeholder="不勾 = 顯示全部",
    )
with fc3:
    keyword = st.text_input("主旨 / 寄件者搜尋(選填)", value="")

show_tags = [t.rsplit(" (", 1)[0] for t in show_tags_labeled]
show_depts = [d.rsplit(" (", 1)[0] for d in show_depts_labeled]


if show_tags:
    pattern = "|".join([t.split(" ")[1] for t in show_tags])
    view_df = df[df["優先級"].str.contains(pattern, na=False)].copy()
else:
    view_df = df.copy()

if show_depts:
    view_df = view_df[view_df["部門"].isin(show_depts)].copy()

if selected_client_name:
    view_df = view_df[view_df["客戶"] == selected_client_name].copy()

if keyword:
    kw = keyword.lower()
    view_df = view_df[
        view_df["主旨"].astype(str).str.lower().str.contains(kw, na=False)
        | view_df["寄件者"].astype(str).str.lower().str.contains(kw, na=False)
    ]


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

deduped_depts = []
for i, s in enumerate(display_df["部門"]):
    deduped_depts.append("" if deduped_senders[i] == "" else s)
display_df["部門"] = deduped_depts


st.subheader(f"📋 待處理清單  ({len(view_df)} 筆)")
st.caption("💡 點選任一列 → 下方可編輯標題與摘要,儲存後跨次打開仍保留")

event = st.dataframe(
    display_df,
    width="stretch",
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    column_order=["寄件者", "部門", "優先級", "主旨", "收信日期", "等待時長", "郵件連結"],
    column_config={
        "寄件者": st.column_config.TextColumn(width="medium"),
        "部門": st.column_config.TextColumn(width="medium"),
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
        "msg_id": None,
        "客戶": None,
        "摘要": None,
        "信件內容": None,
    },
)

selected_rows = event.selection.rows if event and event.selection else []
if selected_rows:
    idx = selected_rows[0]
    row = display_df.iloc[idx]
    msg_id = str(row.get("msg_id", "") or "")
    summary_text = str(row.get("摘要", "") or "")
    body_text = str(row.get("信件內容", "") or "")
    subject = str(row.get("主旨", "") or "")
    link = str(row.get("郵件連結", "") or "")

    st.divider()

    left_col, right_col = st.columns([1, 1])

    with left_col:
        st.markdown("### ✏️ 可編輯區(改完按下方儲存)")
        with st.form(key=f"edit_form_{msg_id}", clear_on_submit=False):
            new_title = st.text_input("📄 主旨", value=subject)
            new_summary = st.text_area(
                "📝 AI 摘要", value=summary_text, height=280,
                help="可手動修改,例如 Gemini 摘錯時"
            )
            saved = st.form_submit_button("💾 儲存修改", use_container_width=True)
            if saved:
                if not msg_id:
                    st.error("缺少 msg_id,無法儲存(請先在 GAS 跑一次 refreshDashboard)")
                else:
                    ok, err = save_edit_to_gas(msg_id, new_title, new_summary)
                    if ok:
                        st.success("已儲存 ✅ 下次 GAS 自動更新時會把你的版本套用回 Sheet")
                        st.cache_data.clear()
                    else:
                        st.error(f"儲存失敗:{err}")

    with right_col:
        st.markdown("### 📧 信件內容(原文,僅供參考)")
        if body_text and body_text.strip():
            st.text_area(
                label="body",
                value=body_text,
                height=400,
                disabled=True,
                label_visibility="collapsed",
            )
        else:
            st.info("尚無信件內容")

    if link:
        st.markdown(f"[🔗 在 Gmail 中開啟]({link})")


st.caption(
    f"頁面載入時間:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} / "
    f"如需查看最新狀態請按右上「🔄 立即重新整理」"
)
