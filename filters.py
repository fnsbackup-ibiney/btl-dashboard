# filters.py
# 用途:BTL Email Classifier 的純邏輯模組
# 設計原則:資料來源(Excel/CSV/IMAP)無關,只接受 DataFrame 進、DataFrame 出
# 未來接 IMAP 時,只需把 IMAP 結果轉成相同欄位的 DataFrame,再呼叫 process_emails() 即可

from __future__ import annotations
import re
from datetime import datetime
from typing import Iterable
import pandas as pd

# ── 設定區(集中管理規則,方便調整) ──────────────────────────────

# 雜訊網域:寄件者欄位若包含這些字串(不分大小寫)即排除
NOISE_DOMAINS: tuple[str, ...] = (
    "blot.new",
    "bolt.new",      # blot.new 常見拼錯
    "cloudhq.net",
    "cloudhq",
    "bolt.eu",
)

# 業務關鍵字:主旨包含其中之一視為「業務信」
BUSINESS_KEYWORDS: tuple[str, ...] = (
    "SKY", "FNS", "BTL", "WH", "FCL",
    "Sendung", "Parcel", "Order",
)

# 行銷話術(主旨層級語義排除)
MARKETING_PHRASES: tuple[str, ...] = (
    "save you hours",
    "available now",
    "now available",
    "things that",      # 例:"3 things that'll save you hours"
    "tips",
    "newsletter",
    "unsubscribe",
)

# 系統備份寄件者(無需人工回覆)
BACKUP_SENDER = "fnsbackup@ibiney.io"


# ── 共用工具 ─────────────────────────────────────────────────

def _safe_lower(value) -> str:
    """安全轉小寫,避免 NaN/None 出錯。"""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).lower()


# ── 過濾判斷函式 ─────────────────────────────────────────────

def is_backup(sender: str) -> bool:
    """是否為系統備份信。"""
    return _safe_lower(sender).strip() == BACKUP_SENDER.lower()


def is_noise_domain(sender: str) -> bool:
    """寄件者是否屬於雜訊網域。"""
    s = _safe_lower(sender)
    return any(dom in s for dom in NOISE_DOMAINS)


def is_marketing(subject: str) -> bool:
    """主旨是否為行銷話術(語義排除,即使寄件者像真人)。"""
    s = _safe_lower(subject)
    return any(phrase in s for phrase in MARKETING_PHRASES)


def has_business_keyword(subject: str) -> bool:
    """主旨是否含業務關鍵字(以單字邊界比對,避免誤判)。"""
    if not subject:
        return False
    text = str(subject)
    for kw in BUSINESS_KEYWORDS:
        # 用 \b 確保整字匹配,例如 "WH" 不會被 "WHEN" 命中
        if re.search(rf"\b{re.escape(kw)}\b", text, flags=re.IGNORECASE):
            return True
    return False


# ── 主處理流程 ───────────────────────────────────────────────

def process_emails(df: pd.DataFrame, today: datetime | None = None) -> pd.DataFrame:
    """
    對郵件 DataFrame 套用過濾與分類邏輯。

    必要欄位(大小寫不敏感,會自動標準化):Sender, Subject, Date

    產出欄位:
        Sender, Subject, Date, WaitDays, Priority, PriorityRank
    """
    if today is None:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    if df is None or df.empty:
        return pd.DataFrame(
            columns=["Sender", "Subject", "Date", "WaitDays", "Priority", "PriorityRank"]
        )

    # 欄位名標準化(忽略大小寫/前後空白)
    rename_map = {}
    for col in df.columns:
        key = str(col).strip().lower()
        if key == "sender":
            rename_map[col] = "Sender"
        elif key == "subject":
            rename_map[col] = "Subject"
        elif key == "date":
            rename_map[col] = "Date"
    df = df.rename(columns=rename_map).copy()

    for required in ("Sender", "Subject", "Date"):
        if required not in df.columns:
            raise ValueError(f"缺少必要欄位:{required}")

    # 解析日期(支援 Excel 序號/字串/datetime)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")

    # 過濾(備份信 / 雜訊網域 / 行銷話術)
    mask_keep = ~(
        df["Sender"].apply(is_backup)
        | df["Sender"].apply(is_noise_domain)
        | df["Subject"].apply(is_marketing)
    )
    df = df[mask_keep].copy()

    # 計算等待天數
    df["WaitDays"] = (today - df["Date"]).dt.days

    # 業務關鍵字旗標
    df["_HasBizKw"] = df["Subject"].apply(has_business_keyword)

    # 標註優先級
    def _priority(row) -> str:
        wait = row["WaitDays"]
        has_kw = row["_HasBizKw"]
        if pd.isna(wait):
            return "⚪️ PENDING"
        if wait >= 1 and has_kw:
            return "🔴 CRITICAL"
        if wait == 0 and has_kw:
            return "🟡 NEW"
        return "⚪️ PENDING"

    df["Priority"] = df.apply(_priority, axis=1)

    # 排序輔助欄(CRITICAL=0, NEW=1, PENDING=2)
    rank_map = {"🔴 CRITICAL": 0, "🟡 NEW": 1, "⚪️ PENDING": 2}
    df["PriorityRank"] = df["Priority"].map(rank_map).fillna(3).astype(int)

    df = df.drop(columns=["_HasBizKw"])
    df = df.sort_values(
        by=["PriorityRank", "WaitDays"], ascending=[True, False]
    ).reset_index(drop=True)

    return df


# ── 資料來源轉接器(未來 IMAP 替換點) ──────────────────────────

def load_from_upload(uploaded_file) -> pd.DataFrame:
    """從 Streamlit 上傳元件讀取 Excel / CSV。"""
    name = (uploaded_file.name or "").lower()
    if name.endswith(".csv"):
        return pd.read_csv(uploaded_file)
    return pd.read_excel(uploaded_file)


def load_from_imap(*args, **kwargs) -> pd.DataFrame:
    """
    未來 IMAP 接通後實作此函式。
    回傳 DataFrame,欄位需有:Sender, Subject, Date(datetime)。
    主程式(app.py)只要切換成呼叫此函式,後續流程不需更動。
    """
    raise NotImplementedError("IMAP 尚未開通,目前請使用上傳模式。")
