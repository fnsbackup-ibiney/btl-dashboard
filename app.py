# app.py
# BTL Email Monitor Dashboard - Multi-user version with Google OAuth
# Each user logs in with their own Gmail and sees their own pending emails.

import base64
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime

import pandas as pd
import requests
import streamlit as st
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


# ── Config from Streamlit Secrets ──────────────────────
CLIENT_ID = st.secrets["GOOGLE_OAUTH_CLIENT_ID"]
CLIENT_SECRET = st.secrets["GOOGLE_OAUTH_CLIENT_SECRET"]
REDIRECT_URI = st.secrets["GOOGLE_OAUTH_REDIRECT_URI"]
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]

OAUTH_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

BUSINESS_KEYWORDS = ["SKY", "FNS", "BTL", "WH", "FCL", "Sendung", "Parcel", "Order"]
NOISE_DOMAINS = ["blot.new", "cloudhq.net", "bolt.eu"]
INTERNAL_DOMAIN = "ibiney.io"
SEARCH_DAYS = 7
BODY_MAX_CHARS = 3000


# ── Department & Client maps(同單人版)─────────────
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


# ── 工具函式 ───────────────────────────────────────
def extract_email(s):
    if not s:
        return ""
    m = re.search(r"<([^>]+)>", str(s))
    if m:
        return m.group(1).strip().lower()
    return str(s).strip().strip('"').lower()


def get_department(sender_raw):
    email = extract_email(sender_raw)
    if not email or "@" not in email:
        return DEFAULT_DEPARTMENT
    return DEPARTMENT_MAP.get(email, DEFAULT_DEPARTMENT)


def get_client(sender_raw):
    email = extract_email(sender_raw)
    if not email or "@" not in email:
        return "Unknown"
    domain = email.split("@")[1]
    base = domain.split(".")[0].split("-")[0].lower()
    return CLIENT_DISPLAY_MAP.get(base, base.capitalize())


def clean_sender(s):
    if not s:
        return s
    s = re.sub(r"\s*<[^>]+>", "", str(s))
    return s.strip().strip('"').strip()


def md_to_html(text):
    if not text:
        return ""
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", str(text))
    return s.replace("\n", "<br>")


def is_internal(email):
    return email.lower().endswith("@" + INTERNAL_DOMAIN.lower())


def is_noise_domain(email):
    lower = email.lower()
    return any(d in lower for d in NOISE_DOMAINS)


def format_age(hours):
    if hours < 1:
        return "< 1 小時"
    if hours < 24:
        return f"{hours} 小時"
    days = hours // 24
    rem = hours % 24
    return f"{days} 天" if rem == 0 else f"{days} 天 {rem} 小時"


# ── OAuth 登入流程 ────────────────────────────────
def build_oauth_flow():
    return Flow.from_client_config(
        {
            "web": {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI],
            }
        },
        scopes=OAUTH_SCOPES,
        redirect_uri=REDIRECT_URI,
    )


def get_login_url():
    flow = build_oauth_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return auth_url


def exchange_code_for_token(code):
    flow = build_oauth_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials
    # 取得使用者資訊
    user_info = requests.get(
        "https://www.googleapis.com/oauth2/v1/userinfo",
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=10,
    ).json()
    return {
        "creds": {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
        },
        "email": user_info.get("email", ""),
        "name": user_info.get("name", ""),
        "picture": user_info.get("picture", ""),
    }


def credentials_from_dict(creds_dict):
    return Credentials(
        token=creds_dict["token"],
        refresh_token=creds_dict.get("refresh_token"),
        token_uri=creds_dict["token_uri"],
        client_id=creds_dict["client_id"],
        client_secret=creds_dict["client_secret"],
        scopes=creds_dict["scopes"],
    )


# ── Gmail 抓信(每使用者各自抓)───────────────────
def build_gmail_query():
    kw_clause = " OR ".join([f"subject:{k}" for k in BUSINESS_KEYWORDS])
    noise_clause = " ".join([f"-from:{d}" for d in NOISE_DOMAINS])
    return f"in:inbox newer_than:{SEARCH_DAYS}d ({kw_clause}) {noise_clause}"


def parse_gmail_message(service, msg_id):
    """抓單一 Gmail message,回傳 dict。"""
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()

    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    subject = headers.get("Subject", "")
    from_raw = headers.get("From", "")
    date_str = headers.get("Date", "")

    # 解析日期
    try:
        msg_date = parsedate_to_datetime(date_str)
        if msg_date.tzinfo is None:
            msg_date = msg_date.replace(tzinfo=timezone.utc)
    except Exception:
        msg_date = datetime.now(timezone.utc)

    # 抓 plain text body
    body = extract_plain_body(msg.get("payload", {}))

    # 是否未讀
    is_unread = "UNREAD" in msg.get("labelIds", [])

    return {
        "id": msg_id,
        "subject": subject,
        "from": from_raw,
        "date": msg_date,
        "body": body,
        "is_unread": is_unread,
    }


def extract_plain_body(payload):
    """遞迴從 Gmail payload 取出 plain text body。"""
    if not payload:
        return ""
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")
    if mime_type == "text/plain" and body_data:
        try:
            return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
        except Exception:
            return ""
    parts = payload.get("parts", [])
    for p in parts:
        text = extract_plain_body(p)
        if text:
            return text
    return ""


def fetch_pending_emails(creds_dict, current_user_email):
    """
    抓使用者 Gmail 中符合條件的 thread,套用 B 邏輯:
    - 找 thread 最後一封外部信
    - 檢查使用者本人之後有沒有回(若有,跳過)
    """
    creds = credentials_from_dict(creds_dict)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    query = build_gmail_query()
    threads_resp = service.users().threads().list(
        userId="me", q=query, maxResults=200
    ).execute()
    threads = threads_resp.get("threads", [])

    items = []
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    now = datetime.now(timezone.utc)

    for t in threads:
        thread_full = service.users().threads().get(
            userId="me", id=t["id"], format="full"
        ).execute()
        messages_meta = thread_full.get("messages", [])
        if not messages_meta:
            continue

        # 找最後一封外部信
        last_external_idx = -1
        for i in range(len(messages_meta) - 1, -1, -1):
            from_header = next(
                (h["value"] for h in messages_meta[i].get("payload", {}).get("headers", [])
                 if h["name"] == "From"),
                "",
            )
            from_email = extract_email(from_header)
            if from_email and not is_internal(from_email):
                last_external_idx = i
                break
        if last_external_idx == -1:
            continue

        # 檢查使用者(登入者本人)之後有沒有回
        user_replied = False
        for j in range(last_external_idx + 1, len(messages_meta)):
            from_header = next(
                (h["value"] for h in messages_meta[j].get("payload", {}).get("headers", [])
                 if h["name"] == "From"),
                "",
            )
            from_email = extract_email(from_header)
            if from_email == current_user_email.lower():
                user_replied = True
                break
        if user_replied:
            continue

        # 取最後外部信完整內容
        last_msg = parse_gmail_message(service, messages_meta[last_external_idx]["id"])
        last_email = extract_email(last_msg["from"])
        if is_noise_domain(last_email):
            continue

        is_today = last_msg["date"] >= today_start
        age_hours = int((now - last_msg["date"]).total_seconds() // 3600)

        items.append({
            "msg_id": last_msg["id"],
            "subject": last_msg["subject"],
            "from": last_msg["from"],
            "date": last_msg["date"],
            "body": last_msg["body"],
            "is_unread": last_msg["is_unread"],
            "is_today": is_today,
            "age_hours": age_hours,
        })

    items.sort(key=lambda x: (
        not x["is_unread"],
        not x["is_today"],
        -x["age_hours"],
    ))
    return items


# ── Gemini 摘要 + 待辦事項 ─────────────────────────
@st.cache_data(ttl=86400, show_spinner=False)
def gemini_summary_and_actions(msg_id, subject, body):
    """以 msg_id 做快取 key。每使用者各自 session 不衝突。"""
    prompt = (
        "Analyze this business email and produce TWO sections in English. "
        "Use EXACTLY this format with [---] as separator (no extra text outside):\n\n"
        "**Theme:** [one concise line about what this email is about]\n"
        "- [Key point 1]\n- [Key point 2]\n- [Key point 3]\n\n"
        "[---]\n\n"
        "**🎯 What you need to do:**\n"
        "1. [specific action you should take]\n"
        "2. [another action if any]\n"
        "3. [another action if any]\n\n"
        "**📅 Deadline:** [extract date from email, or \"Not specified\"]\n"
        "**👤 Awaiting your reply:** [the person waiting]\n"
        "**📌 Context:** [one line of business context]\n\n"
        f"Email Subject: {subject}\n\nEmail Body:\n{body[:BODY_MAX_CHARS]}"
    )

    resp = requests.post(
        GEMINI_API_URL,
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 1500,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        },
        timeout=60,
    )
    if not resp.ok:
        return "(摘要產生失敗)", ""
    data = resp.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        full_text = "".join(p.get("text", "") for p in parts).strip()
        sections = full_text.split("[---]")
        return sections[0].strip(), (sections[1].strip() if len(sections) > 1 else "")
    except (KeyError, IndexError):
        return "(摘要產生失敗)", ""


def gemini_reply_draft(msg_id, subject, body, actions, sender_name, user_first_name):
    prompt = (
        f"Write a professional, concise English email reply on behalf of {user_first_name}. "
        f"Be friendly but business-appropriate. Sign off as \"Best regards,\\n{user_first_name}\".\n\n"
        f"The customer ({sender_name}) wrote:\n---\n{body[:BODY_MAX_CHARS]}\n---\n\n"
        f"Action items to communicate (already analyzed):\n{actions}\n\n"
        "Write a reply that:\n"
        "1. Acknowledges the customer briefly\n"
        "2. Addresses the action items naturally\n"
        "3. Confirms next steps and timing if mentioned\n"
        f"4. Ends with the sign-off above\n\n"
        "Output ONLY the email body text (no Subject line, no commentary, no markdown)."
    )
    resp = requests.post(
        GEMINI_API_URL,
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.5,
                "maxOutputTokens": 800,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        },
        timeout=60,
    )
    if not resp.ok:
        return None
    try:
        parts = resp.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError):
        return None


# ── Streamlit App 主流程 ──────────────────────────
st.set_page_config(page_title="BTL Email Monitor", page_icon="📧", layout="wide")


def show_login_page():
    st.title("📧 BTL Email Monitor")
    st.markdown("---")
    st.markdown("### 請使用 ibiney.io Google 帳號登入")
    st.markdown(
        "登入後,儀表板會顯示**你自己 Gmail 中**符合條件的客戶待回信件 + AI 摘要。"
    )
    auth_url = get_login_url()
    st.link_button("🔐 Sign in with Google", auth_url, type="primary")
    st.caption("⚠️ 首次登入會看到「Google hasn't verified this app」警告 → 點 Advanced → Continue,因為這是 ibiney 公司內部 app。")


def handle_oauth_callback():
    """處理從 Google 回來的 ?code=... query param。"""
    qp = st.query_params
    if "code" not in qp:
        return False
    try:
        result = exchange_code_for_token(qp["code"])
        # 限制只允許 ibiney.io 使用者
        if not result["email"].lower().endswith("@" + INTERNAL_DOMAIN):
            st.error(f"此系統僅限 @{INTERNAL_DOMAIN} 同事使用。你的帳號:{result['email']}")
            st.stop()
        st.session_state["user"] = result
        st.query_params.clear()
        st.rerun()
    except Exception as e:
        st.error(f"登入失敗:{e}")
        st.stop()


@st.fragment
def render_email_detail(item, user_name, user_first_name):
    msg_id = item["msg_id"]
    subject = item["subject"]
    body = item["body"]

    # session-state 編輯覆蓋
    saved = st.session_state.get(f"_saved_{msg_id}", {})
    display_subject = saved.get("title") or subject

    # 摘要 / 待辦(快取 24h,因為每封信 msg_id 唯一)
    with st.spinner("AI 摘要中..."):
        summary, actions = gemini_summary_and_actions(msg_id, subject, body)
    display_summary = saved.get("summary") or summary
    display_actions = saved.get("actions") or actions

    st.divider()
    st.markdown(f"### 📄 {display_subject}")

    if display_actions and display_actions.strip():
        st.markdown(
            f"""
<div style="background-color:#FFF8E1;border-left:6px solid #FFB300;
            padding:18px 24px;border-radius:6px;margin-bottom:20px;">
<h4 style="margin-top:0;color:#E65100;">🎯 你該做的事(優先看!)</h4>
<div style="font-size:15px;line-height:1.8;">{md_to_html(display_actions)}</div>
</div>
            """,
            unsafe_allow_html=True,
        )

    left_col, right_col = st.columns([1, 1])

    with left_col:
        st.markdown("### ✏️ 可編輯區(改完按下方儲存,僅本次 session 有效)")
        with st.form(key=f"edit_{msg_id}", clear_on_submit=False):
            new_title = st.text_input("📄 主旨", value=display_subject)
            new_summary = st.text_area("📝 AI 摘要", value=display_summary, height=200)
            new_actions = st.text_area("🎯 待辦事項", value=display_actions, height=200)
            if st.form_submit_button("💾 儲存(僅本次 session)", use_container_width=True):
                st.session_state[f"_saved_{msg_id}"] = {
                    "title": new_title,
                    "summary": new_summary,
                    "actions": new_actions,
                }
                st.rerun(scope="fragment")

    with right_col:
        st.markdown("### 📧 信件內容(原文,僅供參考)")
        st.text_area(
            "body", value=body, height=520, disabled=True, label_visibility="collapsed",
        )

    gmail_link = f"https://mail.google.com/mail/u/0/#inbox/{msg_id}"
    st.markdown(f"[🔗 在 Gmail 中開啟原信]({gmail_link})")

    # AI 回信草稿
    st.divider()
    st.markdown("### ✍️ AI 一鍵產生回信草稿")
    draft_key = f"_draft_{msg_id}"
    if st.button("✍️ 產生英文回信草稿", use_container_width=True, key=f"btn_{msg_id}"):
        with st.spinner("Gemini 寫回信中(10-20 秒)..."):
            sender_name = clean_sender(item["from"])
            draft = gemini_reply_draft(
                msg_id, subject, body, display_actions, sender_name, user_first_name
            )
        if draft:
            st.session_state[draft_key] = draft
            st.rerun(scope="fragment")
    if draft_key in st.session_state:
        st.markdown("##### 📝 建議回信")
        st.text_area(
            "draft",
            value=st.session_state[draft_key],
            height=300,
            label_visibility="collapsed",
            key=f"d_{msg_id}",
        )
        st.caption("✂️ 滑鼠選取 → ⌘+C 複製 → 開 Gmail Reply → ⌘+V 貼上")


def show_main_dashboard():
    user = st.session_state["user"]
    user_email = user["email"]
    user_name = user.get("name") or user_email
    user_first_name = user_name.split()[0] if user_name else "Me"

    # ── 標題列 ──
    title_col, user_col = st.columns([4, 1])
    with title_col:
        st.title("📧 BTL Email Monitor")
        st.caption(f"已登入:{user_name} ({user_email}) / 顯示你 Gmail 中過去 {SEARCH_DAYS} 天的待回客戶信件")
    with user_col:
        st.write("")
        if st.button("🔄 重新抓取", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        if st.button("🚪 登出", use_container_width=True):
            st.session_state.clear()
            st.query_params.clear()
            st.rerun()

    # ── 抓取郵件 ──
    with st.spinner("📬 正在從你的 Gmail 抓取待回信件(30-90 秒)..."):
        try:
            items = fetch_pending_emails(user["creds"], user_email)
        except Exception as e:
            st.error(f"抓取 Gmail 失敗:{e}")
            st.info("可能是 token 過期,請登出重新登入。")
            st.stop()

    if not items:
        st.success("🎉 你的 Gmail 中目前沒有待回客戶信件,辛苦了!")
        return

    # ── 轉成 DataFrame ──
    rows = []
    for it in items:
        badges = ["🔴 未讀未回" if it["is_unread"] else "🟡 已讀未回"]
        if it["is_today"]:
            badges.append("🔵 當日新進")
        rows.append({
            "msg_id": it["msg_id"],
            "優先級": " / ".join(badges),
            "寄件者": it["from"],
            "主旨": it["subject"],
            "收信日期": it["date"].strftime("%Y-%m-%d %H:%M"),
            "等待時長": format_age(it["age_hours"]),
            "郵件連結": f"https://mail.google.com/mail/u/0/#inbox/{it['msg_id']}",
            "_body": it["body"],
            "_item": it,
        })
    df = pd.DataFrame(rows)
    df["部門"] = df["寄件者"].apply(get_department)
    df["客戶"] = df["寄件者"].apply(get_client)

    # ── KPI 卡 ──
    total = len(df)
    unread_cnt = int(df["優先級"].str.contains("未讀未回", na=False).sum())
    read_cnt = int(df["優先級"].str.contains("已讀未回", na=False).sum())
    today_cnt = int(df["優先級"].str.contains("當日新進", na=False).sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📨 待處理總數", total)
    c2.metric("🔴 未讀未回", unread_cnt)
    c3.metric("🟡 已讀未回", read_cnt)
    c4.metric("🔵 當日新進", today_cnt)

    if unread_cnt > 0:
        st.warning(f"⚠️ 有 **{unread_cnt}** 封還沒打開過,建議優先處理")

    # ── 客戶 pills ──
    st.markdown("##### 🏢 快速依客戶篩選")
    client_counts = df["客戶"].value_counts().to_dict()
    client_options = [f"全部 ({total})"] + [
        f"{c} ({n})" for c, n in sorted(client_counts.items(), key=lambda x: -x[1])
    ]
    selected_client_label = st.pills(
        "客戶", client_options, default=client_options[0],
        label_visibility="collapsed",
    )
    selected_client = None
    if selected_client_label and not selected_client_label.startswith("全部"):
        selected_client = selected_client_label.rsplit(" (", 1)[0]

    st.divider()

    # ── 篩選器 ──
    fc1, fc2, fc3 = st.columns([1, 1, 2])
    with fc1:
        tag_count_map = {
            "🔴 未讀未回": unread_cnt,
            "🟡 已讀未回": read_cnt,
            "🔵 當日新進": today_cnt,
        }
        tag_opts = [f"{t} ({tag_count_map[t]})" for t in ["🔴 未讀未回", "🟡 已讀未回", "🔵 當日新進"]]
        show_tags_l = st.multiselect("狀態(選填)", tag_opts, placeholder="不勾 = 全部")
    with fc2:
        dept_count_map = df["部門"].value_counts().to_dict()
        dept_opts = [f"{d} ({dept_count_map.get(d, 0)})" for d in ALL_DEPARTMENTS]
        show_depts_l = st.multiselect("部門(選填)", dept_opts, placeholder="不勾 = 全部")
    with fc3:
        keyword = st.text_input("主旨 / 寄件者搜尋(選填)")

    show_tags = [t.rsplit(" (", 1)[0] for t in show_tags_l]
    show_depts = [d.rsplit(" (", 1)[0] for d in show_depts_l]

    view_df = df.copy()
    if show_tags:
        pat = "|".join([t.split(" ")[1] for t in show_tags])
        view_df = view_df[view_df["優先級"].str.contains(pat, na=False)]
    if show_depts:
        view_df = view_df[view_df["部門"].isin(show_depts)]
    if selected_client:
        view_df = view_df[view_df["客戶"] == selected_client]
    if keyword:
        kw = keyword.lower()
        view_df = view_df[
            view_df["主旨"].astype(str).str.lower().str.contains(kw, na=False)
            | view_df["寄件者"].astype(str).str.lower().str.contains(kw, na=False)
        ]
    view_df = view_df.reset_index(drop=True)

    # ── 寄件者去重複 ──
    sender_counts = view_df["寄件者"].value_counts().to_dict()
    deduped = []
    prev = None
    for s in view_df["寄件者"]:
        if s == prev:
            deduped.append("")
        else:
            n = sender_counts.get(s, 1)
            cleaned = clean_sender(s)
            deduped.append(f"{cleaned}  ({n})" if n > 1 else cleaned)
        prev = s
    view_df["寄件者"] = deduped
    deduped_depts = []
    for i, d in enumerate(view_df["部門"]):
        deduped_depts.append("" if deduped[i] == "" else d)
    view_df["部門"] = deduped_depts

    st.subheader(f"📋 待處理清單  ({len(view_df)} 筆)")
    st.caption("💡 點選任一列 → 下方展開待辦事項 + 摘要 + 信件原文 + AI 回信草稿")

    event = st.dataframe(
        view_df,
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
                "📧 開啟", display_text="🔗 點此打開", width="small",
            ),
            "msg_id": None, "客戶": None, "_body": None, "_item": None,
        },
    )

    selected_rows = event.selection.rows if event and event.selection else []
    if selected_rows:
        idx = selected_rows[0]
        item = view_df.iloc[idx]["_item"]
        render_email_detail(item, user_name, user_first_name)

    st.caption(f"頁面載入時間:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


# ── Entry ──────────────────────────────────────
def main():
    handle_oauth_callback()
    if "user" not in st.session_state:
        show_login_page()
    else:
        show_main_dashboard()


if __name__ == "__main__":
    main()
