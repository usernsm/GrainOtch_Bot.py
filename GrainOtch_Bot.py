#!/usr/bin/env python3
"""GrainOtch Auto-Registration Bot — Single File"""

# ── Sasta OTP API (inline) ──
import re
import time
import logging
import requests

logger = logging.getLogger(__name__)

OTP_API_BASE_URL = "https://sastaotp.com/stubs/handler_api.php"


class OTPDoctorAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        # Sasta OTP (LiteSpeed/Cloudflare) bina browser User-Agent ke 403 Forbidden deta hai.
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        })
        self._service_cache: dict = {}

    def _get(self, params: dict, timeout: int = 20, retries: int = 3) -> str:
        # sastaotp.com Replit se flaky ho sakta hai — connect-timeout aur 429
        # (Too Many Requests) dono deta hai. Network error pe backoff se retry karo;
        # 429 (rate limit) pe zyada ruko warna rate-limit aur bigadta hai.
        params = {"api_key": self.api_key, **params}
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                r = self.session.get(OTP_API_BASE_URL, params=params, timeout=timeout)
                if r.status_code == 429:
                    raise requests.exceptions.HTTPError("429 Too Many Requests", response=r)
                r.raise_for_status()
                return r.text.strip()
            except requests.exceptions.RequestException as e:
                last_exc = e
                resp = getattr(e, "response", None)
                is_429 = resp is not None and resp.status_code == 429
                logger.warning("_get network error (action=%s, try %d/%d)%s: %s",
                               params.get("action"), attempt, retries,
                               " [429 rate-limit]" if is_429 else "", str(e)[:80])
                if attempt < retries:
                    # 429 pe lamba ruko (5,10,15s), warna chhota backoff (2,4s)
                    time.sleep(5 * attempt if is_429 else 2 * attempt)
        raise last_exc if last_exc else Exception("_get failed")

    def get_balance(self) -> float:
        resp = self._get({"action": "getBalance"})
        if resp.startswith("ACCESS_BALANCE:"):
            return float(resp.split(":")[1])
        raise Exception(f"Balance error: {resp}")

    def get_services(self) -> dict:
        import json as _json

        # Sasta OTP getServices JSON deta hai:
        # {"status":"OK","total":N,"services":{code:{code,name,price,available,multi_sms,...}}}
        params = {"action": "getServices", "format": "json"}

        attempts = 6
        for attempt in range(attempts):
            try:
                resp = self._get(params, timeout=30)
                if resp.startswith("{"):
                    data = _json.loads(resp)
                    services = data.get("services") if isinstance(data, dict) else None
                    if isinstance(services, dict) and services:
                        self._service_cache = services
                        logger.info("getServices OK: %d services (attempt %d)", len(services), attempt + 1)
                        return services
                logger.warning("getServices bad response (attempt %d): %s", attempt + 1, resp[:60])
            except Exception as e:
                logger.warning("getServices error (attempt %d): %s", attempt + 1, e)
            time.sleep(3)

        logger.error("getServices failed all attempts — returning cached (%d items)", len(self._service_cache))
        return self._service_cache

    def find_service_id(self, service_keyword: str) -> str | None:
        # Sasta OTP me service "id" ek string CODE hai (e.g. "grainotch_1_multi").
        # name ya code me keyword match karke woh code lautao.
        services = self.get_services()
        if not services:
            return None

        kw = service_keyword.lower()
        for code, info in services.items():
            name = (info.get("name", "") if isinstance(info, dict) else "").lower()
            if kw in str(code).lower() or kw in name:
                return str(code)

        return None

    def get_number(self, service_id: str, country: str = "91") -> dict:
        # getNumber NON-IDEMPOTENT hai (number kharidta hai). Timeout pe retry karne se
        # duplicate number ban sakta hai (balance waste + orphaned). Isliye retries=1.
        # (Number na mile to upar wala buy-retry loop nayi call karta hai — woh safe hai.)
        resp = self._get({
            "action": "getNumber",
            "service": service_id,
            "country": country,
        }, retries=1)
        if resp.startswith("ACCESS_NUMBER:"):
            parts = resp.split(":")
            return {"id": parts[1], "phone": parts[2]}
        raise Exception(f"getNumber error: {resp}")

    def get_status(self, activation_id: str) -> dict:
        resp = self._get({"action": "getStatus", "id": activation_id})
        if resp == "STATUS_WAIT_CODE":
            return {"status": "waiting", "text": None}
        elif resp == "STATUS_CANCEL":
            return {"status": "cancelled", "text": None}
        elif resp.startswith("STATUS_OK_AND_INFORMING:"):
            return {"status": "ok", "text": resp.split(":", 1)[1]}
        elif resp.startswith("STATUS_OK:"):
            return {"status": "ok", "text": resp.split(":", 1)[1]}
        elif resp.startswith("STATUS_WAIT_RETRY"):
            # status=3 ke baad agle SMS ka wait (lastcode bhi aata hai) — abhi waiting hi hai
            return {"status": "waiting_resend", "text": None}
        elif resp == "STATUS_WAIT_RESEND":
            return {"status": "waiting_resend", "text": None}
        return {"status": "unknown", "raw": resp, "text": None}

    def get_status_v2(self, activation_id: str) -> dict:
        # getStatusV2 = dashboard ke "number history" ka programmatic equivalent.
        # getStatus sirf parsed OTP code deta hai, par getStatusV2 PURA SMS text deta
        # hai — isi se 2nd SMS (Amazon voucher) ka full text nikalta hai. Return:
        # {"status": ok|waiting|cancelled|unknown, "texts": [full sms texts], "text": last}
        import json as _json
        resp = self._get({"action": "getStatusV2", "id": activation_id, "format": "json"})
        texts: list[str] = []

        # Kabhi-kabhi plain STATUS_* bhi aa sakta hai — handle karo.
        if resp.startswith("STATUS_WAIT_CODE") or resp.startswith("STATUS_WAIT_RETRY") \
                or resp == "STATUS_WAIT_RESEND":
            return {"status": "waiting", "texts": [], "text": None}
        if resp == "STATUS_CANCEL":
            return {"status": "cancelled", "texts": [], "text": None}
        if resp.startswith("STATUS_OK"):
            if ":" in resp:
                t = resp.split(":", 1)[1].strip()
                if t:
                    texts.append(t)
            return {"status": "ok", "texts": texts, "text": texts[-1] if texts else None}

        if resp.startswith("{") or resp.startswith("["):
            try:
                data = _json.loads(resp)
            except Exception:
                return {"status": "unknown", "texts": [], "text": None, "raw": resp}

            def _collect(obj):
                if isinstance(obj, dict):
                    t = obj.get("text") or obj.get("fullText") or obj.get("message")
                    if t:
                        texts.append(str(t).strip())
                elif isinstance(obj, list):
                    for o in obj:
                        _collect(o)
                elif isinstance(obj, str) and obj.strip():
                    texts.append(obj.strip())

            container = data if isinstance(data, dict) else {"sms": data}
            _collect(container.get("sms"))
            top = container.get("text")
            if top and str(top).strip() not in texts:
                texts.append(str(top).strip())

            if texts:
                return {"status": "ok", "texts": texts, "text": texts[-1]}

            st = str(container.get("status") or container.get("error") or "").upper()
            if "WAIT" in st:
                return {"status": "waiting", "texts": [], "text": None}
            if "CANCEL" in st:
                return {"status": "cancelled", "texts": [], "text": None}
            return {"status": "unknown", "texts": [], "text": None, "raw": resp}

        return {"status": "unknown", "texts": [], "text": None, "raw": resp}

    def set_status(self, activation_id: str, status: int) -> str:
        # Sasta OTP: status=1 received, status=3 = isi number pe agla SMS maango (FREE,
        # multi_sms ke liye), status=6 = CANCEL. status=6 is bot me KABHI nahi bheja jaata.
        return self._get({"action": "setStatus", "id": activation_id, "status": status})

    def wait_for_sms(self, activation_id: str, max_wait: int = 120,
                     poll_interval: int = 5) -> str | None:
        # Real wall-clock timeout (network retries time ko inflate na karein).
        start = time.monotonic()
        while time.monotonic() - start < max_wait:
            try:
                result = self.get_status(activation_id)
                if result["status"] == "ok":
                    return result["text"]
                elif result["status"] == "cancelled":
                    logger.warning("Activation %s cancelled", activation_id)
                    return None
            except Exception as e:
                logger.warning("get_status error: %s", e)
            time.sleep(poll_interval)
        return None

    def wait_for_second_sms(self, activation_id: str, max_wait: int = 1200,
                             poll_interval: int = 10, prev_text: str | None = None) -> str | None:
        """
        NOTE: yeh sync version ab use NAHI hota — live path async `_await_voucher_sms` hai
        (Sasta OTP multi_sms ke liye setStatus(3) = "agla SMS maango" bhejta hai, jo FREE
        hai aur number CANCEL nahi karta; cancel sirf status=6 hota — woh kabhi nahi bhejte).
        Kuch bhi CANCEL nahi karta (user manually karega).
        """
        start = time.monotonic()
        seen: set[str] = set()
        if prev_text:
            seen.add(prev_text.strip())  # 1st SMS (OTP) ko voucher mat samjho

        # Real wall-clock timeout — network retries "20 min" ko inflate na karein.
        while time.monotonic() - start < max_wait:
            try:
                result = self.get_status(activation_id)
                if result["status"] == "ok" and result.get("text"):
                    text = result["text"].strip()
                    if text not in seen:
                        seen.add(text)
                        logger.info("Voucher-wait SMS aaya: %s", text)
                        if extract_voucher(text):
                            return text  # asli Amazon voucher SMS mil gaya
                        # voucher nahi (shayad OTP dobara aaya) — check SMS karte raho
                        logger.info("SMS me voucher nahi (OTP dobara?) — check SMS karte raho")
                elif result["status"] == "cancelled":
                    logger.warning("Activation %s cancelled during 2nd SMS wait", activation_id)
                    return None
                # waiting / waiting_resend / unknown → bas getStatus poll karte raho
            except Exception as e:
                logger.warning("get_status error: %s", e)

            time.sleep(poll_interval)

        return None


def extract_otp(sms_text: str) -> str | None:
    m = re.search(r'\b(\d{6})\b', sms_text)
    if m:
        return m.group(1)
    m = re.search(r'\b(\d{4})\b', sms_text)
    if m:
        return m.group(1)
    m = re.search(r'\b(\d{8})\b', sms_text)
    if m:
        return m.group(1)
    return None


# Common words jo galti se "code" jaise dikhte hain — fallback me inhe code mat samjho.
_VOUCHER_STOPWORDS = {
    "CONGRATULATIONS", "CONGRATS", "ABHINANDAN", "AMAZON", "VOUCHER", "PASSION",
    "GRAINOTCH", "THEOFFERCLUB", "OFFERCLUB", "PROMO", "REDEEM", "VALID", "FUEL",
    "YOUR", "GIFT", "CARD", "WINNER", "RUPEES", "TERMS", "CONDITIONS", "APPLY",
}


def _voucher_ok(c: str | None) -> bool:
    return bool(c) and c not in _VOUCHER_STOPWORDS


def extract_voucher(sms_text: str) -> str | None:
    # Uppercase the ASCII part for matching (keeps Marathi chars safe)
    sms_upper = sms_text.upper()

    # ── 1. Marathi/Hindi SMS: "CODE हा कोड वापरा" / "CODE कोड वापरा" / "CODE वापरा" ──
    m = re.search(r'([A-Z0-9]{6,20})\s*(?:हा\s*कोड|कोड\s*वापरा|वापरा|HA\s*KOD)', sms_upper)
    if m and _voucher_ok(m.group(1)):
        return m.group(1)

    # ── 2. Amazon standard gift card: XXXX-XXXXXX-XXXX ──
    m = re.search(r'([A-Z0-9]{4}-[A-Z0-9]{4,6}-[A-Z0-9]{4}(?:-[A-Z0-9]{4})?)', sms_upper)
    if m:
        return m.group(1)

    # ── 3. Keyword-prefixed codes ("use code X", "code X to redeem", etc.) ──
    keyword_patterns = [
        r'(?:USE\s*(?:THIS\s*)?CODE|CODE\s*IS)[:\s#]*([A-Z0-9]{6,20})',
        r'([A-Z0-9]{6,20})\s*(?:TO\s*REDEEM|CODE\s*VAPRA)',
        r'(?:AMAZON\s*(?:GIFT\s*CARD|VOUCHER|CODE|GC))[:\s#]*([A-Z0-9]{6,}(?:-[A-Z0-9]{4,})*)',
        r'(?:VOUCHER|GIFT\s*CARD|GIFT\s*CODE|CLAIM\s*CODE|REDEEM)[:\s#]+([A-Z0-9]{6,}(?:-[A-Z0-9]{4,})*)',
        r'CODE\s*[:\s]+([A-Z0-9]{8,})',
    ]
    for pattern in keyword_patterns:
        m = re.search(pattern, sms_upper)
        if m and _voucher_ok(m.group(1)):
            return m.group(1)

    # ── 4. Long alphanumeric block 12-20 chars — digit zaroori, stopword nahi ──
    for c in re.findall(r'(?<![A-Z0-9])([A-Z0-9]{12,20})(?![A-Z0-9])', sms_upper):
        if re.search(r'\d', c) and _voucher_ok(c):
            return c

    # ── 5. Fallback: 8-13 char alnum — digit zaroori, stopword nahi ──
    for c in re.findall(r'(?<![A-Z0-9])([A-Z0-9]{8,13})(?![A-Z0-9])', sms_upper):
        if re.search(r'\d', c) and _voucher_ok(c):
            return c

    return None

# ── Main Bot ──
import os
import re
import io
import csv
import json
import random
import asyncio
import logging
from pathlib import Path
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from telegram import Update, ReplyKeyboardRemove, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

CODES = 0

INDIAN_MALE_NAMES = [
    "Aarav", "Aditya", "Akash", "Anand", "Ankit", "Arjun", "Arnav", "Ashish",
    "Ayaan", "Ayush", "Bhuvan", "Chirag", "Daksh", "Deepak", "Dev", "Dhruv",
    "Farhan", "Gaurav", "Harsh", "Himanshu", "Ishan", "Jai", "Jayesh", "Kabir",
    "Karan", "Kartik", "Krish", "Kunal", "Lakshya", "Manav", "Manish", "Mayank",
    "Mihir", "Mohit", "Nakul", "Neel", "Nikhil", "Nilesh", "Nishant", "Om",
    "Pankaj", "Parth", "Pranav", "Prashant", "Prateek", "Praveen", "Pulkit",
    "Rahul", "Raj", "Rajat", "Rajesh", "Rakesh", "Raman", "Ramesh", "Raunak",
    "Ravi", "Rishabh", "Ritesh", "Rohan", "Rohit", "Sachin", "Sahil", "Saksham",
    "Samir", "Sanjay", "Saurabh", "Shantanu", "Shivam", "Shubham", "Siddharth",
    "Soham", "Sudhir", "Sumit", "Suraj", "Suyash", "Tanmay", "Tarun", "Tushar",
    "Uday", "Vaibhav", "Vijay", "Vikas", "Vikram", "Vinay", "Viraj", "Vishal",
    "Vivek", "Yash", "Yuvraj", "Zaid", "Aakash", "Abhishek", "Amar", "Amitabh",
    "Aniket", "Anubhav", "Ashwin", "Atharv",
]

CITIES = [
    "Ahilyanagar", "Akola", "Amaravati", "Beed", "Bhandara", "Buldhana",
    "Chandrapur", "Chh Sambhajinagar", "Dhule", "Gondia", "Hingoi", "Jalgaon",
    "Jalna", "Kolhapur", "Latur", "Mumbai", "Nagpur", "Nanded", "Nandurbar",
    "Nashik", "Osmanabad", "Parbhani", "Pune", "Raigad", "Ratnagiri", "Sangli",
    "Satara", "Sindhudurg", "Solapur", "Thane", "Washim", "Yavatmal",
]

BASE_URL = "https://grainotch.theofferclub.in"
DB_FILE  = Path(__file__).parent / "codes_db.json"
_db_lock: asyncio.Lock | None = None

def _get_db_lock() -> asyncio.Lock:
    global _db_lock
    if _db_lock is None:
        _db_lock = asyncio.Lock()
    return _db_lock

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE_URL}/home/register",
}

_otp_doctor: OTPDoctorAPI | None = None
_grainotch_service_id: str | None = None
_batch_size: int = 5  # default: 5 codes ek saath

ALLOWED_BATCH_SIZES = [1, 5, 10, 20, 30]

SERVICE_KEYWORD = "grainotch"

# Sasta OTP par GrainOtch ka service code (multi_sms=true). getServices fail ho to yahi use hota hai.
GRAINOTCH_SERVICE_ID_FALLBACK = "grainotch_1_multi"

# Number na mile (NO_NUMBERS) to itni baar dobara buy try hoga, har NUMBER_RETRY_INTERVAL sec pe
NUMBER_BUY_MAX_ATTEMPTS = int(os.environ.get("NUMBER_BUY_MAX_ATTEMPTS", "30"))
NUMBER_RETRY_INTERVAL   = int(os.environ.get("NUMBER_RETRY_INTERVAL", "2"))
# 1st SMS (OTP) ka max wait (seconds) — itne me OTP na aaye to naya number (SAME code) lega
SMS1_WAIT = int(os.environ.get("SMS1_WAIT", "120"))
# 1st OTP na aaye to SAME unique code se itni baar naya number try hoga (purana CANCEL nahi).
# Default 1 = ek number ke baad naya number request NAHI hoga (user preference).
OTP_NEW_NUMBER_ATTEMPTS = int(os.environ.get("OTP_NEW_NUMBER_ATTEMPTS", "1"))
# /resend: usi (existing) activation pe 2nd-SMS voucher dobara maangne ka max wait (sec)
RESEND_WAIT = int(os.environ.get("RESEND_WAIT", "600"))

# ── Processing stop/cancel control ──
# /cancel chalu processing ko rok deta hai. Loops ye flag check karke break karte hain.
# (Numbers/activations CANCEL/FINISH NAHI hote — sirf loop ruk ke return karta hai.)
_stop_flag = False
_processing = False

# Manual OTP fallback: agar website se 1st OTP auto-capture na ho to user
# /otp CODE 123456 bhej de. Jo number abhi us code ka OTP wait kar raha hai,
# wahi turant ye OTP use karke registration verify kar dega.
_manual_otp: dict = {}
_MANUAL_OTP_SENTINEL = "__MANUAL_OTP__"  # _await_first_sms return -> manual OTP ready


def get_otp_doctor() -> OTPDoctorAPI:
    global _otp_doctor
    if _otp_doctor is None:
        key = os.environ.get("SASTA_OTP_API_KEY", "stp_79172220cdb83fab0fba77c970c849cba14c0bc63b7413d9")
        _otp_doctor = OTPDoctorAPI(key)
    return _otp_doctor


async def get_grainotch_service_id() -> str:
    global _grainotch_service_id
    if _grainotch_service_id:
        return _grainotch_service_id

    loop = asyncio.get_event_loop()
    api  = get_otp_doctor()

    def _find():
        return api.find_service_id(SERVICE_KEYWORD)

    try:
        sid = await loop.run_in_executor(None, _find)
        if sid:
            _grainotch_service_id = sid
            logger.info("Grainotch service ID (API): %s", sid)
            return sid
    except Exception as e:
        logger.warning("Service ID API lookup failed: %s", e)

    logger.warning("Using hardcoded fallback service ID: %s", GRAINOTCH_SERVICE_ID_FALLBACK)
    _grainotch_service_id = GRAINOTCH_SERVICE_ID_FALLBACK
    return GRAINOTCH_SERVICE_ID_FALLBACK


BOT_COMMANDS = [
    BotCommand("start",      "🚀 Bot shuru + naye codes do"),
    BotCommand("newcodes",   "➕ Naye codes add karo"),
    BotCommand("run",        "▶️ Pending codes process karo"),
    BotCommand("mevo",       "📋 Saare codes ka status"),
    BotCommand("vouchers",   "🎁 Saved Amazon vouchers"),
    BotCommand("redeem",     "🎟️ Vouchers redeem list"),
    BotCommand("delcode",    "🗑️ Pending code hatao"),
    BotCommand("resend",     "🔁 Voucher dobara maango (usi number)"),
    BotCommand("otp",        "✍️ Manual OTP do (auto-capture fail)"),
    BotCommand("voucher",    "📥 2nd SMS paste karke voucher save karo"),
    BotCommand("recheckall", "🔄 Sab dobara check karo"),
    BotCommand("balance",    "💰 Sasta OTP balance"),
    BotCommand("services",   "🔍 Services list"),
    BotCommand("setservice", "⚙️ Service ID set karo"),
    BotCommand("setbatch",   "🔢 Batch size set karo"),
    BotCommand("export",     "📤 Codes export karo"),
    BotCommand("cancel",     "❌ Current operation cancel"),
]


async def _prefetch_service_id(app) -> None:
    # Telegram command menu register karo — "/" type karte hi list dikhe
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
        logger.info("Bot command menu set (%d commands)", len(BOT_COMMANDS))
    except Exception as e:
        logger.warning("set_my_commands error: %s", e)

    # Background mein chalao — bot turant polling shuru kare, prefetch block na kare
    async def _bg() -> None:
        logger.info("Pre-fetching Grainotch service ID in background...")
        try:
            sid = await get_grainotch_service_id()
            if sid:
                logger.info("Startup: Grainotch service ID cached = %s", sid)
            else:
                logger.warning("Startup: Grainotch service ID not found. Use /setservice to set manually.")
        except Exception as e:
            logger.warning("Startup prefetch error: %s", e)

    # Strong reference rakho — warna fire-and-forget task GC ho sakta hai
    app.bot_data["prefetch_task"] = asyncio.create_task(_bg())


def load_db() -> dict:
    if DB_FILE.exists():
        try:
            with open(DB_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_db(db: dict) -> None:
    tmp = DB_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
    tmp.replace(DB_FILE)


async def upsert_code(code: str, status: str, **extra) -> None:
    async with _get_db_lock():
        db = load_db()
        record = db.get(code, {"code": code, "added_at": datetime.now().isoformat()})
        record["status"] = status
        record.update(extra)
        record["updated_at"] = datetime.now().isoformat()
        db[code] = record
        save_db(db)


def _get_register_page():
    session = requests.Session()
    r = session.get(f"{BASE_URL}/home/register", headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    token_input = soup.find("input", {"name": "token"})
    token = token_input["value"] if token_input else ""
    return session, token


def _request_otp(session, token: str, code: str, mobile: str) -> dict:
    payload = {"phone": mobile, "ccode": code}
    ajax_headers = {
        **HEADERS,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": BASE_URL,
    }
    r = session.post(
        f"{BASE_URL}/home/generateOTP",
        data=payload,
        headers=ajax_headers,
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") == "success":
        return {"success": True, "msg": "OTP sent"}
    msg = data.get("msg") or data.get("msg1") or "Unknown error"
    return {"success": False, "msg": str(msg)}


def _submit_registration(session, token: str, code: str, name: str,
                          mobile: str, otp: str, city: str) -> dict:
    payload = {
        "campaigncode": code,
        "name": name,
        "mobile": mobile,
        "mobile_otp": otp,
        "state": city,
        "question": "Japanese",
        "lda": "yes",
        "terms": "yes",
        "token": token,
        "g-recaptcha-response": "",
    }
    r = session.post(
        f"{BASE_URL}/home/register",
        data=payload,
        headers=HEADERS,
        timeout=30,
        allow_redirects=True,
    )
    text_lower = r.text.lower()
    if any(k in text_lower for k in [
        "thank you", "successfully", "registered", "congratulation",
        "success", "shukriya", "dhanyavaad",
    ]):
        return {"success": True, "msg": "Registration successful!"}

    soup = BeautifulSoup(r.text, "html.parser")
    for el in soup.find_all(class_=["text-danger", "alert", "error", "alert-danger"]):
        msg_text = el.get_text(strip=True)
        if msg_text:
            return {"success": False, "msg": msg_text[:200]}

    return {"success": False, "msg": "Unexpected response — manually verify."}


async def _run(fn, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fn, *args)


_otp_sem: asyncio.Semaphore | None = None


def _get_otp_sem() -> asyncio.Semaphore:
    # otpdoctor.in rate-limit/429 deta hai. Batch me bahut codes ho to bhi ek saath
    # max itni HTTP calls jaane do — warna host overload hoke 429 deta hai.
    # 5 => ek saath 5 codes ka OTP/voucher (2nd SMS) parallel extract ho.
    global _otp_sem
    if _otp_sem is None:
        _otp_sem = asyncio.Semaphore(5)
    return _otp_sem


async def _otp_call(fn, *args):
    async with _get_otp_sem():
        return await _run(fn, *args)


async def _await_first_sms(api, activation_id: str, code: str | None = None,
                           max_wait: int = 120, poll_interval: int = 5):
    # Async 1st-SMS (OTP) wait — har poll chhoti executor call, beech me asyncio.sleep
    # (thread block NAHI hota, isliye batch me saare codes parallel chalte hain).
    # Agar user ne /otp se manual OTP diya hai to wahi turant use hoga (sentinel return).
    start = time.monotonic()
    while time.monotonic() - start < max_wait:
        if _stop_flag:
            return None
        if code is not None and _manual_otp.get(code):
            return _MANUAL_OTP_SENTINEL
        try:
            result = await _otp_call(api.get_status, activation_id)
            if result["status"] == "ok":
                return result["text"]
            elif result["status"] == "cancelled":
                logger.warning("Activation %s cancelled", activation_id)
                return None
        except Exception as e:
            logger.warning("get_status error: %s", e)
        await asyncio.sleep(poll_interval)
    return None


async def _await_voucher_sms(api, activation_id: str, max_wait: int = 1200,
                             poll_interval: int = 10, prev_text=None):
    # Async voucher (2nd-SMS) wait — thread block NAHI hota, isliye batch=5/10/20/30
    # pe bhi saare codes ek saath voucher harvest karte hain. cancel/finish kabhi nahi.
    start = time.monotonic()
    last_retry = -1e9
    seen: set[str] = set()
    if prev_text:
        seen.add(prev_text.strip())

    # Sasta OTP multi_sms: pehla STATUS_OK ke baad activation "done" maana jaata hai, isliye
    # agla SMS (Amazon voucher) lene ke liye setStatus(3) = "isi number pe agla OTP/SMS maango"
    # bhejna padta hai. Yeh FREE hai aur number CANCEL NAHI karta (cancel sirf status=6 hota
    # hai — woh kabhi nahi bhejte). Throttle: har 15s me ek baar status=3 (fast).
    async def _request_next():
        nonlocal last_retry
        now = time.monotonic()
        if now - last_retry < 15:
            return
        try:
            await _otp_call(api.set_status, activation_id, 3)
            last_retry = now
            logger.info("setStatus(3) [agla SMS request] bheja voucher ke liye (elapsed=%ds)",
                        int(now - start))
        except Exception as e:
            logger.warning("setStatus(3) error: %s", e)

    while time.monotonic() - start < max_wait:
        if _stop_flag:
            return None
        await _request_next()
        try:
            # getStatusV2 = "number history refresh" — PURA SMS text deta hai, isliye
            # 2nd SMS ka full Amazon-voucher text yahin mil jaata hai (getStatus sirf
            # parsed code deta tha, full text kho jaata tha).
            result = await _otp_call(api.get_status_v2, activation_id)
            texts = result.get("texts") or ([result["text"]] if result.get("text") else [])
            if result["status"] == "ok" and texts:
                for text in texts:
                    text = (text or "").strip()
                    if not text or text in seen:
                        continue
                    seen.add(text)
                    logger.info("Voucher-wait SMS (full text) aaya: %s", text)
                    if extract_voucher(text):
                        return text  # asli Amazon voucher mil gaya
                    logger.info("SMS me voucher nahi (OTP dobara?) — agla SMS request karte raho")
                    await _request_next()
            elif result["status"] == "cancelled":
                logger.warning("Activation %s cancelled during 2nd SMS wait", activation_id)
                return None
            else:
                # V2 ne kuch nahi diya — getStatus se fallback try karo (purana path).
                fb = await _otp_call(api.get_status, activation_id)
                if fb["status"] == "ok" and fb.get("text"):
                    t = fb["text"].strip()
                    if t and t not in seen:
                        seen.add(t)
                        logger.info("Voucher-wait SMS (getStatus fallback) aaya: %s", t)
                        if extract_voucher(t):
                            return t
                        await _request_next()
                elif fb["status"] == "cancelled":
                    logger.warning("Activation %s cancelled during 2nd SMS wait", activation_id)
                    return None
            # waiting / waiting_resend / unknown → poll karte raho
        except Exception as e:
            logger.warning("voucher get_status error: %s", e)
        await asyncio.sleep(poll_interval)
    return None


async def process_code_auto(code: str, name: str, city: str,
                             update: Update, context: ContextTypes.DEFAULT_TYPE,
                             city_idx: int) -> dict:
    api = get_otp_doctor()
    loop = asyncio.get_event_loop()
    activation_id = None
    phone_clean   = None
    web_session   = None
    token         = None
    otp           = None
    sms1_text     = None

    service_id = await get_grainotch_service_id()
    if not service_id:
        return {"success": False, "msg": "Grainotch service Sasta OTP mein nahi mila. /services se check karo."}

    # ── 1st OTP loop: number assign hone ke baad SMS1_WAIT me OTP na aaye to
    #    SAME unique code se naya number lega. Purana number CANCEL NAHI karta
    #    (user khud website se cancel karega). ──
    last_err = "OTP nahi aaya"
    for otp_attempt in range(1, OTP_NEW_NUMBER_ATTEMPTS + 1):
        if _stop_flag:
            return {"success": False, "msg": "Cancelled by user (/cancel)"}
        activation_id = None
        phone_clean   = None

        # ── Step A: Number kharido — na mile to har NUMBER_RETRY_INTERVAL sec pe retry ──
        await update.message.reply_text(
            f"📱 *Code {esc(code)}* — Sasta OTP se number le raha hoon... "
            f"_(OTP try {otp_attempt}/{OTP_NEW_NUMBER_ATTEMPTS})_\n"
            f"_(number na mile to har {NUMBER_RETRY_INTERVAL} sec me dobara try)_",
            parse_mode="Markdown",
        )

        for buy_try in range(1, NUMBER_BUY_MAX_ATTEMPTS + 1):
            if _stop_flag:
                return {"success": False, "msg": "Cancelled by user (/cancel)"}
            try:
                number_info = await loop.run_in_executor(
                    None, lambda: api.get_number(service_id, country="91")
                )
                activation_id = number_info["id"]
                phone         = number_info["phone"]
                if phone.startswith("91") and len(phone) == 12:
                    phone_clean = phone[2:]
                else:
                    phone_clean = phone.lstrip("+91")
                logger.info("Got number: %s (activation: %s) [otp %d, buy %d]",
                            phone, activation_id, otp_attempt, buy_try)
                break
            except Exception as e:
                err_text = str(e)
                last_err = f"Number nahi mila: {err_text[:100]}"
                logger.warning("Number buy failed (otp %d, buy %d/%d): %s",
                               otp_attempt, buy_try, NUMBER_BUY_MAX_ATTEMPTS, e)
                # ── Terminal errors: retry karne se fayda nahi, turant clear message ──
                err_up = err_text.upper()
                if "NO_BALANCE" in err_up:
                    return {"success": False,
                            "msg": "💸 *Sasta OTP balance kam hai!* GrainOtch number ke liye "
                                   "balance khatam ho gaya. sastaotp.com pe recharge karke dobara try karo.",
                            "phone": "", "activation_id": ""}
                if any(t in err_up for t in ("BAD_KEY", "BAD_SERVICE", "NO_SERVICE", "BAD_ACTION")):
                    return {"success": False,
                            "msg": f"⛔ Sasta OTP error: `{esc(err_text[:80])}` — service/API key check karo.",
                            "phone": "", "activation_id": ""}
                if buy_try < NUMBER_BUY_MAX_ATTEMPTS:
                    # spam na ho — sirf pehli baar aur har 6th try pe update bhejo
                    if buy_try == 1 or buy_try % 6 == 0:
                        await update.message.reply_text(
                            f"⏳ `{esc(code)}` — abhi number available nahi. "
                            f"{NUMBER_RETRY_INTERVAL} sec me dobara try... "
                            f"_(buy try {buy_try}/{NUMBER_BUY_MAX_ATTEMPTS})_",
                            parse_mode="Markdown",
                        )
                    await asyncio.sleep(NUMBER_RETRY_INTERVAL)
                continue
        else:
            # poori buy-retry me number nahi mila → aage badhne ka fayda nahi
            return {"success": False,
                    "msg": f"{last_err} (har {NUMBER_RETRY_INTERVAL}s, {NUMBER_BUY_MAX_ATTEMPTS} try fail)"}

        await update.message.reply_text(
            f"✅ Number mila: `{esc(phone_clean)}`\n"
            f"⏳ GrainOtch pe OTP request bhej raha hoon...",
            parse_mode="Markdown",
        )

        # ── Step B: Website pe OTP request (cancel NAHI) ──
        try:
            web_session, token = await _run(_get_register_page)
            otp_result = await _run(_request_otp, web_session, token, code, phone_clean)
        except Exception as e:
            logger.error("OTP request failed (otp try %d): %s", otp_attempt, e)
            last_err = f"Website OTP error: {str(e)[:100]}"
            await asyncio.sleep(2)
            continue  # SAME code se naya number (purana cancel nahi)

        if not otp_result["success"]:
            last_err = f"OTP send failed: {otp_result['msg']}"
            # SIRF jab clearly code hi galat/use/expire ho tabhi ruko (warna naya number try karte raho).
            # Broad single-word match jaan-bujh ke nahi — transient error pe retry chalu rahe (user preference).
            msg_low = otp_result["msg"].lower()
            terminal = any(p in msg_low for p in [
                "invalid code", "code invalid", "wrong code", "incorrect code",
                "code expired", "expired code", "campaign expired", "campaign ended",
                "already used", "already registered", "already redeemed", "already claimed",
            ])
            if terminal:
                return {"success": False, "msg": last_err,
                        "phone": phone_clean, "activation_id": activation_id or ""}
            await asyncio.sleep(2)
            continue  # SAME code se naya number (transient error — retry)

        # Is naye number ke liye koi purana (stale) manual OTP discard karo — taaki
        # user ka diya OTP sirf CURRENT active number par hi apply ho.
        _manual_otp.pop(code, None)

        await update.message.reply_text(
            f"📨 OTP request gaya! Pehla SMS (OTP) ka wait (max {SMS1_WAIT // 60} min)...\n"
            f"_(Auto-capture na ho to khud bhejo: `/otp {esc(code)} 123456`)_",
            parse_mode="Markdown",
        )

        # ── Step C: 1st SMS (OTP) ka wait — async (thread block NAHI) ──
        # Har 2 sec pe "check SMS" (get_status) — OTP aate hi turant break hoke (fast capture)
        # instant registration submit hota hai (neeche). /otp se manual OTP bhi chalega.
        sms1_text = await _await_first_sms(api, activation_id, code=code,
                                           max_wait=SMS1_WAIT, poll_interval=2)

        # ── Manual OTP (user ne /otp se diya) → seedha registration ──
        if sms1_text == _MANUAL_OTP_SENTINEL:
            otp = _manual_otp.pop(code, None)
            if not otp:
                continue
            sms1_text = f"(Manual OTP user se: {otp})"
            logger.info("Manual OTP used for %s", code)
            await update.message.reply_text(
                f"✍️ `{esc(code)}` — aapka diya OTP `{esc(otp)}` use kar raha hoon...",
                parse_mode="Markdown",
            )
            break  # seedha registration submit

        if not sms1_text:
            last_err = f"OTP SMS nahi aaya (timeout {SMS1_WAIT // 60} min)"
            logger.warning("No SMS1 (otp try %d/%d) — SAME code se naya number, purana cancel nahi",
                           otp_attempt, OTP_NEW_NUMBER_ATTEMPTS)
            if otp_attempt < OTP_NEW_NUMBER_ATTEMPTS:
                await update.message.reply_text(
                    f"⚠️ `{esc(code)}` — {SMS1_WAIT // 60} min me OTP nahi aaya. "
                    f"*Same code* se naya number le raha hoon.\n"
                    f"_(purana number `{esc(phone_clean)}` aap website se cancel kar dena)_\n"
                    f"_(OTP try {otp_attempt + 1}/{OTP_NEW_NUMBER_ATTEMPTS})_",
                    parse_mode="Markdown",
                )
            continue  # SAME code se naya number (purana cancel nahi)

        logger.info("SMS 1 received: %s", sms1_text)
        otp = extract_otp(sms1_text)
        if not otp:
            last_err = f"OTP extract nahi hua. SMS: {sms1_text[:80]}"
            if otp_attempt < OTP_NEW_NUMBER_ATTEMPTS:
                await update.message.reply_text(
                    f"⚠️ `{esc(code)}` — OTP extract nahi hua. Same code se naya number...",
                )
            continue  # SAME code se naya number

        # OTP mil gaya → outer loop se bahar
        break
    else:
        # saari OTP attempts khatam, OTP nahi mila (kuch bhi cancel nahi kiya)
        return {"success": False,
                "msg": f"{last_err} ({OTP_NEW_NUMBER_ATTEMPTS} numbers try kiye, OTP nahi aaya). "
                       f"Purane numbers website se cancel kar sakte ho.",
                "phone": phone_clean or "", "activation_id": activation_id or ""}

    await update.message.reply_text(
        f"🔑 OTP mila: `{esc(otp)}`\n"
        f"⏳ Registration submit kar raha hoon...\n"
        f"👤 Naam: *{esc(name)}* | 🏙️ City: *{esc(city)}*",
        parse_mode="Markdown",
    )

    # ── Step 4: Registration (cancel NAHI) ──
    try:
        reg_result = await _run(_submit_registration, web_session, token,
                                code, name, phone_clean, otp, city)
    except Exception as e:
        logger.error("Registration submit failed: %s", e)
        reg_result = {"success": False, "msg": f"Network error: {str(e)[:80]}"}

    if not reg_result["success"]:
        return {
            "success": False,
            "msg": reg_result["msg"],
            "phone": phone_clean,
            "otp": otp,
            "activation_id": activation_id or "",
        }

    await update.message.reply_text(
        f"✅ *Registration ho gayi!*\n\n"
        f"⏳ Amazon Voucher wala 2nd SMS dhundh raha hoon...\n"
        f"_(Max 20 min wait — har 5 sec pe Check SMS, number cancel nahi hoga)_ 🎁",
        parse_mode="Markdown",
    )

    # ── Step 5: 2nd SMS (voucher) — voucher SMS na aaye to 20 min tak check karte raho.
    #    OTP dobara aaya SMS ignore hota hai. cancel/finish NAHI (user manually karega). ──
    sms2_text = await _await_voucher_sms(api, activation_id, max_wait=1200,
                                         poll_interval=3, prev_text=sms1_text)

    voucher = None
    if sms2_text:
        logger.info("SMS 2 received: %s", sms2_text)
        voucher = extract_voucher(sms2_text)
    else:
        logger.info("No 2nd SMS received (timeout)")

    return {
        "success": True,
        "msg": "Registration + Voucher complete!" if voucher else "Registration ho gayi (voucher SMS nahi aaya)",
        "phone": phone_clean,
        "otp": otp,
        "sms1": sms1_text,
        "sms2": sms2_text,
        "voucher": voucher,
        "name": name,
        "city": city,
        "activation_id": activation_id or "",
    }


def esc(text: str) -> str:
    return str(text).replace("_", r"\_").replace("*", r"\*") \
                    .replace("`", r"\`").replace("[", r"\[")


async def mevo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = load_db()
    if not db:
        await update.message.reply_text(
            "📭 Abhi koi code database mein nahi hai.\n/start karke codes bhejo."
        )
        return

    used    = [r for r in db.values() if r.get("status") == "success"]
    failed  = [r for r in db.values() if r.get("status") == "failed"]
    pending = [r for r in db.values() if r.get("status") == "pending"]

    lines = [f"📋 *FULL CODE REPORT* ({len(db)} total)\n"]

    lines.append(f"✅ *Registered ({len(used)}):*")
    for r in used:
        voucher = r.get("voucher", "")
        voucher_str = f" 🎁`{esc(voucher)}`" if voucher else " _(no voucher)_"
        lines.append(f"  `{esc(r['code'])}` — {esc(r.get('name','-'))}, {esc(r.get('city','-'))}{voucher_str}")

    lines.append("")
    lines.append(f"❌ *Failed ({len(failed)}):*")
    for r in failed:
        lines.append(f"  `{esc(r['code'])}` — {esc(r.get('error','?')[:60])}")
    if not failed:
        lines.append("  _koi nahi_")

    lines.append("")
    lines.append(f"🕐 *Pending ({len(pending)}):*")
    for r in pending:
        lines.append(f"  `{esc(r['code'])}`")
    if not pending:
        lines.append("  _koi nahi_")

    full_msg = "\n".join(lines)
    for i in range(0, len(full_msg), 4000):
        await update.message.reply_text(full_msg[i:i+4000], parse_mode="Markdown")


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        api = get_otp_doctor()
        loop = asyncio.get_event_loop()
        bal  = await loop.run_in_executor(None, api.get_balance)
        await update.message.reply_text(f"💰 Sasta OTP balance: *₹{bal:.2f}*", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Balance error: {e}")


async def services_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔍 Sasta OTP services dhundh raha hoon... _(thoda time lag sakta hai)_", parse_mode="Markdown")
    services = {}
    try:
        api  = get_otp_doctor()
        loop = asyncio.get_event_loop()
        services = await loop.run_in_executor(None, api.get_services)
    except Exception as e:
        logger.warning("services_cmd fetch error: %s", e)

    # ── Server down / list nahi mili → fallback option dikhao (dead-end nahi) ──
    if not services:
        await update.message.reply_text(
            f"⚠️ *Sasta OTP ki service list abhi nahi mili* (server slow/down).\n\n"
            f"Tension nahi — bot phir bhi *GrainOtch MultiSMS* (`{GRAINOTCH_SERVICE_ID_FALLBACK}`) "
            f"use karta hai.\n\n"
            f"Manually select karne ke liye bhejo:\n"
            f"  `/setservice grainotch`\n\n"
            f"_Baad mein /services dobara try kar sakte ho._",
            parse_mode="Markdown",
        )
        return

    def _name(info):
        return (info.get("name", "") if isinstance(info, dict) else "").strip()

    def _price(info):
        return info.get("price", "?") if isinstance(info, dict) else "?"

    grain_entries = [(k, v) for k, v in services.items()
                     if "grain" in (str(k) + _name(v)).lower()]

    if grain_entries:
        lines = ["🌾 *GrainOtch Services mil gayin:*\n"]
        for sid, info in grain_entries:
            mark = " ✅" if (isinstance(info, dict) and info.get("multi_sms")) else ""
            lines.append(
                f"  Code `{esc(str(sid))}`: {esc(_name(info))} — ₹{_price(info)}{mark}"
            )
        lines.append(
            f"\nSelect karne ke liye: `/setservice <code>`\n"
            f"Ya seedha: `/setservice grainotch` (MultiSMS = `{GRAINOTCH_SERVICE_ID_FALLBACK}`)"
        )
    else:
        lines = ["⚠️ GrainOtch service list mein nahi mila. First 20 dikha raha hoon:\n"]
        for sid, info in list(services.items())[:20]:
            lines.append(f"  `{esc(str(sid))}`: {esc(_name(info))}")
        lines.append("\n`/setservice grainotch` se default GrainOtch MultiSMS set ho jayega.")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def setservice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _grainotch_service_id
    args = context.args
    if not args:
        sid = _grainotch_service_id or "not set"
        is_grain = " _(GrainOtch MultiSMS)_" if sid == GRAINOTCH_SERVICE_ID_FALLBACK else ""
        await update.message.reply_text(
            f"📡 *Sasta OTP Service*\n\n"
            f"Abhi selected: `{esc(sid)}`{is_grain}\n\n"
            f"Manually select karne ke liye:\n"
            f"  `/setservice grainotch` — GrainOtch MultiSMS (`{GRAINOTCH_SERVICE_ID_FALLBACK}`)\n"
            f"  `/setservice <ID>` — koi bhi custom ID\n\n"
            f"_Tip: /services se available service IDs dekho._",
            parse_mode="Markdown",
        )
        return

    choice = args[0].strip().lower()
    # Naam se shortcut: "grainotch" / "multisms" → GrainOtch MultiSMS fallback ID
    if choice in ("grainotch", "multisms", "grain"):
        _grainotch_service_id = GRAINOTCH_SERVICE_ID_FALLBACK
        await update.message.reply_text(
            f"✅ Service set: *GrainOtch MultiSMS* — `{esc(_grainotch_service_id)}`",
            parse_mode="Markdown",
        )
        return

    _grainotch_service_id = args[0].strip()
    is_grain = " *(GrainOtch MultiSMS)*" if _grainotch_service_id == GRAINOTCH_SERVICE_ID_FALLBACK else ""
    await update.message.reply_text(
        f"✅ Service ID set: `{esc(_grainotch_service_id)}`{is_grain}", parse_mode="Markdown"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()

    db = load_db()
    pending = [r["code"] for r in db.values() if r.get("status") == "pending"]

    if pending:
        context.user_data["codes"]    = pending
        context.user_data["city_idx"] = 0
        await update.message.reply_text(
            f"🔄 *{len(pending)} pending code(s) mile!* Auto-process shuru hoga.\n\n"
            f"📋 Codes: `{esc(', '.join(pending))}`\n\n"
            f"▶️ `/run` bhejo process shuru karne ke liye\n"
            f"🆕 `/newcodes` bhejo naye codes dene ke liye",
            parse_mode="Markdown",
        )
        return CODES

    await update.message.reply_text(
        f"🥃 *GrainOtch Auto-Registration Bot*\n\n"
        f"📋 *Codes bhejo* — comma ya newline se alag karo:\n\n"
        f"`ABC123DEF4, XYZ987WQR1`\n\n"
        f"⚠️ 10-character codes (A-Z, 0-9)\n"
        f"⚡ Current batch size: *{_batch_size}* ek saath\n\n"
        f"📊 /mevo — codes ka report\n"
        f"🎁 /redeem — Amazon vouchers copy karo\n"
        f"🔁 /resend — voucher na aaya? usi number pe dobara maango\n"
        f"🔄 /recheckall — voucher missing codes retry\n"
        f"✍️ /otp — auto-capture fail? manual OTP: `/otp CODE 123456`\n"
        f"📥 /voucher — history se 2nd SMS paste: `/voucher CODE <SMS>`\n"
        f"📤 /export — CSV download\n"
        f"⚡ /setbatch — batch size set karo (1/5/10/20/30)\n"
        f"💰 /balance — Sasta OTP balance\n"
        f"🔍 /services — Grainotch service check\n"
        f"🚫 /cancel — band karo",
        parse_mode="Markdown",
    )
    return CODES


async def newcodes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "📋 *Naye codes bhejo* — comma ya newline se:\n`ABC123DEF4, XYZ987WQR1`",
        parse_mode="Markdown",
    )
    return CODES


async def receive_codes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if text.lower() in ["/run", "run"]:
        codes = context.user_data.get("codes", [])
        if not codes:
            await update.message.reply_text("Koi codes nahi hain. Pehle codes bhejo.")
            return CODES
        await start_auto_processing(update, context, codes)
        return ConversationHandler.END

    raw        = re.split(r"[,\n\r\t]+", text)
    codes      = [c.strip().upper() for c in raw if c.strip()]
    valid      = [c for c in codes if len(c) == 10 and c.isalnum()]
    invalid    = [c for c in codes if c not in valid]

    if not valid:
        await update.message.reply_text(
            "❌ Koi valid code nahi mila.\n10-character codes bhejo (A-Z, 0-9)."
        )
        return CODES

    db = load_db()
    for c in valid:
        if c not in db:
            await upsert_code(c, "pending")

    db           = load_db()
    already_done = [c for c in valid if db.get(c, {}).get("status") == "success"]
    to_process   = [c for c in valid if db.get(c, {}).get("status") != "success"]

    msg = f"✅ *{len(to_process)} code(s) process honge!*\n"
    if already_done:
        msg += f"⏭️ Already registered skip: `{esc(', '.join(already_done))}`\n"
    if invalid:
        msg += f"⚠️ Invalid skip: `{esc(', '.join(invalid))}`\n"

    if not to_process:
        await update.message.reply_text(msg + "\nSaare codes pehle se done hain!")
        return CODES

    await update.message.reply_text(
        msg + f"\n🤖 *Fully automatic mode!*\n"
        f"Sasta OTP se numbers lega → OTP auto-submit → Voucher save karega\n\n"
        f"▶️ *Processing shuru hoti hai...*",
        parse_mode="Markdown",
    )

    await start_auto_processing(update, context, to_process)
    return ConversationHandler.END


async def _handle_one_result(
    code: str, name: str, city: str, result: dict,
    success_list: list, fail_list: list, voucher_list: list,
    update: Update,
) -> None:
    if result["success"]:
        voucher = result.get("voucher")
        await upsert_code(
            code, "success",
            name=name, city=city,
            mobile=result.get("phone", ""),
            otp=result.get("otp", ""),
            sms1=result.get("sms1", ""),
            sms2=result.get("sms2", ""),
            voucher=voucher or "",
            activation_id=result.get("activation_id", ""),
        )
        success_list.append(code)
        sms2_raw = result.get("sms2", "")
        if voucher:
            voucher_list.append((code, voucher))
            voucher_msg = (
                f"\n\n🎁 *Amazon Voucher Mila!*\n"
                f"┌─────────────────────\n"
                f"│ `{esc(voucher)}`\n"
                f"└─────────────────────\n"
                f"📩 Full SMS: _{esc(sms2_raw)}_"
            )
        elif sms2_raw:
            voucher_msg = (
                f"\n\n⚠️ *2nd SMS aaya par code extract nahi hua*\n"
                f"📩 Full SMS: _{esc(sms2_raw)}_"
            )
        else:
            voucher_msg = "\n\n⚠️ Amazon Voucher SMS nahi aaya (timeout)"
        await update.message.reply_text(
            f"✅ `{esc(code)}` — *Register ho gaya!*\n"
            f"📱 Number: `{esc(result.get('phone','?'))}`"
            f"{voucher_msg}",
            parse_mode="Markdown",
        )
    else:
        await upsert_code(
            code, "failed",
            error=result["msg"],
            mobile=result.get("phone", ""),
            activation_id=result.get("activation_id", ""),
        )
        fail_list.append(code)
        await update.message.reply_text(
            f"❌ `{esc(code)}` — *Failed:* {esc(result['msg'])}",
            parse_mode="Markdown",
        )


async def start_auto_processing(update: Update, context: ContextTypes.DEFAULT_TYPE, codes: list) -> None:
    """Process codes in configurable batch size simultaneously. /cancel se ruk sakta hai."""
    global _processing, _stop_flag
    if _processing:
        await update.message.reply_text(
            "⚠️ Pehle se processing chal rahi hai. /cancel se rok do, phir try karo."
        )
        return
    _stop_flag = False
    _processing = True

    BATCH_SIZE   = _batch_size
    total        = len(codes)
    success_list = []
    fail_list    = []
    voucher_list = []
    cancelled    = False

    try:
        await update.message.reply_text(
            f"🚀 *{total} codes — {BATCH_SIZE} ek saath process honge!*\n"
            f"_(Batch size change: /setbatch 1 | 5 | 10 | 20 | 30)_\n"
            f"_(rokne ke liye /cancel)_",
            parse_mode="Markdown",
        )

        for batch_start in range(0, total, BATCH_SIZE):
            if _stop_flag:
                cancelled = True
                break

            batch        = codes[batch_start:batch_start + BATCH_SIZE]
            batch_names  = [random.choice(INDIAN_MALE_NAMES) for _ in batch]
            # Har batch ke liye random + alag-alag city: shuffle karke cycle karte hain
            # (jab batch size > total cities ho to even spread milta hai, repeat minimum).
            shuffled_cities = CITIES[:]
            random.shuffle(shuffled_cities)
            batch_cities = [shuffled_cities[j % len(shuffled_cities)] for j in range(len(batch))]

            batch_lines = "\n".join(
                f"  `{esc(c)}` — {esc(n)} | {esc(ci)}"
                for c, n, ci in zip(batch, batch_names, batch_cities)
            )
            await update.message.reply_text(
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🔄 *Batch {batch_start // BATCH_SIZE + 1}: {len(batch)} codes ek saath*\n"
                f"{batch_lines}",
                parse_mode="Markdown",
            )

            tasks = [
                process_code_auto(code, name, city, update, context, batch_start + j)
                for j, (code, name, city) in enumerate(zip(batch, batch_names, batch_cities))
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for code, name, city, result in zip(batch, batch_names, batch_cities, results):
                if isinstance(result, Exception):
                    logger.error("Unexpected error for code %s: %s", code, result)
                    result = {"success": False, "msg": f"Unexpected error: {str(result)[:100]}"}
                await _handle_one_result(code, name, city, result,
                                         success_list, fail_list, voucher_list, update)

            if _stop_flag:
                cancelled = True
                break

            if batch_start + BATCH_SIZE < total:
                await asyncio.sleep(2)

        head = ("🛑 *Processing cancel ho gaya!*\n\n" if cancelled
                else "🎉 *Saare codes process ho gaye!*\n\n")
        summary = head + f"📊 *Result: {len(success_list)}/{total} successful*\n\n"
        if voucher_list:
            summary += f"🎁 *Amazon Vouchers ({len(voucher_list)}):*\n"
            for code, v in voucher_list:
                summary += f"  `{esc(code)}` → `{esc(v)}`\n"
            summary += "\n"
        if fail_list:
            summary += f"❌ *Failed codes:* `{esc(', '.join(fail_list))}`\n"
        summary += "\n📋 /mevo | /redeem se vouchers | /start se naye codes"
        await update.message.reply_text(summary, parse_mode="Markdown")
    finally:
        _processing = False
        _stop_flag = False


async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db      = load_db()
    pending = [r["code"] for r in db.values() if r.get("status") == "pending"]
    if not pending:
        await update.message.reply_text(
            "✅ Koi pending code nahi hai.\n/start se naye codes do."
        )
        return
    await update.message.reply_text(
        f"▶️ *{len(pending)} pending codes process ho rahe hain...*",
        parse_mode="Markdown",
    )
    await start_auto_processing(update, context, pending)


async def setbatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set how many codes process simultaneously: /setbatch 5|10|20|30"""
    global _batch_size
    args = context.args

    if not args:
        await update.message.reply_text(
            f"⚡ *Batch Size — Kitne codes ek saath?*\n\n"
            f"Current: *{_batch_size}* codes ek saath\n\n"
            f"Change karne ke liye:\n"
            f"  `/setbatch 1`  — 1 ek baar (slowest, safest)\n"
            f"  `/setbatch 5`  — 5 ek saath (safe)\n"
            f"  `/setbatch 10` — 10 ek saath\n"
            f"  `/setbatch 20` — 20 ek saath\n"
            f"  `/setbatch 30` — 30 ek saath (fast)\n\n"
            f"⚠️ Zyada batch = zyada Sasta OTP balance kharch",
            parse_mode="Markdown",
        )
        return

    try:
        new_size = int(args[0].strip())
    except ValueError:
        await update.message.reply_text("❌ Number do: `/setbatch 5` ya `/setbatch 10`",
                                        parse_mode="Markdown")
        return

    if new_size not in ALLOWED_BATCH_SIZES:
        await update.message.reply_text(
            f"❌ Allowed sizes: {', '.join(str(x) for x in ALLOWED_BATCH_SIZES)}\n"
            f"Example: `/setbatch 10`",
            parse_mode="Markdown",
        )
        return

    _batch_size = new_size
    await update.message.reply_text(
        f"✅ Batch size set: *{_batch_size}* codes ek saath process honge!\n\n"
        f"Ab /start karo ya codes bhejo.",
        parse_mode="Markdown",
    )


async def _resend_one(api, r: dict, update: Update) -> bool:
    code = r["code"]
    activation_id = r.get("activation_id")
    try:
        sms2 = await _await_voucher_sms(api, activation_id, max_wait=RESEND_WAIT,
                                        poll_interval=3, prev_text=r.get("sms1"))
    except Exception as e:
        logger.warning("resend %s error: %s", code, e)
        sms2 = None
    voucher = extract_voucher(sms2) if sms2 else None
    if voucher:
        await upsert_code(code, "success", voucher=voucher, sms2=sms2)
        await update.message.reply_text(
            f"🎁 `{esc(code)}` — voucher mil gaya!\n`{esc(voucher)}`",
            parse_mode="Markdown")
        return True
    await update.message.reply_text(
        f"⌛ `{esc(code)}` — abhi tak voucher nahi aaya ({RESEND_WAIT // 60} min).\n"
        f"_(number `{esc(r.get('mobile', ''))}` website pe check/cancel kar sakte ho.)_",
        parse_mode="Markdown")
    return False


async def _resend_vouchers(api, candidates: list, update: Update) -> int:
    """Saare candidates ke liye USI number pe voucher resend (cancel/finish NAHI).
    /cancel se beech me ruk sakta hai (_stop_flag). Returns: kitne voucher mile."""
    global _processing, _stop_flag
    _stop_flag = False
    _processing = True
    try:
        results = await asyncio.gather(*[_resend_one(api, r, update) for r in candidates])
    finally:
        _processing = False
        _stop_flag = False
    return sum(1 for x in results if x)


async def resend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """2nd-SMS (Amazon voucher) ko USI existing activation pe dobara maango — jinka
    voucher abhi tak nahi aaya. Naya number NAHI, cancel/finish NAHI.
    Usage: /resend            -> saare voucher-pending codes (jinke paas activation_id ho)
           /resend ABC123DEF4 -> sirf ye code(s)."""
    if _processing:
        await update.message.reply_text(
            "⚠️ Pehle se processing chal rahi hai. /cancel se rok do, phir /resend."
        )
        return
    db = load_db()

    if context.args:
        candidates = []
        for raw in context.args:
            code = raw.strip().upper()
            r = db.get(code)
            if not r:
                await update.message.reply_text(
                    f"❌ `{esc(code)}` DB me nahi mila.", parse_mode="Markdown")
                continue
            if r.get("voucher"):
                await update.message.reply_text(
                    f"✅ `{esc(code)}` ka voucher already saved hai. /redeem se dekho.",
                    parse_mode="Markdown")
                continue
            if not r.get("activation_id"):
                await update.message.reply_text(
                    f"⚠️ `{esc(code)}` ka activation ID nahi hai (abhi tak register nahi hua).\n"
                    f"_(Fresh number se try karne ke liye /recheckall use karo.)_",
                    parse_mode="Markdown")
                continue
            candidates.append(r)
    else:
        # No-arg: sirf "register ho gaya par voucher nahi aaya" wale (failed nahi)
        candidates = [r for r in db.values()
                      if r.get("status") == "success"
                      and not r.get("voucher") and r.get("activation_id")]

    if not candidates:
        await update.message.reply_text(
            "📭 Koi aisa code nahi jiska activation ID ho aur voucher pending ho.\n"
            "_(Fresh number se dobara try ke liye /recheckall.)_",
            parse_mode="Markdown")
        return

    await update.message.reply_text(
        f"🔁 *Voucher resend* — {len(candidates)} code(s) ke liye *usi number* pe "
        f"2nd SMS dobara maang raha hoon (max {RESEND_WAIT // 60} min)...\n"
        f"_(Naya number nahi, cancel nahi.)_",
        parse_mode="Markdown")

    api = get_otp_doctor()
    got = await _resend_vouchers(api, candidates, update)
    await update.message.reply_text(
        f"✅ Resend complete — {got}/{len(candidates)} voucher mile. /redeem se dekho.")


async def otp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual OTP do agar website se auto-capture na ho. Jo number abhi us code ka
    OTP wait kar raha hai, wahi turant ye OTP use karke registration verify karega.
    Usage: /otp ABC123DEF4 123456"""
    if len(context.args) != 2:
        await update.message.reply_text(
            "⚠️ Usage: `/otp CODE OTP`\nMisal: `/otp ABC123DEF4 123456`",
            parse_mode="Markdown")
        return
    code = context.args[0].strip().upper()
    otp = context.args[1].strip()
    if not re.fullmatch(r"\d{4,8}", otp):
        await update.message.reply_text(
            "❌ OTP sirf 4-8 digit ka number hona chahiye. Misal: `/otp ABC123DEF4 123456`",
            parse_mode="Markdown")
        return
    _manual_otp[code] = otp
    await update.message.reply_text(
        f"✍️ `{esc(code)}` ke liye OTP `{esc(otp)}` mil gaya.\n"
        f"_(Agar ye number abhi OTP ka wait kar raha hai to ~10 sec me turant verify "
        f"ho jayega. Number cancel nahi hota.)_",
        parse_mode="Markdown")


async def voucher_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sasta OTP number history se 2nd SMS (voucher) copy karke yahan paste karo —
    bot us message me se Amazon voucher code nikaal ke us code ke against save karega.
    Usage: /voucher CODE <pura 2nd SMS yahan paste karo>
    Misal: /voucher ABC123DEF4 अभिनंदन! ... 4EZZ9ZXNZMJUWB हा कोड वापरा. ..."""
    raw = update.message.text or ""
    # "/voucher" prefix hatao (e.g. "/voucher" ya "/voucher@BotName")
    parts = raw.split(maxsplit=1)
    rest = parts[1].strip() if len(parts) > 1 else ""
    if not rest:
        await update.message.reply_text(
            "⚠️ Usage: `/voucher CODE <2nd SMS paste karo>`\n\n"
            "Sasta OTP → *History* → number kholo → *SMS 2* ka *Copy* dabao → "
            "yahan `/voucher <apna code> ` ke baad paste kar do.",
            parse_mode="Markdown")
        return

    # Pehla token = GrainOtch code, baaki = pasted SMS text
    bits = rest.split(maxsplit=1)
    code = bits[0].strip().upper()
    sms_text = bits[1].strip() if len(bits) > 1 else ""
    if not sms_text:
        await update.message.reply_text(
            "⚠️ Code ke baad *2nd SMS ka text* bhi paste karo.\n"
            "Misal: `/voucher ABC123DEF4 अभिनंदन! ... 4EZZ9ZXNZMJUWB हा कोड वापरा ...`",
            parse_mode="Markdown")
        return

    voucher = extract_voucher(sms_text)
    if not voucher:
        await update.message.reply_text(
            f"❌ `{esc(code)}` — is message me se voucher code nahi nikla.\n"
            f"📩 Jo mila: _{esc(sms_text[:200])}_\n\n"
            f"_Pura SMS dobara copy karke bhejo (Amazon voucher wala 2nd SMS)._",
            parse_mode="Markdown")
        return

    await upsert_code(
        code, "success",
        sms2=sms_text,
        voucher=voucher,
    )
    await update.message.reply_text(
        f"🎁 *Voucher save ho gaya!*\n"
        f"┌─────────────────────\n"
        f"│ Code: `{esc(code)}`\n"
        f"│ Voucher: `{esc(voucher)}`\n"
        f"└─────────────────────\n"
        f"📩 Full SMS: _{esc(sms_text[:300])}_\n\n"
        f"_/redeem ya /vouchers se dekho. (Number cancel nahi hota.)_",
        parse_mode="Markdown")


async def recheckall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Voucher-missing codes retry. Jo code pehle se number+OTP verify ho chuke
    (activation_id hai) — unhe DOBARA use NAHI karte: usi number pe voucher resend hota
    hai. Jo kabhi verify nahi hue — un par hi fresh number + OTP try hota hai."""
    if _processing:
        await update.message.reply_text(
            "⚠️ Pehle se processing chal rahi hai. /cancel se rok do, phir /recheckall."
        )
        return

    db = load_db()
    no_voucher = [r for r in db.values() if not r.get("voucher")]
    if not no_voucher:
        await update.message.reply_text(
            "✅ Saare codes ka Amazon voucher already save hai!\n/redeem se dekho."
        )
        return

    # Verified = registration complete (number+OTP ho chuka) => status "success".
    # (activation_id akela kaafi nahi — wo failure paths me bhi set hota hai.)
    def _verified(r):
        return r.get("status") == "success" and r.get("activation_id")
    # Verified -> SAME number pe resend (naya number NAHI, reuse NAHI).
    resend_codes = [r for r in no_voucher if _verified(r)]
    # Kabhi verify nahi hue (failed/pending) -> fresh number + OTP.
    fresh_codes  = [r["code"] for r in no_voucher if not _verified(r)]

    await update.message.reply_text(
        f"🔄 *Recheck All — voucher nahi mila wale codes:*\n\n"
        f"  🔁 Verified (same number resend): {len(resend_codes)}\n"
        f"  🆕 Never verified (fresh number): {len(fresh_codes)}\n\n"
        f"_(Verified codes DOBARA use nahi honge — sirf voucher resend.)_",
        parse_mode="Markdown",
    )

    if resend_codes:
        await update.message.reply_text(
            f"🔁 {len(resend_codes)} verified code(s) ke liye *usi number* pe voucher "
            f"maang raha hoon (max {RESEND_WAIT // 60} min)...",
            parse_mode="Markdown",
        )
        api = get_otp_doctor()
        got = await _resend_vouchers(api, resend_codes, update)
        await update.message.reply_text(
            f"🔁 Resend done — {got}/{len(resend_codes)} voucher mile."
        )

    if fresh_codes:
        for code in fresh_codes:
            await upsert_code(code, "pending")
        await start_auto_processing(update, context, fresh_codes)
    else:
        await update.message.reply_text("✅ Recheck complete. /redeem se vouchers dekho.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    global _stop_flag
    context.user_data.clear()
    if _processing:
        _stop_flag = True
        await update.message.reply_text(
            "🛑 *Processing rok raha hoon...*\n"
            "Current code(s) khatam hote hi ruk jayega.\n"
            "_(Numbers cancel NAHI honge — website se manually karna.)_",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        await update.message.reply_text(
            "❌ Cancel. Abhi koi processing chal nahi rahi thi.\n/start se shuru karo.",
            reply_markup=ReplyKeyboardRemove(),
        )
    return ConversationHandler.END


async def export_codes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = load_db()
    if not db:
        await update.message.reply_text("📭 Database mein koi code nahi.")
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Code", "Status", "Name", "City", "Mobile", "OTP", "Voucher", "Activation ID", "SMS1", "SMS2", "Error", "Added At"])
    for r in db.values():
        writer.writerow([
            r.get("code", ""), r.get("status", ""), r.get("name", ""),
            r.get("city", ""), r.get("mobile", ""), r.get("otp", ""),
            r.get("voucher", ""), r.get("activation_id", ""),
            r.get("sms1", ""), r.get("sms2", ""),
            r.get("error", ""), r.get("added_at", ""),
        ])

    buf.seek(0)
    fb = io.BytesIO(buf.getvalue().encode("utf-8"))
    fb.name = "codes_report.csv"

    used     = sum(1 for r in db.values() if r.get("status") == "success")
    failed   = sum(1 for r in db.values() if r.get("status") == "failed")
    pending  = sum(1 for r in db.values() if r.get("status") == "pending")
    vouchers = sum(1 for r in db.values() if r.get("voucher"))

    await update.message.reply_document(
        document=fb,
        filename="codes_report.csv",
        caption=(
            f"📊 *Codes Report*\n"
            f"Total: {len(db)} | ✅ {used} | ❌ {failed} | 🕐 {pending} | 🎟️ {vouchers} vouchers"
        ),
        parse_mode="Markdown",
    )


async def delcode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text(
            "🗑️ *Pending code hatane ka tarika:*\n"
            "  `/delcode <code>` — ek (ya kai) pending code hatao\n"
            "  `/delcode all` — saare pending code hatao\n\n"
            "_Sirf PENDING codes hatte hain — success/voucher wale safe rehte hain._\n"
            "_Pending list dekhne ke liye /mevo bhejo._",
            parse_mode="Markdown",
        )
        return

    async with _get_db_lock():
        db = load_db()

        # saare pending hatao
        if len(args) == 1 and args[0].lower() == "all":
            pending_codes = [c for c, r in db.items() if r.get("status") == "pending"]
            if not pending_codes:
                await update.message.reply_text("ℹ️ Koi pending code nahi hai.")
                return
            for c in pending_codes:
                db.pop(c, None)
            save_db(db)
            await update.message.reply_text(
                f"🗑️ *{len(pending_codes)} pending code(s) hata diye.*",
                parse_mode="Markdown",
            )
            return

        # specific code(s) hatao — sirf pending
        removed, skipped, notfound = [], [], []
        for raw in args:
            code = raw.strip().upper()  # codes DB me uppercase store hote hain
            rec = db.get(code)
            if rec is None:
                notfound.append(code)
            elif rec.get("status") != "pending":
                skipped.append((code, rec.get("status")))
            else:
                db.pop(code, None)
                removed.append(code)
        save_db(db)

    lines: list[str] = []
    if removed:
        lines.append(f"🗑️ *Hataye ({len(removed)}):*")
        lines += [f"  `{esc(c)}`" for c in removed]
    if skipped:
        lines.append("\n⚠️ *Pending nahi the (chhoda):*")
        lines += [f"  `{esc(c)}` ({esc(st or '?')})" for c, st in skipped]
    if notfound:
        lines.append("\n❓ *Mila nahi:*")
        lines += [f"  `{esc(c)}`" for c in notfound]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def vouchers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = load_db()
    voucher_records = [(r["code"], r["voucher"]) for r in db.values()
                       if r.get("voucher") and r.get("status") == "success"]

    if not voucher_records:
        await update.message.reply_text("🎁 Abhi koi Amazon Voucher save nahi hua.")
        return

    lines = [f"🎁 *Amazon Vouchers ({len(voucher_records)}):*\n"]
    for code, v in voucher_records:
        lines.append(f"  `{esc(code)}` → `{esc(v)}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def redeem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = load_db()

    records = [
        r for r in db.values()
        if r.get("voucher") and r.get("status") == "success"
    ]

    if not records:
        await update.message.reply_text(
            "🎁 Abhi koi Amazon Voucher save nahi hua.\n"
            "Codes process karne ke baad yahan aayenge."
        )
        return

    records.sort(key=lambda r: r.get("updated_at", ""), reverse=True)
    total = len(records)

    header = (
        f"🎁 *Amazon Vouchers — {total} code(s)*\n"
        f"_(Newest first | Copy karke amazon.in pe redeem karo)_\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )

    voucher_lines = []
    for i, r in enumerate(records, 1):
        voucher   = r["voucher"]
        code      = r.get("code", "?")
        timestamp = r.get("updated_at", "")[:10]
        voucher_lines.append(
            f"*{i}.* `{esc(voucher)}`\n"
            f"     📋 Code: `{esc(code)}` | 📅 {timestamp}"
        )

    plain_codes = "\n".join(r["voucher"] for r in records)
    plain_block = (
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 *Sirf Codes (bulk copy):*\n"
        f"```\n{plain_codes}\n```"
    )

    full_msg = header + "\n\n".join(voucher_lines) + plain_block

    if len(full_msg) <= 4096:
        await update.message.reply_text(full_msg, parse_mode="Markdown")
    else:
        await update.message.reply_text(header + "\n\n".join(voucher_lines), parse_mode="Markdown")
        await update.message.reply_text(
            f"📋 *Bulk Copy ({total} codes):*\n```\n{plain_codes}\n```",
            parse_mode="Markdown",
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception:", exc_info=context.error)
    if isinstance(update, Update) and update.message:
        await update.message.reply_text(
            "⚠️ Kuch error aa gaya. /cancel karke /start se dobara try karo."
        )


def main():
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "8497589257:AAEap7ffScWuJbBi726MAHKm639q4xJnets")

    # concurrent_updates(True) zaroori hai — warna processing ke dauraan /cancel
    # queue me pada rehta hai aur handle hi nahi hota (PTB default = sequential).
    app = Application.builder().token(bot_token).concurrent_updates(True).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("newcodes", newcodes),
        ],
        states={
            CODES: [
                CommandHandler("run", receive_codes),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_codes),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("run",        run_cmd))
    app.add_handler(CommandHandler("cancel",     cancel))
    app.add_handler(CommandHandler("mevo",       mevo))
    app.add_handler(CommandHandler("export",     export_codes))
    app.add_handler(CommandHandler("balance",    balance_cmd))
    app.add_handler(CommandHandler("services",   services_cmd))
    app.add_handler(CommandHandler("setservice", setservice_cmd))
    app.add_handler(CommandHandler("resend",     resend_cmd))
    app.add_handler(CommandHandler("otp",        otp_cmd))
    app.add_handler(CommandHandler("voucher",    voucher_cmd))
    app.add_handler(CommandHandler("recheckall", recheckall_cmd))
    app.add_handler(CommandHandler("setbatch",   setbatch_cmd))
    app.add_handler(CommandHandler("vouchers",   vouchers_cmd))
    app.add_handler(CommandHandler("redeem",     redeem_cmd))
    app.add_handler(CommandHandler("delcode",    delcode_cmd))
    app.add_error_handler(error_handler)

    app.post_init = _prefetch_service_id

    logger.info("Bot starting (fully automatic mode)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()