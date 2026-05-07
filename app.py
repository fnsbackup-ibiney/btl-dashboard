# app.py
# BTL Email Monitor - Group same-topic emails with unified title

import base64
import re
import urllib.parse
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import pandas as pd
import requests
import streamlit as st
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


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
SEARCH_DAYS = 3
BODY_MAX_CHARS = 3000
# 整 thread 餵 Gemini 時,單封信內容上限(避免超大附件信吃光配額)
THREAD_PER_MSG_CHARS = 1500
# 整 thread 總上限(Gemini Flash context 1M tokens 雖然吃得下,但仍設保險閾值)
THREAD_TOTAL_CHARS = 12000


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


def extract_theme(summary_text):
    if not summary_text:
        return ""
    m = re.search(r"\*\*Theme:\*\*\s*(.+?)(?:\n|$)", summary_text)
    if m:
        return m.group(1).strip().strip('"').strip("'")
    return ""


def extract_topic_key(text):
    """
    從文字中抓「訂單編號 / 款號」,作為「同主題」配對的 key。
    例:'Re: SKY 80025 sample' → 'SKY-80025'
        'BRAX 06388 mockup'   → 'BRAX-06388'
        'updated PI for #2317' → 'NUM-2317'
        '客戶問候' → None
    """
    if not text:
        return None
    upper = str(text).upper()
    # 主要模式:品牌前綴 + 4-6 位數字
    m = re.search(
        r"\b(SKY|BRAX|WH|YAN|BIN|FNS|BTL|FCL|HW|BX|FS|YT|YAN|MJ)[\s\-/]*(\d{4,6})\b",
        upper,
    )
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    # 備案:單獨的 5-6 位數字(避免 4 位被誤抓成年份)
    m = re.search(r"\b(\d{5,6})\b", upper)
    if m:
        return f"NUM-{m.group(1)}"
    return None


def get_login_url():
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(OAUTH_SCOPES),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"


def exchange_code_for_token(code):
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    if not resp.ok:
        raise Exception(f"Token exchange failed: {resp.text}")
    token_data = resp.json()
    user_info = requests.get(
        "https://www.googleapis.com/oauth2/v1/userinfo",
        headers={"Authorization": f"Bearer {token_data['access_token']}"},
        timeout=10,
    ).json()
    return {
        "creds": {
            "token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token"),
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scopes": OAUTH_SCOPES,
        },
        # Phase 6 驗證用:Google id_token,後續可以拿來換 Firebase ID token
        "id_token": token_data.get("id_token", ""),
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


def build_gmail_query():
    kw_clause = " OR ".join([f"subject:{k}" for k in BUSINESS_KEYWORDS])
    noise_clause = " ".join([f"-from:{d}" for d in NOISE_DOMAINS])
    return f"in:inbox newer_than:{SEARCH_DAYS}d ({kw_clause}) {noise_clause}"


def parse_gmail_message(service, msg_id):
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    subject = headers.get("Subject", "")
    from_raw = headers.get("From", "")
    date_str = headers.get("Date", "")
    try:
        msg_date = parsedate_to_datetime(date_str)
        if msg_date.tzinfo is None:
            msg_date = msg_date.replace(tzinfo=timezone.utc)
    except Exception:
        msg_date = datetime.now(timezone.utc)
    body = extract_plain_body(msg.get("payload", {}))
    is_unread = "UNREAD" in msg.get("labelIds", [])
    return {
        "id": msg_id, "subject": subject, "from": from_raw,
        "date": msg_date, "body": body, "is_unread": is_unread,
    }


def extract_plain_body(payload):
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


def build_thread_transcript(messages_meta):
    """把整個 thread 串成一份「對話紀錄」文字,給 Gemini 做有完整脈絡的摘要。

    每封信輸出格式:
        [編號] From: <寄件者>  Date: <日期>
        <內文(裁切到 THREAD_PER_MSG_CHARS)>

    總長度若超過 THREAD_TOTAL_CHARS,從最舊的那幾封開始截掉,優先保留近期對話。
    """
    blocks = []
    for idx, m in enumerate(messages_meta, start=1):
        headers = {h["name"]: h["value"] for h in m.get("payload", {}).get("headers", [])}
        from_h = headers.get("From", "(unknown)")
        date_h = headers.get("Date", "")
        body = extract_plain_body(m.get("payload", {}))
        body = (body or "").strip()
        if len(body) > THREAD_PER_MSG_CHARS:
            body = body[:THREAD_PER_MSG_CHARS] + "...[truncated]"
        blocks.append(f"[{idx}] From: {from_h}  Date: {date_h}\n{body}")
    transcript = "\n\n---\n\n".join(blocks)
    # 太長時砍最早的訊息,保留最近的
    if len(transcript) > THREAD_TOTAL_CHARS:
        while len(transcript) > THREAD_TOTAL_CHARS and len(blocks) > 1:
            blocks.pop(0)
            transcript = "\n\n---\n\n".join(blocks)
        transcript = "[Note: earliest messages omitted to fit context]\n\n" + transcript
    return transcript


def fetch_pending_emails(creds_dict, current_user_email):
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

        last_external_idx = -1
        for i in range(len(messages_meta) - 1, -1, -1):
            from_h = next(
                (h["value"] for h in messages_meta[i].get("payload", {}).get("headers", [])
                 if h["name"] == "From"), "",
            )
            from_email = extract_email(from_h)
            if from_email and not is_internal(from_email):
                last_external_idx = i
                break
        if last_external_idx == -1:
            continue

        user_replied = False
        for j in range(last_external_idx + 1, len(messages_meta)):
            from_h = next(
                (h["value"] for h in messages_meta[j].get("payload", {}).get("headers", [])
                 if h["name"] == "From"), "",
            )
            from_email = extract_email(from_h)
            if from_email == current_user_email.lower():
                user_replied = True
                break
        if user_replied:
            continue

        last_msg = parse_gmail_message(service, messages_meta[last_external_idx]["id"])
        last_email = extract_email(last_msg["from"])
        if is_noise_domain(last_email):
            continue

        is_today = last_msg["date"] >= today_start
        age_hours = int((now - last_msg["date"]).total_seconds() // 3600)

        # 整個 thread 拼成完整對話紀錄,讓 Gemini 看到全部脈絡
        thread_text = build_thread_transcript(messages_meta)

        items.append({
            "msg_id": last_msg["id"], "subject": last_msg["subject"],
            "from": last_msg["from"], "date": last_msg["date"],
            "body": last_msg["body"], "thread_text": thread_text,
            "is_unread": last_msg["is_unread"],
            "is_today": is_today, "age_hours": age_hours,
        })

    items.sort(key=lambda x: (
        not x["is_unread"], not x["is_today"], -x["age_hours"],
    ))
    return items


@st.cache_data(ttl=86400, show_spinner=False)
def gemini_summary_and_actions(msg_id, subject, thread_text):
    """根據完整 thread(而不只是最後一封)產出摘要 + 待辦。

    cache key 包含 thread_text → 若 thread 多了一封新信,自動 re-summarize。
    """
    prompt = (
        "You are analyzing an email THREAD (full conversation history below). "
        "Produce TWO sections in English based on the WHOLE thread context, "
        "but with the focus on what the LATEST external message asks for. "
        "Use EXACTLY this format with [---] as separator (no extra text outside):\n\n"
        "**Theme:** [a short clean title MAX 8 words, format: 'OrderNumber MainTopic']\n"
        "- [Key point 1 — must reflect thread context, not just last msg]\n"
        "- [Key point 2 — include earlier commitments/dates if relevant]\n"
        "- [Key point 3]\n\n"
        "[---]\n\n"
        "**🎯 What you need to do:**\n"
        "1. [specific action — consider what's already been promised earlier in thread]\n"
        "2. [another action if any]\n"
        "3. [another action if any]\n\n"
        "**📅 Deadline:** [extract date from anywhere in thread, or \"Not specified\"]\n"
        "**👤 Awaiting your reply:** [the person waiting]\n"
        "**📌 Context:** [one line including key history from earlier messages]\n\n"
        f"Email Subject: {subject}\n\n"
        f"=== Email Thread ===\n{thread_text}\n=== End of Thread ==="
    )
    resp = requests.post(
        GEMINI_API_URL,
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.3, "maxOutputTokens": 1500,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        },
        timeout=60,
    )
    if not resp.ok:
        return "(摘要產生失敗)", ""
    try:
        parts = resp.json()["candidates"][0]["content"]["parts"]
        full_text = "".join(p.get("text", "") for p in parts).strip()
        sections = full_text.split("[---]")
        return sections[0].strip(), (sections[1].strip() if len(sections) > 1 else "")
    except (KeyError, IndexError):
        return "(摘要產生失敗)", ""


def gemini_reply_draft(msg_id, subject, thread_text, actions, sender_name, user_first_name):
    """根據完整 thread 寫回信草稿,避免重述 thread 早期已討論過的內容。"""
    prompt = (
        f"Write a professional, concise English email reply on behalf of {user_first_name}. "
        f"Be friendly but business-appropriate. Sign off as \"Best regards,\\n{user_first_name}\".\n\n"
        f"You are replying to {sender_name}. Below is the FULL thread history — "
        f"use it to avoid repeating what was already discussed and to maintain consistency "
        f"with prior commitments.\n\n"
        f"=== Email Thread ===\n{thread_text}\n=== End of Thread ===\n\n"
        f"Action items to communicate (already analyzed from the whole thread):\n{actions}\n\n"
        "Write a reply that:\n"
        "1. Acknowledges the latest message briefly\n"
        "2. Addresses the action items naturally, referencing earlier thread context only when useful\n"
        "3. Confirms next steps and timing if mentioned\n"
        f"4. Ends with the sign-off above\n\n"
        "Output ONLY the email body text (no Subject line, no commentary, no markdown)."
    )
    resp = requests.post(
        GEMINI_API_URL,
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.5, "maxOutputTokens": 800,
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


def precompute_summaries(items):
    out = {}
    for it in items:
        # 用整 thread(thread_text)而不是只有最後一封(body)— 讓 Gemini 看到完整脈絡
        summary, actions = gemini_summary_and_actions(
            it["msg_id"], it["subject"], it["thread_text"]
        )
        out[it["msg_id"]] = {"summary": summary, "actions": actions, "theme": extract_theme(summary)}
    return out


def compute_grouped_titles(items, summary_cache):
    """
    判定哪些信是「同主題」(透過訂單編號配對),產出每封信的最終顯示標題。

    邏輯:
      1. 每封信抽出訂單編號(從原主旨 + AI Theme 找)
      2. 計數:有多少信用同一個訂單編號
      3. 若 ≥ 2 → 該組共用一個統一標題(訂單編號)
      4. 若 = 1 或 None → 保持原主旨
    """
    # Step 1:每封信的 topic key
    topic_keys = {}
    for it in items:
        cached = summary_cache.get(it["msg_id"], {})
        theme = cached.get("theme", "")
        # 先看原主旨,沒抓到再看 AI theme
        key = extract_topic_key(it["subject"]) or extract_topic_key(theme)
        topic_keys[it["msg_id"]] = key

    # Step 2:計數
    key_counts = Counter(k for k in topic_keys.values() if k)

    # Step 3:給每封信派標題
    titles = {}
    for it in items:
        msg_id = it["msg_id"]
        key = topic_keys[msg_id]
        if key and key_counts[key] >= 2:
            # 同主題的多封信 → 統一標題
            if key.startswith("NUM-"):
                titles[msg_id] = f"#{key[4:]} 相關信件"
            else:
                # 例如 SKY-80025 → "SKY 80025"
                brand, num = key.split("-", 1)
                titles[msg_id] = f"{brand} {num}"
        else:
            # 唯一主題或抓不到編號 → 保留原主旨
            titles[msg_id] = it["subject"]
    return titles, topic_keys, key_counts


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
    qp = st.query_params
    if "code" not in qp:
        return False
    try:
        result = exchange_code_for_token(qp["code"])
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
def render_email_detail(item, user_name, user_first_name, summary_cache, display_title):
    msg_id = item["msg_id"]
    subject = item["subject"]
    body = item["body"]

    cached = summary_cache.get(msg_id, {})
    summary = cached.get("summary", "")
    actions = cached.get("actions", "")

    saved = st.session_state.get(f"_saved_{msg_id}", {})
    final_title = saved.get("title") or display_title
    display_summary = saved.get("summary") or summary
    display_actions = saved.get("actions") or actions

    st.divider()
    st.markdown(f"### 📄 {final_title}")
    if subject and final_title != subject:
        st.caption(f"📨 原始主旨:{subject}")

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
            new_title = st.text_input("📄 標題", value=final_title)
            new_summary = st.text_area("📝 AI 摘要", value=display_summary, height=200)
            new_actions = st.text_area("🎯 待辦事項", value=display_actions, height=200)
            if st.form_submit_button("💾 儲存(僅本次 session)", use_container_width=True):
                st.session_state[f"_saved_{msg_id}"] = {
                    "title": new_title, "summary": new_summary, "actions": new_actions,
                }
                st.rerun(scope="fragment")

    with right_col:
        st.markdown("### 📧 信件內容(原文,僅供參考)")
        st.text_area(
            "body", value=body, height=520, disabled=True, label_visibility="collapsed",
        )

    gmail_link = f"https://mail.google.com/mail/u/0/#inbox/{msg_id}"
    st.markdown(f"[🔗 在 Gmail 中開啟原信]({gmail_link})")

    st.divider()
    st.markdown("### ✍️ AI 一鍵產生回信草稿")
    draft_key = f"_draft_{msg_id}"
    if st.button("✍️ 產生英文回信草稿", use_container_width=True, key=f"btn_{msg_id}"):
        with st.spinner("Gemini 寫回信中(10-20 秒)..."):
            sender_name = clean_sender(item["from"])
            # 把整 thread 餵給草稿生成,避免回信跟早期討論不一致
            thread_text_for_draft = item.get("thread_text", body)
            draft = gemini_reply_draft(
                msg_id, subject, thread_text_for_draft, display_actions,
                sender_name, user_first_name,
            )
        if draft:
            st.session_state[draft_key] = draft
            st.rerun(scope="fragment")
    if draft_key in st.session_state:
        st.markdown("##### 📝 建議回信")
        st.text_area(
            "draft", value=st.session_state[draft_key], height=300,
            label_visibility="collapsed", key=f"d_{msg_id}",
        )
        st.caption("✂️ 滑鼠選取 → ⌘+C 複製 → 開 Gmail Reply → ⌘+V 貼上")


def phase6_firestore_probe(user):
    """Phase 6 驗證:測試 user OAuth token 能否寫入 Firestore。

    跑兩個 Plan:
    - Plan A:用 Google access_token 直接呼叫 Firestore REST API
    - Plan B:用 Google id_token 換 Firebase ID token,再寫 Firestore
    每個 Plan 寫一筆到 test_probe collection,印出 HTTP 狀態與回應。
    """
    project_id = "trims-f8e4a"
    firebase_api_key = st.secrets.get("FIREBASE_WEB_API_KEY", "")
    creds = user.get("creds", {})
    access_token = creds.get("token", "")
    id_token = user.get("id_token", "")
    email = user.get("email", "")

    st.markdown("### 🧪 Phase 6 — Firestore 寫入可行性驗證")
    st.caption("測試會寫一筆到 `test_probe/<email>`,不影響正式資料。Rules 已限制只能 ibiney.io 寫入。")

    # ── Plan A:直接用 access_token 呼 Firestore REST ────────
    st.markdown("#### Plan A:Google access_token → Firestore REST")
    if not access_token:
        st.error("找不到 access_token,請重新登入")
    else:
        url_a = (
            f"https://firestore.googleapis.com/v1/projects/{project_id}"
            f"/databases/(default)/documents/test_probe?documentId=planA_{email.replace('@', '_at_')}"
        )
        body_a = {"fields": {
            "plan": {"stringValue": "A"},
            "email": {"stringValue": email},
            "ts": {"stringValue": datetime.now(timezone.utc).isoformat()},
        }}
        try:
            resp_a = requests.post(
                url_a,
                headers={"Authorization": f"Bearer {access_token}"},
                json=body_a, timeout=15,
            )
            st.write(f"HTTP **{resp_a.status_code}** {'✅ 可行' if resp_a.ok else '❌ 不通'}")
            with st.expander("查看回應內容"):
                st.code(resp_a.text[:1500])
        except Exception as e:
            st.error(f"Plan A 例外:{e}")

    st.divider()

    # ── Plan B:id_token 換 Firebase token → Firestore ───────
    st.markdown("#### Plan B:Google id_token → Firebase ID token → Firestore")
    if not id_token:
        st.warning(
            "⚠️ 此 session 沒有 id_token(你登入是在加這個欄位之前)。\n\n"
            "請按右上「🚪 登出」重新登入,新 session 才有 id_token,Plan B 才能測。"
        )
    elif not firebase_api_key:
        st.warning(
            "⚠️ Streamlit Secrets 沒設 `FIREBASE_WEB_API_KEY`。\n\n"
            "Plan B 需要這個 key 才能呼叫 Firebase Identity Toolkit 換 token。\n"
            "去 https://console.firebase.google.com/project/trims-f8e4a/settings/general → 看 Web API Key,"
            "貼進 Streamlit Cloud → btl-dashboard → Settings → Secrets,新增:\n"
            "`FIREBASE_WEB_API_KEY = \"...\"`"
        )
    else:
        try:
            exchange_url = (
                f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithIdp"
                f"?key={firebase_api_key}"
            )
            exchange_body = {
                "postBody": f"id_token={id_token}&providerId=google.com",
                "requestUri": REDIRECT_URI,
                "returnSecureToken": True,
            }
            ex_resp = requests.post(exchange_url, json=exchange_body, timeout=15)
            if not ex_resp.ok:
                st.write(f"Token exchange HTTP **{ex_resp.status_code}** ❌")
                with st.expander("查看 exchange 失敗回應"):
                    st.code(ex_resp.text[:1500])
            else:
                firebase_id_token = ex_resp.json().get("idToken", "")
                st.write("Token exchange ✅ 成功")
                url_b = (
                    f"https://firestore.googleapis.com/v1/projects/{project_id}"
                    f"/databases/(default)/documents/test_probe?documentId=planB_{email.replace('@', '_at_')}"
                )
                body_b = {"fields": {
                    "plan": {"stringValue": "B"},
                    "email": {"stringValue": email},
                    "ts": {"stringValue": datetime.now(timezone.utc).isoformat()},
                }}
                resp_b = requests.post(
                    url_b,
                    headers={"Authorization": f"Bearer {firebase_id_token}"},
                    json=body_b, timeout=15,
                )
                st.write(f"Firestore 寫入 HTTP **{resp_b.status_code}** {'✅ 可行' if resp_b.ok else '❌ 不通'}")
                with st.expander("查看寫入回應"):
                    st.code(resp_b.text[:1500])
        except Exception as e:
            st.error(f"Plan B 例外:{e}")


def show_main_dashboard():
    user = st.session_state["user"]
    user_email = user["email"]
    user_name = user.get("name") or user_email
    user_first_name = user_name.split()[0] if user_name else "Me"

    title_col, user_col = st.columns([4, 1])
    with title_col:
        st.title("📧 BTL Email Monitor")
        st.caption(f"已登入:{user_name} ({user_email}) / 顯示你 Gmail 中過去 {SEARCH_DAYS} 天的待回客戶信件")
    with user_col:
        st.write("")
        if st.button("🔄 重新抓取", use_container_width=True):
            st.session_state.pop("pending_items", None)
            st.session_state.pop("summary_cache_for_table", None)
            st.cache_data.clear()
            st.rerun()
        if st.button("🚪 登出", use_container_width=True):
            st.session_state.clear()
            st.query_params.clear()
            st.rerun()

    # Phase 6 Firestore 寫入驗證(收在 expander 裡,不影響日常使用)
    with st.sidebar.expander("🧪 Phase 6 驗證(開發用)"):
        if st.button("執行 Firestore 寫入測試"):
            st.session_state["_phase6_probe"] = True
    if st.session_state.get("_phase6_probe"):
        phase6_firestore_probe(user)
        if st.button("關閉驗證面板"):
            st.session_state.pop("_phase6_probe", None)
            st.rerun()
        st.divider()

    if "pending_items" not in st.session_state:
        with st.spinner("📬 正在從你的 Gmail 抓取待回信件(30-90 秒,只發生一次)..."):
            try:
                st.session_state["pending_items"] = fetch_pending_emails(
                    user["creds"], user_email
                )
            except Exception as e:
                st.error(f"抓取 Gmail 失敗:{e}")
                st.info("可能是 token 過期,請登出重新登入。")
                st.stop()
    items = st.session_state["pending_items"]

    if not items:
        st.success("🎉 你的 Gmail 中目前沒有待回客戶信件,辛苦了!")
        return

    if "summary_cache_for_table" not in st.session_state:
        with st.spinner(f"🤖 AI 正在整理 {len(items)} 封信..."):
            st.session_state["summary_cache_for_table"] = precompute_summaries(items)
    summary_cache = st.session_state["summary_cache_for_table"]

    # 分組計算每封信的最終顯示標題
    grouped_titles, topic_keys, key_counts = compute_grouped_titles(items, summary_cache)

    # 統計分組情況
    grouped_count = sum(1 for it in items
                        if topic_keys[it["msg_id"]]
                        and key_counts[topic_keys[it["msg_id"]]] >= 2)

    rows = []
    for it in items:
        badges = ["🔴 未讀未回" if it["is_unread"] else "🟡 已讀未回"]
        if it["is_today"]:
            badges.append("🔵 當日新進")
        rows.append({
            "msg_id": it["msg_id"],
            "優先級": " / ".join(badges),
            "寄件者": it["from"],
            "標題": grouped_titles[it["msg_id"]],
            "收信日期": it["date"].strftime("%Y-%m-%d %H:%M"),
            "等待時長": format_age(it["age_hours"]),
            "郵件連結": f"https://mail.google.com/mail/u/0/#inbox/{it['msg_id']}",
            "_body": it["body"],
            "_item": it,
        })
    df = pd.DataFrame(rows)
    df["部門"] = df["寄件者"].apply(get_department)
    df["客戶"] = df["寄件者"].apply(get_client)

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

    if grouped_count > 0:
        st.info(f"🔗 偵測到 **{grouped_count}** 封信屬於同主題分組(共用統一標題)")

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

    fc1, fc2, fc3 = st.columns([1, 1, 2])
    with fc1:
        tag_count_map = {
            "🔴 未讀未回": unread_cnt, "🟡 已讀未回": read_cnt, "🔵 當日新進": today_cnt,
        }
        tag_opts = [f"{t} ({tag_count_map[t]})" for t in ["🔴 未讀未回", "🟡 已讀未回", "🔵 當日新進"]]
        show_tags_l = st.multiselect("狀態(選填)", tag_opts, placeholder="不勾 = 全部")
    with fc2:
        dept_count_map = df["部門"].value_counts().to_dict()
        dept_opts = [f"{d} ({dept_count_map.get(d, 0)})" for d in ALL_DEPARTMENTS]
        show_depts_l = st.multiselect("部門(選填)", dept_opts, placeholder="不勾 = 全部")
    with fc3:
        keyword = st.text_input("標題 / 寄件者搜尋(選填)")

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
            view_df["標題"].astype(str).str.lower().str.contains(kw, na=False)
            | view_df["寄件者"].astype(str).str.lower().str.contains(kw, na=False)
        ]
    view_df = view_df.reset_index(drop=True)

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
    st.caption("💡 同主題多封信會共用標題;單封信保留原主旨")

    event = st.dataframe(
        view_df,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_order=["寄件者", "部門", "優先級", "標題", "收信日期", "等待時長", "郵件連結"],
        column_config={
            "寄件者": st.column_config.TextColumn(width="medium"),
            "部門": st.column_config.TextColumn(width="medium"),
            "優先級": st.column_config.TextColumn(width="medium"),
            "標題": st.column_config.TextColumn(width="large"),
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
        # 點開時要把該信的最終標題傳進去
        display_title_for_detail = grouped_titles[item["msg_id"]]
        render_email_detail(item, user_name, user_first_name, summary_cache, display_title_for_detail)

    st.caption(f"頁面載入時間:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def main():
    handle_oauth_callback()
    if "user" not in st.session_state:
        show_login_page()
    else:
        show_main_dashboard()


if __name__ == "__main__":
    main()
