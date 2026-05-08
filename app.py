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
# 整 thread 喂 Gemini 时,单封信内容上限(避免超大附件信吃光配额)
THREAD_PER_MSG_CHARS = 1500
# 整 thread 总上限(Gemini Flash context 1M tokens 虽然吃得下,但仍设保险阈值)
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
        return "< 1 小时"
    if hours < 24:
        return f"{hours} 小时"
    days = hours // 24
    rem = hours % 24
    return f"{days} 天" if rem == 0 else f"{days} 天 {rem} 小时"


def extract_theme(summary_text):
    if not summary_text:
        return ""
    m = re.search(r"\*\*Theme:\*\*\s*(.+?)(?:\n|$)", summary_text)
    if m:
        return m.group(1).strip().strip('"').strip("'")
    return ""


def extract_topic_key(text):
    """
    从文字中抓「订单编号 / 款号」,作为「同主题」配对的 key。
    例:'Re: SKY 80025 sample' → 'SKY-80025'
        'BRAX 06388 mockup'   → 'BRAX-06388'
        'updated PI for #2317' → 'NUM-2317'
        '客户问候' → None
    """
    if not text:
        return None
    upper = str(text).upper()
    # 主要模式:品牌前缀 + 4-6 位数字
    m = re.search(
        r"\b(SKY|BRAX|WH|YAN|BIN|FNS|BTL|FCL|HW|BX|FS|YT|YAN|MJ)[\s\-/]*(\d{4,6})\b",
        upper,
    )
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    # 备案:单独的 5-6 位数字(避免 4 位被误抓成年份)
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
        # Phase 6 验证用:Google id_token,后续可以拿来换 Firebase ID token
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
    """把整个 thread 串成一份「对话纪录」文字,给 Gemini 做有完整脉络的摘要。

    每封信输出格式:
        [编号] From: <寄件者>  Date: <日期>
        <内文(裁切到 THREAD_PER_MSG_CHARS)>

    总长度若超过 THREAD_TOTAL_CHARS,从最旧的那几封开始截掉,优先保留近期对话。
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
    # 太长时砍最早的讯息,保留最近的
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

        # 整个 thread 拼成完整对话纪录,让 Gemini 看到全部脉络
        thread_text = build_thread_transcript(messages_meta)

        # Debug 用:抓最后一封外部信的原始 labelIds(看是不是 Gmail 端就标错)
        last_external_meta = messages_meta[last_external_idx]
        last_external_labels = last_external_meta.get("labelIds", [])

        items.append({
            "msg_id": last_msg["id"], "subject": last_msg["subject"],
            "from": last_msg["from"], "date": last_msg["date"],
            "body": last_msg["body"], "thread_text": thread_text,
            "is_unread": last_msg["is_unread"],
            "is_today": is_today, "age_hours": age_hours,
            # Debug 资讯
            "_debug_thread_id": t["id"],
            "_debug_msg_count": len(messages_meta),
            "_debug_last_external_idx": last_external_idx,
            "_debug_last_external_labels": last_external_labels,
        })

    items.sort(key=lambda x: (
        not x["is_unread"], not x["is_today"], -x["age_hours"],
    ))
    return items


@st.cache_data(ttl=86400, show_spinner=False)
def gemini_summary_and_actions(msg_id, subject, thread_text):
    """根据完整 thread(而不只是最后一封)产出摘要 + 待办。

    cache key 包含 thread_text → 若 thread 多了一封新信,自动 re-summarize。
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
        return "(摘要产生失败)", ""
    try:
        parts = resp.json()["candidates"][0]["content"]["parts"]
        full_text = "".join(p.get("text", "") for p in parts).strip()
        sections = full_text.split("[---]")
        return sections[0].strip(), (sections[1].strip() if len(sections) > 1 else "")
    except (KeyError, IndexError):
        return "(摘要产生失败)", ""


def gemini_reply_draft(msg_id, subject, thread_text, actions, sender_name, user_first_name):
    """根据完整 thread 写回信草稿,避免重述 thread 早期已讨论过的内容。"""
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
        # 用整 thread(thread_text)而不是只有最后一封(body)— 让 Gemini 看到完整脉络
        summary, actions = gemini_summary_and_actions(
            it["msg_id"], it["subject"], it["thread_text"]
        )
        out[it["msg_id"]] = {"summary": summary, "actions": actions, "theme": extract_theme(summary)}
    return out


def compute_grouped_titles(items, summary_cache):
    """
    判定哪些信是「同主题」(透过订单编号配对),产出每封信的最终显示标题。

    逻辑:
      1. 每封信抽出订单编号(从原主旨 + AI Theme 找)
      2. 计数:有多少信用同一个订单编号
      3. 若 ≥ 2 → 该组共用一个统一标题(订单编号)
      4. 若 = 1 或 None → 保持原主旨
    """
    # Step 1:每封信的 topic key
    topic_keys = {}
    for it in items:
        cached = summary_cache.get(it["msg_id"], {})
        theme = cached.get("theme", "")
        # 先看原主旨,没抓到再看 AI theme
        key = extract_topic_key(it["subject"]) or extract_topic_key(theme)
        topic_keys[it["msg_id"]] = key

    # Step 2:计数
    key_counts = Counter(k for k in topic_keys.values() if k)

    # Step 3:给每封信派标题
    titles = {}
    for it in items:
        msg_id = it["msg_id"]
        key = topic_keys[msg_id]
        if key and key_counts[key] >= 2:
            # 同主题的多封信 → 统一标题
            if key.startswith("NUM-"):
                titles[msg_id] = f"#{key[4:]} 相关信件"
            else:
                # 例如 SKY-80025 → "SKY 80025"
                brand, num = key.split("-", 1)
                titles[msg_id] = f"{brand} {num}"
        else:
            # 唯一主题或抓不到编号 → 保留原主旨
            titles[msg_id] = it["subject"]
    return titles, topic_keys, key_counts


st.set_page_config(page_title="BTL Email Monitor", page_icon="📧", layout="wide")


def show_login_page():
    st.title("📧 BTL Email Monitor")
    st.markdown("---")
    st.markdown("### 请使用 ibiney.io Google 帐号登入")
    st.markdown(
        "登入后,仪表板会显示**你自己 Gmail 中**符合条件的客户待回信件 + AI 摘要。"
    )
    auth_url = get_login_url()
    st.link_button("🔐 Sign in with Google", auth_url, type="primary")
    st.caption("⚠️ 首次登入会看到「Google hasn't verified this app」警告 → 点 Advanced → Continue,因为这是 ibiney 公司内部 app。")


def _encode_session_marker(refresh_token, email):
    """把 refresh_token + email 编成可放 URL 的短字串。

    refresh_token 不是密码,但仍是敏感资讯;放 URL 算是 trade-off。
    用 base64 url-safe encoding 避免特殊字元。
    """
    import json as _json
    payload = _json.dumps({"rt": refresh_token, "em": email})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_session_marker(marker):
    """反向解码。失败回 None。"""
    import json as _json
    try:
        decoded = base64.urlsafe_b64decode(marker.encode("ascii")).decode("utf-8")
        data = _json.loads(decoded)
        return data.get("rt"), data.get("em")
    except Exception:
        return None, None


def _refresh_access_token(refresh_token):
    """用 refresh_token 换新的 access_token + id_token。

    Google OAuth refresh_token 不会过期(除非用户撤销),
    所以可以一直用它来重新拿短期的 access_token。
    """
    try:
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        if not resp.ok:
            return None
        return resp.json()  # {access_token, expires_in, id_token, ...}
    except Exception:
        return None


def restore_session_from_url():
    """从 URL `?_s=...` 还原使用者 session(支援浏览器重新整理)。

    流程:
    1. 看 URL 有没有 `_s` 参数
    2. 解出 refresh_token + email
    3. 用 refresh_token 换新 access_token
    4. 抓 user info(name / picture)
    5. 写进 session_state["user"]
    """
    qp = st.query_params
    if "_s" not in qp:
        return False
    if "user" in st.session_state:
        return True  # 已经有了

    refresh_token, email = _decode_session_marker(qp["_s"])
    if not refresh_token or not email:
        # marker 坏了,清掉它
        try:
            del st.query_params["_s"]
        except Exception:
            pass
        return False

    token_data = _refresh_access_token(refresh_token)
    if not token_data:
        return False

    try:
        user_info = requests.get(
            "https://www.googleapis.com/oauth2/v1/userinfo",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
            timeout=10,
        ).json()
    except Exception:
        return False

    st.session_state["user"] = {
        "creds": {
            "token": token_data["access_token"],
            "refresh_token": refresh_token,  # 保留,refresh_token 不会过期
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scopes": OAUTH_SCOPES,
        },
        "id_token": token_data.get("id_token", ""),
        "email": user_info.get("email", email),
        "name": user_info.get("name", ""),
        "picture": user_info.get("picture", ""),
    }
    return True


def handle_oauth_callback():
    qp = st.query_params
    if "code" not in qp:
        return False
    try:
        result = exchange_code_for_token(qp["code"])
        if not result["email"].lower().endswith("@" + INTERNAL_DOMAIN):
            st.error(f"此系统仅限 @{INTERNAL_DOMAIN} 同事使用。你的帐号:{result['email']}")
            st.stop()
        st.session_state["user"] = result

        # 把 refresh_token + email 写进 URL,让重新整理时可以还原 session
        rt = result.get("creds", {}).get("refresh_token", "")
        if rt:
            marker = _encode_session_marker(rt, result["email"])
            st.query_params.clear()
            st.query_params["_s"] = marker
        else:
            st.query_params.clear()
        st.rerun()
    except Exception as e:
        st.error(f"登入失败:{e}")
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
<h4 style="margin-top:0;color:#E65100;">🎯 你该做的事(优先看!)</h4>
<div style="font-size:15px;line-height:1.8;">{md_to_html(display_actions)}</div>
</div>
            """,
            unsafe_allow_html=True,
        )

    left_col, right_col = st.columns([1, 1])

    with left_col:
        st.markdown("### ✏️ 可编辑区(改完按下方储存到云端,跨 session 保留)")
        with st.form(key=f"edit_{msg_id}", clear_on_submit=False):
            new_title = st.text_input("📄 标题", value=final_title)
            new_summary = st.text_area("📝 AI 摘要", value=display_summary, height=200)
            new_actions = st.text_area("🎯 待办事项", value=display_actions, height=200)
            if st.form_submit_button("💾 储存到云端", use_container_width=True):
                # 1. 立即更新 session_state(本页立刻反映新值,不等网路)
                st.session_state[f"_saved_{msg_id}"] = {
                    "title": new_title, "summary": new_summary, "actions": new_actions,
                }
                # 2. 同步写进 Firestore(跨 session 保留)
                user_for_save = st.session_state.get("user", {})
                ok, msg = save_edit_to_firestore(
                    user_for_save, msg_id, new_title, new_summary, new_actions,
                )
                if ok:
                    st.success(f"✅ {msg}(关 tab 再开还在)")
                else:
                    st.warning(
                        f"⚠️ 云端储存失败:{msg}。本次编辑仍会在 session 内保留,"
                        "但关 tab 后会遗失。"
                    )
                st.rerun(scope="fragment")

    with right_col:
        st.markdown("### 📧 信件内容(原文,仅供参考)")
        st.text_area(
            "body", value=body, height=520, disabled=True, label_visibility="collapsed",
        )

    gmail_link = f"https://mail.google.com/mail/u/0/#inbox/{msg_id}"
    st.markdown(f"[🔗 在 Gmail 中开启原信]({gmail_link})")

    st.divider()
    st.markdown("### ✍️ AI 一键产生回信草稿")
    draft_key = f"_draft_{msg_id}"
    if st.button("✍️ 产生英文回信草稿", use_container_width=True, key=f"btn_{msg_id}"):
        with st.spinner("Gemini 写回信中(10-20 秒)..."):
            sender_name = clean_sender(item["from"])
            # 把整 thread 喂给草稿生成,避免回信跟早期讨论不一致
            thread_text_for_draft = item.get("thread_text", body)
            draft = gemini_reply_draft(
                msg_id, subject, thread_text_for_draft, display_actions,
                sender_name, user_first_name,
            )
        if draft:
            st.session_state[draft_key] = draft
            st.rerun(scope="fragment")
    if draft_key in st.session_state:
        st.markdown("##### 📝 建议回信")
        st.text_area(
            "draft", value=st.session_state[draft_key], height=300,
            label_visibility="collapsed", key=f"d_{msg_id}",
        )
        st.caption("✂️ 滑鼠选取 → ⌘+C 复制 → 开 Gmail Reply → ⌘+V 贴上")


def get_firebase_id_token(user):
    """把 Google id_token 换成 Firebase ID token(每 50 分钟 cache 一次,避免每次储存都重换)。

    回传 Firebase ID token 字串,失败回 None。
    """
    cached = st.session_state.get("_firebase_id_token")
    cached_at = st.session_state.get("_firebase_id_token_at", 0)
    now_ts = datetime.now(timezone.utc).timestamp()
    # Firebase ID token 1 小时过期,提前 10 分钟 refresh
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
    """把使用者编辑写进 Firestore (users/{email}/edits/{msg_id})。

    用 PATCH(updateMask)做 upsert:已存在就覆盖指定栏位,不存在就建。
    回传 (success: bool, message: str)。
    """
    firebase_token = get_firebase_id_token(user)
    if not firebase_token:
        return False, "未取得 Firebase token(请重新登入)"

    email = user.get("email", "")
    if not email:
        return False, "无使用者 email"

    project_id = "trims-f8e4a"
    # PATCH 端点 + updateMask 达成「upsert」(已存在则更新,不存在则建立)
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
            return True, "已储存到云端"
        return False, f"储存失败 HTTP {resp.status_code}"
    except Exception as e:
        return False, f"储存例外:{e}"


def load_edits_from_firestore(user):
    """从 Firestore 捞这个 user 所有的编辑纪录(users/{email}/edits/*)。

    回传 dict: { msg_id: {title, summary, actions, updated_at} }
    Firestore 没资料或失败时回空 dict(不影响 dashboard 主功能)。
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
    """扫描使用者过去 SEARCH_DAYS 天的待回 thread,统计附件分布。

    产出 multi-modal PRD 需要的真实数据:附件类型、客户分布、业务话题分布。
    回传 dict 结构,在 streamlit 端渲染成表格。
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
        "mime_types": Counter(),       # 每个 MIME 类型计数
        "file_extensions": Counter(),  # 副档名计数
        "client_attachments": Counter(),  # 客户 → 附件数
        "subject_keywords": Counter(),    # 主旨关键字
        "size_buckets": Counter(),     # 附件大小分布
        "thread_with_attachment_count": 0,  # 多少 thread 至少有 1 个附件
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

            # 主旨关键字统计
            for word in business_words:
                if word.lower() in subject.lower():
                    stats["subject_keywords"][word] += 1

            # 找附件:递回扫 payload.parts,filename 非空就是附件
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
                    # 副档名
                    fname = att["filename"]
                    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else "(no_ext)"
                    stats["file_extensions"][ext] += 1
                    # 客户
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
    """执行 + 渲染附件分析结果(PRD 证据用)。"""
    st.markdown("### 📊 附件分析(Multi-modal PRD 数据收集)")
    st.caption(
        f"扫描你过去 {SEARCH_DAYS} 天 inbox 业务 thread 的附件分布 — "
        "这份数据会用来决定 multi-modal 该优先支援什么档案类型。"
    )

    if "_attachment_stats" not in st.session_state:
        with st.spinner(f"分析 Gmail 附件中(20-40 秒)..."):
            try:
                st.session_state["_attachment_stats"] = analyze_attachments(
                    user["creds"], user["email"]
                )
            except Exception as e:
                st.error(f"分析失败:{e}")
                return

    stats = st.session_state["_attachment_stats"]

    # ── 总览 ──────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("扫描 Thread 数", stats["total_threads"])
    c2.metric("总信件数", stats["total_messages"])
    c3.metric("有附件的信", stats["messages_with_attachments"])
    c4.metric("总附件数", stats["total_attachments"])

    if stats["total_messages"] == 0:
        st.warning("没有找到任何信件")
        return

    # ── 附件密度 ──────────────────────────────────────
    pct_with = round(100 * stats["messages_with_attachments"] / stats["total_messages"], 1)
    pct_thread = round(100 * stats["thread_with_attachment_count"] / stats["total_threads"], 1)
    st.markdown(f"""
    **📌 附件密度**:
    - 有附件的信占 **{pct_with}%**({stats['messages_with_attachments']}/{stats['total_messages']})
    - 有附件的 thread 占 **{pct_thread}%**({stats['thread_with_attachment_count']}/{stats['total_threads']})
    - 平均每封有附件的信 = **{round(stats['total_attachments'] / max(stats['messages_with_attachments'], 1), 2)} 个**附件
    """)

    # ── 副档名 Top 10 ────────────────────────────────
    st.markdown("#### 📁 附件副档名分布(这个最关键 — 决定要支援哪些格式)")
    if stats["file_extensions"]:
        ext_df = pd.DataFrame(
            stats["file_extensions"].most_common(15),
            columns=["副档名", "出现次数"],
        )
        ext_df["占比"] = ext_df["出现次数"].apply(
            lambda n: f"{round(100 * n / stats['total_attachments'], 1)}%"
        )
        st.dataframe(ext_df, use_container_width=True, hide_index=True)
    else:
        st.info("没有附件可分析")

    # ── MIME 类型 ────────────────────────────────────
    st.markdown("#### 🔬 MIME 类型(技术精确版)")
    if stats["mime_types"]:
        mime_df = pd.DataFrame(
            stats["mime_types"].most_common(15),
            columns=["MIME", "次数"],
        )
        st.dataframe(mime_df, use_container_width=True, hide_index=True)

    # ── 客户分布 ────────────────────────────────────
    st.markdown("#### 🏢 哪些客户最常寄附件?")
    if stats["client_attachments"]:
        client_df = pd.DataFrame(
            stats["client_attachments"].most_common(10),
            columns=["客户", "附件数"],
        )
        st.dataframe(client_df, use_container_width=True, hide_index=True)

    # ── 附件大小 ───────────────────────────────────
    st.markdown("#### 📦 附件大小分布(影响 Gemini upload 时间)")
    if stats["size_buckets"]:
        size_df = pd.DataFrame(
            list(stats["size_buckets"].items()),
            columns=["大小范围", "个数"],
        )
        st.dataframe(size_df, use_container_width=True, hide_index=True)

    # ── 主旨关键字 ──────────────────────────────────
    st.markdown("#### 💬 主旨业务关键字 Top 10(内容话题分布)")
    if stats["subject_keywords"]:
        kw_df = pd.DataFrame(
            stats["subject_keywords"].most_common(10),
            columns=["关键字", "出现次数"],
        )
        st.dataframe(kw_df, use_container_width=True, hide_index=True)

    # ── 给 PRD 的洞察 ───────────────────────────────
    st.markdown("#### 💡 对 PRD 的初步洞察")
    top_exts = stats["file_extensions"].most_common(3)
    if top_exts:
        top_summary = ", ".join([f"`.{e}` ({n})" for e, n in top_exts])
        st.info(
            f"**Top 3 副档名**:{top_summary}\n\n"
            f"→ Multi-modal 第一阶段建议优先支援:**{top_exts[0][0]}**"
            f"({round(100 * top_exts[0][1] / stats['total_attachments'], 1)}% 涵盖率)"
        )


def phase6_firestore_probe(user):
    """Phase 6 验证:测试 user OAuth token 能否写入 Firestore。

    跑两个 Plan:
    - Plan A:用 Google access_token 直接呼叫 Firestore REST API
    - Plan B:用 Google id_token 换 Firebase ID token,再写 Firestore
    每个 Plan 写一笔到 test_probe collection,印出 HTTP 状态与回应。
    """
    project_id = "trims-f8e4a"
    firebase_api_key = st.secrets.get("FIREBASE_WEB_API_KEY", "")
    creds = user.get("creds", {})
    access_token = creds.get("token", "")
    id_token = user.get("id_token", "")
    email = user.get("email", "")

    st.markdown("### 🧪 Phase 6 — Firestore 写入可行性验证")
    st.caption("测试会写一笔到 `test_probe/<email>`,不影响正式资料。Rules 已限制只能 ibiney.io 写入。")

    # ── Plan A:直接用 access_token 呼 Firestore REST ────────
    st.markdown("#### Plan A:Google access_token → Firestore REST")
    if not access_token:
        st.error("找不到 access_token,请重新登入")
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
            with st.expander("查看回应内容"):
                st.code(resp_a.text[:1500])
        except Exception as e:
            st.error(f"Plan A 例外:{e}")

    st.divider()

    # ── Plan B:id_token 换 Firebase token → Firestore ───────
    st.markdown("#### Plan B:Google id_token → Firebase ID token → Firestore")
    if not id_token:
        st.warning(
            "⚠️ 此 session 没有 id_token(你登入是在加这个栏位之前)。\n\n"
            "请按右上「🚪 登出」重新登入,新 session 才有 id_token,Plan B 才能测。"
        )
    elif not firebase_api_key:
        st.warning(
            "⚠️ Streamlit Secrets 没设 `FIREBASE_WEB_API_KEY`。\n\n"
            "Plan B 需要这个 key 才能呼叫 Firebase Identity Toolkit 换 token。\n"
            "去 https://console.firebase.google.com/project/trims-f8e4a/settings/general → 看 Web API Key,"
            "贴进 Streamlit Cloud → btl-dashboard → Settings → Secrets,新增:\n"
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
                with st.expander("查看 exchange 失败回应"):
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
                st.write(f"Firestore 写入 HTTP **{resp_b.status_code}** {'✅ 可行' if resp_b.ok else '❌ 不通'}")
                with st.expander("查看写入回应"):
                    st.code(resp_b.text[:1500])
        except Exception as e:
            st.error(f"Plan B 例外:{e}")


# ═══════════════════════════════════════════════════════════════
# Quality Report:用客观数据验证「系统真的有效吗」
# ═══════════════════════════════════════════════════════════════

def gemini_critique_summary(thread_text, ai_summary, ai_actions):
    """让另一个 Gemini call 扮演评审,评分既有的 AI 摘要 + 待办。

    回传 dict:{theme_score, bullets_score, actions_score, awaiting_correct, notes}
    每个分数 1-5。失败回 None。
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
    """抓 SEARCH_DAYS 天内所有「主旨含业务关键字」的 thread,回传 thread metadata list。

    与 fetch_pending_emails 不同,这里不套用「最后外部寄件者 + 我未回」过滤。
    用来做双向比对:看完整 universe 大小,跟 dashboard 显示的差距。
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

        # 找最后一封外部 + 之后是否有当前 user 回复
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
                # 注意:这里无法精确判断「当前 user」是谁,因为这函式不传 user_email
                # 改成:看是不是 internal domain → 任一 ibiney 都算「我们有人回了」
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
    """执行 4 大测试,产出 Quality Report dict。

    Test A: 过滤逻辑双向比对(universe vs dashboard)
    Test B: AI 摘要 self-critique(随机抽样)
    Test C: Last-external-sender 逻辑 confusion matrix
    Test D: 速度与配额
    """
    import time
    import random

    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "user_email": user.get("email", ""),
        "search_days": SEARCH_DAYS,
    }

    # ── Test A:过滤双向比对 + Test C:逻辑 confusion ──
    t_start = time.time()
    universe = fetch_all_business_threads(user["creds"], with_subjects=True)
    universe_secs = round(time.time() - t_start, 1)

    # universe 中:有外部信 + 内部还没回 = 应该显示
    should_show = [u for u in universe
                   if u["has_external_message"]
                   and not u["internal_replied_after_external"]]
    should_hide = [u for u in universe
                   if not u["has_external_message"]
                   or u["internal_replied_after_external"]]

    items = st.session_state.get("pending_items", [])
    shown_thread_ids = set()
    # pending_items 没存 thread_id,改用 msg_id 对 last 一封 → 反查 universe 里的 thread
    # 这里放宽:用 last_external_from + subject 比对
    shown_subjects = {(it["subject"], extract_email(it["from"])): it for it in items}

    matched_show = 0
    missed_show = []  # 应该显示但没显示
    for u in should_show:
        key = (u["last_subject"], extract_email(u["last_external_from"] or ""))
        if key in shown_subjects:
            matched_show += 1
        else:
            missed_show.append(u)

    false_positive = []  # 显示了但 universe 认为不该
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
            "sample_size": 0, "note": "没有可评估的摘要(可能 dashboard 还没抓信)"
        }

    # ── Test C:Confusion matrix(基于 universe vs dashboard 对照) ──
    tp = matched_show                         # 该显示且显示
    fn = len(missed_show)                     # 该显示但没显示
    fp = len(false_positive)                  # 不该显示却显示
    tn = len(should_hide) - fp                # 不该显示也没显示
    report["test_c"] = {
        "true_positive": tp, "false_negative": fn,
        "false_positive": fp, "true_negative": max(tn, 0),
        "f1_score": round(2 * precision * recall / max(precision + recall, 1), 1),
    }

    # ── Test D:速度 / 配额 ──
    n_items = len(items)
    # 估算:每封信摘要约 0.7 ~ 1.5 token-burst,平均约 1 call/封
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
    """执行并显示 Quality Report。"""
    st.markdown("### 📋 Quality Report — 系统正确性验证")
    st.caption(
        "对 dashboard 做 4 项客观验证:过滤正确性、AI 摘要品质、逻辑压力测试、速度与配额。"
        " 产出可量化的指标,作为「系统实际有效」的客观证据。"
    )

    if "_quality_report" not in st.session_state:
        if "pending_items" not in st.session_state:
            st.warning("请先让 dashboard 抓完信件再跑 Quality Report")
            return
        with st.spinner("⏳ 跑验证中(可能花 30-60 秒,会多用 ~10 次 Gemini 呼叫)..."):
            st.session_state["_quality_report"] = run_quality_report(user)

    report = st.session_state["_quality_report"]

    # ── Test A:过滤正确性 ──
    a = report.get("test_a", {})
    st.markdown("#### 1️⃣ 过滤逻辑正确性(Precision / Recall)")
    c1, c2, c3 = st.columns(3)
    c1.metric("精准率 Precision", f"{a.get('precision_pct', 0)}%",
              help="dashboard 显示的信中有多少确实该回")
    c2.metric("召回率 Recall", f"{a.get('recall_pct', 0)}%",
              help="该显示的信有多少被显示")
    c3.metric("F1 Score", f"{report.get('test_c', {}).get('f1_score', 0)}",
              help="精准率与召回率的调和平均")

    a_summary = pd.DataFrame([
        ["业务 thread 全集大小", a.get("universe_size", 0)],
        ["逻辑上应该显示", a.get("should_show_count", 0)],
        ["dashboard 实际显示", a.get("dashboard_shown_count", 0)],
        ["匹配上(该显示且显示)", a.get("matched", 0)],
        ["漏掉(该显示没显示)", a.get("missed", 0)],
        ["误判(显示但不该)", a.get("false_positive", 0)],
    ], columns=["项目", "数量"])
    st.dataframe(a_summary, use_container_width=True, hide_index=True)

    if a.get("missed_examples"):
        with st.expander(f"⚠️ 漏判例子({a.get('missed', 0)} 件)"):
            for ex in a["missed_examples"]:
                st.markdown(f"- **{ex['subject']}** — from {ex['from']}")

    # ── Test B:AI 品质 ──
    b = report.get("test_b", {})
    st.markdown("#### 2️⃣ AI 摘要品质(Gemini self-critique 评分,1-5)")
    if b.get("sample_size", 0) > 0:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Theme 准确度", f"{b['avg_theme_score']} / 5")
        c2.metric("Bullets 准确度", f"{b['avg_bullets_score']} / 5")
        c3.metric("Actions 可行度", f"{b['avg_actions_score']} / 5")
        c4.metric("Awaiting 正确", f"{b['avg_awaiting_correct']} / 5")
        st.caption(f"样本数:{b['sample_size']} 封 / 评估耗时 {b.get('critique_time_secs', 0)} 秒")

        if b.get("weak_examples"):
            with st.expander("⚠️ 主要弱点(评审指出的问题)"):
                for note in b["weak_examples"]:
                    st.markdown(f"- {note}")
    else:
        st.info(b.get("note", "没有可评估的摘要"))

    # ── Test C:Confusion ──
    c = report.get("test_c", {})
    st.markdown("#### 3️⃣ 逻辑压力测试(Confusion Matrix)")
    cm = pd.DataFrame([
        ["**该显示**", c.get("true_positive", 0), c.get("false_negative", 0)],
        ["**不该显示**", c.get("false_positive", 0), c.get("true_negative", 0)],
    ], columns=["实际情况 / 预测", "✅ 显示了", "❌ 没显示"])
    st.dataframe(cm, use_container_width=True, hide_index=True)

    # ── Test D:速度配额 ──
    d = report.get("test_d", {})
    st.markdown("#### 4️⃣ 速度与配额")
    d_df = pd.DataFrame([
        ["抓 Gmail 全集耗时", f"{d.get('fetch_universe_secs', 0)} 秒"],
        ["dashboard 显示信数", d.get("items_in_dashboard", 0)],
        ["评估摘要耗时", f"{d.get('summary_critique_secs', 0)} 秒"],
        ["当天 Gemini 呼叫数(1 人)", d.get("estimated_gemini_calls_today_1user", 0)],
        ["当天 Gemini 呼叫数(估算 5 人)", d.get("estimated_gemini_calls_today_5users", 0)],
        ["免费额度", d.get("free_quota_per_day", 1500)],
        ["5 人并用配额占比", f"{d.get('quota_usage_5user_pct', 0)}%"],
        ["配额是否安全", "✅ 是" if d.get("quota_safe") else "❌ 会撞墙"],
    ], columns=["指标", "数值"])
    st.dataframe(d_df, use_container_width=True, hide_index=True)

    # ── 整体结论 ──
    st.markdown("#### 🎯 整体结论(可复制贴到报告 / 讯息)")
    verdict = (
        f"**BTL Email Monitor Quality Report — {report['ts'][:10]}**\n\n"
        f"- 过滤正确性:Precision **{a.get('precision_pct', 0)}%** / Recall **{a.get('recall_pct', 0)}%** "
        f"(F1 = {c.get('f1_score', 0)})\n"
        f"- AI 摘要品质(N={b.get('sample_size', 0)} 样本):"
        f"Theme {b.get('avg_theme_score', 'N/A')}/5、"
        f"Bullets {b.get('avg_bullets_score', 'N/A')}/5、"
        f"Actions {b.get('avg_actions_score', 'N/A')}/5、"
        f"Awaiting 判断 {b.get('avg_awaiting_correct', 'N/A')}/5\n"
        f"- Confusion: TP={c.get('true_positive', 0)}, "
        f"FN={c.get('false_negative', 0)}, FP={c.get('false_positive', 0)}, "
        f"TN={c.get('true_negative', 0)}\n"
        f"- 速度:抓信 {d.get('fetch_universe_secs', 0)} 秒,"
        f"5 人并用估占配额 {d.get('quota_usage_5user_pct', 0)}%\n"
        f"- 结论:{'✅ 系统运作正常,可进入下一阶段' if (a.get('precision_pct', 0) >= 80 and (b.get('avg_theme_score', 0) or 0) >= 3.5) else '⚠️ 有改进空间'}\n"
    )
    st.code(verdict, language="markdown")
    st.caption("👆 全选复制这段,可贴到工作群组或报告。")


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
    """读取一笔 reminder 的状态(updated / dismissed / 无纪录)。

    Phase 1:暂用 session_state。Phase 2 整合时改成读 Firestore
    users/{email}/reminders/{msg_id}。
    """
    return st.session_state.get(f"_reminder_state_{msg_id}", "pending")


def set_reminder_state(user, msg_id, state):
    """标记 reminder 状态:updated / dismissed / pending"""
    st.session_state[f"_reminder_state_{msg_id}"] = state
    # TODO Phase 2:同步写进 Firestore reminders/{msg_id}


def render_excel_update_panel(user, reminders=None):
    """顶部面板 — 显示「需要更新大货表」的提醒清单。

    系统绝不直接改 Excel,只负责提醒人工去更新。
    """
    if reminders is None:
        reminders = SAMPLE_EXCEL_REMINDERS  # Phase 1 demo data

    # 过滤掉已标记 updated 或 dismissed 的
    pending = [r for r in reminders
               if get_reminder_state(user, r["msg_id"]) == "pending"]

    if not pending:
        return  # 没有 pending 的就不显示

    high = [r for r in pending if r["confidence"] == "high"]
    medium = [r for r in pending if r["confidence"] == "medium"]

    with st.expander(
        f"📊 大货表 Excel 待更新项目 ({len(pending)} 笔) "
        f"— 🔴 明确变动 {len(high)} / 🟡 可能需要变动 {len(medium)}",
        expanded=False,
    ):
        st.caption(
            "🛡️ **系统永远不会直接修改你的 Excel** — "
            "以下项目是 AI 侦测到客户在信件中 confirm / 变更 / 留下 comment,"
            "建议你手动去更新大货表。"
        )

        for idx, r in enumerate(pending):
            confidence_emoji = "🔴" if r["confidence"] == "high" else "🟡"
            confidence_text = "明确变动" if r["confidence"] == "high" else "可能需要变动"

            st.markdown("---")
            st.markdown(
                f"### #{idx + 1}  {confidence_emoji}  "
                f"{r['client']} / {r['style']} — {r['title']}  "
                f"<span style='color:#888;font-size:0.8em'>({confidence_text})</span>",
                unsafe_allow_html=True,
            )

            # ── 信件来源 ─────────────────────────────────
            st.markdown(
                f"📧 **{r['email_subject']}**  \n"
                f"👤 {r['from']}   📅 {r['email_date']}"
            )

            # ── 原信原文片段(灰底引用框) ─────────────
            st.markdown("📝 **客户说了什么**:")
            st.markdown(
                f"<div style='background:#f1f3f4;border-left:4px solid #1a73e8;"
                f"padding:10px 14px;border-radius:4px;color:#444;"
                f"font-style:italic;margin:6px 0;'>"
                f"\"{r['quote']}\"</div>",
                unsafe_allow_html=True,
            )

            # ── 三个按钮 ─────────────────────────────
            b1, b2, b3 = st.columns(3)
            with b1:
                if st.button("✓ 已更新大货表", key=f"upd_{r['msg_id']}",
                             use_container_width=True):
                    set_reminder_state(user, r["msg_id"], "updated")
                    st.toast("✅ 已标记为已更新", icon="✅")
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
                if st.button("✗ 不需要(误判)", key=f"dis_{r['msg_id']}",
                             use_container_width=True):
                    set_reminder_state(user, r["msg_id"], "dismissed")
                    st.toast("已标记为误判,以后不再提醒", icon="🚫")
                    st.rerun()


def render_source_demo():
    """Demo:用假资料展示「来源」栏看起来长怎样,让用户决定要不要做真版。

    展示 3 种来源:🌍 外部、📨 Forward、🏢 内部
    并且 Forward 的信件会显示「原始客户」抽出来当寄件者。
    """
    st.markdown("### 🎨 「來源」欄 — Demo 預覽")
    st.caption(
        "下面是**假資料**示範,展示加上「來源」欄之後 dashboard 會長什麼樣。"
        "看完後你可以決定要不要做真實版本。"
    )

    st.markdown("#### 📋 Demo 1:**只加來源欄**(不抽原客戶)")
    st.caption("Forward 信件的寄件者顯示為內部同事")
    demo1 = pd.DataFrame([
        {
            "來源": "🌍 外部",
            "寄件者": "franky@skyfashion-jx.com",
            "主旨": "Re: SKY 80025 trims approval",
            "等待": "1 天",
        },
        {
            "來源": "📨 Forward",
            "寄件者": "BTL FNS R <fns@ibiney.io>",
            "主旨": "Fwd: WH/W26 - approval and comments",
            "等待": "2 天",
        },
        {
            "來源": "📨 Forward",
            "寄件者": "BTL FNS SKY <jcam@ibiney.io>",
            "主旨": "Re: SKY/W26 - 80028 trims approval",
            "等待": "5 小時",
        },
        {
            "來源": "🏢 內部",
            "寄件者": "BTL Ivy <ivy@ibiney.io>",
            "主旨": "new source of accessories - supplier SAB",
            "等待": "8 小時",
        },
        {
            "來源": "🌍 外部",
            "寄件者": "Patrizia Brück <P.Brueck@fuchsschmitt.de>",
            "主旨": "AW: Hangloops",
            "等待": "1 天",
        },
    ])
    st.dataframe(demo1, use_container_width=True, hide_index=True)
    st.info(
        "👀 **看 Demo 1 的問題**:第 2、3 行的 Forward 信,你只看到 `BTL FNS R / BTL FNS SKY` "
        "(同事名稱),不知道**真正的客戶是誰**(其實是 Skyfashion 的人)。"
    )

    st.divider()

    st.markdown("#### 📋 Demo 2:**抽出原客戶**(推薦版)")
    st.caption("Forward 信件的寄件者顯示為真實客戶,emoji 提示這是 Forward 進來的")
    demo2 = pd.DataFrame([
        {
            "來源": "🌍 外部",
            "寄件者": "franky@skyfashion-jx.com",
            "主旨": "Re: SKY 80025 trims approval",
            "等待": "1 天",
        },
        {
            "來源": "📨 Forward",
            "寄件者": "franky@skyfashion-jx.com  (原)",  # 抽出來
            "主旨": "Fwd: WH/W26 - approval and comments",
            "等待": "2 天",
        },
        {
            "來源": "📨 Forward",
            "寄件者": "tony@skyfashion-jx.com  (原)",
            "主旨": "Re: SKY/W26 - 80028 trims approval",
            "等待": "5 小時",
        },
        {
            "來源": "🏢 內部",
            "寄件者": "BTL Ivy <ivy@ibiney.io>",
            "主旨": "new source of accessories - supplier SAB",
            "等待": "8 小時",
        },
        {
            "來源": "🌍 外部",
            "寄件者": "Patrizia Brück <P.Brueck@fuchsschmitt.de>",
            "主旨": "AW: Hangloops",
            "等待": "1 天",
        },
    ])
    st.dataframe(demo2, use_container_width=True, hide_index=True)
    st.success(
        "✅ **Demo 2 的好處**:第 2、3 行你**直接看到客戶是 Skyfashion 的 franky / tony**,"
        "客戶分類也會自動歸到「Skyfashion」(否則會歸到「OTHER」)。\n\n"
        "「(原)」是個小提示,告訴你這個寄件者是從 Forward 內文裡抽出來的,"
        "不是直接收件。"
    )

    st.divider()

    st.markdown("#### 🎯 兩個版本比較")
    compare = pd.DataFrame([
        ["看到客戶名", "❌ 看不到", "✅ 一眼看到"],
        ["客戶分類準確", "❌ 全歸到 OTHER", "✅ 自動歸到 Skyfashion / Fuchsschmitt"],
        ["要點開信才知道內容", "✅ 是", "❌ 不用"],
        ["實作複雜度", "簡單(10 分鐘)", "中等(30 分鐘)"],
        ["實用度", "🟡 中", "🟢 高"],
    ], columns=["維度", "Demo 1 (簡單)", "Demo 2 (推薦)"])
    st.dataframe(compare, use_container_width=True, hide_index=True)

    st.divider()

    st.markdown("#### 🤔 看完 demo 你的選擇")
    st.markdown("""
看完上面兩個版本,跟 Claude 說一句:

- **「做 Demo 2」** — 完整版,有原客戶抽取
- **「做 Demo 1」** — 簡單版,只加 emoji
- **「都不要,先這樣」** — 維持現狀
- **「我還想改 demo,我覺得 ___」** — 提你想要的調整
    """)


def trace_filter_pipeline(creds_dict, current_user_email, search_days_override=None):
    """逐层追踪过滤流程,记录每封信被哪个规则挡掉。

    回传 dict:
        - universe_threads: 全集 thread 数
        - layers: list of {layer_name, before, after, dropped_examples}
        - final_items: 最后通过的 thread 列表(给 dashboard 用的格式)
        - all_dropped: 所有被挡的信件 + 原因
    """
    days = search_days_override or SEARCH_DAYS
    creds = credentials_from_dict(creds_dict)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    all_dropped = []  # 每个被挡的 thread 都会记一笔

    # ── 第 0 层:Gmail 业务关键字搜寻(已经在 query 端过滤) ──
    # 这层是 Gmail server-side 过滤,我们抓不到「被这层挡掉」的信
    # 但可以另外抓「不带 keyword 过滤」的所有 inbox thread,看差异
    query_with_kw = (
        f"in:inbox newer_than:{days}d "
        f"({' OR '.join([f'subject:{k}' for k in BUSINESS_KEYWORDS])}) "
        f"{' '.join([f'-from:{d}' for d in NOISE_DOMAINS])}"
    )
    query_no_kw = f"in:inbox newer_than:{days}d"

    threads_full = service.users().threads().list(
        userId="me", q=query_no_kw, maxResults=200
    ).execute().get("threads", [])

    threads_filtered = service.users().threads().list(
        userId="me", q=query_with_kw, maxResults=200
    ).execute().get("threads", [])

    filtered_ids = {t["id"] for t in threads_filtered}

    layer_keyword = {
        "name": "1. 主旨业务关键字过滤",
        "rule": f"主旨必须包含: {', '.join(BUSINESS_KEYWORDS)} 之一",
        "before": len(threads_full),
        "after": len(threads_filtered),
        "dropped_examples": [],
    }

    # 抓「被关键字层挡掉」的 thread 实例(全集 - 通过) 取前 10 笔
    dropped_by_kw_ids = [t["id"] for t in threads_full if t["id"] not in filtered_ids][:10]
    for tid in dropped_by_kw_ids:
        try:
            tdata = service.users().threads().get(
                userId="me", id=tid, format="metadata",
                metadataHeaders=["Subject", "From"],
            ).execute()
            msgs = tdata.get("messages", [])
            if msgs:
                last = msgs[-1]
                hd = {h["name"]: h["value"] for h in last.get("payload", {}).get("headers", [])}
                example = {
                    "subject": hd.get("Subject", ""),
                    "from": hd.get("From", ""),
                    "thread_id": tid,
                    "reason": "主旨没有任何业务关键字",
                }
                layer_keyword["dropped_examples"].append(example)
                all_dropped.append(example)
        except Exception:
            pass

    # ── 第 2 层 ~ 第 5 层:在通过 keyword 的 thread 内逐一检查 ──
    layer_external = {
        "name": "2. 必须有外部寄件者",
        "rule": "thread 中至少要有 1 封信不是 @ibiney.io 寄出",
        "before": len(threads_filtered),
        "after": 0, "dropped_examples": [],
    }
    layer_noise = {
        "name": "3. 排除噪音域名",
        "rule": f"最后一封外部信不能来自: {', '.join(NOISE_DOMAINS)}",
        "before": 0, "after": 0, "dropped_examples": [],
    }
    layer_user_replied = {
        "name": "4. B 逻辑:你还没回过",
        "rule": f"最后一封外部信之后, {current_user_email} 不能已回过",
        "before": 0, "after": 0, "dropped_examples": [],
    }

    final_items = []

    for t in threads_filtered:
        try:
            full = service.users().threads().get(
                userId="me", id=t["id"], format="full",
            ).execute()
            messages = full.get("messages", [])
            if not messages:
                continue

            last_subject = ""
            last_from_overall = ""
            if messages:
                hd = {h["name"]: h["value"] for h in messages[-1].get("payload", {}).get("headers", [])}
                last_subject = hd.get("Subject", "")
                last_from_overall = hd.get("From", "")

            # Layer 2: 必须有外部寄件者
            last_ext_idx = -1
            for i in range(len(messages) - 1, -1, -1):
                hd2 = {h["name"]: h["value"] for h in messages[i].get("payload", {}).get("headers", [])}
                em = extract_email(hd2.get("From", ""))
                if em and not is_internal(em):
                    last_ext_idx = i
                    break
            if last_ext_idx == -1:
                ex = {
                    "subject": last_subject, "from": last_from_overall,
                    "thread_id": t["id"], "reason": "整 thread 都是内部信件",
                }
                layer_external["dropped_examples"].append(ex)
                all_dropped.append(ex)
                continue
            layer_external["after"] += 1
            layer_noise["before"] += 1

            # Layer 3: 噪音域名
            last_ext_hd = {h["name"]: h["value"] for h in messages[last_ext_idx].get("payload", {}).get("headers", [])}
            last_ext_from = last_ext_hd.get("From", "")
            last_ext_email = extract_email(last_ext_from)
            if is_noise_domain(last_ext_email):
                ex = {
                    "subject": last_subject, "from": last_ext_from,
                    "thread_id": t["id"], "reason": f"寄件域名是噪音 ({last_ext_email})",
                }
                layer_noise["dropped_examples"].append(ex)
                all_dropped.append(ex)
                continue
            layer_noise["after"] += 1
            layer_user_replied["before"] += 1

            # Layer 4: B 逻辑 - 当前 user 是否已回过
            user_replied = False
            for j in range(last_ext_idx + 1, len(messages)):
                hd3 = {h["name"]: h["value"] for h in messages[j].get("payload", {}).get("headers", [])}
                em = extract_email(hd3.get("From", ""))
                if em == current_user_email.lower():
                    user_replied = True
                    break
            if user_replied:
                ex = {
                    "subject": last_subject, "from": last_ext_from,
                    "thread_id": t["id"],
                    "reason": f"{current_user_email} 在最后一封外部信之后已回过",
                }
                layer_user_replied["dropped_examples"].append(ex)
                all_dropped.append(ex)
                continue
            layer_user_replied["after"] += 1

            # 通过所有层 → final
            final_items.append({
                "subject": last_subject, "from": last_ext_from,
                "thread_id": t["id"], "msg_count": len(messages),
            })
        except Exception as e:
            ex = {
                "subject": "(error)", "from": "",
                "thread_id": t["id"], "reason": f"处理异常: {e}",
            }
            all_dropped.append(ex)

    return {
        "search_days": days,
        "current_user": current_user_email,
        "layers": [layer_keyword, layer_external, layer_noise, layer_user_replied],
        "final_count": len(final_items),
        "final_items": final_items,
        "all_dropped": all_dropped,
    }


def render_filter_trace(user):
    """显示「过滤追踪」面板:逐层显示每层挡掉多少信件,例子。"""
    st.markdown("### 🕵️ 过滤追踪面板")
    st.caption(
        "逐层显示过滤流程。每层挡掉的信件可以展开看具体例子,"
        "判断「这封该擋还是该放过」。"
    )

    # 让用户决定 search range:有时候问题在 3 天太短
    days = st.slider(
        "搜寻范围(天)", min_value=1, max_value=14, value=SEARCH_DAYS,
        help="目前 dashboard 设定 3 天。可以拉长来看更早的信是否被遗漏",
    )

    if st.button("🔍 开始追踪"):
        with st.spinner(f"扫描过去 {days} 天 inbox 中..."):
            try:
                trace = trace_filter_pipeline(user["creds"], user["email"], days)
                st.session_state["_filter_trace_result"] = trace
            except Exception as e:
                st.error(f"追踪失败: {e}")
                return

    if "_filter_trace_result" not in st.session_state:
        return

    trace = st.session_state["_filter_trace_result"]

    # ── 总览 ──
    st.markdown("#### 📊 过滤漏斗")
    layers = trace["layers"]
    funnel_rows = []
    for i, layer in enumerate(layers):
        funnel_rows.append({
            "层级": layer["name"],
            "进入数": layer["before"],
            "通过数": layer["after"],
            "被挡": layer["before"] - layer["after"],
            "通过率": f"{round(100 * layer['after'] / max(layer['before'], 1), 1)}%",
        })
    funnel_rows.append({
        "层级": "✅ Dashboard 最终显示",
        "进入数": layers[-1]["after"],
        "通过数": trace["final_count"],
        "被挡": 0,
        "通过率": "100%",
    })
    st.dataframe(pd.DataFrame(funnel_rows), use_container_width=True, hide_index=True)

    st.divider()

    # ── 每层挡掉的实例 ──
    st.markdown("#### 🔬 每层挡掉的具体例子(展开看)")
    for layer in layers:
        dropped_count = layer["before"] - layer["after"]
        with st.expander(f"{layer['name']} — 挡掉 {dropped_count} 笔"):
            st.caption(f"**规则**:{layer['rule']}")
            if not layer["dropped_examples"]:
                st.info("没有被这层挡掉的信件")
                continue
            for ex in layer["dropped_examples"]:
                st.markdown(f"""
- **{ex.get('subject', '')[:80]}**
  - From: `{ex.get('from', '')}`
  - 原因: {ex.get('reason', '')}
  - Thread: `{ex.get('thread_id', '')}`
""")

    st.divider()

    # ── 通过的最终列表 ──
    st.markdown(f"#### ✅ 最终通过 dashboard 显示的 {trace['final_count']} 笔")
    if trace["final_items"]:
        df = pd.DataFrame(trace["final_items"])
        st.dataframe(df, use_container_width=True, hide_index=True)

    # ── 下载完整 dump ──
    import json as _json
    st.download_button(
        "⬇️ 下載完整追踪 dump (JSON)",
        data=_json.dumps(trace, default=str, indent=2, ensure_ascii=False),
        file_name=f"btl_filter_trace_{user.get('email', '').split('@')[0]}.json",
        mime="application/json",
    )


def render_read_unread_debug(user):
    """Debug 面板:列出每封信的 Gmail labels + 系统判断结果。

    专门用来验证「未读未回 / 已读未回」的正确性。
    显示原始 labelIds、计算结果、是否一致。
    """
    st.markdown("### 🔍 已读/未读 偵錯面板")
    st.caption(
        "列出 dashboard 显示的每封信件,显示 Gmail 端的原始 labels 和系统判断。"
        "对比可以看出系统是否准确反映 Gmail 状态。"
    )

    items = st.session_state.get("pending_items", [])
    if not items:
        st.warning("dashboard 还没抓信,请等抓完再来")
        return

    # 总览
    unread_count = sum(1 for it in items if it.get("is_unread"))
    read_count = len(items) - unread_count
    c1, c2, c3 = st.columns(3)
    c1.metric("总信件数", len(items))
    c2.metric("🔴 未读未回", unread_count)
    c3.metric("🟡 已读未回", read_count)

    st.divider()

    # 每封信的 debug 资讯
    rows = []
    for idx, it in enumerate(items, start=1):
        labels = it.get("_debug_last_external_labels", [])
        labels_str = ", ".join(labels) if labels else "(no labels)"
        is_unread = it.get("is_unread", False)
        has_unread_label = "UNREAD" in labels

        # 一致性检查
        if is_unread == has_unread_label:
            consistency = "✅ 一致"
        else:
            consistency = "❌ 不一致"

        rows.append({
            "#": idx,
            "Subject": it["subject"][:50],
            "is_unread (系统判断)": "🔴 未读" if is_unread else "🟡 已读",
            "UNREAD label (Gmail 原始)": "✅" if has_unread_label else "❌",
            "判断一致性": consistency,
            "Thread 长度": it.get("_debug_msg_count", 0),
            "完整 labels": labels_str,
        })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.markdown("#### 🧐 解读结果")
    st.markdown(
        """
- **一致**:系统判断 = Gmail 标签 → 正确反映 Gmail 状态
- **不一致**:可能是 Gmail labels 之外的因素影响判断 → 需要查
- 如果觉得「系统说未读但其实我已读过」 → 看「UNREAD label」栏:
  - 仍是 ✅ → Gmail 那封信真的还有 UNREAD 标签(可能 mobile preview 不算 open)
  - 是 ❌ → 那是系统 bug,要 debug
- 如果觉得「系统说已读但我从没打开」 → 看「UNREAD label」栏:
  - 是 ❌ → Gmail 那封信被标 read 了(有人打开过 / API fetch 自动标了)
  - 是 ✅ → 系统 bug
        """
    )

    # 可以下载完整 JSON 给 dev 看
    import json as _json
    full_dump = []
    for it in items:
        full_dump.append({k: v for k, v in it.items() if not k.startswith("body") and not k.startswith("thread_text")})
    st.download_button(
        "⬇️ 下載完整 debug dump (JSON)",
        data=_json.dumps(full_dump, default=str, indent=2, ensure_ascii=False),
        file_name=f"btl_debug_dump_{user.get('email', 'user').split('@')[0]}.json",
        mime="application/json",
    )


def show_main_dashboard():
    user = st.session_state["user"]
    user_email = user["email"]
    user_name = user.get("name") or user_email
    user_first_name = user_name.split()[0] if user_name else "Me"

    title_col, user_col = st.columns([4, 1])
    with title_col:
        st.title("📧 BTL Email Monitor")
        st.caption(f"已登入:{user_name} ({user_email}) / 显示你 Gmail 中过去 {SEARCH_DAYS} 天的待回客户信件")
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

    # 开发/PRD 用工具(收在 expander 里,不影响日常使用)
    with st.sidebar.expander("🧪 开发工具"):
        if st.button("🎨 「來源」欄 demo 預覽"):
            st.session_state["_source_demo"] = True
        if st.button("🕵️ 过滤追踪(看哪些信被挡掉)"):
            st.session_state["_filter_trace"] = True
            st.session_state.pop("_filter_trace_result", None)
        if st.button("🔍 已读/未读 偵錯"):
            st.session_state["_read_unread_debug"] = True
        if st.button("📋 Quality Report(系统验证)"):
            st.session_state["_quality_check"] = True
            st.session_state.pop("_quality_report", None)  # 重新跑
        if st.button("Phase 6 Firestore 写入测试"):
            st.session_state["_phase6_probe"] = True
        if st.button("📊 附件分析(PRD 用)"):
            st.session_state["_attachment_analysis"] = True
            st.session_state.pop("_attachment_stats", None)  # 重新分析

    if st.session_state.get("_source_demo"):
        render_source_demo()
        if st.button("关闭 demo 预览"):
            st.session_state.pop("_source_demo", None)
            st.rerun()
        st.divider()

    if st.session_state.get("_filter_trace"):
        render_filter_trace(user)
        if st.button("关闭过滤追踪"):
            st.session_state.pop("_filter_trace", None)
            st.session_state.pop("_filter_trace_result", None)
            st.rerun()
        st.divider()

    if st.session_state.get("_read_unread_debug"):
        render_read_unread_debug(user)
        if st.button("关闭偵錯面板"):
            st.session_state.pop("_read_unread_debug", None)
            st.rerun()
        st.divider()

    if st.session_state.get("_quality_check"):
        render_quality_report(user)
        if st.button("关闭 Quality Report"):
            st.session_state.pop("_quality_check", None)
            st.session_state.pop("_quality_report", None)
            st.rerun()
        st.divider()

    if st.session_state.get("_phase6_probe"):
        phase6_firestore_probe(user)
        if st.button("关闭验证面板"):
            st.session_state.pop("_phase6_probe", None)
            st.rerun()
        st.divider()

    if st.session_state.get("_attachment_analysis"):
        render_attachment_analysis(user)
        if st.button("关闭附件分析面板"):
            st.session_state.pop("_attachment_analysis", None)
            st.session_state.pop("_attachment_stats", None)
            st.rerun()
        st.divider()

    # 大货表 Excel 更新提醒(Phase 1:demo 资料,Phase 2 接 AI)
    render_excel_update_panel(user)

    if "pending_items" not in st.session_state:
        with st.spinner("📬 正在从你的 Gmail 抓取待回信件(30-90 秒,只发生一次)..."):
            try:
                st.session_state["pending_items"] = fetch_pending_emails(
                    user["creds"], user_email
                )
            except Exception as e:
                st.error(f"抓取 Gmail 失败:{e}")
                st.info("可能是 token 过期,请登出重新登入。")
                st.stop()
    items = st.session_state["pending_items"]

    # 从 Firestore 载入这个 user 之前所有的编辑,merge 进 session_state
    # (只在第一次进 dashboard 时跑一次,失败也不挡使用)
    if not st.session_state.get("_firestore_edits_loaded"):
        cloud_edits = load_edits_from_firestore(user)
        for edit_msg_id, edit_data in cloud_edits.items():
            saved_key = f"_saved_{edit_msg_id}"
            # 云端有资料但 session 还没载入 → 从云端带回来
            if saved_key not in st.session_state:
                st.session_state[saved_key] = {
                    "title": edit_data.get("title", ""),
                    "summary": edit_data.get("summary", ""),
                    "actions": edit_data.get("actions", ""),
                }
        st.session_state["_firestore_edits_loaded"] = True
        if cloud_edits:
            st.toast(f"☁️ 已从云端载入 {len(cloud_edits)} 笔之前的编辑", icon="✅")

    if not items:
        st.success("🎉 你的 Gmail 中目前没有待回客户信件,辛苦了!")
        return

    if "summary_cache_for_table" not in st.session_state:
        with st.spinner(f"🤖 AI 正在整理 {len(items)} 封信..."):
            st.session_state["summary_cache_for_table"] = precompute_summaries(items)
    summary_cache = st.session_state["summary_cache_for_table"]

    # 分组计算每封信的最终显示标题
    grouped_titles, topic_keys, key_counts = compute_grouped_titles(items, summary_cache)

    # 统计分组情况
    grouped_count = sum(1 for it in items
                        if topic_keys[it["msg_id"]]
                        and key_counts[topic_keys[it["msg_id"]]] >= 2)

    rows = []
    for it in items:
        badges = ["🔴 未读未回" if it["is_unread"] else "🟡 已读未回"]
        if it["is_today"]:
            badges.append("🔵 当日新进")
        rows.append({
            "msg_id": it["msg_id"],
            "优先级": " / ".join(badges),
            "寄件者": it["from"],
            "标题": grouped_titles[it["msg_id"]],
            "收信日期": it["date"].strftime("%Y-%m-%d %H:%M"),
            "等待时长": format_age(it["age_hours"]),
            "邮件连结": f"https://mail.google.com/mail/u/0/#inbox/{it['msg_id']}",
            "_body": it["body"],
            "_item": it,
        })
    df = pd.DataFrame(rows)
    df["部门"] = df["寄件者"].apply(get_department)
    df["客户"] = df["寄件者"].apply(get_client)

    total = len(df)
    unread_cnt = int(df["优先级"].str.contains("未读未回", na=False).sum())
    read_cnt = int(df["优先级"].str.contains("已读未回", na=False).sum())
    today_cnt = int(df["优先级"].str.contains("当日新进", na=False).sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📨 待处理总数", total)
    c2.metric("🔴 未读未回", unread_cnt)
    c3.metric("🟡 已读未回", read_cnt)
    c4.metric("🔵 当日新进", today_cnt)

    if unread_cnt > 0:
        st.warning(f"⚠️ 有 **{unread_cnt}** 封还没打开过,建议优先处理")

    st.markdown("##### 🏢 快速依客户筛选")
    client_counts = df["客户"].value_counts().to_dict()
    client_options = [f"全部 ({total})"] + [
        f"{c} ({n})" for c, n in sorted(client_counts.items(), key=lambda x: -x[1])
    ]
    selected_client_label = st.pills(
        "客户", client_options, default=client_options[0],
        label_visibility="collapsed",
    )
    selected_client = None
    if selected_client_label and not selected_client_label.startswith("全部"):
        selected_client = selected_client_label.rsplit(" (", 1)[0]

    st.divider()

    fc1, fc2, fc3 = st.columns([1, 1, 2])
    with fc1:
        tag_count_map = {
            "🔴 未读未回": unread_cnt, "🟡 已读未回": read_cnt, "🔵 当日新进": today_cnt,
        }
        tag_opts = [f"{t} ({tag_count_map[t]})" for t in ["🔴 未读未回", "🟡 已读未回", "🔵 当日新进"]]
        show_tags_l = st.multiselect("状态(选填)", tag_opts, placeholder="不勾 = 全部")
    with fc2:
        dept_count_map = df["部门"].value_counts().to_dict()
        dept_opts = [f"{d} ({dept_count_map.get(d, 0)})" for d in ALL_DEPARTMENTS]
        show_depts_l = st.multiselect("部门(选填)", dept_opts, placeholder="不勾 = 全部")
    with fc3:
        keyword = st.text_input("标题 / 寄件者搜寻(选填)")

    show_tags = [t.rsplit(" (", 1)[0] for t in show_tags_l]
    show_depts = [d.rsplit(" (", 1)[0] for d in show_depts_l]

    view_df = df.copy()
    if show_tags:
        pat = "|".join([t.split(" ")[1] for t in show_tags])
        view_df = view_df[view_df["优先级"].str.contains(pat, na=False)]
    if show_depts:
        view_df = view_df[view_df["部门"].isin(show_depts)]
    if selected_client:
        view_df = view_df[view_df["客户"] == selected_client]
    if keyword:
        kw = keyword.lower()
        view_df = view_df[
            view_df["标题"].astype(str).str.lower().str.contains(kw, na=False)
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
    for i, d in enumerate(view_df["部门"]):
        deduped_depts.append("" if deduped[i] == "" else d)
    view_df["部门"] = deduped_depts

    st.subheader(f"📋 待处理清单  ({len(view_df)} 笔)")
    st.caption("💡 同主题多封信会共用标题;单封信保留原主旨")

    event = st.dataframe(
        view_df,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_order=["寄件者", "部门", "优先级", "标题", "收信日期", "等待时长", "邮件连结"],
        column_config={
            "寄件者": st.column_config.TextColumn(width="medium"),
            "部门": st.column_config.TextColumn(width="medium"),
            "优先级": st.column_config.TextColumn(width="medium"),
            "标题": st.column_config.TextColumn(width="large"),
            "收信日期": st.column_config.TextColumn(width="small"),
            "等待时长": st.column_config.TextColumn(width="small"),
            "邮件连结": st.column_config.LinkColumn(
                "📧 开启", display_text="🔗 点此打开", width="small",
            ),
            "msg_id": None, "客户": None, "_body": None, "_item": None,
        },
    )

    selected_rows = event.selection.rows if event and event.selection else []
    if selected_rows:
        idx = selected_rows[0]
        item = view_df.iloc[idx]["_item"]
        # 点开时要把该信的最终标题传进去
        display_title_for_detail = grouped_titles[item["msg_id"]]
        render_email_detail(item, user_name, user_first_name, summary_cache, display_title_for_detail)

    st.caption(f"页面载入时间:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def main():
    handle_oauth_callback()
    # 没 user 时,先试从 URL 还原(支援浏览器重新整理免重新登入)
    if "user" not in st.session_state:
        restore_session_from_url()
    if "user" not in st.session_state:
        show_login_page()
    else:
        show_main_dashboard()


if __name__ == "__main__":
    main()
