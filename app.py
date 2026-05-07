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
        st.markdown("### ✏️ 可編輯區(改完按下方儲存到雲端,跨 session 保留)")
        with st.form(key=f"edit_{msg_id}", clear_on_submit=False):
            new_title = st.text_input("📄 標題", value=final_title)
            new_summary = st.text_area("📝 AI 摘要", value=display_summary, height=200)
            new_actions = st.text_area("🎯 待辦事項", value=display_actions, height=200)
            if st.form_submit_button("💾 儲存到雲端", use_container_width=True):
                # 1. 立即更新 session_state(本頁立刻反映新值,不等網路)
                st.session_state[f"_saved_{msg_id}"] = {
                    "title": new_title, "summary": new_summary, "actions": new_actions,
                }
                # 2. 同步寫進 Firestore(跨 session 保留)
                user_for_save = st.session_state.get("user", {})
                ok, msg = save_edit_to_firestore(
                    user_for_save, msg_id, new_title, new_summary, new_actions,
                )
                if ok:
                    st.success(f"✅ {msg}(關 tab 再開還在)")
                else:
                    st.warning(
                        f"⚠️ 雲端儲存失敗:{msg}。本次編輯仍會在 session 內保留,"
                        "但關 tab 後會遺失。"
                    )
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


def get_firebase_id_token(user):
    """把 Google id_token 換成 Firebase ID token(每 50 分鐘 cache 一次,避免每次儲存都重換)。

    回傳 Firebase ID token 字串,失敗回 None。
    """
    cached = st.session_state.get("_firebase_id_token")
    cached_at = st.session_state.get("_firebase_id_token_at", 0)
    now_ts = datetime.now(timezone.utc).timestamp()
    # Firebase ID token 1 小時過期,提前 10 分鐘 refresh
    if cached and (now_ts - cached_at < 3000):
        return cached

    api_key = st.secrets.get("FIREBASE_WEB_API_KEY", "")
    google_id_token = user.get("id_token", "")
    if not api_key or not google_id_token:
        return None

    try:
        resp = requests.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithIdp?key={api_key}",
            json={
                "postBody": f"id_token={google_id_token}&providerId=google.com",
                "requestUri": REDIRECT_URI,
                "returnSecureToken": True,
            },
            timeout=10,
        )
        if not resp.ok:
            return None
        firebase_token = resp.json().get("idToken", "")
        st.session_state["_firebase_id_token"] = firebase_token
        st.session_state["_firebase_id_token_at"] = now_ts
        return firebase_token
    except Exception:
        return None


def save_edit_to_firestore(user, msg_id, title, summary, actions):
    """把使用者編輯寫進 Firestore (users/{email}/edits/{msg_id})。

    用 PATCH(updateMask)做 upsert:已存在就覆蓋指定欄位,不存在就建。
    回傳 (success: bool, message: str)。
    """
    firebase_token = get_firebase_id_token(user)
    if not firebase_token:
        return False, "未取得 Firebase token(請重新登入)"

    email = user.get("email", "")
    if not email:
        return False, "無使用者 email"

    project_id = "trims-f8e4a"
    # PATCH 端點 + updateMask 達成「upsert」(已存在則更新,不存在則建立)
    url = (
        f"https://firestore.googleapis.com/v1/projects/{project_id}"
        f"/databases/(default)/documents/users/{email}/edits/{msg_id}"
        "?updateMask.fieldPaths=title"
        "&updateMask.fieldPaths=summary"
        "&updateMask.fieldPaths=actions"
        "&updateMask.fieldPaths=updated_at"
    )
    body = {"fields": {
        "title": {"stringValue": title or ""},
        "summary": {"stringValue": summary or ""},
        "actions": {"stringValue": actions or ""},
        "updated_at": {"stringValue": datetime.now(timezone.utc).isoformat()},
    }}
    try:
        resp = requests.patch(
            url,
            headers={"Authorization": f"Bearer {firebase_token}"},
            json=body, timeout=10,
        )
        if resp.ok:
            return True, "已儲存到雲端"
        return False, f"儲存失敗 HTTP {resp.status_code}"
    except Exception as e:
        return False, f"儲存例外:{e}"


def load_edits_from_firestore(user):
    """從 Firestore 撈這個 user 所有的編輯紀錄(users/{email}/edits/*)。

    回傳 dict: { msg_id: {title, summary, actions, updated_at} }
    Firestore 沒資料或失敗時回空 dict(不影響 dashboard 主功能)。
    """
    firebase_token = get_firebase_id_token(user)
    if not firebase_token:
        return {}

    email = user.get("email", "")
    if not email:
        return {}

    project_id = "trims-f8e4a"
    url = (
        f"https://firestore.googleapis.com/v1/projects/{project_id}"
        f"/databases/(default)/documents/users/{email}/edits"
    )
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {firebase_token}"},
            timeout=10,
        )
        if not resp.ok:
            return {}
        docs = resp.json().get("documents", [])
        out = {}
        for d in docs:
            # name 格式: projects/.../documents/users/{email}/edits/{msg_id}
            doc_msg_id = d.get("name", "").rsplit("/", 1)[-1]
            fields = d.get("fields", {})
            out[doc_msg_id] = {
                "title": fields.get("title", {}).get("stringValue", ""),
                "summary": fields.get("summary", {}).get("stringValue", ""),
                "actions": fields.get("actions", {}).get("stringValue", ""),
                "updated_at": fields.get("updated_at", {}).get("stringValue", ""),
            }
        return out
    except Exception:
        return {}


def analyze_attachments(creds_dict, current_user_email):
    """掃描使用者過去 SEARCH_DAYS 天的待回 thread,統計附件分佈。

    產出 multi-modal PRD 需要的真實數據:附件類型、客戶分佈、業務話題分佈。
    回傳 dict 結構,在 streamlit 端渲染成表格。
    """
    creds = credentials_from_dict(creds_dict)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    query = build_gmail_query()
    threads_resp = service.users().threads().list(
        userId="me", q=query, maxResults=200
    ).execute()
    threads = threads_resp.get("threads", [])

    stats = {
        "total_threads": 0,
        "total_messages": 0,
        "messages_with_attachments": 0,
        "messages_without_attachments": 0,
        "total_attachments": 0,
        "mime_types": Counter(),       # 每個 MIME 類型計數
        "file_extensions": Counter(),  # 副檔名計數
        "client_attachments": Counter(),  # 客戶 → 附件數
        "subject_keywords": Counter(),    # 主旨關鍵字
        "size_buckets": Counter(),     # 附件大小分布
        "thread_with_attachment_count": 0,  # 多少 thread 至少有 1 個附件
    }

    business_words = [
        "PI", "PO", "Order", "Sample", "Sendung", "Parcel", "Color",
        "Approval", "Rejection", "Delivery", "Shipping", "Quote",
        "Invoice", "Spec", "Quality", "Production", "Material",
    ]

    for t in threads:
        thread_full = service.users().threads().get(
            userId="me", id=t["id"], format="full"
        ).execute()
        messages_meta = thread_full.get("messages", [])
        if not messages_meta:
            continue

        stats["total_threads"] += 1
        thread_has_any_attachment = False

        for m in messages_meta:
            stats["total_messages"] += 1
            headers = {h["name"]: h["value"] for h in m.get("payload", {}).get("headers", [])}
            from_h = headers.get("From", "")
            subject = headers.get("Subject", "")

            # 主旨關鍵字統計
            for word in business_words:
                if word.lower() in subject.lower():
                    stats["subject_keywords"][word] += 1

            # 找附件:遞迴掃 payload.parts,filename 非空就是附件
            attachments = []
            def walk_parts(payload):
                if not payload:
                    return
                filename = payload.get("filename", "")
                if filename:
                    mime = payload.get("mimeType", "unknown")
                    size = payload.get("body", {}).get("size", 0)
                    attachments.append({
                        "filename": filename,
                        "mime": mime,
                        "size": size,
                    })
                for sub in payload.get("parts", []) or []:
                    walk_parts(sub)
            walk_parts(m.get("payload", {}))

            if attachments:
                stats["messages_with_attachments"] += 1
                thread_has_any_attachment = True
                stats["total_attachments"] += len(attachments)

                client = get_client(from_h)
                for att in attachments:
                    mime = att["mime"]
                    stats["mime_types"][mime] += 1
                    # 副檔名
                    fname = att["filename"]
                    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else "(no_ext)"
                    stats["file_extensions"][ext] += 1
                    # 客戶
                    stats["client_attachments"][client] += 1
                    # 大小分布
                    size = att["size"]
                    if size < 100_000:
                        stats["size_buckets"]["< 100KB"] += 1
                    elif size < 1_000_000:
                        stats["size_buckets"]["100KB - 1MB"] += 1
                    elif size < 10_000_000:
                        stats["size_buckets"]["1MB - 10MB"] += 1
                    else:
                        stats["size_buckets"]["> 10MB"] += 1
            else:
                stats["messages_without_attachments"] += 1

        if thread_has_any_attachment:
            stats["thread_with_attachment_count"] += 1

    return stats


def render_attachment_analysis(user):
    """執行 + 渲染附件分析結果(PRD 證據用)。"""
    st.markdown("### 📊 附件分析(Multi-modal PRD 數據收集)")
    st.caption(
        f"掃描你過去 {SEARCH_DAYS} 天 inbox 業務 thread 的附件分佈 — "
        "這份數據會用來決定 multi-modal 該優先支援什麼檔案類型。"
    )

    if "_attachment_stats" not in st.session_state:
        with st.spinner(f"分析 Gmail 附件中(20-40 秒)..."):
            try:
                st.session_state["_attachment_stats"] = analyze_attachments(
                    user["creds"], user["email"]
                )
            except Exception as e:
                st.error(f"分析失敗:{e}")
                return

    stats = st.session_state["_attachment_stats"]

    # ── 總覽 ──────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("掃描 Thread 數", stats["total_threads"])
    c2.metric("總信件數", stats["total_messages"])
    c3.metric("有附件的信", stats["messages_with_attachments"])
    c4.metric("總附件數", stats["total_attachments"])

    if stats["total_messages"] == 0:
        st.warning("沒有找到任何信件")
        return

    # ── 附件密度 ──────────────────────────────────────
    pct_with = round(100 * stats["messages_with_attachments"] / stats["total_messages"], 1)
    pct_thread = round(100 * stats["thread_with_attachment_count"] / stats["total_threads"], 1)
    st.markdown(f"""
    **📌 附件密度**:
    - 有附件的信佔 **{pct_with}%**({stats['messages_with_attachments']}/{stats['total_messages']})
    - 有附件的 thread 佔 **{pct_thread}%**({stats['thread_with_attachment_count']}/{stats['total_threads']})
    - 平均每封有附件的信 = **{round(stats['total_attachments'] / max(stats['messages_with_attachments'], 1), 2)} 個**附件
    """)

    # ── 副檔名 Top 10 ────────────────────────────────
    st.markdown("#### 📁 附件副檔名分佈(這個最關鍵 — 決定要支援哪些格式)")
    if stats["file_extensions"]:
        ext_df = pd.DataFrame(
            stats["file_extensions"].most_common(15),
            columns=["副檔名", "出現次數"],
        )
        ext_df["佔比"] = ext_df["出現次數"].apply(
            lambda n: f"{round(100 * n / stats['total_attachments'], 1)}%"
        )
        st.dataframe(ext_df, use_container_width=True, hide_index=True)
    else:
        st.info("沒有附件可分析")

    # ── MIME 類型 ────────────────────────────────────
    st.markdown("#### 🔬 MIME 類型(技術精確版)")
    if stats["mime_types"]:
        mime_df = pd.DataFrame(
            stats["mime_types"].most_common(15),
            columns=["MIME", "次數"],
        )
        st.dataframe(mime_df, use_container_width=True, hide_index=True)

    # ── 客戶分佈 ────────────────────────────────────
    st.markdown("#### 🏢 哪些客戶最常寄附件?")
    if stats["client_attachments"]:
        client_df = pd.DataFrame(
            stats["client_attachments"].most_common(10),
            columns=["客戶", "附件數"],
        )
        st.dataframe(client_df, use_container_width=True, hide_index=True)

    # ── 附件大小 ───────────────────────────────────
    st.markdown("#### 📦 附件大小分佈(影響 Gemini upload 時間)")
    if stats["size_buckets"]:
        size_df = pd.DataFrame(
            list(stats["size_buckets"].items()),
            columns=["大小範圍", "個數"],
        )
        st.dataframe(size_df, use_container_width=True, hide_index=True)

    # ── 主旨關鍵字 ──────────────────────────────────
    st.markdown("#### 💬 主旨業務關鍵字 Top 10(內容話題分佈)")
    if stats["subject_keywords"]:
        kw_df = pd.DataFrame(
            stats["subject_keywords"].most_common(10),
            columns=["關鍵字", "出現次數"],
        )
        st.dataframe(kw_df, use_container_width=True, hide_index=True)

    # ── 給 PRD 的洞察 ───────────────────────────────
    st.markdown("#### 💡 對 PRD 的初步洞察")
    top_exts = stats["file_extensions"].most_common(3)
    if top_exts:
        top_summary = ", ".join([f"`.{e}` ({n})" for e, n in top_exts])
        st.info(
            f"**Top 3 副檔名**:{top_summary}\n\n"
            f"→ Multi-modal 第一階段建議優先支援:**{top_exts[0][0]}**"
            f"({round(100 * top_exts[0][1] / stats['total_attachments'], 1)}% 涵蓋率)"
        )


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


# ═══════════════════════════════════════════════════════════════
# Quality Report:用客觀數據驗證「系統真的有效嗎」
# ═══════════════════════════════════════════════════════════════

def gemini_critique_summary(thread_text, ai_summary, ai_actions):
    """讓另一個 Gemini call 扮演評審,評分既有的 AI 摘要 + 待辦。

    回傳 dict:{theme_score, bullets_score, actions_score, awaiting_correct, notes}
    每個分數 1-5。失敗回 None。
    """
    prompt = (
        "You are an expert evaluator. Below is an email thread and an AI-generated "
        "summary + action items. Score the AI output on 4 dimensions (1=worst, 5=perfect):\n\n"
        "1. theme_score: Is the Theme line accurate to what the thread is actually about?\n"
        "2. bullets_score: Do the 3 bullets capture the most important points across the WHOLE thread?\n"
        "3. actions_score: Are the action items specific and actionable for the recipient?\n"
        "4. awaiting_correct: Is the 'Awaiting your reply' judgment correct given the thread state? "
        "(5 = totally correct, 1 = clearly wrong)\n"
        "5. notes: One-line critique of the biggest weakness, or 'None' if perfect.\n\n"
        "Output STRICT JSON only, no markdown, no commentary:\n"
        '{"theme_score": N, "bullets_score": N, "actions_score": N, "awaiting_correct": N, "notes": "..."}\n\n'
        f"=== Email Thread ===\n{thread_text[:8000]}\n=== End ===\n\n"
        f"=== AI Summary ===\n{ai_summary}\n=== End ===\n\n"
        f"=== AI Actions ===\n{ai_actions}\n=== End ==="
    )
    try:
        resp = requests.post(
            GEMINI_API_URL,
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.1, "maxOutputTokens": 400,
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            },
            timeout=30,
        )
        if not resp.ok:
            return None
        text = "".join(p.get("text", "") for p in resp.json()["candidates"][0]["content"]["parts"]).strip()
        # Strip markdown fences if Gemini still added them
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        import json as _json
        return _json.loads(text)
    except Exception:
        return None


def fetch_all_business_threads(creds_dict, with_subjects=True):
    """抓 SEARCH_DAYS 天內所有「主旨含業務關鍵字」的 thread,回傳 thread metadata list。

    與 fetch_pending_emails 不同,這裡不套用「最後外部寄件者 + 我未回」過濾。
    用來做雙向比對:看完整 universe 大小,跟 dashboard 顯示的差距。
    """
    creds = credentials_from_dict(creds_dict)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    query = build_gmail_query()
    resp = service.users().threads().list(
        userId="me", q=query, maxResults=200
    ).execute()
    threads = resp.get("threads", [])

    out = []
    for t in threads:
        full = service.users().threads().get(
            userId="me", id=t["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        messages = full.get("messages", [])
        if not messages:
            continue

        # 找最後一封外部 + 之後是否有當前 user 回覆
        last_ext_from = None
        last_ext_idx = -1
        for i in range(len(messages) - 1, -1, -1):
            headers = {h["name"]: h["value"] for h in messages[i].get("payload", {}).get("headers", [])}
            from_email = extract_email(headers.get("From", ""))
            if from_email and not is_internal(from_email):
                last_ext_from = headers.get("From", "")
                last_ext_idx = i
                break

        user_replied_after = False
        if last_ext_idx >= 0:
            for j in range(last_ext_idx + 1, len(messages)):
                headers = {h["name"]: h["value"] for h in messages[j].get("payload", {}).get("headers", [])}
                from_email = extract_email(headers.get("From", ""))
                # 注意:這裡無法精確判斷「當前 user」是誰,因為這函式不傳 user_email
                # 改成:看是不是 internal domain → 任一 ibiney 都算「我們有人回了」
                if from_email and is_internal(from_email):
                    user_replied_after = True
                    break

        last_msg_headers = {h["name"]: h["value"] for h in messages[-1].get("payload", {}).get("headers", [])}

        out.append({
            "thread_id": t["id"],
            "msg_count": len(messages),
            "last_subject": last_msg_headers.get("Subject", ""),
            "last_from": last_msg_headers.get("From", ""),
            "last_external_from": last_ext_from,
            "has_external_message": last_ext_idx >= 0,
            "internal_replied_after_external": user_replied_after,
        })
    return out


def run_quality_report(user, sample_size=10):
    """執行 4 大測試,產出 Quality Report dict。

    Test A: 過濾邏輯雙向比對(universe vs dashboard)
    Test B: AI 摘要 self-critique(隨機抽樣)
    Test C: Last-external-sender 邏輯 confusion matrix
    Test D: 速度與配額
    """
    import time
    import random

    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "user_email": user.get("email", ""),
        "search_days": SEARCH_DAYS,
    }

    # ── Test A:過濾雙向比對 + Test C:邏輯 confusion ──
    t_start = time.time()
    universe = fetch_all_business_threads(user["creds"], with_subjects=True)
    universe_secs = round(time.time() - t_start, 1)

    # universe 中:有外部信 + 內部還沒回 = 應該顯示
    should_show = [u for u in universe
                   if u["has_external_message"]
                   and not u["internal_replied_after_external"]]
    should_hide = [u for u in universe
                   if not u["has_external_message"]
                   or u["internal_replied_after_external"]]

    items = st.session_state.get("pending_items", [])
    shown_thread_ids = set()
    # pending_items 沒存 thread_id,改用 msg_id 對 last 一封 → 反查 universe 裡的 thread
    # 這裡放寬:用 last_external_from + subject 比對
    shown_subjects = {(it["subject"], extract_email(it["from"])): it for it in items}

    matched_show = 0
    missed_show = []  # 應該顯示但沒顯示
    for u in should_show:
        key = (u["last_subject"], extract_email(u["last_external_from"] or ""))
        if key in shown_subjects:
            matched_show += 1
        else:
            missed_show.append(u)

    false_positive = []  # 顯示了但 universe 認為不該
    should_hide_keys = {(u["last_subject"], extract_email(u["last_external_from"] or "")): u
                        for u in should_hide}
    for k, _ in shown_subjects.items():
        if k in should_hide_keys:
            false_positive.append(k)

    precision = round(100 * matched_show / max(len(items), 1), 1) if items else 0
    recall = round(100 * matched_show / max(len(should_show), 1), 1) if should_show else 0

    report["test_a"] = {
        "universe_size": len(universe),
        "should_show_count": len(should_show),
        "dashboard_shown_count": len(items),
        "matched": matched_show,
        "missed": len(missed_show),
        "false_positive": len(false_positive),
        "precision_pct": precision,
        "recall_pct": recall,
        "missed_examples": [
            {"subject": m["last_subject"], "from": m["last_external_from"]}
            for m in missed_show[:5]
        ],
        "false_positive_examples": list(false_positive[:5]),
        "fetch_time_secs": universe_secs,
    }

    # ── Test B:AI 摘要 self-critique ──
    summary_cache = st.session_state.get("summary_cache_for_table", {})
    eligible = [it for it in items if it["msg_id"] in summary_cache]

    sample = random.sample(eligible, min(sample_size, len(eligible))) if eligible else []
    critiques = []
    t_critique_start = time.time()

    for it in sample:
        cache_entry = summary_cache.get(it["msg_id"], {})
        ai_summary = cache_entry.get("summary", "")
        ai_actions = cache_entry.get("actions", "")
        if not ai_summary:
            continue
        result = gemini_critique_summary(
            it.get("thread_text", it.get("body", "")), ai_summary, ai_actions
        )
        if result:
            critiques.append({
                "msg_id": it["msg_id"],
                "subject": it["subject"][:60],
                **result,
            })
    critique_secs = round(time.time() - t_critique_start, 1)

    if critiques:
        avg_theme = round(sum(c.get("theme_score", 0) for c in critiques) / len(critiques), 2)
        avg_bullets = round(sum(c.get("bullets_score", 0) for c in critiques) / len(critiques), 2)
        avg_actions = round(sum(c.get("actions_score", 0) for c in critiques) / len(critiques), 2)
        avg_await = round(sum(c.get("awaiting_correct", 0) for c in critiques) / len(critiques), 2)
        weak_notes = [c["notes"] for c in critiques
                      if c.get("notes") and c["notes"].lower() != "none"]
        report["test_b"] = {
            "sample_size": len(critiques),
            "avg_theme_score": avg_theme,
            "avg_bullets_score": avg_bullets,
            "avg_actions_score": avg_actions,
            "avg_awaiting_correct": avg_await,
            "weak_examples": weak_notes[:5],
            "critique_time_secs": critique_secs,
            "extra_gemini_calls": len(critiques),
        }
    else:
        report["test_b"] = {
            "sample_size": 0, "note": "沒有可評估的摘要(可能 dashboard 還沒抓信)"
        }

    # ── Test C:Confusion matrix(基於 universe vs dashboard 對照) ──
    tp = matched_show                         # 該顯示且顯示
    fn = len(missed_show)                     # 該顯示但沒顯示
    fp = len(false_positive)                  # 不該顯示卻顯示
    tn = len(should_hide) - fp                # 不該顯示也沒顯示
    report["test_c"] = {
        "true_positive": tp, "false_negative": fn,
        "false_positive": fp, "true_negative": max(tn, 0),
        "f1_score": round(2 * precision * recall / max(precision + recall, 1), 1),
    }

    # ── Test D:速度 / 配額 ──
    n_items = len(items)
    # 估算:每封信摘要約 0.7 ~ 1.5 token-burst,平均約 1 call/封
    estimated_calls_today = n_items + len(critiques)
    GEMINI_FREE_QUOTA = 1500
    est_5_user = estimated_calls_today * 5

    report["test_d"] = {
        "fetch_universe_secs": universe_secs,
        "items_in_dashboard": n_items,
        "summary_critique_secs": critique_secs,
        "estimated_gemini_calls_today_1user": estimated_calls_today,
        "estimated_gemini_calls_today_5users": est_5_user,
        "free_quota_per_day": GEMINI_FREE_QUOTA,
        "quota_usage_5user_pct": round(100 * est_5_user / GEMINI_FREE_QUOTA, 1),
        "quota_safe": est_5_user < GEMINI_FREE_QUOTA,
    }

    return report


def render_quality_report(user):
    """執行並顯示 Quality Report。"""
    st.markdown("### 📋 Quality Report — 系統正確性驗證")
    st.caption(
        "對 dashboard 做 4 項客觀驗證:過濾正確性、AI 摘要品質、邏輯壓力測試、速度與配額。"
        " 產出可量化的指標,作為「系統實際有效」的客觀證據。"
    )

    if "_quality_report" not in st.session_state:
        if "pending_items" not in st.session_state:
            st.warning("請先讓 dashboard 抓完信件再跑 Quality Report")
            return
        with st.spinner("⏳ 跑驗證中(可能花 30-60 秒,會多用 ~10 次 Gemini 呼叫)..."):
            st.session_state["_quality_report"] = run_quality_report(user)

    report = st.session_state["_quality_report"]

    # ── Test A:過濾正確性 ──
    a = report.get("test_a", {})
    st.markdown("#### 1️⃣ 過濾邏輯正確性(Precision / Recall)")
    c1, c2, c3 = st.columns(3)
    c1.metric("精準率 Precision", f"{a.get('precision_pct', 0)}%",
              help="dashboard 顯示的信中有多少確實該回")
    c2.metric("召回率 Recall", f"{a.get('recall_pct', 0)}%",
              help="該顯示的信有多少被顯示")
    c3.metric("F1 Score", f"{report.get('test_c', {}).get('f1_score', 0)}",
              help="精準率與召回率的調和平均")

    a_summary = pd.DataFrame([
        ["業務 thread 全集大小", a.get("universe_size", 0)],
        ["邏輯上應該顯示", a.get("should_show_count", 0)],
        ["dashboard 實際顯示", a.get("dashboard_shown_count", 0)],
        ["匹配上(該顯示且顯示)", a.get("matched", 0)],
        ["漏掉(該顯示沒顯示)", a.get("missed", 0)],
        ["誤判(顯示但不該)", a.get("false_positive", 0)],
    ], columns=["項目", "數量"])
    st.dataframe(a_summary, use_container_width=True, hide_index=True)

    if a.get("missed_examples"):
        with st.expander(f"⚠️ 漏判例子({a.get('missed', 0)} 件)"):
            for ex in a["missed_examples"]:
                st.markdown(f"- **{ex['subject']}** — from {ex['from']}")

    # ── Test B:AI 品質 ──
    b = report.get("test_b", {})
    st.markdown("#### 2️⃣ AI 摘要品質(Gemini self-critique 評分,1-5)")
    if b.get("sample_size", 0) > 0:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Theme 準確度", f"{b['avg_theme_score']} / 5")
        c2.metric("Bullets 準確度", f"{b['avg_bullets_score']} / 5")
        c3.metric("Actions 可行度", f"{b['avg_actions_score']} / 5")
        c4.metric("Awaiting 正確", f"{b['avg_awaiting_correct']} / 5")
        st.caption(f"樣本數:{b['sample_size']} 封 / 評估耗時 {b.get('critique_time_secs', 0)} 秒")

        if b.get("weak_examples"):
            with st.expander("⚠️ 主要弱點(評審指出的問題)"):
                for note in b["weak_examples"]:
                    st.markdown(f"- {note}")
    else:
        st.info(b.get("note", "沒有可評估的摘要"))

    # ── Test C:Confusion ──
    c = report.get("test_c", {})
    st.markdown("#### 3️⃣ 邏輯壓力測試(Confusion Matrix)")
    cm = pd.DataFrame([
        ["**該顯示**", c.get("true_positive", 0), c.get("false_negative", 0)],
        ["**不該顯示**", c.get("false_positive", 0), c.get("true_negative", 0)],
    ], columns=["實際情況 / 預測", "✅ 顯示了", "❌ 沒顯示"])
    st.dataframe(cm, use_container_width=True, hide_index=True)

    # ── Test D:速度配額 ──
    d = report.get("test_d", {})
    st.markdown("#### 4️⃣ 速度與配額")
    d_df = pd.DataFrame([
        ["抓 Gmail 全集耗時", f"{d.get('fetch_universe_secs', 0)} 秒"],
        ["dashboard 顯示信數", d.get("items_in_dashboard", 0)],
        ["評估摘要耗時", f"{d.get('summary_critique_secs', 0)} 秒"],
        ["當天 Gemini 呼叫數(1 人)", d.get("estimated_gemini_calls_today_1user", 0)],
        ["當天 Gemini 呼叫數(估算 5 人)", d.get("estimated_gemini_calls_today_5users", 0)],
        ["免費額度", d.get("free_quota_per_day", 1500)],
        ["5 人並用配額占比", f"{d.get('quota_usage_5user_pct', 0)}%"],
        ["配額是否安全", "✅ 是" if d.get("quota_safe") else "❌ 會撞牆"],
    ], columns=["指標", "數值"])
    st.dataframe(d_df, use_container_width=True, hide_index=True)

    # ── 整體結論 ──
    st.markdown("#### 🎯 整體結論(可複製貼到報告 / 訊息)")
    verdict = (
        f"**BTL Email Monitor Quality Report — {report['ts'][:10]}**\n\n"
        f"- 過濾正確性:Precision **{a.get('precision_pct', 0)}%** / Recall **{a.get('recall_pct', 0)}%** "
        f"(F1 = {c.get('f1_score', 0)})\n"
        f"- AI 摘要品質(N={b.get('sample_size', 0)} 樣本):"
        f"Theme {b.get('avg_theme_score', 'N/A')}/5、"
        f"Bullets {b.get('avg_bullets_score', 'N/A')}/5、"
        f"Actions {b.get('avg_actions_score', 'N/A')}/5、"
        f"Awaiting 判斷 {b.get('avg_awaiting_correct', 'N/A')}/5\n"
        f"- Confusion: TP={c.get('true_positive', 0)}, "
        f"FN={c.get('false_negative', 0)}, FP={c.get('false_positive', 0)}, "
        f"TN={c.get('true_negative', 0)}\n"
        f"- 速度:抓信 {d.get('fetch_universe_secs', 0)} 秒,"
        f"5 人並用估佔配額 {d.get('quota_usage_5user_pct', 0)}%\n"
        f"- 結論:{'✅ 系統運作正常,可進入下一階段' if (a.get('precision_pct', 0) >= 80 and (b.get('avg_theme_score', 0) or 0) >= 3.5) else '⚠️ 有改進空間'}\n"
    )
    st.code(verdict, language="markdown")
    st.caption("👆 全選複製這段,可貼到工作群組或報告。")


# ═══════════════════════════════════════════════════════════════
# Excel Update Reminders (alert-only, NEVER touches the spreadsheet)
# ═══════════════════════════════════════════════════════════════

# Phase 1 sample data — replace with real AI detection in Phase 2
SAMPLE_EXCEL_REMINDERS = [
    {
        "msg_id": "sample_001",
        "client": "Skyfashion",
        "style": "SKY 80025",
        "title": "Color ratio change",
        "confidence": "high",          # high | medium
        "email_subject": "Re: SKY 80025 trims approval",
        "email_date": "2026-05-06 14:23",
        "from": "franky@skyfashion-jx.com",
        "quote": (
            "OK but please change ratio to navy 60% / black 40%, "
            "others remain. Please update PI accordingly."
        ),
        "before_after": [
            ("navy", "50%", "60%"),
            ("black", "50%", "40%"),
        ],
        "suggested_columns": ["Color ratio (navy / black)", "PI version"],
        "gmail_link": "https://mail.google.com/mail/u/0/#inbox/sample_001",
    },
    {
        "msg_id": "sample_002",
        "client": "Fuchsschmitt",
        "style": "WH 80512",
        "title": "Delivery date push",
        "confidence": "high",
        "email_subject": "WH 80512 — delivery update needed",
        "email_date": "2026-05-07 09:11",
        "from": "l.bickert@fuchsschmitt.de",
        "quote": (
            "Due to factory holiday, we need to push delivery from "
            "Sept 15 to Sept 22. Please confirm and update accordingly."
        ),
        "before_after": [
            ("Delivery date", "2026-09-15", "2026-09-22"),
        ],
        "suggested_columns": ["Delivery date", "Production schedule"],
        "gmail_link": "https://mail.google.com/mail/u/0/#inbox/sample_002",
    },
    {
        "msg_id": "sample_003",
        "client": "Brax",
        "style": "BRAX 06388",
        "title": "Pricing comment (review)",
        "confidence": "medium",
        "email_subject": "Comments on PI BRAX 06388",
        "email_date": "2026-05-07 11:45",
        "from": "sohaib.irshad@brax.com",
        "quote": (
            "Please double-check pricing for color #03 — seems higher "
            "than agreed. Awaiting your revised PI."
        ),
        "before_after": [],   # no clear before/after, comment only
        "suggested_columns": ["Unit price (color #03)", "Comments"],
        "gmail_link": "https://mail.google.com/mail/u/0/#inbox/sample_003",
    },
]


def get_reminder_state(user, msg_id):
    """讀取一筆 reminder 的狀態(updated / dismissed / 無紀錄)。

    Phase 1:暫用 session_state。Phase 2 整合時改成讀 Firestore
    users/{email}/reminders/{msg_id}。
    """
    return st.session_state.get(f"_reminder_state_{msg_id}", "pending")


def set_reminder_state(user, msg_id, state):
    """標記 reminder 狀態:updated / dismissed / pending"""
    st.session_state[f"_reminder_state_{msg_id}"] = state
    # TODO Phase 2:同步寫進 Firestore reminders/{msg_id}


def render_excel_update_panel(user, reminders=None):
    """頂部面板 — 顯示「需要更新大貨表」的提醒清單。

    系統絕不直接改 Excel,只負責提醒人工去更新。
    """
    if reminders is None:
        reminders = SAMPLE_EXCEL_REMINDERS  # Phase 1 demo data

    # 過濾掉已標記 updated 或 dismissed 的
    pending = [r for r in reminders
               if get_reminder_state(user, r["msg_id"]) == "pending"]

    if not pending:
        return  # 沒有 pending 的就不顯示

    high = [r for r in pending if r["confidence"] == "high"]
    medium = [r for r in pending if r["confidence"] == "medium"]

    with st.expander(
        f"📊 大貨表 Excel 待更新項目 ({len(pending)} 筆) "
        f"— 🔴 明確變動 {len(high)} / 🟡 可能需要變動 {len(medium)}",
        expanded=False,
    ):
        st.caption(
            "🛡️ **系統永遠不會直接修改你的 Excel** — "
            "以下項目是 AI 偵測到客戶在信件中 confirm / 變更 / 留下 comment,"
            "建議你手動去更新大貨表。"
        )

        for idx, r in enumerate(pending):
            confidence_emoji = "🔴" if r["confidence"] == "high" else "🟡"
            confidence_text = "明確變動" if r["confidence"] == "high" else "可能需要變動"

            st.markdown("---")
            st.markdown(
                f"### #{idx + 1}  {confidence_emoji}  "
                f"{r['client']} / {r['style']} — {r['title']}  "
                f"<span style='color:#888;font-size:0.8em'>({confidence_text})</span>",
                unsafe_allow_html=True,
            )

            # ── 信件來源 ─────────────────────────────────
            st.markdown(
                f"📧 **{r['email_subject']}**  \n"
                f"👤 {r['from']}   📅 {r['email_date']}"
            )

            # ── 原信原文片段(灰底引用框) ─────────────
            st.markdown("📝 **客戶說了什麼**:")
            st.markdown(
                f"<div style='background:#f1f3f4;border-left:4px solid #1a73e8;"
                f"padding:10px 14px;border-radius:4px;color:#444;"
                f"font-style:italic;margin:6px 0;'>"
                f"\"{r['quote']}\"</div>",
                unsafe_allow_html=True,
            )

            # ── 三個按鈕 ─────────────────────────────
            b1, b2, b3 = st.columns(3)
            with b1:
                if st.button("✓ 已更新大貨表", key=f"upd_{r['msg_id']}",
                             use_container_width=True):
                    set_reminder_state(user, r["msg_id"], "updated")
                    st.toast("✅ 已標記為已更新", icon="✅")
                    st.rerun()
            with b2:
                st.markdown(
                    f"<a href='{r['gmail_link']}' target='_blank' "
                    f"style='display:block;text-align:center;padding:8px;"
                    f"background:#fafafa;border:1px solid #dadce0;"
                    f"border-radius:6px;text-decoration:none;color:#1a73e8;'>"
                    f"📧 跳到原信</a>",
                    unsafe_allow_html=True,
                )
            with b3:
                if st.button("✗ 不需要(誤判)", key=f"dis_{r['msg_id']}",
                             use_container_width=True):
                    set_reminder_state(user, r["msg_id"], "dismissed")
                    st.toast("已標記為誤判,以後不再提醒", icon="🚫")
                    st.rerun()


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

    # 開發/PRD 用工具(收在 expander 裡,不影響日常使用)
    with st.sidebar.expander("🧪 開發工具"):
        if st.button("📋 Quality Report(系統驗證)"):
            st.session_state["_quality_check"] = True
            st.session_state.pop("_quality_report", None)  # 重新跑
        if st.button("Phase 6 Firestore 寫入測試"):
            st.session_state["_phase6_probe"] = True
        if st.button("📊 附件分析(PRD 用)"):
            st.session_state["_attachment_analysis"] = True
            st.session_state.pop("_attachment_stats", None)  # 重新分析

    if st.session_state.get("_quality_check"):
        render_quality_report(user)
        if st.button("關閉 Quality Report"):
            st.session_state.pop("_quality_check", None)
            st.session_state.pop("_quality_report", None)
            st.rerun()
        st.divider()

    if st.session_state.get("_phase6_probe"):
        phase6_firestore_probe(user)
        if st.button("關閉驗證面板"):
            st.session_state.pop("_phase6_probe", None)
            st.rerun()
        st.divider()

    if st.session_state.get("_attachment_analysis"):
        render_attachment_analysis(user)
        if st.button("關閉附件分析面板"):
            st.session_state.pop("_attachment_analysis", None)
            st.session_state.pop("_attachment_stats", None)
            st.rerun()
        st.divider()

    # 大貨表 Excel 更新提醒(Phase 1:demo 資料,Phase 2 接 AI)
    render_excel_update_panel(user)

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

    # 從 Firestore 載入這個 user 之前所有的編輯,merge 進 session_state
    # (只在第一次進 dashboard 時跑一次,失敗也不擋使用)
    if not st.session_state.get("_firestore_edits_loaded"):
        cloud_edits = load_edits_from_firestore(user)
        for edit_msg_id, edit_data in cloud_edits.items():
            saved_key = f"_saved_{edit_msg_id}"
            # 雲端有資料但 session 還沒載入 → 從雲端帶回來
            if saved_key not in st.session_state:
                st.session_state[saved_key] = {
                    "title": edit_data.get("title", ""),
                    "summary": edit_data.get("summary", ""),
                    "actions": edit_data.get("actions", ""),
                }
        st.session_state["_firestore_edits_loaded"] = True
        if cloud_edits:
            st.toast(f"☁️ 已從雲端載入 {len(cloud_edits)} 筆之前的編輯", icon="✅")

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
