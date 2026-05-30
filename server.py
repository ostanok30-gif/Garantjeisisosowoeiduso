#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Guarantee Bot (ESCROW)
TON + USDT only. No fiat, no cards.
FULL VERSION — Single file.
"""

import asyncio
import atexit
import contextlib
import fcntl
import hashlib
import html
import json
import logging
import logging.handlers
import math
import os
import random
import re
import signal
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from typing import Optional, Tuple, Dict, List, Any
from pathlib import Path

# Telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
from telegram.request import HTTPXRequest
from telegram.error import NetworkError, BadRequest, Conflict, Forbidden

# TON
try:
    from pytoniq import LiteBalancer, WalletV4R2, WalletV5R1
    from pytoniq_core import Address, begin_cell
    HAS_PYTONIQ = True
except ImportError:
    HAS_PYTONIQ = False
    LiteBalancer = None
    WalletV4R2 = None
    WalletV5R1 = None
    Address = None
    begin_cell = None

# Dotenv
try:
    from dotenv import load_dotenv
    load_dotenv()
except:
    pass

# ========== ENVIRONMENT VARIABLES ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "8647879379:AAEA17ZXW3cOBwwjdxkWM90s1Tlv9yrs5R8")
BOT_USERNAME = os.getenv("BOT_USERNAME", "Bahsjsjsjbot")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "8640180536")
ADMIN_IDS = {int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()}
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "ocbob").lstrip("@")
TON_DEPOSIT_ADDRESS = os.getenv("TON_DEPOSIT_ADDRESS", "UQB3dmza7H4Buls_Cwettv4tdnEvbHqzXfz2Z5I7q3tQBVVu")
TON_API_KEY = os.getenv("TON_API_KEY", "986493c43b797405535f702a1b5909eed936c30830d51aa9d80a46e30133ab18")
BOT_WALLET_MNEMONIC = os.getenv("BOT_WALLET_MNEMONIC", "all sea photo pave among approve rubber off stick spell sweet zoo arrest into scale wood height vacuum empty model answer basket energy usual")

# ========== PATHS ==========
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(PROJECT_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
DB_PATH = os.path.join(LOGS_DIR, "bot_data.db")
LOCK_FILE = os.path.join(LOGS_DIR, "bot.lock")

# ========== CONSTANTS ==========
USDT_JETTON_MASTER = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
NANO_TON = 1_000_000_000
USDT_DECIMALS = 6

# Deal limits
MIN_DEAL_TON = 0.5
MAX_DEAL_TON = 1_000_000.0
MIN_DEAL_USDT = 0.5
MAX_DEAL_USDT = 1_000_000.0

# Deposit limits
MIN_DEPOSIT_TON = 0.5
MIN_DEPOSIT_USDT = 0.5

# Withdrawal limits
MIN_WITHDRAW_TON = 0.5
MIN_WITHDRAW_USDT = 0.5

# Commission
COMMISSION_PERCENT = 2

# Timeouts
DEAL_TIMEOUT_MIN = 1440  # 24 hours
DEAL_DISPUTE_AUTO_TIMEOUT_HOURS = 24
DEAL_TIMEOUT_CHECK_INTERVAL_SEC = 60

# Payout
PAYOUT_JETTON_GAS_TON = 0.05
PAYOUT_TON_GAS_RESERVE = 0.2
DAILY_WITHDRAW_CAP_TON = 1000.0
DAILY_WITHDRAW_CAP_USDT = 5000.0

# TON monitoring
TON_POLL_INTERVAL_SEC = 15
TON_POLL_LIMIT = 50
TON_MAINNET = True

# Price cache
PRICE_CACHE_TTL_SEC = 300

# Retry delays for payout
RETRY_DELAYS = [0, 5, 15, 45, 120, 300]

# ========== GLOBAL STATE ==========
user_data: Dict[int, dict] = {}
deals: Dict[str, dict] = {}
_BALANCE_LOCK = threading.Lock()
_WD_USER_LOCKS: Dict[int, threading.Lock] = {}
_DEAL_LOCKS: Dict[str, asyncio.Lock] = {}
_DISPUTE_LOCKS: Dict[str, asyncio.Lock] = {}
_LAST_LT = 0
_LAST_LT_LOADED = False
_lock_file_handle = None
_shutdown_flag = threading.Event()

# ========== LOGGING ==========
log_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOGS_DIR, "bot.log"),
    maxBytes=20_000_000,
    backupCount=10,
    encoding="utf-8"
)
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[log_handler, logging.StreamHandler()])
logger = logging.getLogger(__name__)

# ========== SINGLE INSTANCE LOCK ==========
def acquire_lock():
    global _lock_file_handle
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            os.close(fd)
            return False
        _lock_file_handle = fd
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        os.fsync(fd)
        return True
    except Exception:
        if _lock_file_handle is not None:
            try:
                os.close(_lock_file_handle)
            except:
                pass
            _lock_file_handle = None
        return False

def release_lock():
    global _lock_file_handle
    try:
        if _lock_file_handle is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(_lock_file_handle, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(_lock_file_handle, fcntl.LOCK_UN)
            except:
                pass
            try:
                os.close(_lock_file_handle)
            except:
                pass
            _lock_file_handle = None
        if os.path.isfile(LOCK_FILE):
            try:
                os.remove(LOCK_FILE)
            except:
                pass
    except:
        pass

# ========== DATABASE ==========
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with db_connect() as conn:
        # Users table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                ton_address TEXT,
                balance_currencies TEXT DEFAULT '{}',
                successful_deals INTEGER DEFAULT 0,
                lang TEXT DEFAULT 'ru',
                likes INTEGER DEFAULT 0,
                dislikes INTEGER DEFAULT 0,
                registered_at INTEGER,
                total_volume_usd REAL DEFAULT 0
            )
        ''')
        
        # Deals table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS deals (
                deal_id TEXT PRIMARY KEY,
                amount REAL,
                description TEXT,
                seller_id INTEGER,
                buyer_id INTEGER,
                status TEXT,
                currency TEXT,
                created_at TEXT,
                escrow_collected INTEGER DEFAULT 0,
                seller_voted INTEGER DEFAULT 0,
                buyer_voted INTEGER DEFAULT 0,
                join_notification_sent INTEGER DEFAULT 0
            )
        ''')
        
        # Deposits table (idempotency)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS deposits (
                tx_hash TEXT PRIMARY KEY,
                user_id INTEGER,
                currency TEXT,
                amount REAL,
                created_at INTEGER
            )
        ''')
        
        # Withdrawals table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                currency TEXT,
                amount REAL,
                address TEXT,
                status TEXT DEFAULT 'pending',
                created_at INTEGER,
                processed_at INTEGER,
                tx_hash TEXT,
                error TEXT,
                broadcast_at INTEGER
            )
        ''')
        
        # Settings table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        
        # Workers table (for admin helpers)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS workers (
                user_id INTEGER PRIMARY KEY
            )
        ''')
        
        # Indexes
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deals_seller ON deals(seller_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deals_buyer ON deals(buyer_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deals_status ON deals(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON withdrawals(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status)")
        
        # Partial unique index for pending withdrawals (anti-double-click)
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_withdrawals_pending_unique "
                "ON withdrawals(user_id, currency, amount, address) WHERE status='pending'"
            )
        except sqlite3.OperationalError as e:
            logger.warning(f"Pending unique index: {e}")
    
    logger.info("Database initialized")

def load_data():
    global user_data, deals
    user_data.clear()
    deals.clear()
    
    with db_connect() as conn:
        # Load users
        cur = conn.execute("SELECT user_id, ton_address, balance_currencies, successful_deals, lang, likes, dislikes, registered_at, total_volume_usd FROM users")
        for row in cur.fetchall():
            uid, addr, bc_json, cnt, lang, likes, dislikes, reg, volume = row
            try:
                bc = json.loads(bc_json) if bc_json else {}
            except:
                bc = {"TON": 0.0, "USDT": 0.0}
            user_data[uid] = {
                "ton_address": addr or "",
                "balance_currencies": bc,
                "successful_deals": cnt or 0,
                "lang": lang or "ru",
                "likes": likes or 0,
                "dislikes": dislikes or 0,
                "registered_at": reg or int(time.time()),
                "total_volume_usd": volume or 0.0
            }
        
        # Load deals
        cur = conn.execute("SELECT deal_id, amount, description, seller_id, buyer_id, status, currency, created_at, escrow_collected, seller_voted, buyer_voted, join_notification_sent FROM deals")
        for row in cur.fetchall():
            did, amt, desc, seller, buyer, status, cur, created, escrow, sv, bv, join_sent = row
            deals[did] = {
                "amount": amt,
                "description": desc,
                "seller_id": seller,
                "buyer_id": buyer,
                "status": status,
                "currency": cur,
                "created_at": created,
                "escrow_collected": bool(escrow),
                "seller_voted": bool(sv),
                "buyer_voted": bool(bv),
                "join_notification_sent": bool(join_sent)
            }
    
    logger.info(f"Loaded {len(user_data)} users, {len(deals)} deals")

def save_user(uid: int):
    u = user_data.get(uid, {})
    with db_connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (user_id, ton_address, balance_currencies, successful_deals, lang, likes, dislikes, registered_at, total_volume_usd) VALUES (?,?,?,?,?,?,?,?,?)",
            (uid, 
             u.get("ton_address", ""), 
             json.dumps(u.get("balance_currencies", {})), 
             u.get("successful_deals", 0), 
             u.get("lang", "ru"), 
             u.get("likes", 0), 
             u.get("dislikes", 0), 
             u.get("registered_at", int(time.time())),
             u.get("total_volume_usd", 0.0))
        )

def save_deal(did: str):
    d = deals.get(did, {})
    with db_connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO deals (deal_id, amount, description, seller_id, buyer_id, status, currency, created_at, escrow_collected, seller_voted, buyer_voted, join_notification_sent) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, 
             d.get("amount"), 
             d.get("description"), 
             d.get("seller_id"), 
             d.get("buyer_id"), 
             d.get("status"), 
             d.get("currency"), 
             d.get("created_at"), 
             1 if d.get("escrow_collected") else 0,
             1 if d.get("seller_voted") else 0,
             1 if d.get("buyer_voted") else 0,
             1 if d.get("join_notification_sent") else 0)
        )

def delete_deal(did: str):
    with db_connect() as conn:
        conn.execute("DELETE FROM deals WHERE deal_id = ?", (did,))
    if did in deals:
        del deals[did]

def ensure_user(uid: int):
    if uid not in user_data:
        user_data[uid] = {
            "ton_address": "",
            "balance_currencies": {"TON": 0.0, "USDT": 0.0},
            "successful_deals": 0,
            "lang": "ru",
            "likes": 0,
            "dislikes": 0,
            "registered_at": int(time.time()),
            "total_volume_usd": 0.0
        }
        save_user(uid)

def is_worker(uid: int) -> bool:
    try:
        with db_connect() as conn:
            cur = conn.execute("SELECT 1 FROM workers WHERE user_id = ?", (uid,))
            return cur.fetchone() is not None
    except:
        return False

# ========== BALANCE HELPERS ==========
def get_balance(uid: int, currency: str) -> float:
    ensure_user(uid)
    return float(user_data[uid].get("balance_currencies", {}).get(currency, 0.0))

def add_balance(uid: int, currency: str, amount: float):
    with _BALANCE_LOCK:
        ensure_user(uid)
        cur = user_data[uid].setdefault("balance_currencies", {})
        cur[currency] = cur.get(currency, 0.0) + float(amount)
    save_user(uid)

def sub_balance(uid: int, currency: str, amount: float) -> bool:
    with _BALANCE_LOCK:
        ensure_user(uid)
        bal = user_data[uid].get("balance_currencies", {}).get(currency, 0.0)
        if bal < amount - 1e-9:
            return False
        user_data[uid]["balance_currencies"][currency] = bal - amount
    save_user(uid)
    return True

def get_ton_address(uid: int) -> str:
    ensure_user(uid)
    return user_data[uid].get("ton_address", "")

def set_ton_address(uid: int, addr: str):
    ensure_user(uid)
    user_data[uid]["ton_address"] = addr
    save_user(uid)

def add_successful_deal(uid: int):
    ensure_user(uid)
    user_data[uid]["successful_deals"] = user_data[uid].get("successful_deals", 0) + 1
    save_user(uid)

def add_rating(uid: int, is_like: bool):
    ensure_user(uid)
    if is_like:
        user_data[uid]["likes"] = user_data[uid].get("likes", 0) + 1
    else:
        user_data[uid]["dislikes"] = user_data[uid].get("dislikes", 0) + 1
    save_user(uid)

# ========== DEPOSIT ==========
def deposit_exists(tx_hash: str) -> bool:
    with db_connect() as conn:
        cur = conn.execute("SELECT 1 FROM deposits WHERE tx_hash = ?", (tx_hash,))
        return cur.fetchone() is not None

def record_deposit(tx_hash: str, uid: int, currency: str, amount: float) -> bool:
    with db_connect() as conn:
        try:
            conn.execute("INSERT INTO deposits (tx_hash, user_id, currency, amount, created_at) VALUES (?,?,?,?,?)",
                        (tx_hash, uid, currency, amount, int(time.time())))
            conn.commit()
            add_balance(uid, currency, amount)
            logger.info(f"Deposit: +{amount} {currency} -> user {uid} (tx={tx_hash[:10]})")
            return True
        except sqlite3.IntegrityError:
            logger.warning(f"Duplicate deposit: {tx_hash}")
            return False

# ========== WITHDRAWAL ==========
def create_withdrawal(uid: int, currency: str, amount: float, address: str) -> int:
    lock = _WD_USER_LOCKS.setdefault(uid, threading.Lock())
    with lock:
        # Anti-double-click: check for recent pending
        with db_connect() as conn:
            cur = conn.execute(
                "SELECT 1 FROM withdrawals WHERE user_id=? AND status='pending' AND created_at > ?",
                (uid, int(time.time()) - 30)
            )
            if cur.fetchone():
                raise ValueError("recent_pending")
        
        # Check daily cap
        cap = DAILY_WITHDRAW_CAP_TON if currency == "TON" else DAILY_WITHDRAW_CAP_USDT
        if cap > 0:
            with db_connect() as conn:
                cur = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM withdrawals WHERE user_id=? AND currency=? AND status IN ('pending','sent') AND created_at > ?",
                    (uid, currency, int(time.time()) - 86400)
                )
                day_sum = float(cur.fetchone()[0] or 0)
                if day_sum + amount > cap + 1e-9:
                    raise ValueError("daily_cap_exceeded")
        
        # Check and debit balance
        if not sub_balance(uid, currency, amount):
            raise ValueError("insufficient_funds")
        
        # Create withdrawal record
        with db_connect() as conn:
            cur = conn.execute(
                "INSERT INTO withdrawals (user_id, currency, amount, address, status, created_at) VALUES (?,?,?,?,?,?)",
                (uid, currency, amount, address, "pending", int(time.time()))
            )
            wid = cur.lastrowid
            conn.commit()
            logger.info(f"Withdrawal #{wid}: {amount} {currency} -> {address[:12]}... by user {uid}")
            return wid

def mark_withdrawal_sent(wid: int, tx_hash: str):
    with db_connect() as conn:
        conn.execute("UPDATE withdrawals SET status='sent', processed_at=?, tx_hash=? WHERE id=?", 
                    (int(time.time()), tx_hash, wid))

def mark_withdrawal_error(wid: int, error: str):
    with db_connect() as conn:
        conn.execute("UPDATE withdrawals SET error=? WHERE id=?", (error[:500], wid))

def mark_withdrawal_broadcasting(wid: int) -> bool:
    with db_connect() as conn:
        cur = conn.execute("UPDATE withdrawals SET broadcast_at=? WHERE id=? AND broadcast_at IS NULL", 
                          (int(time.time()), wid))
        return cur.rowcount > 0

def get_pending_withdrawals() -> List[tuple]:
    with db_connect() as conn:
        cur = conn.execute(
            "SELECT id, user_id, currency, amount, address FROM withdrawals WHERE status='pending' AND (error IS NULL OR error='')"
        )
        return cur.fetchall()

def get_stuck_withdrawals() -> List[tuple]:
    with db_connect() as conn:
        cur = conn.execute(
            "SELECT id, user_id, currency, amount, address FROM withdrawals WHERE status='pending' AND broadcast_at IS NOT NULL AND broadcast_at < ? AND (tx_hash IS NULL OR tx_hash='')",
            (int(time.time()) - 3600,)
        )
        return cur.fetchall()

# ========== PRICE (USD) ==========
_price_cache = {"ts": 0.0, "ton_usd": 0.0, "usdt_usd": 1.0}

def _fetch_prices():
    import urllib.request
    url = "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network,tether&vs_currencies=usd"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ForSale/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ton = float((data.get("the-open-network") or {}).get("usd") or 0.0)
        usdt = float((data.get("tether") or {}).get("usd") or 1.0)
        if ton > 0:
            _price_cache["ton_usd"] = ton
            _price_cache["usdt_usd"] = usdt
            _price_cache["ts"] = time.time()
    except Exception as e:
        logger.debug(f"Price fetch failed: {e}")

def _refresh_prices():
    if time.time() - _price_cache["ts"] > PRICE_CACHE_TTL_SEC:
        _fetch_prices()

def ton_usd() -> float:
    _refresh_prices()
    return _price_cache["ton_usd"]

def usdt_usd() -> float:
    _refresh_prices()
    return _price_cache["usdt_usd"]

def to_usd(amount: float, currency: str) -> float:
    if currency == "TON":
        return amount * ton_usd()
    elif currency == "USDT":
        return amount * usdt_usd()
    return 0.0

def format_amount(value: float, currency: str) -> str:
    if currency == "USDT":
        return f"{value:.2f}".rstrip("0").rstrip(".") or "0"
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"

# ========== TEXT MESSAGES WITH PREMIUM EMOJI IDs ==========
TEXTS = {
    "ru": {
        "start": "<tg-emoji emoji-id=\"5118454879039259395\">🤖</tg-emoji> <b>Garate bot</b>\n\n<tg-emoji emoji-id=\"5104960787579929462\">💎</tg-emoji> Комиссия сервиса: {comm}%\n<tg-emoji emoji-id=\"5085022089103016925\">🛟</tg-emoji> Поддержка: @{support}\n\n<tg-emoji emoji-id=\"5134122666331996794\">🛡️</tg-emoji> Ваши средства под защитой.",
        "menu": "<tg-emoji emoji-id=\"5118454879039259395\">🤖</tg-emoji> <b>Меню</b>",
        "create_deal": "<tg-emoji emoji-id=\"5118454879039259395\">🤖</tg-emoji> <b>Создать сделку</b>\n\nВыберите подходящий вариант:",
        "enter_amount": "<tg-emoji emoji-id=\"5172834782823842584\">💎</tg-emoji> <b>Сумма сделки</b>\n\nВведи количество {currency}:",
        "enter_desc": "<tg-emoji emoji-id=\"4918327330239152795\">📝</tg-emoji> <b>Описание</b>\n\nОпишите, что продаёте:",
        "deal_created": "<tg-emoji emoji-id=\"4916036072560919511\">✅</tg-emoji> <b>Сделка создана!</b>\n\n<tg-emoji emoji-id=\"5116648080787112958\">💰</tg-emoji> Сумма: {amount} {currency}\n<tg-emoji emoji-id=\"5134472688986756318\">📦</tg-emoji> {desc}\n\n<tg-emoji emoji-id=\"5116113383128564448\">🔗</tg-emoji> <b>Ссылка для покупателя:</b>\nhttps://t.me/{bot_username}?start={deal_id}",
        "deal_info": "<tg-emoji emoji-id=\"5104960787579929462\">💎</tg-emoji> <b>Детали сделки:</b>\n\n<tg-emoji emoji-id=\"4904848288345228262\">👤</tg-emoji> Продавец: @{seller}\n<tg-emoji emoji-id=\"5116648080787112958\">💰</tg-emoji> Сумма: {amount} {currency}\n<tg-emoji emoji-id=\"4915853119839011973\">📦</tg-emoji> Товар: {desc}\n\n<tg-emoji emoji-id=\"4911656069207426158\">💸</tg-emoji> Оплата с внутреннего баланса",
        "insufficient": "<tg-emoji emoji-id=\"5121063440311386962\">❌</tg-emoji> <b>Не хватает средств</b>\n\n<tg-emoji emoji-id=\"5118686540985271080\">💰</tg-emoji> Ваш баланс: {balance} {currency}",
        "payment_ok": "<tg-emoji emoji-id=\"5123163417326126159\">✅</tg-emoji> <b>Оплата подтверждена!</b>\n\nОжидай передачи товара.",
        "seller_payment": "<tg-emoji emoji-id=\"4915853119839011973\">📦</tg-emoji> <b>Оплата подтверждена.</b>\n\n<tg-emoji emoji-id=\"4904848288345228262\">👤</tg-emoji> Покупатель: @{buyer}\n<tg-emoji emoji-id=\"4915853119839011973\">📦</tg-emoji> Товар: {desc}\n\n<tg-emoji emoji-id=\"5116113383128564448\">🔗</tg-emoji> Передай товар и нажми кнопку",
        "seller_sent": "<tg-emoji emoji-id=\"5123163417326126159\">✅</tg-emoji> <b>Товар отмечен как переданный</b>\n\nОжидаем подтверждения от покупателя.",
        "buyer_notify": "<tg-emoji emoji-id=\"5085022089103016925\">🛟</tg-emoji> <b>Продавец передал товар!</b>\n\nПроверь и подтверди получение:",
        "deal_completed": "<tg-emoji emoji-id=\"5116309444090661129\">🏁</tg-emoji> <b>Сделка завершена!</b>\n\n<tg-emoji emoji-id=\"5116648080787112958\">💰</tg-emoji> Продавцу зачислено {amount} {currency} (комиссия {comm}%)\n\n<tg-emoji emoji-id=\"5116445341150872576\">💎</tg-emoji> Средства на внутреннем балансе",
        "rate_seller": "<tg-emoji emoji-id=\"5116163917713769254\">⭐</tg-emoji> <b>Оцени продавца:</b>",
        "rate_buyer": "<tg-emoji emoji-id=\"5116163917713769254\">⭐</tg-emoji> <b>Оцени покупателя:</b>",
        "deal_cancelled": "<tg-emoji emoji-id=\"5121063440311386962\">❌</tg-emoji> <b>Сделка отменена</b>\n\n<tg-emoji emoji-id=\"5116648080787112958\">💰</tg-emoji> Средства возвращены покупателю.",
        "wallet": "<tg-emoji emoji-id=\"5116093437300442328\">💎</tg-emoji> <b>Кошелёк</b>\n\n<tg-emoji emoji-id=\"4902715076873553054\">🔹</tg-emoji> TON: {ton}\n<tg-emoji emoji-id=\"5116648080787112958\">💰</tg-emoji> USDT: {usdt}\n\n<tg-emoji emoji-id=\"5116204921766544244\">📤</tg-emoji> Адрес для вывода: {addr}",
        "deposit_ton": "<tg-emoji emoji-id=\"5116395218882528029\">📥</tg-emoji> <b>Пополнение TON</b>\n\nОтправь TON на адрес:\n{address}\n\n<tg-emoji emoji-id=\"5116275208906343429\">❗️</tg-emoji> <b>ОБЯЗАТЕЛЬНО</b> укажи в комментарии:\nuser_{uid}\n\n<tg-emoji emoji-id=\"4902715076873553054\">🔹</tg-emoji> Минимум: {min_ton} TON",
        "deposit_usdt": "<tg-emoji emoji-id=\"5116395218882528029\">📥</tg-emoji> <b>Пополнение USDT (сеть TON)</b>\n\nОтправь USDT на адрес:\n{address}\n\n<tg-emoji emoji-id=\"5116275208906343429\">❗️</tg-emoji> <b>ОБЯЗАТЕЛЬНО</b> укажи в комментарии:\nuser_{uid}\n\n<tg-emoji emoji-id=\"4902715076873553054\">🔹</tg-emoji> Минимум: {min_usdt} USDT",
        "withdraw": "<tg-emoji emoji-id=\"4904500559203009298\">💸</tg-emoji> <b>Вывод средств</b>\n\nВыбери валюту:",
        "withdraw_ton": "<tg-emoji emoji-id=\"4902715076873553054\">🔹</tg-emoji> <b>Вывод TON</b>\n\nВведи сумму (мин. {min} TON):",
        "withdraw_usdt": "<tg-emoji emoji-id=\"4902715076873553054\">🔹</tg-emoji> <b>Вывод USDT</b>\n\nВведи сумму (мин. {min} USDT):",
        "withdraw_addr": "<tg-emoji emoji-id=\"5116395218882528029\">📥</tg-emoji> <b>Адрес получателя</b>\n\nВведи TON-адрес (EQ/UQ...):",
        "withdraw_submitted": "<tg-emoji emoji-id=\"5116395218882528029\">📥</tg-emoji> <b>Заявка на вывод принята</b>\n\n💸 {amount} {currency} → {address}\n\nТранзакция в обработке, ожидайте.",
        "profile": "<tg-emoji emoji-id=\"4904848288345228262\">👤</tg-emoji> <b>Профиль:</b>\n\n<tg-emoji emoji-id=\"5084613633418199991\">🆔</tg-emoji> ID: {uid}\n<tg-emoji emoji-id=\"5123163417326126159\">✅</tg-emoji> Сделок: {deals}\n<tg-emoji emoji-id=\"4915896438879159184\">⭐</tg-emoji> Рейтинг: +{likes} / -{dislikes}\n\n<tg-emoji emoji-id=\"4902715076873553054\">🔹</tg-emoji> TON: {ton}\n<tg-emoji emoji-id=\"5116648080787112958\">💰</tg-emoji> USDT: {usdt}",
        "set_address": "<tg-emoji emoji-id=\"4916086774649848789\">🔗</tg-emoji> <b>Привязка TON-адреса</b>\n\nОтправь свой TON-адрес (EQ или UQ):",
        "addr_saved": "<tg-emoji emoji-id=\"5123163417326126159\">✅</tg-emoji> <b>Адрес сохранён</b>\n{addr}",
        "my_deals": "<tg-emoji emoji-id=\"5118686540985271080\">💰</tg-emoji> <b>Мои сделки</b>",
        "no_deals": "📭 Нет сделок",
        "deal_item": "{num}. {amount} {currency} | {status}",
        "deal_status_active": "Активна",
        "deal_status_confirmed": "Оплачена",
        "deal_status_seller_sent": "Товар передан",
        "deal_status_completed": "Завершена",
        "deal_status_cancelled": "Отменена",
        "error_network": "<tg-emoji emoji-id=\"4906943755644306322\">🌐</tg-emoji> Ошибка сети, повтори позже",
        "withdraw_no_addr": "<tg-emoji emoji-id=\"5121063440311386962\">❌</tg-emoji> Сначала привяжи TON-адрес в меню",
        "deposit_disabled": "⚠️ Пополнение временно недоступно",
        "unknown": "❓ Неизвестная команда",
        "back": "Назад",
        "menu_btn": "Меню",
        "create_deal_btn": "Создать сделку",
        "profile_btn": "Профиль",
        "wallet_btn": "Кошелёк",
        "deals_btn": "Сделки",
        "address_btn": "Адрес",
        "deposit_btn": "Внести",
        "withdraw_btn": "Вывести",
        "ton_btn": "TON",
        "usdt_btn": "USDT",
        "pay_btn": "Оплатить",
        "confirm_sent_btn": "Товар передан",
        "confirm_received_btn": "Получил",
        "cancel_btn": "Отменить",
        "liked_btn": "Хорошо",
        "disliked_btn": "Плохо",
        "open_dispute_btn": "Открыть спор",
        "admin_panel": "Админ-панель",
        "admin_stats": "Статистика",
        "admin_balance": "Баланс",
        "admin_wallet": "Кошелёк",
        "admin_withdrawals": "Выводы",
        "admin_disputes": "Споры",
        "admin_back": "Назад",
        "admin_stats_message": "📊 <b>Статистика</b>\n\n👥 Пользователей: {users}\n📋 Сделок: {deals}\n✅ Завершено: {completed}\n❌ Отменено: {cancelled}\n⏳ Активно: {active}\n\n💰 Объём (USD): {volume}$",
        "admin_balance_ask": "💰 <b>Изменение баланса</b>\n\nВведи: <code>user_id сумма TON|USDT</code>\nПример: <code>12345 10 TON</code>",
        "admin_balance_success": "✅ Баланс пользователя <code>{uid}</code> изменён\n{currency}: <code>{amount}</code>",
        "admin_wallet_info": "🏦 <b>Кошелёк бота</b>\n\nАдрес для пополнений:\n<code>{address}</code>\n\nБаланс:\nTON: <code>{ton}</code>\nUSDT: <code>{usdt}</code>",
        "admin_withdrawals_list": "📤 <b>Заявки на вывод</b>\n\n{wds}",
        "admin_withdrawal_item": "#{id} | {amount} {currency}\nПользователь: <code>{user_id}</code>\nАдрес: <code>{address}</code>\nСоздана: {created}",
        "admin_withdrawal_none": "Нет активных заявок",
        "admin_disputes_list": "⚠️ <b>Открытые споры</b>\n\n{disputes}",
        "admin_dispute_item": "#{deal_id} | {amount} {currency}\nПродавец: <code>{seller}</code>\nПокупатель: <code>{buyer}</code>",
        "admin_dispute_none": "Нет открытых споров",
        "dispute_opened": "⚠️ <b>Спор открыт!</b>\n\nСделка <code>{deal_id}</code> заблокирована. Администратор разберётся.",
        "dispute_already": "❌ Спор по этой сделке уже открыт",
        "dispute_cannot": "❌ Нельзя открыть спор для этого статуса сделки",
        "dispute_resolved_seller": "⚖️ <b>Спор разрешён в пользу продавца</b>\n\nСредства переведены продавцу.",
        "dispute_resolved_buyer": "⚖️ <b>Спор разрешён в пользу покупателя</b>\n\nСредства возвращены покупателю.",
    },
    "en": {
        "start": "<tg-emoji emoji-id=\"5118454879039259395\">🤖</tg-emoji> <b>Garate bot</b>\n\n<tg-emoji emoji-id=\"5104960787579929462\">💎</tg-emoji> Commission: {comm}%\n<tg-emoji emoji-id=\"5085022089103016925\">🛟</tg-emoji> Support: @{support}\n\n<tg-emoji emoji-id=\"5134122666331996794\">🛡️</tg-emoji> Your funds are protected.",
        "menu": "<tg-emoji emoji-id=\"5118454879039259395\">🤖</tg-emoji> <b>Menu</b>",
        "create_deal": "<tg-emoji emoji-id=\"5118454879039259395\">🤖</tg-emoji> <b>Create deal</b>\n\nChoose option:",
        "enter_amount": "<tg-emoji emoji-id=\"5172834782823842584\">💎</tg-emoji> <b>Deal amount</b>\n\nEnter amount in {currency}:",
        "enter_desc": "<tg-emoji emoji-id=\"4918327330239152795\">📝</tg-emoji> <b>Description</b>\n\nDescribe what you're selling:",
        "deal_created": "<tg-emoji emoji-id=\"4916036072560919511\">✅</tg-emoji> <b>Deal created!</b>\n\n<tg-emoji emoji-id=\"5116648080787112958\">💰</tg-emoji> Amount: {amount} {currency}\n<tg-emoji emoji-id=\"5134472688986756318\">📦</tg-emoji> {desc}\n\n<tg-emoji emoji-id=\"5116113383128564448\">🔗</tg-emoji> <b>Buyer link:</b>\nhttps://t.me/{bot_username}?start={deal_id}",
        "deal_info": "<tg-emoji emoji-id=\"5104960787579929462\">💎</tg-emoji> <b>Deal details:</b>\n\n<tg-emoji emoji-id=\"4904848288345228262\">👤</tg-emoji> Seller: @{seller}\n<tg-emoji emoji-id=\"5116648080787112958\">💰</tg-emoji> Amount: {amount} {currency}\n<tg-emoji emoji-id=\"4915853119839011973\">📦</tg-emoji> Item: {desc}\n\n<tg-emoji emoji-id=\"4911656069207426158\">💸</tg-emoji> Pay from internal balance",
        "insufficient": "<tg-emoji emoji-id=\"5121063440311386962\">❌</tg-emoji> <b>Insufficient balance</b>\n\n<tg-emoji emoji-id=\"5118686540985271080\">💰</tg-emoji> Your balance: {balance} {currency}",
        "payment_ok": "<tg-emoji emoji-id=\"5123163417326126159\">✅</tg-emoji> <b>Payment confirmed!</b>\n\nWait for delivery.",
        "seller_payment": "<tg-emoji emoji-id=\"4915853119839011973\">📦</tg-emoji> <b>Payment confirmed.</b>\n\n<tg-emoji emoji-id=\"4904848288345228262\">👤</tg-emoji> Buyer: @{buyer}\n<tg-emoji emoji-id=\"4915853119839011973\">📦</tg-emoji> Item: {desc}\n\n<tg-emoji emoji-id=\"5116113383128564448\">🔗</tg-emoji> Deliver item and click button",
        "seller_sent": "<tg-emoji emoji-id=\"5123163417326126159\">✅</tg-emoji> <b>Item marked as delivered</b>\n\nWaiting for buyer confirmation.",
        "buyer_notify": "<tg-emoji emoji-id=\"5085022089103016925\">🛟</tg-emoji> <b>Seller delivered the item!</b>\n\nCheck and confirm receipt:",
        "deal_completed": "<tg-emoji emoji-id=\"5116309444090661129\">🏁</tg-emoji> <b>Deal completed!</b>\n\n<tg-emoji emoji-id=\"5116648080787112958\">💰</tg-emoji> Seller received {amount} {currency} ({comm}% fee)\n\n<tg-emoji emoji-id=\"5116445341150872576\">💎</tg-emoji> Funds on internal balance",
        "rate_seller": "<tg-emoji emoji-id=\"5116163917713769254\">⭐</tg-emoji> <b>Rate seller:</b>",
        "rate_buyer": "<tg-emoji emoji-id=\"5116163917713769254\">⭐</tg-emoji> <b>Rate buyer:</b>",
        "deal_cancelled": "<tg-emoji emoji-id=\"5121063440311386962\">❌</tg-emoji> <b>Deal cancelled</b>\n\n<tg-emoji emoji-id=\"5116648080787112958\">💰</tg-emoji> Funds returned to buyer.",
        "wallet": "<tg-emoji emoji-id=\"5116093437300442328\">💎</tg-emoji> <b>Wallet</b>\n\n<tg-emoji emoji-id=\"4902715076873553054\">🔹</tg-emoji> TON: {ton}\n<tg-emoji emoji-id=\"5116648080787112958\">💰</tg-emoji> USDT: {usdt}\n\n<tg-emoji emoji-id=\"5116204921766544244\">📤</tg-emoji> Withdrawal address: {addr}",
        "deposit_ton": "<tg-emoji emoji-id=\"5116395218882528029\">📥</tg-emoji> <b>Deposit TON</b>\n\nSend TON to address:\n{address}\n\n<tg-emoji emoji-id=\"5116275208906343429\">❗️</tg-emoji> <b>MUST</b> include comment:\nuser_{uid}\n\n<tg-emoji emoji-id=\"4902715076873553054\">🔹</tg-emoji> Minimum: {min_ton} TON",
        "deposit_usdt": "<tg-emoji emoji-id=\"5116395218882528029\">📥</tg-emoji> <b>Deposit USDT (TON network)</b>\n\nSend USDT to address:\n{address}\n\n<tg-emoji emoji-id=\"5116275208906343429\">❗️</tg-emoji> <b>MUST</b> include comment:\nuser_{uid}\n\n<tg-emoji emoji-id=\"4902715076873553054\">🔹</tg-emoji> Minimum: {min_usdt} USDT",
        "withdraw": "<tg-emoji emoji-id=\"4904500559203009298\">💸</tg-emoji> <b>Withdraw</b>\n\nChoose currency:",
        "withdraw_ton": "<tg-emoji emoji-id=\"4902715076873553054\">🔹</tg-emoji> <b>Withdraw TON</b>\n\nEnter amount (min {min} TON):",
        "withdraw_usdt": "<tg-emoji emoji-id=\"4902715076873553054\">🔹</tg-emoji> <b>Withdraw USDT</b>\n\nEnter amount (min {min} USDT):",
        "withdraw_addr": "<tg-emoji emoji-id=\"5116395218882528029\">📥</tg-emoji> <b>Recipient address</b>\n\nEnter TON address (EQ/UQ...):",
        "withdraw_submitted": "<tg-emoji emoji-id=\"5116395218882528029\">📥</tg-emoji> <b>Withdrawal request accepted</b>\n\n💸 {amount} {currency} → {address}\n\nTransaction in progress.",
        "profile": "<tg-emoji emoji-id=\"4904848288345228262\">👤</tg-emoji> <b>Profile:</b>\n\n<tg-emoji emoji-id=\"5084613633418199991\">🆔</tg-emoji> ID: {uid}\n<tg-emoji emoji-id=\"5123163417326126159\">✅</tg-emoji> Deals: {deals}\n<tg-emoji emoji-id=\"4915896438879159184\">⭐</tg-emoji> Rating: +{likes} / -{dislikes}\n\n<tg-emoji emoji-id=\"4902715076873553054\">🔹</tg-emoji> TON: {ton}\n<tg-emoji emoji-id=\"5116648080787112958\">💰</tg-emoji> USDT: {usdt}",
        "set_address": "<tg-emoji emoji-id=\"4916086774649848789\">🔗</tg-emoji> <b>Link TON address</b>\n\nSend your TON address (EQ or UQ):",
        "addr_saved": "<tg-emoji emoji-id=\"5123163417326126159\">✅</tg-emoji> <b>Address saved</b>\n{addr}",
        "my_deals": "<tg-emoji emoji-id=\"5118686540985271080\">💰</tg-emoji> <b>My deals</b>",
        "no_deals": "📭 No deals",
        "deal_item": "{num}. {amount} {currency} | {status}",
        "deal_status_active": "Active",
        "deal_status_confirmed": "Paid",
        "deal_status_seller_sent": "Item sent",
        "deal_status_completed": "Completed",
        "deal_status_cancelled": "Cancelled",
        "error_network": "<tg-emoji emoji-id=\"4906943755644306322\">🌐</tg-emoji> Network error, try again",
        "withdraw_no_addr": "<tg-emoji emoji-id=\"5121063440311386962\">❌</tg-emoji> Link your TON address first",
        "deposit_disabled": "⚠️ Deposits temporarily unavailable",
        "unknown": "❓ Unknown command",
        "back": "Back",
        "menu_btn": "Menu",
        "create_deal_btn": "Create deal",
        "profile_btn": "Profile",
        "wallet_btn": "Wallet",
        "deals_btn": "Deals",
        "address_btn": "Address",
        "deposit_btn": "Deposit",
        "withdraw_btn": "Withdraw",
        "ton_btn": "TON",
        "usdt_btn": "USDT",
        "pay_btn": "Pay",
        "confirm_sent_btn": "Item delivered",
        "confirm_received_btn": "Received",
        "cancel_btn": "Cancel",
        "liked_btn": "Good",
        "disliked_btn": "Bad",
        "open_dispute_btn": "Open dispute",
        "admin_panel": "Admin panel",
        "admin_stats": "Statistics",
        "admin_balance": "Balance",
        "admin_wallet": "Wallet",
        "admin_withdrawals": "Withdrawals",
        "admin_disputes": "Disputes",
        "admin_back": "Back",
        "admin_stats_message": "📊 <b>Statistics</b>\n\n👥 Users: {users}\n📋 Deals: {deals}\n✅ Completed: {completed}\n❌ Cancelled: {cancelled}\n⏳ Active: {active}\n\n💰 Volume (USD): {volume}$",
        "admin_balance_ask": "💰 <b>Change balance</b>\n\nEnter: <code>user_id amount TON|USDT</code>\nExample: <code>12345 10 TON</code>",
        "admin_balance_success": "✅ User <code>{uid}</code> balance updated\n{currency}: <code>{amount}</code>",
        "admin_wallet_info": "🏦 <b>Bot wallet</b>\n\nDeposit address:\n<code>{address}</code>\n\nBalance:\nTON: <code>{ton}</code>\nUSDT: <code>{usdt}</code>",
        "admin_withdrawals_list": "📤 <b>Withdrawal requests</b>\n\n{wds}",
        "admin_withdrawal_item": "#{id} | {amount} {currency}\nUser: <code>{user_id}</code>\nAddress: <code>{address}</code>\nCreated: {created}",
        "admin_withdrawal_none": "No active requests",
        "admin_disputes_list": "⚠️ <b>Open disputes</b>\n\n{disputes}",
        "admin_dispute_item": "#{deal_id} | {amount} {currency}\nSeller: <code>{seller}</code>\nBuyer: <code>{buyer}</code>",
        "admin_dispute_none": "No open disputes",
        "dispute_opened": "⚠️ <b>Dispute opened!</b>\n\nDeal <code>{deal_id}</code> locked. Admin will resolve.",
        "dispute_already": "❌ Dispute already open for this deal",
        "dispute_cannot": "❌ Cannot open dispute for this deal status",
        "dispute_resolved_seller": "⚖️ <b>Dispute resolved in favor of seller</b>\n\nFunds transferred to seller.",
        "dispute_resolved_buyer": "⚖️ <b>Dispute resolved in favor of buyer</b>\n\nFunds returned to buyer.",
    }
}

def get_text(uid: int, key: str, **kwargs) -> str:
    lang = user_data.get(uid, {}).get("lang", "ru")
    text = TEXTS[lang].get(key, TEXTS["ru"].get(key, key))
    try:
        return text.format(**kwargs)
    except:
        return text

# ========== KEYBOARDS (NO EMOJI ON BUTTONS) ==========
def main_menu(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(get_text(uid, "create_deal_btn"), callback_data="create_deal")],
        [InlineKeyboardButton(get_text(uid, "profile_btn"), callback_data="profile"),
         InlineKeyboardButton(get_text(uid, "wallet_btn"), callback_data="wallet")],
    ])

def profile_menu(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(get_text(uid, "deals_btn"), callback_data="my_deals"),
         InlineKeyboardButton(get_text(uid, "address_btn"), callback_data="set_address")],
        [InlineKeyboardButton(get_text(uid, "back"), callback_data="menu")],
    ])

def wallet_menu(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(get_text(uid, "deposit_btn"), callback_data="deposit"),
         InlineKeyboardButton(get_text(uid, "withdraw_btn"), callback_data="withdraw")],
        [InlineKeyboardButton(get_text(uid, "back"), callback_data="menu")],
    ])

def currency_select(uid: int, action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("TON", callback_data=f"{action}_TON"),
         InlineKeyboardButton("USDT", callback_data=f"{action}_USDT")],
        [InlineKeyboardButton(get_text(uid, "back"), callback_data="wallet" if action == "deposit" else "withdraw_back")],
    ])

def deal_currency_select(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("TON", callback_data="deal_currency_TON"),
         InlineKeyboardButton("USDT", callback_data="deal_currency_USDT")],
        [InlineKeyboardButton(get_text(uid, "back"), callback_data="menu")],
    ])

def back_button(uid: int, callback: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(get_text(uid, "back"), callback_data=callback)]])

def deal_buttons(uid: int, deal_id: str, status: str, role: str) -> InlineKeyboardMarkup:
    buttons = []
    if status == "confirmed" and role == "seller":
        buttons.append([InlineKeyboardButton(get_text(uid, "confirm_sent_btn"), callback_data=f"confirm_sent_{deal_id}")])
    elif status == "seller_sent" and role == "buyer":
        buttons.append([InlineKeyboardButton(get_text(uid, "confirm_received_btn"), callback_data=f"confirm_received_{deal_id}")])
    if status in ["active", "confirmed", "seller_sent"] and role in ["seller", "buyer"]:
        buttons.append([InlineKeyboardButton(get_text(uid, "cancel_btn"), callback_data=f"cancel_deal_{deal_id}")])
    if status not in ["completed", "cancelled"] and role in ["seller", "buyer"]:
        buttons.append([InlineKeyboardButton(get_text(uid, "open_dispute_btn"), callback_data=f"open_dispute_{deal_id}")])
    buttons.append([InlineKeyboardButton(get_text(uid, "back"), callback_data="my_deals")])
    return InlineKeyboardMarkup(buttons)

def rating_buttons(uid: int, deal_id: str, target_role: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(get_text(uid, "liked_btn"), callback_data=f"rate_{deal_id}_{target_role}_up"),
         InlineKeyboardButton(get_text(uid, "disliked_btn"), callback_data=f"rate_{deal_id}_{target_role}_down")],
        [InlineKeyboardButton(get_text(uid, "menu_btn"), callback_data="menu")],
    ])

def admin_menu(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(get_text(uid, "admin_stats"), callback_data="admin_stats"),
         InlineKeyboardButton(get_text(uid, "admin_wallet"), callback_data="admin_wallet")],
        [InlineKeyboardButton(get_text(uid, "admin_balance"), callback_data="admin_balance"),
         InlineKeyboardButton(get_text(uid, "admin_withdrawals"), callback_data="admin_withdrawals")],
        [InlineKeyboardButton(get_text(uid, "admin_disputes"), callback_data="admin_disputes")],
        [InlineKeyboardButton(get_text(uid, "back"), callback_data="menu")],
    ])

# ========== HELPER FUNCTIONS ==========
def get_deal_status_text(uid: int, status: str) -> str:
    status_map = {
        "active": "deal_status_active",
        "confirmed": "deal_status_confirmed",
        "seller_sent": "deal_status_seller_sent",
        "completed": "deal_status_completed",
        "cancelled": "deal_status_cancelled",
        "disputed": "deal_status_cancelled",
    }
    return get_text(uid, status_map.get(status, "deal_status_active"))

def format_deal_item(num: int, amount: float, currency: str, status: str, uid: int) -> str:
    status_text = get_deal_status_text(uid, status)
    return f"{num}. {format_amount(amount, currency)} {currency} | {status_text}"

def get_user_name(uid: int) -> str:
    return f"user{uid}"

async def get_telegram_username(context, uid: int) -> str:
    try:
        chat = await context.bot.get_chat(uid)
        if chat.username:
            return chat.username
        first = chat.first_name or ""
        last = chat.last_name or ""
        if first or last:
            return f"{first} {last}".strip()
        return get_user_name(uid)
    except:
        return get_user_name(uid)

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def is_deal_participant(deal: dict, uid: int) -> bool:
    return uid == deal.get("seller_id") or uid == deal.get("buyer_id")

def get_deal_role(deal: dict, uid: int) -> Optional[str]:
    if uid == deal.get("seller_id"):
        return "seller"
    elif uid == deal.get("buyer_id"):
        return "buyer"
    return None

def resolve_deal_id(short_id: str) -> Optional[str]:
    if not short_id:
        return None
    short_id = short_id.strip().lstrip("#").lower()
    if short_id in deals:
        return short_id
    if len(short_id) >= 6:
        matches = [did for did in deals if did.startswith(short_id)]
        if len(matches) == 1:
            return matches[0]
    return None

def format_created_at(created_str: Optional[str]) -> str:
    if not created_str:
        return "-"
    try:
        dt = datetime.strptime(created_str, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d.%m.%Y %H:%M")
    except:
        return created_str[:16]

# ========== TON DEPOSIT MONITOR ==========
def _api_base_v3() -> str:
    return "https://toncenter.com/api/v3" if TON_MAINNET else "https://testnet.toncenter.com/api/v3"

def _normalize_addr_raw(addr: str) -> str:
    if not addr:
        return ""
    s = addr.strip()
    if ":" in s:
        wc, rest = s.split(":", 1)
        if len(rest) == 64:
            return f"{wc}:{rest.lower()}"
    return s

def _parse_user_id_from_memo(comment: Optional[str]) -> Optional[int]:
    if not comment:
        return None
    match = re.search(r"user[_\s-]?(\d+)", comment, re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except:
            return None
    return None

def _fetch_actions(start_lt: int = 0) -> List[dict]:
    if not TON_DEPOSIT_ADDRESS or not TON_API_KEY:
        return []
    params = [
        ("account", TON_DEPOSIT_ADDRESS),
        ("action_type", "ton_transfer"),
        ("action_type", "jetton_transfer"),
        ("sort", "asc"),
        ("limit", str(TON_POLL_LIMIT)),
    ]
    if start_lt > 0:
        params.append(("start_lt", str(start_lt)))
    url = _api_base_v3() + "/actions?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "ForSale/1.0", "X-API-Key": TON_API_KEY, "accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("actions") or []
    except Exception as e:
        logger.warning(f"Fetch actions failed: {e}")
        return []

def _action_lt(action: dict) -> int:
    raw = action.get("end_lt") or action.get("start_lt") or "0"
    try:
        return int(str(raw))
    except:
        return 0

def _extract_ton_transfer(action: dict) -> Optional[Tuple[str, int, float]]:
    if not action.get("success", True):
        return None
    d = action.get("details") or {}
    dest_raw = _normalize_addr_raw(d.get("destination") or "")
    our_raw = _normalize_addr_raw(TON_DEPOSIT_ADDRESS)
    if dest_raw and our_raw and dest_raw != our_raw:
        return None
    src_raw = _normalize_addr_raw(d.get("source") or "")
    if src_raw and our_raw and src_raw == our_raw:
        return None
    if d.get("encrypted"):
        return None
    try:
        value_nano = int(str(d.get("value") or "0"))
    except:
        return None
    if value_nano <= 0:
        return None
    amount = value_nano / NANO_TON
    if amount < MIN_DEPOSIT_TON:
        return None
    user_id = _parse_user_id_from_memo(d.get("comment") or "")
    if user_id is None:
        return None
    txs = action.get("transactions") or []
    tx_hash = (txs[0] if txs else "").strip()
    if not tx_hash:
        return None
    return tx_hash, user_id, amount

def _extract_jetton_transfer(action: dict) -> Optional[Tuple[str, int, float]]:
    if not action.get("success", True):
        return None
    d = action.get("details") or {}
    asset = _normalize_addr_raw(d.get("asset") or "")
    usdt_raw = _normalize_addr_raw(USDT_JETTON_MASTER)
    if asset != usdt_raw:
        return None
    receiver_raw = _normalize_addr_raw(d.get("receiver") or "")
    our_raw = _normalize_addr_raw(TON_DEPOSIT_ADDRESS)
    if receiver_raw and our_raw and receiver_raw != our_raw:
        return None
    if d.get("is_encrypted_comment"):
        return None
    try:
        amt_int = int(str(d.get("amount") or "0"))
    except:
        return None
    if amt_int <= 0:
        return None
    amount = amt_int / (10 ** USDT_DECIMALS)
    if amount < MIN_DEPOSIT_USDT:
        return None
    user_id = _parse_user_id_from_memo(d.get("comment") or "")
    if user_id is None:
        return None
    txs = action.get("transactions") or []
    tx_hash = (txs[0] if txs else "").strip()
    if not tx_hash:
        return None
    return tx_hash, user_id, amount

def _seed_cursor() -> int:
    if not TON_DEPOSIT_ADDRESS or not TON_API_KEY:
        return 0
    params = [
        ("account", TON_DEPOSIT_ADDRESS),
        ("action_type", "ton_transfer"),
        ("action_type", "jetton_transfer"),
        ("sort", "desc"),
        ("limit", "1"),
    ]
    url = _api_base_v3() + "/actions?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "ForSale/1.0", "X-API-Key": TON_API_KEY, "accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        actions = data.get("actions") or []
        if actions:
            return _action_lt(actions[0])
    except Exception as e:
        logger.warning(f"Seed cursor failed: {e}")
    return 0

def _load_last_lt() -> int:
    try:
        with db_connect() as conn:
            cur = conn.execute("SELECT value FROM bot_settings WHERE key = 'ton_monitor_last_lt'")
            row = cur.fetchone()
            if row and row[0]:
                return int(row[0])
    except:
        pass
    return 0

def _save_last_lt(lt: int):
    try:
        with db_connect() as conn:
            conn.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", 
                        ("ton_monitor_last_lt", str(lt)))
    except:
        pass

def _process_deposits_once():
    global _LAST_LT, _LAST_LT_LOADED
    if not _LAST_LT_LOADED:
        _LAST_LT = _load_last_lt()
        if _LAST_LT == 0:
            seeded = _seed_cursor()
            if seeded > 0:
                _LAST_LT = seeded
                _save_last_lt(seeded)
                logger.info(f"Deposit monitor seeded at LT {seeded}")
        _LAST_LT_LOADED = True
    
    actions = _fetch_actions(start_lt=_LAST_LT if _LAST_LT > 0 else 0)
    max_lt = _LAST_LT
    for action in actions:
        lt = _action_lt(action)
        if lt > max_lt:
            max_lt = lt
        atype = action.get("type", "")
        if atype == "ton_transfer":
            parsed = _extract_ton_transfer(action)
            if parsed:
                tx_hash, user_id, amount = parsed
                if not deposit_exists(tx_hash):
                    if record_deposit(tx_hash, user_id, "TON", amount):
                        logger.info(f"Deposit: +{amount} TON -> user {user_id}")
        elif atype == "jetton_transfer":
            parsed = _extract_jetton_transfer(action)
            if parsed:
                tx_hash, user_id, amount = parsed
                if not deposit_exists(tx_hash):
                    if record_deposit(tx_hash, user_id, "USDT", amount):
                        logger.info(f"Deposit: +{amount} USDT -> user {user_id}")
    if max_lt > _LAST_LT:
        _LAST_LT = max_lt
        _save_last_lt(max_lt)

def deposit_monitor_loop():
    global _shutdown_flag
    while not _shutdown_flag.is_set():
        try:
            _process_deposits_once()
        except Exception as e:
            logger.warning(f"Deposit monitor error: {e}")
        _shutdown_flag.wait(timeout=TON_POLL_INTERVAL_SEC)

# ========== EXPIRY WATCHER ==========
def _parse_deal_created(created_str: Optional[str]) -> Optional[float]:
    if not created_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(created_str, fmt).timestamp()
        except:
            continue
    return None

def _cancel_expired_deals(application):
    timeout_sec = DEAL_TIMEOUT_MIN * 60
    cutoff = time.time() - timeout_sec
    to_cancel = []
    for did, deal in deals.items():
        if deal.get("status") != "active":
            continue
        created_ts = _parse_deal_created(deal.get("created_at"))
        if created_ts and created_ts <= cutoff:
            to_cancel.append(did)
    
    for did in to_cancel:
        deal = deals.get(did)
        if not deal or deal.get("status") != "active":
            continue
        # Refund if there's a buyer (shouldn't happen, but just in case)
        if deal.get("escrow_collected") and deal.get("buyer_id"):
            add_balance(deal["buyer_id"], deal["currency"], deal["amount"])
        deal["status"] = "cancelled"
        save_deal(did)
        logger.info(f"Auto-cancelled expired deal {did}")
        
        # Notify participants
        for pid in [deal.get("seller_id"), deal.get("buyer_id")]:
            if pid:
                asyncio.run_coroutine_threadsafe(
                    application.bot.send_message(pid, get_text(pid, "deal_cancelled"), parse_mode="HTML"),
                    application._loop
                )

def _auto_open_disputes(application):
    hours = DEAL_DISPUTE_AUTO_TIMEOUT_HOURS
    if hours <= 0:
        return
    cutoff = time.time() - (hours * 3600)
    to_dispute = []
    for did, deal in deals.items():
        status = deal.get("status", "")
        if status not in ["confirmed", "seller_sent"]:
            continue
        created_ts = _parse_deal_created(deal.get("created_at"))
        if created_ts and created_ts <= cutoff:
            to_dispute.append(did)
    
    for did in to_dispute:
        deal = deals.get(did)
        if not deal or deal.get("status") not in ["confirmed", "seller_sent"]:
            continue
        deal["status"] = "disputed"
        save_deal(did)
        logger.info(f"Auto-opened dispute for deal {did}")
        
        # Notify participants
        for pid in [deal.get("seller_id"), deal.get("buyer_id")]:
            if pid:
                asyncio.run_coroutine_threadsafe(
                    application.bot.send_message(pid, get_text(pid, "dispute_opened", deal_id=did[:8]), parse_mode="HTML"),
                    application._loop
                )

def expiry_watcher_loop(application):
    global _shutdown_flag
    while not _shutdown_flag.is_set():
        try:
            _cancel_expired_deals(application)
            _auto_open_disputes(application)
        except Exception as e:
            logger.warning(f"Expiry watcher error: {e}")
        _shutdown_flag.wait(timeout=DEAL_TIMEOUT_CHECK_INTERVAL_SEC)

# ========== PAYOUT (AUTO-WITHDRAWAL) ==========
def _get_mnemonic_words() -> List[str]:
    raw = BOT_WALLET_MNEMONIC.strip()
    if not raw:
        return []
    raw = raw.replace(",", " ")
    return [w.strip() for w in raw.split() if w.strip()]

def _wallet_lock_path() -> str:
    mnemo = "".join(_get_mnemonic_words())
    h = hashlib.sha256(mnemo.encode()).hexdigest()[:16] if mnemo else "default"
    return os.path.join(tempfile.gettempdir(), f"forsale_hot_wallet_{h}.lock")

def _build_text_comment(memo: str):
    b = begin_cell()
    b.store_uint(0, 32)
    b.store_snake_string(memo)
    return b.end_cell()

def _build_jetton_transfer_body(dest_address: str, jetton_amount: int, memo: str = ""):
    b = begin_cell()
    b.store_uint(0x0f8a7ea5, 32)
    b.store_uint(0, 64)
    b.store_coins(jetton_amount)
    b.store_address(Address(dest_address))
    b.store_address(Address(dest_address))
    b.store_bit(0)
    b.store_coins(1)
    if memo:
        fb = begin_cell()
        fb.store_uint(0, 32)
        fb.store_snake_string(memo)
        b.store_bit(1)
        b.store_ref(fb.end_cell())
    else:
        b.store_bit(0)
    return b.end_cell()

async def _open_provider():
    prov = LiteBalancer.from_mainnet_config(trust_level=2)
    await prov.start_up()
    return prov

async def _open_wallet(provider):
    mnemonics = _get_mnemonic_words()
    return await WalletV4R2.from_mnemonic(provider, mnemonics)

async def _get_usdt_wallet_address(provider, owner_address: str) -> Optional[str]:
    try:
        result = await provider.run_get_method(
            address=USDT_JETTON_MASTER,
            method="get_wallet_address",
            stack=[begin_cell().store_address(Address(owner_address)).end_cell().begin_parse()]
        )
        return result[0].load_address().to_str()
    except Exception as e:
        logger.warning(f"Get USDT wallet address failed: {e}")
        return None

async def _wait_for_outgoing(memo_marker: str, timeout: int = 90, jetton: bool = False) -> Optional[str]:
    deadline = asyncio.get_event_loop().time() + timeout
    provider = None
    try:
        provider = await _open_provider()
        wallet = await _open_wallet(provider)
        addr = wallet.address.to_str()
        while asyncio.get_event_loop().time() < deadline:
            try:
                txs = await provider.get_transactions(address=addr, count=20)
                for tx in txs:
                    for msg in (tx.out_msgs or []):
                        comment = ""
                        try:
                            if msg.body:
                                s = msg.body.begin_parse()
                                if s.remaining_bits >= 32:
                                    op = s.load_uint(32)
                                    if op == 0:
                                        comment = s.load_snake_string()
                                    elif op == 0x0f8a7ea5 and jetton:
                                        s.load_uint(64)
                                        s.load_coins()
                                        s.load_address()
                                        s.load_address()
                                        if s.load_bit():
                                            s.load_ref()
                                        s.load_coins()
                                        if s.load_bit():
                                            fb = s.load_ref().begin_parse()
                                            if fb.remaining_bits >= 32 and fb.load_uint(32) == 0:
                                                comment = fb.load_snake_string()
                        except:
                            pass
                        if memo_marker in comment:
                            return tx.cell.hash.hex()
            except:
                pass
            await asyncio.sleep(3)
    finally:
        if provider:
            try:
                await provider.close_all()
            except:
                pass
    return None

async def send_ton(destination: str, amount_ton: float, memo: str = "") -> Tuple[bool, Optional[str], Optional[str]]:
    if not HAS_PYTONIQ:
        return False, None, "pytoniq not installed"
    mnemonics = _get_mnemonic_words()
    if len(mnemonics) != 24:
        return False, None, "Invalid mnemonic (24 words required)"
    
    unique_id = uuid.uuid4().hex[:8]
    full_memo = f"{memo} | id:{unique_id}" if memo else f"id:{unique_id}"
    amount_nano = int(amount_ton * 1e9)
    
    # File lock for cross-process safety
    lock_path = _wallet_lock_path()
    import filelock
    flock = filelock.FileLock(lock_path, timeout=120)
    
    try:
        await asyncio.to_thread(flock.acquire, timeout=120)
    except Exception as e:
        return False, None, f"Lock failed: {e}"
    
    try:
        for attempt, delay in enumerate(RETRY_DELAYS, 1):
            if delay > 0:
                await asyncio.sleep(delay)
            provider = None
            try:
                provider = await _open_provider()
                wallet = await _open_wallet(provider)
                balance = await wallet.get_balance()
                if balance < amount_nano + int(PAYOUT_TON_GAS_RESERVE * 1e9):
                    return False, None, f"Insufficient TON balance: {balance/1e9:.4f}"
                
                await wallet.transfer(destination=destination, amount=amount_nano, body=_build_text_comment(full_memo))
                logger.info(f"TON transfer broadcast: {amount_ton} to {destination[:12]}")
                
                tx_hash = await _wait_for_outgoing(f"id:{unique_id}", timeout=90, jetton=False)
                if tx_hash:
                    return True, tx_hash, None
                return True, None, "Broadcast OK, but tx hash not confirmed"
            except Exception as e:
                logger.warning(f"Attempt {attempt} failed: {e}")
                if attempt == len(RETRY_DELAYS):
                    return False, None, str(e)
            finally:
                if provider:
                    try:
                        await provider.close_all()
                    except:
                        pass
    finally:
        try:
            flock.release()
        except:
            pass
    return False, None, "All attempts failed"

async def send_usdt(destination: str, amount_usdt: float, memo: str = "") -> Tuple[bool, Optional[str], Optional[str]]:
    if not HAS_PYTONIQ:
        return False, None, "pytoniq not installed"
    mnemonics = _get_mnemonic_words()
    if len(mnemonics) != 24:
        return False, None, "Invalid mnemonic (24 words required)"
    
    unique_id = uuid.uuid4().hex[:8]
    full_memo = f"{memo} | id:{unique_id}" if memo else f"id:{unique_id}"
    amount_raw = int(amount_usdt * 10**6)
    attach_ton_nano = int(PAYOUT_JETTON_GAS_TON * 1e9)
    
    lock_path = _wallet_lock_path()
    import filelock
    flock = filelock.FileLock(lock_path, timeout=120)
    
    try:
        await asyncio.to_thread(flock.acquire, timeout=120)
    except Exception as e:
        return False, None, f"Lock failed: {e}"
    
    try:
        for attempt, delay in enumerate(RETRY_DELAYS, 1):
            if delay > 0:
                await asyncio.sleep(delay)
            provider = None
            try:
                provider = await _open_provider()
                wallet = await _open_wallet(provider)
                owner_addr = wallet.address.to_str()
                
                # Check USDT balance
                jetton_wallet = await _get_usdt_wallet_address(provider, owner_addr)
                if not jetton_wallet:
                    return False, None, "Cannot resolve USDT jetton wallet"
                
                result = await provider.run_get_method(address=jetton_wallet, method="get_wallet_data", stack=[])
                usdt_balance = int(result[0]) / 10**6
                if usdt_balance < amount_usdt - 1e-6:
                    return False, None, f"Insufficient USDT balance: {usdt_balance:.2f}"
                
                # Check TON balance for gas
                ton_balance = await wallet.get_balance()
                if ton_balance < attach_ton_nano + int(PAYOUT_TON_GAS_RESERVE * 1e9):
                    return False, None, f"Insufficient TON for gas: {ton_balance/1e9:.4f}"
                
                body = _build_jetton_transfer_body(destination, amount_raw, full_memo)
                await wallet.transfer(destination=jetton_wallet, amount=attach_ton_nano, body=body)
                logger.info(f"USDT transfer broadcast: {amount_usdt} to {destination[:12]}")
                
                tx_hash = await _wait_for_outgoing(f"id:{unique_id}", timeout=90, jetton=True)
                if tx_hash:
                    return True, tx_hash, None
                return True, None, "Broadcast OK, but tx hash not confirmed"
            except Exception as e:
                logger.warning(f"Attempt {attempt} failed: {e}")
                if attempt == len(RETRY_DELAYS):
                    return False, None, str(e)
            finally:
                if provider:
                    try:
                        await provider.close_all()
                    except:
                        pass
    finally:
        try:
            flock.release()
        except:
            pass
    return False, None, "All attempts failed"

async def process_pending_withdrawals(application):
    withdrawals = get_pending_withdrawals()
    for wid, uid, currency, amount, address in withdrawals:
        if mark_withdrawal_broadcasting(wid):
            lang = user_data.get(uid, {}).get("lang", "ru")
            asyncio.create_task(_do_auto_payout(application, wid, uid, currency, amount, address, lang))

async def _do_auto_payout(application, wid: int, uid: int, currency: str, amount: float, address: str, lang: str):
    try:
        if currency == "TON":
            ok, tx_hash, err = await send_ton(address, amount, f"withdraw #{wid}")
        else:
            ok, tx_hash, err = await send_usdt(address, amount, f"withdraw #{wid}")
        
        if ok:
            mark_withdrawal_sent(wid, tx_hash or "broadcasted")
            try:
                await application.bot.send_message(
                    uid,
                    f"✅ Вывод #{wid} отправлен!\n{format_amount(amount, currency)} {currency} → {address[:20]}...",
                    parse_mode="HTML"
                )
            except:
                pass
        else:
            mark_withdrawal_error(wid, err or "Unknown error")
            try:
                await application.bot.send_message(
                    uid,
                    f"❌ Вывод #{wid} не удался: {err[:200]}\nОбратитесь в поддержку.",
                    parse_mode="HTML"
                )
            except:
                pass
            # Notify admins
            for aid in ADMIN_IDS:
                try:
                    await application.bot.send_message(
                        aid,
                        f"⚠️ Auto-payout #{wid} failed: {amount} {currency} → {address}\nError: {err[:200]}",
                        parse_mode="HTML"
                    )
                except:
                    pass
    except Exception as e:
        logger.error(f"Auto-payout #{wid} exception: {e}")
        mark_withdrawal_error(wid, str(e)[:200])

# ========== MAIN CALLBACK HANDLER ==========
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user = query.from_user
    uid = user.id
    data = query.data
    ensure_user(uid)
    
    # ========== MAIN MENU NAVIGATION ==========
    if data == "menu":
        text = get_text(uid, "start", comm=COMMISSION_PERCENT, support=SUPPORT_USERNAME)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=main_menu(uid))
        return
    
    if data == "profile":
        ton_bal = get_balance(uid, "TON")
        usdt_bal = get_balance(uid, "USDT")
        text = get_text(uid, "profile",
            uid=uid,
            deals=user_data[uid].get("successful_deals", 0),
            likes=user_data[uid].get("likes", 0),
            dislikes=user_data[uid].get("dislikes", 0),
            ton=format_amount(ton_bal, "TON"),
            usdt=format_amount(usdt_bal, "USDT"))
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=profile_menu(uid))
        return
    
    if data == "wallet":
        addr = get_ton_address(uid) or "-"
        text = get_text(uid, "wallet",
            ton=format_amount(get_balance(uid, "TON"), "TON"),
            usdt=format_amount(get_balance(uid, "USDT"), "USDT"),
            addr=addr)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=wallet_menu(uid))
        return
    
    if data == "create_deal":
        await query.edit_message_text(get_text(uid, "create_deal"), parse_mode="HTML", reply_markup=deal_currency_select(uid))
        return
    
    if data.startswith("deal_currency_"):
        currency = data.split("_")[2]
        context.user_data["deal_currency"] = currency
        await query.edit_message_text(get_text(uid, "enter_amount", currency=currency), parse_mode="HTML", reply_markup=back_button(uid, "create_deal"))
        context.user_data["state"] = "awaiting_amount"
        return
    
    if data == "my_deals":
        user_deals = [(did, d) for did, d in deals.items() if d.get("seller_id") == uid or d.get("buyer_id") == uid]
        if not user_deals:
            await query.edit_message_text(get_text(uid, "no_deals"), parse_mode="HTML", reply_markup=back_button(uid, "profile"))
            return
        
        # Sort by created_at (newest first)
        user_deals.sort(key=lambda x: x[1].get("created_at", ""), reverse=True)
        
        text = get_text(uid, "my_deals") + "\n\n"
        for i, (did, d) in enumerate(user_deals[:10], 1):
            status_text = get_deal_status_text(uid, d.get("status", "active"))
            text += f"{i}. {format_amount(d['amount'], d['currency'])} {d['currency']} | {status_text}\n"
        
        # Create buttons for each deal (limited to 10)
        kb_buttons = []
        for i, (did, d) in enumerate(user_deals[:10], 1):
            kb_buttons.append([InlineKeyboardButton(f"#{did[:8]}", callback_data=f"view_deal_{did}")])
        kb_buttons.append([InlineKeyboardButton(get_text(uid, "back"), callback_data="profile")])
        
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb_buttons))
        return
    
    if data.startswith("view_deal_"):
        did = data[10:]
        deal = deals.get(did)
        if not deal:
            await query.answer("Сделка не найдена", show_alert=True)
            return
        role = get_deal_role(deal, uid)
        if not role:
            await query.answer("Вы не участник этой сделки", show_alert=True)
            return
        
        seller_name = await get_telegram_username(context, deal["seller_id"])
        buyer_name = await get_telegram_username(context, deal["buyer_id"]) if deal.get("buyer_id") else "не назначен"
        
        text = f"📄 <b>Сделка #{did[:8]}</b>\n\n"
        text += f"👤 Продавец: @{seller_name}\n"
        text += f"👤 Покупатель: @{buyer_name}\n"
        text += f"💰 Сумма: {format_amount(deal['amount'], deal['currency'])} {deal['currency']}\n"
        text += f"📦 Товар: {deal['description'][:100]}\n"
        text += f"📅 Создана: {format_created_at(deal.get('created_at'))}\n"
        text += f"📊 Статус: {get_deal_status_text(uid, deal['status'])}"
        
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=deal_buttons(uid, did, deal["status"], role))
        return
    
    if data == "set_address":
        await query.edit_message_text(get_text(uid, "set_address"), parse_mode="HTML", reply_markup=back_button(uid, "profile"))
        context.user_data["state"] = "awaiting_address"
        return
    
    if data == "deposit":
        if not TON_DEPOSIT_ADDRESS or not TON_API_KEY:
            await query.edit_message_text(get_text(uid, "deposit_disabled"), parse_mode="HTML", reply_markup=back_button(uid, "wallet"))
            return
        await query.edit_message_text(get_text(uid, "withdraw"), parse_mode="HTML", reply_markup=currency_select(uid, "deposit"))
        return
    
    if data.startswith("deposit_"):
        currency = data.split("_")[1]
        if currency == "TON":
            text = get_text(uid, "deposit_ton",
                address=TON_DEPOSIT_ADDRESS,
                uid=uid,
                min_ton=MIN_DEPOSIT_TON)
        else:
            text = get_text(uid, "deposit_usdt",
                address=TON_DEPOSIT_ADDRESS,
                uid=uid,
                min_usdt=MIN_DEPOSIT_USDT)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=back_button(uid, "wallet"))
        return
    
    if data == "withdraw":
        addr = get_ton_address(uid)
        if not addr:
            await query.edit_message_text(get_text(uid, "withdraw_no_addr"), parse_mode="HTML", reply_markup=back_button(uid, "wallet"))
            return
        await query.edit_message_text(get_text(uid, "withdraw"), parse_mode="HTML", reply_markup=currency_select(uid, "withdraw"))
        return
    
    if data == "withdraw_back":
        addr = get_ton_address(uid) or "-"
        text = get_text(uid, "wallet",
            ton=format_amount(get_balance(uid, "TON"), "TON"),
            usdt=format_amount(get_balance(uid, "USDT"), "USDT"),
            addr=addr)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=wallet_menu(uid))
        return
    
    if data.startswith("withdraw_"):
        currency = data.split("_")[1]
        context.user_data["withdraw_currency"] = currency
        min_amt = MIN_WITHDRAW_TON if currency == "TON" else MIN_WITHDRAW_USDT
        await query.edit_message_text(get_text(uid, f"withdraw_{currency.lower()}", min=min_amt), parse_mode="HTML", reply_markup=back_button(uid, "withdraw"))
        context.user_data["state"] = "awaiting_withdraw_amount"
        return
    
    # ========== DEAL ACTIONS ==========
    if data.startswith("pay_"):
        deal_id = data[4:]
        deal = deals.get(deal_id)
        if not deal or deal.get("status") != "active":
            await query.answer("Сделка не активна", show_alert=True)
            return
        if deal.get("buyer_id") is not None and deal.get("buyer_id") != uid:
            await query.answer("У сделки уже есть покупатель", show_alert=True)
            return
        if uid == deal.get("seller_id"):
            await query.answer("Нельзя купить свою сделку", show_alert=True)
            return
        
        currency = deal["currency"]
        amount = deal["amount"]
        balance = get_balance(uid, currency)
        
        if balance < amount - 1e-9:
            text = get_text(uid, "insufficient", balance=format_amount(balance, currency), currency=currency)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=back_button(uid, "menu"))
            return
        
        lock = _DEAL_LOCKS.setdefault(deal_id, asyncio.Lock())
        async with lock:
            deal = deals.get(deal_id)
            if deal.get("status") != "active":
                await query.answer("Сделка уже не активна", show_alert=True)
                return
            if deal.get("buyer_id") is not None and deal.get("buyer_id") != uid:
                await query.answer("У сделки уже есть покупатель", show_alert=True)
                return
            
            # Process payment
            if not sub_balance(uid, currency, amount):
                await query.answer("Недостаточно средств", show_alert=True)
                return
            
            deal["buyer_id"] = uid
            deal["status"] = "confirmed"
            deal["escrow_collected"] = True
            save_deal(deal_id)
            
            # Notify buyer
            await query.edit_message_text(get_text(uid, "payment_ok"), parse_mode="HTML", reply_markup=back_button(uid, "menu"))
            
            # Notify seller
            seller_id = deal["seller_id"]
            buyer_name = await get_telegram_username(context, uid)
            seller_text = get_text(seller_id, "seller_payment",
                buyer=buyer_name,
                desc=deal["description"][:100])
            seller_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(get_text(seller_id, "confirm_sent_btn"), callback_data=f"confirm_sent_{deal_id}")],
                [InlineKeyboardButton(get_text(seller_id, "cancel_btn"), callback_data=f"cancel_deal_{deal_id}")],
                [InlineKeyboardButton(get_text(seller_id, "open_dispute_btn"), callback_data=f"open_dispute_{deal_id}")],
            ])
            await context.bot.send_message(seller_id, seller_text, parse_mode="HTML", reply_markup=seller_kb)
            
            # Update volume stats
            usd_value = to_usd(amount, currency)
            ensure_user(seller_id)
            user_data[seller_id]["total_volume_usd"] = user_data[seller_id].get("total_volume_usd", 0) + usd_value
            save_user(seller_id)
        
        _DEAL_LOCKS.pop(deal_id, None)
        return
    
    if data.startswith("confirm_sent_"):
        deal_id = data[13:]
        deal = deals.get(deal_id)
        if not deal or deal.get("status") != "confirmed":
            await query.answer("Невозможно подтвердить", show_alert=True)
            return
        if uid != deal.get("seller_id"):
            await query.answer("Только продавец может подтвердить", show_alert=True)
            return
        
        lock = _DEAL_LOCKS.setdefault(deal_id, asyncio.Lock())
        async with lock:
            deal = deals.get(deal_id)
            if deal.get("status") != "confirmed":
                await query.answer("Статус сделки изменился", show_alert=True)
                return
            
            deal["status"] = "seller_sent"
            save_deal(deal_id)
            
            await query.edit_message_text(get_text(uid, "seller_sent"), parse_mode="HTML", reply_markup=back_button(uid, "menu"))
            
            buyer_id = deal["buyer_id"]
            if buyer_id:
                buyer_text = get_text(buyer_id, "buyer_notify")
                buyer_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton(get_text(buyer_id, "confirm_received_btn"), callback_data=f"confirm_received_{deal_id}")],
                    [InlineKeyboardButton(get_text(buyer_id, "cancel_btn"), callback_data=f"cancel_deal_{deal_id}")],
                    [InlineKeyboardButton(get_text(buyer_id, "open_dispute_btn"), callback_data=f"open_dispute_{deal_id}")],
                ])
                await context.bot.send_message(buyer_id, buyer_text, parse_mode="HTML", reply_markup=buyer_kb)
        
        _DEAL_LOCKS.pop(deal_id, None)
        return
    
    if data.startswith("confirm_received_"):
        deal_id = data[18:]
        deal = deals.get(deal_id)
        if not deal or deal.get("status") != "seller_sent":
            await query.answer("Невозможно подтвердить", show_alert=True)
            return
        if uid != deal.get("buyer_id"):
            await query.answer("Только покупатель может подтвердить", show_alert=True)
            return
        
        lock = _DEAL_LOCKS.setdefault(deal_id, asyncio.Lock())
        async with lock:
            deal = deals.get(deal_id)
            if deal.get("status") != "seller_sent":
                await query.answer("Статус сделки изменился", show_alert=True)
                return
            
            amount = deal["amount"]
            currency = deal["currency"]
            fee = amount * COMMISSION_PERCENT / 100
            seller_amount = amount - fee
            seller_id = deal["seller_id"]
            
            # Pay seller
            if deal.get("escrow_collected"):
                add_balance(seller_id, currency, seller_amount)
                add_successful_deal(seller_id)
            
            deal["status"] = "completed"
            save_deal(deal_id)
            
            # Notify buyer with rating
            await query.edit_message_text(
                get_text(uid, "deal_completed",
                    amount=format_amount(seller_amount, currency),
                    currency=currency,
                    comm=COMMISSION_PERCENT),
                parse_mode="HTML",
                reply_markup=rating_buttons(uid, deal_id, "seller"))
            
            # Notify seller with rating
            seller_text = get_text(seller_id, "deal_completed",
                amount=format_amount(seller_amount, currency),
                currency=currency,
                comm=COMMISSION_PERCENT)
            await context.bot.send_message(seller_id, seller_text, parse_mode="HTML",
                reply_markup=rating_buttons(seller_id, deal_id, "buyer"))
        
        _DEAL_LOCKS.pop(deal_id, None)
        return
    
    if data.startswith("cancel_deal_"):
        deal_id = data[12:]
        deal = deals.get(deal_id)
        if not deal:
            await query.answer("Сделка не найдена", show_alert=True)
            return
        if not is_deal_participant(deal, uid):
            await query.answer("Вы не участник сделки", show_alert=True)
            return
        
        status = deal.get("status", "")
        if status in ["completed", "cancelled", "disputed"]:
            await query.answer("Сделку уже нельзя отменить", show_alert=True)
            return
        
        if status == "seller_sent":
            await query.answer("Товар уже передан, используйте спор", show_alert=True)
            return
        
        lock = _DEAL_LOCKS.setdefault(deal_id, asyncio.Lock())
        async with lock:
            deal = deals.get(deal_id)
            if deal.get("status") in ["completed", "cancelled", "disputed"]:
                return
            
            # Refund if payment was collected
            if deal.get("escrow_collected") and deal.get("buyer_id"):
                add_balance(deal["buyer_id"], deal["currency"], deal["amount"])
            
            deal["status"] = "cancelled"
            save_deal(deal_id)
            
            await query.edit_message_text(get_text(uid, "deal_cancelled"), parse_mode="HTML", reply_markup=back_button(uid, "menu"))
            
            # Notify other party
            other_id = deal["seller_id"] if uid == deal.get("buyer_id") else deal.get("buyer_id")
            if other_id:
                await context.bot.send_message(other_id, get_text(other_id, "deal_cancelled"), parse_mode="HTML")
        
        _DEAL_LOCKS.pop(deal_id, None)
        return
    
    if data.startswith("open_dispute_"):
        deal_id = data[13:]
        deal = deals.get(deal_id)
        if not deal:
            await query.answer("Сделка не найдена", show_alert=True)
            return
        if not is_deal_participant(deal, uid):
            await query.answer("Вы не участник сделки", show_alert=True)
            return
        
        status = deal.get("status", "")
        if status in ["completed", "cancelled", "disputed"]:
            await query.answer("Нельзя открыть спор", show_alert=True)
            return
        
        if status not in ["confirmed", "seller_sent"]:
            await query.answer(get_text(uid, "dispute_cannot"), show_alert=True)
            return
        
        lock = _DISPUTE_LOCKS.setdefault(deal_id, asyncio.Lock())
        async with lock:
            deal = deals.get(deal_id)
            if deal.get("status") == "disputed":
                await query.answer(get_text(uid, "dispute_already"), show_alert=True)
                return
            
            deal["status"] = "disputed"
            save_deal(deal_id)
            
            await query.edit_message_text(
                get_text(uid, "dispute_opened", deal_id=deal_id[:8]),
                parse_mode="HTML",
                reply_markup=back_button(uid, "menu"))
            
            # Notify other party
            other_id = deal["seller_id"] if uid == deal.get("buyer_id") else deal.get("buyer_id")
            if other_id:
                await context.bot.send_message(other_id, get_text(other_id, "dispute_opened", deal_id=deal_id[:8]), parse_mode="HTML")
            
            # Notify admins
            for aid in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        aid,
                        f"⚠️ <b>Открыт спор</b>\nСделка: #{deal_id[:8]}\nСумма: {deal['amount']} {deal['currency']}\nУчастники: {deal['seller_id']} / {deal['buyer_id']}",
                        parse_mode="HTML"
                    )
                except:
                    pass
        
        _DISPUTE_LOCKS.pop(deal_id, None)
        return
    
    # ========== RATING ==========
    if data.startswith("rate_"):
        parts = data.split("_")
        if len(parts) >= 4:
            deal_id = parts[1]
            target_role = parts[2]
            rating = parts[3]
            deal = deals.get(deal_id)
            if deal and deal.get("status") == "completed":
                voted_key = f"{target_role}_voted"
                if not deal.get(voted_key):
                    target_id = deal["seller_id"] if target_role == "buyer" else deal["buyer_id"]
                    if target_id:
                        add_rating(target_id, rating == "up")
                        deal[voted_key] = True
                        save_deal(deal_id)
            await query.edit_message_text("Спасибо за оценку!", parse_mode="HTML", reply_markup=main_menu(uid))
        return
    
    # ========== ADMIN PANEL ==========
    if not is_admin(uid):
        await query.answer(get_text(uid, "admin_only"), show_alert=True)
        return
    
    if data == "admin_panel":
        await query.edit_message_text("🔧 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_menu(uid))
        return
    
    if data == "admin_stats":
        total_users = len(user_data)
        total_deals = len(deals)
        completed = sum(1 for d in deals.values() if d.get("status") == "completed")
        cancelled = sum(1 for d in deals.values() if d.get("status") == "cancelled")
        active = sum(1 for d in deals.values() if d.get("status") in ["active", "confirmed", "seller_sent"])
        total_volume = sum(u.get("total_volume_usd", 0) for u in user_data.values())
        
        text = get_text(uid, "admin_stats_message",
            users=total_users,
            deals=total_deals,
            completed=completed,
            cancelled=cancelled,
            active=active,
            volume=f"{total_volume:.2f}")
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=back_button(uid, "admin_panel"))
        return
    
    if data == "admin_wallet":
        text = get_text(uid, "admin_wallet_info", address=TON_DEPOSIT_ADDRESS or "не задан", ton="?", usdt="?")
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=back_button(uid, "admin_panel"))
        return
    
    if data == "admin_balance":
        await query.edit_message_text(get_text(uid, "admin_balance_ask"), parse_mode="HTML", reply_markup=back_button(uid, "admin_panel"))
        context.user_data["admin_state"] = "awaiting_balance_change"
        return
    
    if data == "admin_withdrawals":
        withdrawals = get_pending_withdrawals()
        if not withdrawals:
            await query.edit_message_text(get_text(uid, "admin_withdrawal_none"), parse_mode="HTML", reply_markup=back_button(uid, "admin_panel"))
            return
        
        wds_text = ""
        for wid, w_uid, currency, amount, address in withdrawals[:20]:
            wds_text += get_text(uid, "admin_withdrawal_item",
                id=wid,
                amount=format_amount(amount, currency),
                currency=currency,
                user_id=w_uid,
                address=address[:20] + "...",
                created=datetime.fromtimestamp(int(time.time())).strftime("%d.%m %H:%M"))
            wds_text += "\n\n"
        
        text = get_text(uid, "admin_withdrawals_list", wds=wds_text)
        # Add action buttons for each withdrawal
        kb = [[InlineKeyboardButton("✅ Выплачено", callback_data=f"admin_wd_sent_{wid}"),
               InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_wd_reject_{wid}")] 
              for wid, _, _, _, _ in withdrawals[:5]]
        kb.append([InlineKeyboardButton(get_text(uid, "back"), callback_data="admin_panel")])
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        return
    
    if data.startswith("admin_wd_sent_"):
        wid = int(data.split("_")[3])
        with db_connect() as conn:
            conn.execute("UPDATE withdrawals SET status='sent', processed_at=? WHERE id=?", (int(time.time()), wid))
        await query.answer(f"Заявка #{wid} помечена как выплаченная")
        await button_callback(update, context)
        return
    
    if data.startswith("admin_wd_reject_"):
        wid = int(data.split("_")[3])
        with db_connect() as conn:
            cur = conn.execute("SELECT user_id, currency, amount FROM withdrawals WHERE id=?", (wid,))
            row = cur.fetchone()
            if row:
                user_id, currency, amount = row
                add_balance(user_id, currency, amount)
                conn.execute("UPDATE withdrawals SET status='rejected', processed_at=? WHERE id=?", (int(time.time()), wid))
        await query.answer(f"Заявка #{wid} отклонена, средства возвращены")
        await button_callback(update, context)
        return
    
    if data == "admin_disputes":
        disputed_deals = [(did, d) for did, d in deals.items() if d.get("status") == "disputed"]
        if not disputed_deals:
            await query.edit_message_text(get_text(uid, "admin_dispute_none"), parse_mode="HTML", reply_markup=back_button(uid, "admin_panel"))
            return
        
        disputes_text = ""
        kb = []
        for did, d in disputed_deals[:10]:
            disputes_text += get_text(uid, "admin_dispute_item",
                deal_id=did[:8],
                amount=format_amount(d["amount"], d["currency"]),
                currency=d["currency"],
                seller=d["seller_id"],
                buyer=d["buyer_id"])
            disputes_text += "\n"
            kb.append([InlineKeyboardButton(f"#{did[:8]}", callback_data=f"admin_resolve_{did}")])
        
        kb.append([InlineKeyboardButton(get_text(uid, "back"), callback_data="admin_panel")])
        await query.edit_message_text(get_text(uid, "admin_disputes_list", disputes=disputes_text), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        return
    
    if data.startswith("admin_resolve_"):
        deal_id = data[14:]
        deal = deals.get(deal_id)
        if not deal or deal.get("status") != "disputed":
            await query.answer("Спор уже разрешён", show_alert=True)
            return
        
        # Create resolution buttons
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚖️ В пользу продавца", callback_data=f"admin_resolve_seller_{deal_id}"),
             InlineKeyboardButton("⚖️ В пользу покупателя", callback_data=f"admin_resolve_buyer_{deal_id}")],
            [InlineKeyboardButton(get_text(uid, "back"), callback_data="admin_disputes")],
        ])
        await query.edit_message_text(f"Разрешение спора по сделке #{deal_id[:8]}\n\nВыберите сторону:", parse_mode="HTML", reply_markup=kb)
        return
    
    if data.startswith("admin_resolve_seller_"):
        deal_id = data[21:]
        deal = deals.get(deal_id)
        if not deal or deal.get("status") != "disputed":
            await query.answer("Спор уже разрешён", show_alert=True)
            return
        
        amount = deal["amount"]
        currency = deal["currency"]
        fee = amount * COMMISSION_PERCENT / 100
        seller_amount = amount - fee
        seller_id = deal["seller_id"]
        buyer_id = deal["buyer_id"]
        
        if deal.get("escrow_collected"):
            add_balance(seller_id, currency, seller_amount)
            add_successful_deal(seller_id)
        
        deal["status"] = "completed"
        save_deal(deal_id)
        
        await context.bot.send_message(seller_id, get_text(seller_id, "dispute_resolved_seller"), parse_mode="HTML")
        if buyer_id:
            await context.bot.send_message(buyer_id, get_text(buyer_id, "dispute_resolved_seller"), parse_mode="HTML")
        
        await query.edit_message_text("✅ Спор разрешён в пользу продавца", parse_mode="HTML", reply_markup=back_button(uid, "admin_panel"))
        return
    
    if data.startswith("admin_resolve_buyer_"):
        deal_id = data[20:]
        deal = deals.get(deal_id)
        if not deal or deal.get("status") != "disputed":
            await query.answer("Спор уже разрешён", show_alert=True)
            return
        
        buyer_id = deal["buyer_id"]
        if deal.get("escrow_collected") and buyer_id:
            add_balance(buyer_id, deal["currency"], deal["amount"])
        
        deal["status"] = "cancelled"
        save_deal(deal_id)
        
        seller_id = deal["seller_id"]
        await context.bot.send_message(seller_id, get_text(seller_id, "dispute_resolved_buyer"), parse_mode="HTML")
        if buyer_id:
            await context.bot.send_message(buyer_id, get_text(buyer_id, "dispute_resolved_buyer"), parse_mode="HTML")
        
        await query.edit_message_text("✅ Спор разрешён в пользу покупателя", parse_mode="HTML", reply_markup=back_button(uid, "admin_panel"))
        return
    
    await query.answer(get_text(uid, "unknown"), show_alert=True)

# ========== TEXT MESSAGE HANDLER ==========
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    uid = user.id
    text = update.message.text.strip()
    ensure_user(uid)
    
    # Admin FSM
    admin_state = context.user_data.get("admin_state")
    if admin_state == "awaiting_balance_change" and is_admin(uid):
        parts = text.split()
        if len(parts) >= 3:
            try:
                target_uid = int(parts[0])
                amount = float(parts[1].replace(",", "."))
                currency = parts[2].upper()
                if currency not in ["TON", "USDT"]:
                    await update.message.reply_text("❌ Валюта должна быть TON или USDT")
                    return
                if amount <= 0:
                    await update.message.reply_text("❌ Сумма должна быть положительной")
                    return
                if currency == "TON":
                    add_balance(target_uid, "TON", amount)
                else:
                    add_balance(target_uid, "USDT", amount)
                await update.message.reply_text(get_text(uid, "admin_balance_success", uid=target_uid, currency=currency, amount=format_amount(amount, currency)), parse_mode="HTML")
            except ValueError:
                await update.message.reply_text("❌ Неверный формат")
        else:
            await update.message.reply_text("❌ Формат: user_id сумма TON|USDT")
        context.user_data.pop("admin_state", None)
        return
    
    # State machine for deal creation / withdrawal
    state = context.user_data.get("state")
    
    if state == "awaiting_amount":
        try:
            amount = float(text.replace(",", "."))
            currency = context.user_data.get("deal_currency", "TON")
            min_amt = MIN_DEAL_TON if currency == "TON" else MIN_DEAL_USDT
            max_amt = MAX_DEAL_TON if currency == "TON" else MAX_DEAL_USDT
            
            if amount < min_amt:
                await update.message.reply_text(f"❌ Минимальная сумма: {min_amt} {currency}")
                return
            if amount > max_amt:
                await update.message.reply_text(f"❌ Максимальная сумма: {max_amt} {currency}")
                return
            
            context.user_data["deal_amount"] = amount
            context.user_data["state"] = "awaiting_description"
            await update.message.reply_text(get_text(uid, "enter_desc"), parse_mode="HTML")
        except ValueError:
            await update.message.reply_text("❌ Введите число")
        return
    
    if state == "awaiting_description":
        desc = text[:200]
        currency = context.user_data.get("deal_currency", "TON")
        amount = context.user_data.get("deal_amount", 0)
        deal_id = uuid.uuid4().hex[:16]
        
        deals[deal_id] = {
            "amount": amount,
            "description": desc,
            "seller_id": uid,
            "buyer_id": None,
            "status": "active",
            "currency": currency,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "escrow_collected": False,
            "seller_voted": False,
            "buyer_voted": False,
            "join_notification_sent": False
        }
        save_deal(deal_id)
        
        context.user_data.pop("state", None)
        context.user_data.pop("deal_amount", None)
        context.user_data.pop("deal_currency", None)
        
        deal_link = f"https://t.me/{BOT_USERNAME}?start={deal_id}"
        text_msg = get_text(uid, "deal_created",
            amount=format_amount(amount, currency),
            currency=currency,
            desc=desc[:50],
            bot_username=BOT_USERNAME,
            deal_id=deal_id)
        
        # Add copy button for the link
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Копировать ссылку", callback_data=f"copy_{deal_id}")],
            [InlineKeyboardButton(get_text(uid, "back"), callback_data="menu")],
        ])
        await update.message.reply_text(text_msg, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        return
    
    if state == "awaiting_address":
        addr = text.strip()
        if not re.match(r'^[EU]Q[A-Za-z0-9_-]{46}$', addr):
            await update.message.reply_text("❌ Неверный TON-адрес. Должен начинаться с EQ или UQ, 48 символов.")
            return
        set_ton_address(uid, addr)
        context.user_data.pop("state", None)
        await update.message.reply_text(get_text(uid, "addr_saved", addr=addr), parse_mode="HTML", reply_markup=back_button(uid, "profile"))
        return
    
    if state == "awaiting_withdraw_amount":
        currency = context.user_data.get("withdraw_currency", "TON")
        min_amt = MIN_WITHDRAW_TON if currency == "TON" else MIN_WITHDRAW_USDT
        
        try:
            amount = float(text.replace(",", "."))
            if amount < min_amt:
                await update.message.reply_text(f"❌ Минимальная сумма вывода: {min_amt} {currency}")
                return
            
            balance = get_balance(uid, currency)
            if balance < amount - 1e-9:
                await update.message.reply_text(f"❌ Недостаточно средств. Баланс: {format_amount(balance, currency)} {currency}")
                return
            
            # Check daily cap
            cap = DAILY_WITHDRAW_CAP_TON if currency == "TON" else DAILY_WITHDRAW_CAP_USDT
            if cap > 0:
                with db_connect() as conn:
                    cur = conn.execute(
                        "SELECT COALESCE(SUM(amount), 0) FROM withdrawals WHERE user_id=? AND currency=? AND status IN ('pending','sent') AND created_at > ?",
                        (uid, currency, int(time.time()) - 86400)
                    )
                    day_sum = float(cur.fetchone()[0] or 0)
                    if day_sum + amount > cap + 1e-9:
                        await update.message.reply_text(f"❌ Превышен суточный лимит вывода: {cap} {currency}")
                        return
            
            context.user_data["withdraw_amount"] = amount
            context.user_data["state"] = "awaiting_withdraw_address"
            await update.message.reply_text(get_text(uid, "withdraw_addr"), parse_mode="HTML")
        except ValueError:
            await update.message.reply_text("❌ Введите число")
        return
    
    if state == "awaiting_withdraw_address":
        addr = text.strip()
        if not re.match(r'^[EU]Q[A-Za-z0-9_-]{46}$', addr):
            await update.message.reply_text("❌ Неверный TON-адрес")
            return
        
        currency = context.user_data.get("withdraw_currency", "TON")
        amount = context.user_data.get("withdraw_amount", 0)
        
        try:
            wid = create_withdrawal(uid, currency, amount, addr)
            context.user_data.pop("state", None)
            context.user_data.pop("withdraw_amount", None)
            context.user_data.pop("withdraw_currency", None)
            
            await update.message.reply_text(
                get_text(uid, "withdraw_submitted",
                    amount=format_amount(amount, currency),
                    currency=currency,
                    address=addr[:20] + "..."),
                parse_mode="HTML",
                reply_markup=back_button(uid, "wallet"))
            
            # Auto-payout in background if pytoniq available
            if HAS_PYTONIQ and BOT_WALLET_MNEMONIC:
                asyncio.create_task(_do_auto_payout_single(update, wid, uid, currency, amount, addr))
        except ValueError as e:
            if str(e) == "recent_pending":
                await update.message.reply_text("⏳ У вас уже есть активная заявка на вывод. Подождите.")
            elif str(e) == "daily_cap_exceeded":
                cap = DAILY_WITHDRAW_CAP_TON if currency == "TON" else DAILY_WITHDRAW_CAP_USDT
                await update.message.reply_text(f"❌ Превышен суточный лимит: {cap} {currency}")
            else:
                await update.message.reply_text(f"❌ Ошибка: {e}")
        return
    
    # If no state, just ignore (unknown command)
    await update.message.reply_text(get_text(uid, "unknown"), parse_mode="HTML")

async def _do_auto_payout_single(update, wid: int, uid: int, currency: str, amount: float, address: str):
    """Single auto-payout wrapper"""
    try:
        if not mark_withdrawal_broadcasting(wid):
            return
        
        if currency == "TON":
            ok, tx_hash, err = await send_ton(address, amount, f"withdraw #{wid}")
        else:
            ok, tx_hash, err = await send_usdt(address, amount, f"withdraw #{wid}")
        
        if ok:
            mark_withdrawal_sent(wid, tx_hash or "broadcasted")
            try:
                await update.effective_message.reply_text(
                    f"✅ Вывод #{wid} отправлен в сеть!\n{format_amount(amount, currency)} {currency} → {address[:20]}...",
                    parse_mode="HTML"
                )
            except:
                pass
            logger.info(f"Auto-payout #{wid}: {amount} {currency} -> {address[:12]}")
        else:
            mark_withdrawal_error(wid, err or "Unknown error")
            logger.warning(f"Auto-payout #{wid} failed: {err}")
            try:
                await update.effective_message.reply_text(
                    f"❌ Вывод #{wid} не удался: {err[:200]}\nСредства возвращены на баланс? Обратитесь к администратору.",
                    parse_mode="HTML"
                )
            except:
                pass
    except Exception as e:
        logger.error(f"Auto-payout #{wid} exception: {e}")
        mark_withdrawal_error(wid, str(e)[:200])

# ========== COMMAND HANDLERS ==========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    uid = user.id
    ensure_user(uid)
    
    args = context.args
    if args:
        deal_id = resolve_deal_id(args[0])
        if deal_id and deal_id in deals:
            deal = deals[deal_id]
            if deal.get("status") == "active" and deal.get("buyer_id") is None:
                if uid == deal.get("seller_id"):
                    await update.message.reply_text(get_text(uid, "unknown"), parse_mode="HTML")
                    return
                
                seller_id = deal["seller_id"]
                seller_name = await get_telegram_username(context, seller_id)
                text = get_text(uid, "deal_info",
                    seller=seller_name,
                    amount=format_amount(deal["amount"], deal["currency"]),
                    currency=deal["currency"],
                    desc=deal["description"][:100])
                
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton(get_text(uid, "pay_btn"), callback_data=f"pay_{deal_id}")],
                    [InlineKeyboardButton(get_text(uid, "back"), callback_data="menu")],
                ])
                await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
                return
    
    # Regular start
    text = get_text(uid, "start", comm=COMMISSION_PERCENT, support=SUPPORT_USERNAME)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=main_menu(uid))

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text(get_text(user.id, "admin_only") if user else "Admin only", parse_mode="HTML")
        return
    text = "🔧 <b>Админ-панель</b>"
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=admin_menu(user.id))

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    if is_admin(user.id):
        help_text = """
<b>👑 Админ-команды:</b>

/admin - открыть админ-панель

<b>Управление балансом:</b>
• В админ-панели → Баланс
• Формат: <code>user_id сумма TON|USDT</code>

<b>Заявки на вывод:</b>
• В админ-панели → Выводы
• Можно отметить как выплаченные или отклонить (с возвратом)

<b>Споры:</b>
• В админ-панели → Споры
• Выберите сторону для разрешения

<b>Работа с воркерами:</b>
• Добавить воркера: INSERT INTO workers (user_id) VALUES (12345);
• Воркеры могут использовать /deals и /money
"""
        await update.message.reply_text(help_text, parse_mode="HTML")
    else:
        help_text = """
<b>🤖 Как пользоваться ботом:</b>

1. <b>Создать сделку</b> — выбери валюту (TON/USDT), укажи сумму и описание. Бот даст ссылку для покупателя.

2. <b>Кошелёк</b> — твой внутренний баланс. Пополни через отправку TON/USDT на указанный адрес с комментарием user_<id>. Вывод средств — авто-выплата на любой TON-адрес.

3. <b>Профиль</b> — статистика сделок и рейтинг.

4. <b>Привязать адрес</b> — укажи свой TON-адрес для вывода средств.

<b>Комиссия:</b> {}% с продавца при успешной сделке.

<b>Поддержка:</b> @{}
""".format(COMMISSION_PERCENT, SUPPORT_USERNAME)
        await update.message.reply_text(help_text, parse_mode="HTML")

async def cmd_deals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/deals <count> - для воркеров"""
    user = update.effective_user
    if not user or not is_worker(user.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /deals <количество>")
        return
    try:
        count = int(args[0])
        if count < 0:
            await update.message.reply_text("❌ Не может быть отрицательным")
            return
        ensure_user(user.id)
        user_data[user.id]["successful_deals"] = count
        save_user(user.id)
        await update.message.reply_text(f"✅ Количество сделок установлено: {count}")
    except ValueError:
        await update.message.reply_text("❌ Введите число")

async def cmd_money(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/money <amount> - для воркеров"""
    user = update.effective_user
    if not user or not is_worker(user.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /money <сумма USD>")
        return
    try:
        amount = float(args[0].replace(",", "."))
        if amount < 0:
            await update.message.reply_text("❌ Не может быть отрицательной")
            return
        ensure_user(user.id)
        user_data[user.id]["total_volume_usd"] = amount
        save_user(user.id)
        await update.message.reply_text(f"✅ Объём сделок установлен: {amount:.2f}$")
    except ValueError:
        await update.message.reply_text("❌ Введите число")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, Conflict):
        logger.warning("Conflict: Another bot instance running")
        return
    logger.error(f"Error: {context.error}", exc_info=context.error)
    # Notify admins
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(
                aid,
                f"⚠️ Bot error: {str(context.error)[:200]}",
                parse_mode="HTML"
            )
        except:
            pass

# ========== MAIN ==========
def signal_handler(signum, frame):
    logger.info("Received shutdown signal")
    _shutdown_flag.set()

def main():
    # Single instance lock
    if not acquire_lock():
        print("Another instance is already running. Exiting.")
        sys.exit(1)
    atexit.register(release_lock)
    
    # Signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Initialize
    init_db()
    load_data()
    
    # Create application
    request = HTTPXRequest(connect_timeout=30, read_timeout=30)
    application = Application.builder().token(BOT_TOKEN).request(request).concurrent_updates(50).build()
    
    # Register handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("deals", cmd_deals))
    application.add_handler(CommandHandler("money", cmd_money))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.add_error_handler(error_handler)
    
    # Start background threads
    if TON_API_KEY and TON_DEPOSIT_ADDRESS:
        deposit_thread = threading.Thread(target=deposit_monitor_loop, name="DepositMonitor", daemon=True)
        deposit_thread.start()
        logger.info("Deposit monitor started")
    
    expiry_thread = threading.Thread(target=expiry_watcher_loop, args=(application,), name="ExpiryWatcher", daemon=True)
    expiry_thread.start()
    logger.info("Expiry watcher started")
    
    # Process existing pending withdrawals on startup
if HAS_PYTONIQ and BOT_WALLET_MNEMONIC:
    pass  # Will be processed after loop starts

logger.info("Bot started")
    
    # Run polling
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        _shutdown_flag.set()
        release_lock()
        logger.info("Bot stopped")

if __name__ == "__main__":
    main()
if __name__ == "__main__":
    main()