import asyncio
import math
import base64
import io
import os
import random
import re
import secrets
import sqlite3
import string
import time
from html import escape
from urllib.parse import urlparse

import aiohttp
import discord
from aiohttp import web
from discord import app_commands


# ---------- Settings (read from config.txt) ----------
def load_config():
    if not os.path.exists("config.txt"):
        with open("config.txt", "w", encoding="utf-8") as f:
            f.write(
                "TOKEN=PASTE_YOUR_TOKEN_HERE\n"
                "ADMIN_PASSWORD=CHOOSE_A_LONG_PASSWORD\n"
                "# Optional:\n"
                "# SHOP_TITLE=Vending Machine\n"
                "# WEB_PORT=8080\n"
                "# TRON_ADDRESS=T...\n# USDT_RATE_KRW=1450\n# TRONGRID_API_KEY=\n"
                "# LTC_ADDRESS=ltc1...\n# LTC_RATE_KRW=  (optional fixed rate; live Upbit price is used by default)\n"
                "# BANK_INFO=Bank 123-456-789 Owner (can also be set in the web panel)\n"
            )
        print("Created config.txt. Fill in TOKEN and ADMIN_PASSWORD, then run the bot again.")
        raise SystemExit
    cfg = {}
    with open("config.txt", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


CONFIG = load_config()
TOKEN = CONFIG.get("TOKEN", "")
if not TOKEN or TOKEN.startswith("PASTE"):
    print("Put your bot token in config.txt (TOKEN=...) and run again.")
    raise SystemExit
ADMIN_PASSWORD = CONFIG.get("ADMIN_PASSWORD", "")
SHOP_TITLE = CONFIG.get("SHOP_TITLE", "Vending Machine")
WEB_HOST = "127.0.0.1"  # the tunnel/proxy connects locally
WEB_PORT = int(CONFIG["WEB_PORT"]) if CONFIG.get("WEB_PORT", "").isdigit() else 8080
TRON_ADDRESS = CONFIG.get("TRON_ADDRESS", "")  # RECEIVE-ONLY wallet address (never a private key)
TRONGRID_API_KEY = CONFIG.get("TRONGRID_API_KEY", "")
USDT_RATE_KRW = int(v) if (v := CONFIG.get("USDT_RATE_KRW", "")).isdigit() else 0  # fixed KRW per 1 USDT
BANK_INFO = CONFIG.get("BANK_INFO", "")
if TRON_ADDRESS and not re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", TRON_ADDRESS):
    print("WARNING: TRON_ADDRESS is not a valid TRON address. Crypto payments are disabled.")
    TRON_ADDRESS = ""
CRYPTO_ON = bool(TRON_ADDRESS and USDT_RATE_KRW > 0)
LTC_ADDRESS = CONFIG.get("LTC_ADDRESS", "")  # RECEIVE-ONLY Litecoin address (never a private key)
LTC_RATE_KRW = int(v) if (v := CONFIG.get("LTC_RATE_KRW", "")).isdigit() else 0  # optional fixed KRW per 1 LTC
if LTC_ADDRESS and not re.fullmatch(r"[LM3][1-9A-HJ-NP-Za-km-z]{26,33}|ltc1[02-9ac-hj-np-z]{39,59}", LTC_ADDRESS):
    print("WARNING: LTC_ADDRESS is not a valid Litecoin address. LTC payments are disabled.")
    LTC_ADDRESS = ""
LTC_ON = bool(LTC_ADDRESS)
DEFAULT_DESC = "Pick a product from the menu, enter a quantity and an optional coupon, and it is delivered instantly.\nUse Charge to add balance."
USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
ORDER_MINUTES = 60
MIN_CHARGE, MAX_CHARGE = 1000, 1000000
MAX_QTY = 50  # most items one purchase can take

# ---------- Database ----------
db = sqlite3.connect("vending.db")
db.executescript(
    """
CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, price INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
    content TEXT NOT NULL, sold_to INTEGER, sold_at TEXT);
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, method TEXT, krw INTEGER,
    expected INTEGER, depositor TEXT, status TEXT, tx_id TEXT UNIQUE, created_ts INTEGER);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sales (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product_name TEXT, price INTEGER, at TEXT);
CREATE TABLE IF NOT EXISTS coupons (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind TEXT NOT NULL, value INTEGER NOT NULL, max_uses INTEGER NOT NULL DEFAULT 0, used INTEGER NOT NULL DEFAULT 0,
    min_amount INTEGER NOT NULL DEFAULT 0, product_id INTEGER, expires TEXT, per_user INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS seen_tx (key TEXT PRIMARY KEY, at TEXT);
CREATE TABLE IF NOT EXISTS coupon_uses (coupon_id INTEGER, user_id INTEGER, at TEXT);
CREATE TABLE IF NOT EXISTS ref_rewards (id INTEGER PRIMARY KEY AUTOINCREMENT, referrer INTEGER, referee INTEGER,
    order_id INTEGER, amount INTEGER, at TEXT);
"""
)


def add_column(table: str, column: str, decl: str):
    """Upgrade an older vending.db in place (existing data is kept)."""
    if column not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


add_column("sales", "product_id", "INTEGER")
add_column("sales", "qty", "INTEGER NOT NULL DEFAULT 1")
add_column("sales", "discount", "INTEGER NOT NULL DEFAULT 0")
add_column("sales", "coupon", "TEXT")
add_column("sales", "content", "TEXT")
add_column("users", "referrer", "INTEGER")
db.commit()


def S(key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_S(key: str, value: str):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()


def kst_today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 9 * 3600))


# ---------- Shop logic ----------
def get_balance(uid: int) -> int:
    row = db.execute("SELECT balance FROM users WHERE user_id = ?", (uid,)).fetchone()
    return row[0] if row else 0


def add_balance(uid: int, amount: int):
    db.execute(
        "INSERT INTO users (user_id, balance) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?",
        (uid, amount, amount),
    )
    db.commit()


def stock(pid: int) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM items WHERE product_id = ? AND sold_to IS NULL", (pid,)
    ).fetchone()[0]


def find_coupon(code: str, uid: int, pid: int, total: int):
    """Returns (coupon_id, discount, error_message)."""
    row = db.execute(
        "SELECT id, kind, value, max_uses, used, min_amount, product_id, expires, per_user, active "
        "FROM coupons WHERE code = ?",
        (code,),
    ).fetchone()
    if not row or not row[9]:
        return None, 0, "That coupon code does not exist."
    cid, kind, value, max_uses, used, min_amount, c_pid, expires, per_user, _ = row
    if expires and expires < kst_today():
        return None, 0, "This coupon has expired."
    if max_uses and used >= max_uses:
        return None, 0, "This coupon has been fully used up."
    if c_pid and c_pid != pid:
        return None, 0, "This coupon cannot be used on this product."
    if total < min_amount:
        return None, 0, f"This coupon needs a purchase of at least ₩{min_amount:,}."
    if per_user and db.execute(
        "SELECT 1 FROM coupon_uses WHERE coupon_id = ? AND user_id = ?", (cid, uid)
    ).fetchone():
        return None, 0, "You have already used this coupon."
    discount = total * value // 100 if kind == "percent" else value
    return cid, min(discount, total), None


def sell(uid: int, pid: int, qty: int = 1, code: str = ""):
    """Returns (status, value): ok / no_product / bad_qty / no_stock / coupon / no_balance."""
    prod = db.execute("SELECT name, price FROM products WHERE id = ?", (pid,)).fetchone()
    if not prod:
        return "no_product", None
    name, price = prod
    if not 1 <= qty <= MAX_QTY:
        return "bad_qty", None
    items = db.execute(
        "SELECT id, content FROM items WHERE product_id = ? AND sold_to IS NULL ORDER BY id LIMIT ?", (pid, qty)
    ).fetchall()
    if len(items) < qty:
        return "no_stock", len(items)
    total = price * qty
    cid, discount = None, 0
    if code:
        cid, discount, err = find_coupon(code, uid, pid, total)
        if err:
            return "coupon", err
    pay = total - discount
    if get_balance(uid) < pay:
        return "no_balance", pay
    content = "\n".join(c for _, c in items)
    try:
        db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (pay, uid))
        db.executemany(
            "UPDATE items SET sold_to = ?, sold_at = datetime('now') WHERE id = ?", [(uid, i) for i, _ in items]
        )
        db.execute(
            "INSERT INTO sales (user_id, product_id, product_name, qty, price, discount, coupon, content, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (uid, pid, name, qty, pay, discount, code.upper() if cid else None, content),
        )
        if cid:
            db.execute("UPDATE coupons SET used = used + 1 WHERE id = ?", (cid,))
            db.execute("INSERT INTO coupon_uses (coupon_id, user_id, at) VALUES (?, ?, datetime('now'))", (cid, uid))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return "ok", {
        "name": name, "qty": qty, "total": total, "discount": discount, "pay": pay,
        "coupon": code.upper() if cid else "", "content": content,
    }


def product_options():
    rows = db.execute("SELECT id, name, price FROM products ORDER BY id LIMIT 25").fetchall()
    opts = [
        discord.SelectOption(label=f"{n} · ₩{p:,} · {stock(i)} in stock"[:100], value=str(i)) for i, n, p in rows
    ]
    return opts or [discord.SelectOption(label="No products yet", value="0")]


async def send_text(interaction: discord.Interaction, text: str, file_text: str = "", filename: str = "items.txt", **kw):
    """Ephemeral reply; long item contents are attached as a .txt file instead of inline."""
    if file_text:
        kw["file"] = discord.File(io.BytesIO(file_text.encode("utf-8")), filename=filename)
    await interaction.response.send_message(text, ephemeral=True, **kw)


# ---------- Referral ----------
def ref_percent() -> int:
    v = S("ref_percent", "0")
    return min(int(v), 100) if v.isdigit() else 0


def get_referrer(uid: int):
    row = db.execute("SELECT referrer FROM users WHERE user_id = ?", (uid,)).fetchone()
    return row[0] if row else None


def register_referrer(uid: int, rid: int):
    """Returns an error message, or None when saved."""
    if get_referrer(uid):
        return "You already registered a referrer (it cannot be changed)."
    if rid == uid:
        return "You cannot refer yourself."
    if get_referrer(rid) == uid:
        return "Two users cannot refer each other."
    if db.execute("SELECT 1 FROM orders WHERE user_id = ? AND status = 'paid'", (uid,)).fetchone():
        return "A referrer can only be registered before your first charge."
    db.execute(
        "INSERT INTO users (user_id, balance, referrer) VALUES (?, 0, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET referrer = excluded.referrer WHERE users.referrer IS NULL",
        (uid, rid),
    )
    db.commit()
    return None


def give_referral_reward(uid: int, krw: int, order_id: int):
    """Pays the referrer a share of a completed charge. Returns (referrer_id, reward) or None."""
    pct, rid = ref_percent(), get_referrer(uid)
    if not pct or not rid:
        return None
    if S("ref_first_only") == "1":
        paid = db.execute("SELECT COUNT(*) FROM orders WHERE user_id = ? AND status = 'paid'", (uid,)).fetchone()[0]
        if paid > 1:
            return None
    reward = krw * pct // 100
    if reward <= 0:
        return None
    add_balance(rid, reward)
    db.execute(
        "INSERT INTO ref_rewards (referrer, referee, order_id, amount, at) VALUES (?, ?, ?, ?, datetime('now'))",
        (rid, uid, order_id, reward),
    )
    db.commit()
    return rid, reward


# ---------- Discord shop UI ----------
class BuyModal(discord.ui.Modal):
    def __init__(self, pid: int, name: str, price: int):
        super().__init__(title=f"Buy {name}"[:45])
        self.pid = pid
        self.qty = discord.ui.TextInput(
            label=f"Quantity (₩{price:,} each · {stock(pid)} left)"[:45], default="1", max_length=3
        )
        self.code = discord.ui.TextInput(label="Coupon code (optional)", required=False, max_length=30)
        self.add_item(self.qty)
        self.add_item(self.code)

    async def on_submit(self, interaction: discord.Interaction):
        uid = interaction.user.id
        qty_text = self.qty.value.strip()
        if not qty_text.isdigit():
            await interaction.response.send_message(f"❌ Enter a quantity between 1 and {MAX_QTY}.", ephemeral=True)
            return
        status, value = sell(uid, self.pid, int(qty_text), self.code.value.strip())
        if status == "ok":
            v = value
            coupon_note = f" · coupon {v['coupon']} -₩{v['discount']:,}" if v["coupon"] else ""
            await send_log(
                "purchase", f"🛒 <@{uid}> bought **{v['name']}** x{v['qty']} (₩{v['pay']:,}{coupon_note})"
            )
            head = (
                f"✅ Purchased **{v['name']}** x{v['qty']}!\n"
                f"Paid ₩{v['pay']:,}" + (f" (discount ₩{v['discount']:,})" if v["discount"] else "")
                + f" · Balance ₩{get_balance(uid):,}\n"
            )
            if len(head) + len(v["content"]) + 10 <= 1900:
                await send_text(interaction, f"{head}\n||{v['content']}||")
            else:
                await send_text(interaction, head + "Your items are attached as a file.", v["content"])
            return
        if status == "no_balance":
            msg = f"❌ Not enough balance. Total ₩{value:,}, your balance ₩{get_balance(uid):,}."
        elif status == "no_stock":
            msg = f"❌ Not enough stock ({value} left)."
        elif status == "bad_qty":
            msg = f"❌ Enter a quantity between 1 and {MAX_QTY}."
        elif status == "coupon":
            msg = f"❌ {value}"
        else:
            msg = "❌ Product not found. Ask an admin to repost the shop."
        await interaction.response.send_message(msg, ephemeral=True)


class ReferralModal(discord.ui.Modal, title="Register referrer"):
    ref = discord.ui.TextInput(label="Referrer's Discord user ID", placeholder="123456789012345678", max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        text = self.ref.value.strip()
        if not text.isdigit():
            await interaction.response.send_message("❌ Enter a numeric Discord user ID.", ephemeral=True)
            return
        rid = int(text)
        try:
            user = await bot.fetch_user(rid)
        except Exception:
            user = None
        if user is None or user.bot:
            await interaction.response.send_message("❌ No user found with that ID.", ephemeral=True)
            return
        err = register_referrer(interaction.user.id, rid)
        if err:
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ {user.mention} is now your referrer.", ephemeral=True)


class ReferralView(discord.ui.View):
    def __init__(self, can_register: bool):
        super().__init__(timeout=300)
        self.register.disabled = not can_register

    @discord.ui.button(label="Register referrer", style=discord.ButtonStyle.primary, emoji="🤝")
    async def register(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ReferralModal())


class Shop(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.buy.options = product_options()

    @discord.ui.select(custom_id="shop:buy", placeholder="Choose a product to buy...", options=[], row=0)
    async def buy(self, interaction: discord.Interaction, select: discord.ui.Select):
        pid = int(select.values[0])
        prod = db.execute("SELECT name, price FROM products WHERE id = ?", (pid,)).fetchone()
        if prod:
            await interaction.response.send_modal(BuyModal(pid, prod[0], prod[1]))
        else:
            await interaction.response.send_message(
                "❌ Product not found. Ask an admin to repost the shop.", ephemeral=True
            )
        try:  # reset the menu (and refresh stock counts) so the same product can be picked again
            await interaction.message.edit(view=Shop())
        except Exception:
            pass

    @discord.ui.button(label="Charge", style=discord.ButtonStyle.success, emoji="💳", custom_id="shop:charge", row=1)
    async def charge(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (CRYPTO_ON or LTC_ON or bank_on()):
            await interaction.response.send_message("❌ Online charging is not set up.", ephemeral=True)
            return
        await interaction.response.send_message("Choose a payment method:", view=ChargeView(), ephemeral=True)

    @discord.ui.button(label="Balance", style=discord.ButtonStyle.secondary, emoji="💰", custom_id="shop:balance", row=1)
    async def balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(f"💰 Your balance: ₩{get_balance(interaction.user.id):,}", ephemeral=True)

    @discord.ui.button(label="Purchase history", style=discord.ButtonStyle.secondary, emoji="🧾", custom_id="shop:history", row=1)
    async def history(self, interaction: discord.Interaction, button: discord.ui.Button):
        rows = db.execute(
            "SELECT datetime(at, '+9 hours'), product_name, qty, price, content FROM sales "
            "WHERE user_id = ? ORDER BY id DESC LIMIT 10",
            (interaction.user.id,),
        ).fetchall()
        if not rows:
            await interaction.response.send_message("🧾 You have no purchases yet.", ephemeral=True)
            return
        lines = [f"`{at[:16]}` **{n}** x{q} · ₩{p:,}" for at, n, q, p, _ in rows]
        embed = discord.Embed(title="Recent purchases (last 10)", description="\n".join(lines), color=0x6C5CE7)
        full = "\n\n".join(f"[{at} KST] {n} x{q}\n{c}" for at, n, q, _, c in rows if c)
        if full:
            embed.set_footer(text="The items you bought are in the attached file.")
        await send_text(interaction, "", full, "purchase_history.txt", embed=embed)

    @discord.ui.button(label="Referral", style=discord.ButtonStyle.secondary, emoji="🤝", custom_id="shop:referral", row=1)
    async def referral(self, interaction: discord.Interaction, button: discord.ui.Button):
        pct = ref_percent()
        if not pct:
            await interaction.response.send_message("The referral program is not active right now.", ephemeral=True)
            return
        uid = interaction.user.id
        cnt = db.execute("SELECT COUNT(*) FROM users WHERE referrer = ?", (uid,)).fetchone()[0]
        earned = db.execute("SELECT COALESCE(SUM(amount), 0) FROM ref_rewards WHERE referrer = ?", (uid,)).fetchone()[0]
        mine = get_referrer(uid)
        when = "on their first charge" if S("ref_first_only") == "1" else "every time they charge"
        embed = discord.Embed(
            title="🤝 Referral",
            description=(
                f"When a friend you referred charges, you get **{pct}%** of it as balance ({when}).\n"
                f"Give your friends your user ID: `{uid}`\n\n"
                f"Friends referred: **{cnt}** · Rewards earned: **₩{earned:,}**\n"
                f"Your referrer: {f'<@{mine}>' if mine else 'none'}"
            ),
            color=0x6C5CE7,
        )
        await interaction.response.send_message(embed=embed, view=ReferralView(not mine), ephemeral=True)


# ---------- Payments (crypto + bank transfer) ----------
def expire_old():
    db.execute(
        "UPDATE orders SET status = 'expired' WHERE status = 'pending' AND created_ts < ?",
        (int(time.time()) - ORDER_MINUTES * 60,),
    )
    db.commit()


def parse_amount(text: str):
    digits = text.replace(",", "").strip()
    if not digits.isdigit():
        return None
    n = int(digits)
    return n if MIN_CHARGE <= n <= MAX_CHARGE else None


def pending_count(uid: int) -> int:
    return db.execute("SELECT COUNT(*) FROM orders WHERE user_id = ? AND status = 'pending'", (uid,)).fetchone()[0]


def create_order(uid, method, krw, expected=None, depositor=None) -> int:
    cur = db.execute(
        "INSERT INTO orders (user_id, method, krw, expected, depositor, status, created_ts) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (uid, method, krw, expected, depositor, int(time.time())),
    )
    db.commit()
    return cur.lastrowid


def crypto_quote(method: str, krw: int, price: float) -> int:
    """Exact amount to send, in the coin's smallest unit (USDT: 1e-6, LTC: 1e-8), rounded up slightly."""
    if method == "crypto":  # USDT, rounded up to 0.01 USDT
        return -(-krw * 100 // USDT_RATE_KRW) * 10_000
    return math.ceil(krw * 100_000_000 / price / 1000) * 1000  # LTC, rounded up to 0.00001 LTC


_ltc_price = [0.0, 0.0]  # [fetched_at, KRW per LTC]


async def ltc_price_krw():
    """Live KRW price from Upbit (cached 60s). Falls back to LTC_RATE_KRW from config.txt."""
    if LTC_RATE_KRW:
        return float(LTC_RATE_KRW)
    if time.time() - _ltc_price[0] < 60:
        return _ltc_price[1]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.upbit.com/v1/ticker", params={"markets": "KRW-LTC"},
                timeout=aiohttp.ClientTimeout(total=2.5),
            ) as resp:
                price = float((await resp.json())[0]["trade_price"])
        if price > 0:
            _ltc_price[:] = [time.time(), price]
            return price
    except Exception as e:
        print("LTC price fetch failed:", e)
    return _ltc_price[1] or None


def complete_order(order_id: int, tx_id=None, credit=None):
    """Mark paid exactly once and credit the balance. Returns (user_id, krw, referral) or None.
    `credit` overrides the order amount (crypto: what the payment was actually worth)."""
    try:
        cur = db.execute(
            "UPDATE orders SET status = 'paid', tx_id = ?, krw = COALESCE(?, krw) "
            "WHERE id = ? AND status IN ('pending', 'expired')",
            (tx_id, credit, order_id),
        )
    except sqlite3.IntegrityError:  # this transaction was already used
        db.rollback()
        return None
    if not cur.rowcount:
        db.commit()
        return None
    uid, krw = db.execute("SELECT user_id, krw FROM orders WHERE id = ?", (order_id,)).fetchone()
    add_balance(uid, krw)
    return uid, krw, give_referral_reward(uid, krw, order_id)


async def send_log(kind: str, text: str):
    channel_id = S(f"log_{kind}")
    if not channel_id.isdigit():
        return
    try:
        channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
        await channel.send(text)
    except Exception:
        pass  # missing channel or permissions: ignore


async def notify_paid(uid: int, krw: int, ref=None):
    await send_log("charge", f"💳 <@{uid}> charged ₩{krw:,}")
    try:
        user = await bot.fetch_user(uid)
        await user.send(f"✅ Payment received! ₩{krw:,} was added. Balance: ₩{get_balance(uid):,}")
    except Exception:
        pass  # DMs may be closed; the balance is credited anyway
    if ref:
        rid, reward = ref
        await send_log("charge", f"🤝 <@{rid}> got a ₩{reward:,} referral reward (<@{uid}> charged)")
        try:
            user = await bot.fetch_user(rid)
            await user.send(f"🤝 A friend you referred charged, so you got ₩{reward:,}! Balance: ₩{get_balance(rid):,}")
        except Exception:
            pass


# ----- TXID verification -----
COIN = {"crypto": "USDT", "ltc": "LTC"}
TX_KEY = {"crypto": "{}", "ltc": "ltc:{}"}  # how the tx id is stored in orders.tx_id (USDT keeps the raw id)
MIN_PAID = 0.9  # a payment must be worth at least 90% of the quote (exchanges sometimes deduct fees)


async def fetch_usdt_tx(session, txid: str, since: int):
    """Returns ("ok", units_received, block_time) / ("pending", ...) / ("missing", ...)."""
    url = f"https://api.trongrid.io/v1/accounts/{TRON_ADDRESS}/transactions/trc20"
    params = {
        "only_to": "true", "only_confirmed": "true", "limit": "200", "contract_address": USDT_TRC20,
        "min_timestamp": str(since * 1000), "order_by": "block_timestamp,asc",
    }
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
    async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        data = await resp.json()
    value, when = 0, 0
    for tx in data.get("data", []):
        if tx.get("transaction_id", "").lower() == txid and tx.get("to") == TRON_ADDRESS:
            value += int(tx.get("value") or 0)
            when = int(tx.get("block_timestamp") or 0) // 1000
    return ("ok", value, when) if value else ("missing", 0, 0)


async def fetch_ltc_tx(session, txid: str, since: int):
    async with session.get(f"https://litecoinspace.org/api/tx/{txid}", timeout=aiohttp.ClientTimeout(total=15)) as resp:
        if resp.status != 200:
            return "missing", 0, 0
        tx = await resp.json()
    value = sum(int(o.get("value") or 0) for o in tx.get("vout") or [] if o.get("scriptpubkey_address") == LTC_ADDRESS)
    if not value:
        return "wrong_address", 0, 0
    status = tx.get("status") or {}
    if not status.get("confirmed"):
        return "pending", value, 0
    return "ok", value, int(status.get("block_time") or 0)


FETCH = {"crypto": fetch_usdt_tx, "ltc": fetch_ltc_tx}


async def claim_txid(uid: int, txid: str, order_id=None):
    """Checks a submitted TXID against this user's open crypto orders. Returns a message for the user."""
    txid = txid.strip().lower().removeprefix("0x")
    if not re.fullmatch(r"[0-9a-f]{64}", txid):
        return "❌ That doesn't look like a TXID. It is a 64-character code of letters and numbers."
    for method in ("crypto", "ltc"):
        row = db.execute("SELECT user_id FROM orders WHERE tx_id = ?", (TX_KEY[method].format(txid),)).fetchone()
        if row:
            if row[0] == uid:
                return f"✅ This payment was already credited to you. Balance: ₩{get_balance(uid):,}"
            return "❌ This TXID has already been used."
    expire_old()
    sql = (
        "SELECT id, method, krw, expected, created_ts FROM orders WHERE user_id = ? AND method IN ('crypto', 'ltc') "
        "AND status IN ('pending', 'expired') AND created_ts > ?"
    )
    args = [uid, int(time.time()) - 86400]  # requests can be claimed up to 24h later
    if order_id:
        sql += " AND id = ?"
        args.append(order_id)
    orders = db.execute(sql + " ORDER BY id DESC", args).fetchall()
    if not orders:
        return "❌ You have no open crypto charge request. Press Charge and create one first."
    results, pending_seen = {}, False
    async with aiohttp.ClientSession() as session:
        for oid, method, krw, expected, created in orders:
            if method not in results:
                try:
                    results[method] = await FETCH[method](session, txid, min(o[4] for o in orders) - 60)
                except Exception as e:
                    print("TXID check failed:", e)
                    return "❌ Could not reach the blockchain right now. Please try again in a minute."
            state, value, when = results[method]
            if state == "pending":
                pending_seen = True
                continue
            if state != "ok" or not expected:
                continue
            if not (created - 60 <= when <= created + ORDER_MINUTES * 60):
                continue  # must be sent while this request was open
            if value < expected * MIN_PAID:
                return (
                    f"❌ That payment ({value / (1e6 if method == 'crypto' else 1e8):g} {COIN[method]}) is less than "
                    "the amount requested. Contact an admin."
                )
            credit = krw * value // expected  # credit what was actually sent, at the locked rate
            done = complete_order(oid, TX_KEY[method].format(txid), credit)
            if not done:
                return "❌ This TXID has already been used."
            await notify_paid(*done)
            return f"✅ Payment confirmed! ₩{done[1]:,} was added. Balance: ₩{get_balance(uid):,}"
    if pending_seen:
        return "⏳ Found your payment, but it is not confirmed yet. Try again in a few minutes."
    return (
        "❌ No matching payment found. Check that you sent it to the right address and network, "
        "after creating the request. TRON transfers can take about a minute to show up, so try again shortly."
    )


# ----- Automatic wallet check (every 30 seconds) -----
async def list_usdt_incoming(session, since: int):
    """Confirmed USDT transfers into our wallet since `since`: [(txid, value, block_time)]."""
    url = f"https://api.trongrid.io/v1/accounts/{TRON_ADDRESS}/transactions/trc20"
    params = {
        "only_to": "true", "only_confirmed": "true", "limit": "50", "contract_address": USDT_TRC20,
        "min_timestamp": str(since * 1000),
    }
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
    async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        data = await resp.json()
    found = {}
    for tx in data.get("data", []):
        if tx.get("to") != TRON_ADDRESS or not tx.get("transaction_id"):
            continue
        txid = tx["transaction_id"].lower()
        value, _ = found.get(txid, (0, 0))
        found[txid] = (value + int(tx.get("value") or 0), int(tx.get("block_timestamp") or 0) // 1000)
    return [(t, v, w) for t, (v, w) in found.items()]


async def list_ltc_incoming(session, since: int):
    """Confirmed LTC payments into our wallet (latest ones): [(txid, value, block_time)]."""
    url = f"https://litecoinspace.org/api/address/{LTC_ADDRESS}/txs"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        txs = await resp.json()
    out = []
    for tx in txs:
        status = tx.get("status") or {}
        if not status.get("confirmed") or not tx.get("txid"):
            continue
        value = sum(int(o.get("value") or 0) for o in tx.get("vout") or [] if o.get("scriptpubkey_address") == LTC_ADDRESS)
        if value:
            out.append((tx["txid"].lower(), value, int(status.get("block_time") or 0)))
    return out


LIST_INCOMING = {"crypto": list_usdt_incoming, "ltc": list_ltc_incoming}


async def auto_check(session):
    """Credits a payment by itself when exactly one open request matches its exact amount.
    Anything ambiguous is left for the customer's TXID and reported in the charge log."""
    expire_old()
    now = int(time.time())
    orders = db.execute(
        "SELECT id, method, expected, created_ts FROM orders WHERE method IN ('crypto', 'ltc') "
        "AND status IN ('pending', 'expired') AND created_ts > ?",
        (now - 86400,),
    ).fetchall()
    enabled = {"crypto": CRYPTO_ON, "ltc": LTC_ON}
    # only poll while a request is open (plus 10 minutes for slow confirmations)
    active = {m for _, m, _, c in orders if enabled[m] and c > now - ORDER_MINUTES * 60 - 600}
    credited = set()
    for method in active:
        since = min(c for _, m, _, c in orders if m == method) - 60
        for txid, value, when in await LIST_INCOMING[method](session, since):
            if when < since:
                continue
            key = TX_KEY[method].format(txid)
            if db.execute("SELECT 1 FROM orders WHERE tx_id = ?", (key,)).fetchone():
                continue  # already credited
            matches = [
                oid for oid, m, expected, created in orders
                if m == method and oid not in credited and expected == value
                and created - 60 <= when <= created + ORDER_MINUTES * 60
            ]
            if len(matches) == 1:
                done = complete_order(matches[0], key)
                if done:
                    credited.add(matches[0])
                    await notify_paid(*done)
                continue
            if db.execute("SELECT 1 FROM seen_tx WHERE key = ?", (key,)).fetchone():
                continue  # already reported
            db.execute("INSERT OR IGNORE INTO seen_tx (key, at) VALUES (?, datetime('now'))", (key,))
            db.commit()
            amount = value / (1e6 if method == "crypto" else 1e8)
            reason = "matches several open requests" if matches else "doesn't match any open request"
            await send_log(
                "charge",
                f"⚠️ Received {amount:g} {COIN[method]} that {reason}, so it was not credited automatically. "
                f"It will be credited when the customer submits the TXID, or approve it in the panel.\nTXID: `{txid}`",
            )


async def crypto_loop():
    await bot.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await auto_check(session)
            except Exception as e:
                print("wallet check failed:", e)
            await asyncio.sleep(30)


async def deposit_hook(request: web.Request):
    """POST /api/deposit?token=... with JSON {"text": "<bank deposit SMS>"} (sent by a phone automation)."""
    if not secrets.compare_digest(request.query.get("token", "").encode(), deposit_token().encode()):
        return web.Response(status=403, text="Forbidden")
    try:
        data = await request.json()
    except Exception:
        data = dict(await request.post())
    text = str(data.get("text", ""))
    if "입금" not in text:  # only deposit messages (not withdrawals)
        return web.Response(status=400, text="Not a deposit message")
    expire_old()
    amounts = {int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", text) if n.replace(",", "").isdigit()}
    for oid, krw, name in db.execute(
        "SELECT id, krw, depositor FROM orders WHERE method = 'bank' AND status = 'pending' ORDER BY id"
    ).fetchall():
        if krw in amounts and name and name.lower() in text.lower():
            done = complete_order(oid)
            if done:
                await notify_paid(*done)
                return web.Response(text="ok")
    return web.Response(status=404, text="No matching order")


class TxidModal(discord.ui.Modal, title="Submit your TXID"):
    txid = discord.ui.TextInput(
        label="Transaction ID (TXID / hash)", placeholder="64 letters and numbers", min_length=64, max_length=70
    )

    def __init__(self, order_id=None):
        super().__init__()
        self.order_id = order_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)  # blockchain lookups can take a few seconds
        msg = await claim_txid(interaction.user.id, self.txid.value, self.order_id)
        await interaction.followup.send(msg, ephemeral=True)


class PayView(discord.ui.View):
    def __init__(self, order_id: int):
        super().__init__(timeout=ORDER_MINUTES * 60)
        self.order_id = order_id

    @discord.ui.button(label="I paid · Submit TXID", style=discord.ButtonStyle.success, emoji="🧾")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TxidModal(self.order_id))


class CoinModal(discord.ui.Modal):
    amount = discord.ui.TextInput(label="Amount to charge (KRW)", placeholder="10000")

    def __init__(self, method: str):
        super().__init__(title=f"Charge with {'USDT (TRC-20)' if method == 'crypto' else 'Litecoin (LTC)'}")
        self.method = method

    async def on_submit(self, interaction: discord.Interaction):
        krw = parse_amount(self.amount.value)
        if krw is None:
            await interaction.response.send_message(
                f"❌ Enter an amount between ₩{MIN_CHARGE:,} and ₩{MAX_CHARGE:,}.", ephemeral=True
            )
            return
        expire_old()
        if pending_count(interaction.user.id) >= 3:
            await interaction.response.send_message("❌ Too many open payment requests. Pay or wait for them to expire.", ephemeral=True)
            return
        if self.method == "crypto":
            price, address, network, decimals, unit = USDT_RATE_KRW, TRON_ADDRESS, "TRON (TRC-20)", 2, 1_000_000
        else:
            price = await ltc_price_krw()
            if not price:
                await interaction.response.send_message("❌ Could not get the LTC price right now. Please try again.", ephemeral=True)
                return
            address, network, decimals, unit = LTC_ADDRESS, "Litecoin", 5, 100_000_000
        expected = crypto_quote(self.method, krw, price)
        oid = create_order(interaction.user.id, self.method, krw, expected=expected)
        coin = COIN[self.method]
        ts = int(time.time()) + ORDER_MINUTES * 60
        embed = discord.Embed(
            title=f"Pay with {coin}",
            description=(
                f"**1.** Send `{expected / unit:.{decimals}f}` {coin} on the **{network}** network to:\n`{address}`\n"
                f"Rate: 1 {coin} = ₩{price:,.0f} (locked for this request)\n\n"
                f"**2.** ₩{krw:,} is usually added **automatically** within a few minutes of the payment confirming.\n\n"
                "**3.** Not added after 5 minutes? Copy the **TXID** (transaction ID / hash) from your wallet or "
                "exchange withdrawal history, press **I paid · Submit TXID** below and paste it.\n\n"
                f"Send it before <t:{ts}:t> (<t:{ts}:R>).\n"
                "If an exchange takes its fee out of the amount, you are credited for what actually arrives.\n"
                f"⚠️ Only send {coin} on the {network} network."
            ),
            color=0xDB9509 if self.method == "crypto" else 0x345D9D,
        )
        await interaction.response.send_message(embed=embed, view=PayView(oid), ephemeral=True)


class BankModal(discord.ui.Modal, title="Charge by bank transfer"):
    amount = discord.ui.TextInput(label="Amount to charge (KRW)", placeholder="10000")
    depositor = discord.ui.TextInput(label="Depositor name (exactly as on your transfer)", max_length=30)

    async def on_submit(self, interaction: discord.Interaction):
        krw, name = parse_amount(self.amount.value), self.depositor.value.strip()
        if krw is None or not name:
            await interaction.response.send_message(
                f"❌ Enter an amount between ₩{MIN_CHARGE:,} and ₩{MAX_CHARGE:,} and your depositor name.",
                ephemeral=True,
            )
            return
        expire_old()
        if pending_count(interaction.user.id) >= 3:
            await interaction.response.send_message("❌ Too many open payment requests. Pay or wait for them to expire.", ephemeral=True)
            return
        create_order(interaction.user.id, "bank", krw, depositor=name)
        ts = int(time.time()) + ORDER_MINUTES * 60
        embed = discord.Embed(
            title="Pay by bank transfer",
            description=(
                f"Send **exactly** ₩{krw:,} to:\n{bank_info()}\n\nDepositor name: **{name}**\n"
                f"Your balance is added automatically once the deposit is detected. Expires <t:{ts}:R>."
            ),
            color=0xDB9509,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ChargeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.bank.disabled = not bank_on()

    @discord.ui.button(label="Tether (USDT TRC-20)", style=discord.ButtonStyle.primary, emoji="💵", disabled=not CRYPTO_ON)
    async def crypto(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CoinModal("crypto"))

    @discord.ui.button(label="Litecoin (LTC)", style=discord.ButtonStyle.primary, emoji="🪙", disabled=not LTC_ON)
    async def ltc(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CoinModal("ltc"))

    @discord.ui.button(label="Bank transfer", style=discord.ButtonStyle.primary, emoji="🏦")
    async def bank(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BankModal())

    @discord.ui.button(label="Submit TXID", style=discord.ButtonStyle.secondary, emoji="🧾", row=1,
                       disabled=not (CRYPTO_ON or LTC_ON))
    async def txid(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TxidModal())


# ---------- Web panel (/admin) ----------
NAV = [
    ("dashboard", "Dashboard"), ("stats", "Statistics"), ("products", "Products"), ("coupons", "Coupons"),
    ("orders", "Orders"), ("users", "Users / Balance"), ("charges", "Charges"), ("referral", "Referrals"),
    ("bank", "Bank Settings"), ("panel", "Panel Settings"), ("logs", "Log Settings"),
]
HIDDEN = {"stock": "products"}  # sub-pages and the menu item they belong to
CSS = """*{box-sizing:border-box}body{margin:0;display:flex;min-height:100vh;font-family:system-ui,sans-serif;background:#0b0b0e;color:#eee}
aside{width:220px;background:#111114;border-right:1px solid #222;padding:18px 12px;flex-shrink:0}aside h1{margin:0;font-size:18px}
aside small{color:#777;letter-spacing:1px;display:block;margin-bottom:18px}aside a{display:block;padding:10px 12px;border-radius:8px;color:#aaa;text-decoration:none;margin-bottom:2px}
aside a.on{background:#1c1b2e;color:#fff}main{flex:1;padding:24px;max-width:1100px;min-width:0}h2{margin:0 0 4px}p.sub{color:#888;margin:0 0 18px}
a{color:#a29bfe}.card{background:#111114;border:1px solid #222;border-radius:14px;padding:16px;margin-bottom:16px;overflow-x:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}.grid .card{margin-bottom:0}
.num{font-size:26px;font-weight:700}.mut{color:#888;font-size:13px}table{width:100%;border-collapse:collapse}td,th{padding:9px 6px;border-bottom:1px solid #222;text-align:left;font-size:14px;word-break:break-all;vertical-align:middle}
form{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0;align-items:center}form.blk{display:block}td form{margin:2px 0}
input,select,textarea,button{padding:9px;border-radius:8px;border:1px solid #333;background:#16161a;color:#eee;font:inherit}
textarea{width:100%;min-height:80px}button{background:#6c5ce7;border:0;cursor:pointer}button.s{background:#222}button.d{background:#5c1f2a}
code{background:#16161a;padding:2px 6px;border-radius:6px;word-break:break-all}.row{display:flex;gap:6px;flex-wrap:wrap}
.pv{border-left:4px solid #6c5ce7;background:#16161a;padding:12px;border-radius:6px}.bar{height:8px;background:#6c5ce7;border-radius:4px;min-width:2px}
.on1{color:#55efc4}.off1{color:#888}
@media(max-width:700px){body{flex-direction:column}aside{width:auto;display:flex;flex-wrap:wrap;gap:4px}main{padding:16px}}"""


def deposit_token() -> str:
    token = S("deposit_token")
    if not token:
        token = secrets.token_hex(24)
        set_S("deposit_token", token)
    return token


def bank_info() -> str:
    return S("bank_info") or BANK_INFO


def bank_on() -> bool:
    return bool(bank_info())


def check_admin(request) -> bool:
    if not ADMIN_PASSWORD or ADMIN_PASSWORD.startswith("CHOOSE"):
        return False
    try:
        auth = request.headers.get("Authorization", "")
        password = base64.b64decode(auth.split(" ", 1)[1]).decode().split(":", 1)[1]
    except Exception:
        return False
    return secrets.compare_digest(password.encode(), ADMIN_PASSWORD.encode())


def unauthorized():
    return web.Response(status=401, text="Login required", headers={"WWW-Authenticate": 'Basic realm="admin"'})


def form(action, page, fields, button, cls="", confirm="", ret_id=None):
    oc = f" onsubmit=\"return confirm('{confirm}')\"" if confirm else ""
    idf = f"<input type='hidden' name='id' value='{ret_id}'>" if ret_id is not None else ""
    return (
        f"<form method='post'{oc}><input type='hidden' name='action' value='{action}'>"
        f"<input type='hidden' name='p' value='{page}'>{idf}{fields}<button class='{cls}'>{button}</button></form>"
    )


def stat_cards(pairs) -> str:
    return "<div class='grid'>" + "".join(
        f"<div class='card'><div class='mut'>{k}</div><div class='num'>{v}</div></div>" for k, v in pairs
    ) + "</div>"


def table(headers, rows_html, empty="No data") -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = rows_html or f"<tr><td colspan={len(headers)} class='mut' style='text-align:center;padding:24px'>{empty}</td></tr>"
    return f"<table><tr>{head}</tr>{body}</table>"


def render_page(page: str, base: str, q=None) -> str:
    q = q or {}
    e = escape
    if page == "products":
        prods = db.execute("SELECT id, name, price FROM products ORDER BY id").fetchall()
        rows = ""
        for i, n, p in prods:
            edit = form(
                "edit_product", page,
                f"<input type=hidden name=product_id value={i}>"
                f"<input name=name value='{e(n)}' required maxlength=60>"
                f"<input name=price type=number min=0 value={p} required style='width:120px'>",
                "Save", "s",
            )
            delete = form(
                "delete_product", page, f"<input type=hidden name=product_id value={i}>", "Delete", "d",
                "Delete this product and its remaining stock?",
            )
            rows += f"<tr><td>{i}</td><td>{edit}</td><td><a href='/admin?p=stock&id={i}'>{stock(i)} · manage stock →</a></td><td>{delete}</td></tr>"
        opts = "".join(f"<option value='{i}'>{e(n)}</option>" for i, n, _ in prods)
        add_fields = "<input name=name placeholder='Product name' required maxlength=60><input name=price type=number min=0 placeholder=Price required>"
        stock_fields = f"<select name=product_id>{opts}</select><textarea name=lines required></textarea>"
        body = (
            "<h2>Products</h2><p class='sub'>Edit names and prices, view and delete stock</p><div class='card'>"
            + table(["ID", "Name / Price", "Stock", ""], rows, "No products yet")
            + form("add_product", page, add_fields, "+ Add product") + "</div>"
            "<div class='card'><b>Add stock</b> <span class='mut'>(one item per line)</span>"
            + form("add_stock", page, stock_fields, "Add stock") + "</div>"
            "<p class='mut'>After adding, deleting or editing products, run /shop again in Discord to repost the panel. "
            "(The menu also refreshes itself whenever someone uses it.)</p>"
        )
    elif page == "stock":
        pid = int(q.get("id", "0")) if str(q.get("id", "0")).isdigit() else 0
        prod = db.execute("SELECT name, price FROM products WHERE id = ?", (pid,)).fetchone()
        if not prod:
            return "<h2>Stock</h2><p class='sub'>Product not found. <a href='/admin?p=products'>← Products</a></p>"
        unsold = db.execute(
            "SELECT id, content FROM items WHERE product_id = ? AND sold_to IS NULL ORDER BY id LIMIT 500", (pid,)
        ).fetchall()
        sold = db.execute(
            "SELECT datetime(sold_at, '+9 hours'), sold_to, content FROM items WHERE product_id = ? AND sold_to IS NOT NULL "
            "ORDER BY sold_at DESC, id DESC LIMIT 50",
            (pid,),
        ).fetchall()
        urows = "".join(
            f"<tr><td><input type=checkbox name=item_id value={i} class=ck></td><td>{i}</td><td><code>{e(c)}</code></td></tr>"
            for i, c in unsold
        )
        srows = "".join(f"<tr><td>{at}</td><td>{u}</td><td><code>{e(c)}</code></td></tr>" for at, u, c in sold)
        toggle = "<input type=checkbox onclick=\"document.querySelectorAll('.ck').forEach(c=>c.checked=this.checked)\">"
        body = (
            f"<h2>Stock · {e(prod[0])}</h2><p class='sub'><a href='/admin?p=products'>← Products</a> · "
            f"{stock(pid)} in stock · price ₩{prod[1]:,}</p>"
            "<div class='card'><b>Add stock</b> <span class='mut'>(one item per line)</span>"
            + form("add_stock", "stock", f"<input type=hidden name=product_id value={pid}><textarea name=lines required></textarea>", "Add stock", ret_id=pid)
            + "</div><div class='card'><div class='row' style='justify-content:space-between'><b>Unsold stock</b>"
            + form("delete_all_stock", "stock", f"<input type=hidden name=product_id value={pid}>", "Delete all unsold", "d", "Delete ALL unsold stock?", pid)
            + "</div><form method='post' class='blk' onsubmit=\"return confirm('Delete the selected items?')\">"
            f"<input type=hidden name=action value=delete_items><input type=hidden name=p value=stock><input type=hidden name=id value={pid}>"
            + table([toggle, "ID", "Content"], urows, "No unsold stock")
            + "<button class='d' style='margin-top:10px'>Delete selected</button></form>"
            + ("<p class='mut'>Showing the first 500 items.</p>" if len(unsold) >= 500 else "")
            + "</div><div class='card'><b>Recently sold</b> <span class='mut'>(last 50)</span>"
            + table(["Sold at (KST)", "Buyer ID", "Content"], srows, "Nothing sold yet") + "</div>"
        )
    elif page == "coupons":
        names = dict(db.execute("SELECT id, name FROM products ORDER BY id").fetchall())
        rows = ""
        for cid, code, kind, value, mx, used, mn, cpid, exp, per, active in db.execute(
            "SELECT id, code, kind, value, max_uses, used, min_amount, product_id, expires, per_user, active "
            "FROM coupons ORDER BY id DESC"
        ):
            disc = f"{value}%" if kind == "percent" else f"₩{value:,}"
            target = e(names.get(cpid, "deleted product")) if cpid else "All"
            hid = f"<input type=hidden name=coupon_id value={cid}>"
            state = "<span class='on1'>Active</span>" if active else "<span class='off1'>Paused</span>"
            actions = (
                form("toggle_coupon", page, hid, "Pause" if active else "Resume", "s")
                + form("delete_coupon", page, hid, "Delete", "d", "Delete this coupon?")
            )
            rows += (
                f"<tr><td><code>{e(code)}</code></td><td>{disc}</td><td>{used} / {mx or '∞'}</td><td>₩{mn:,}</td>"
                f"<td>{target}</td><td>{exp or '-'}</td><td>{'Once' if per else 'Unlimited'}</td><td>{state}</td>"
                f"<td><div class='row'>{actions}</div></td></tr>"
            )
        opts = "<option value=''>All products</option>" + "".join(f"<option value='{i}'>{e(n)}</option>" for i, n in names.items())
        fields = (
            "<input name=code placeholder='Code (blank = random)' maxlength=30 pattern='[A-Za-z0-9_\\-]{2,30}'>"
            "<select name=kind><option value=percent>% off</option><option value=fixed>₩ off</option></select>"
            "<input name=value type=number min=1 placeholder='Amount' required style='width:110px'>"
            "<input name=max_uses type=number min=0 placeholder='Total uses (0 = unlimited)'>"
            "<input name=min_amount type=number min=0 placeholder='Minimum purchase'>"
            f"<select name=product_id>{opts}</select>"
            "<label class='mut'>Expires <input name=expires type=date></label>"
            "<label class='row' style='align-items:center'><input type=checkbox name=per_user value=1 checked> Once per user</label>"
        )
        used_total = db.execute("SELECT COUNT(*), COALESCE(SUM(discount), 0) FROM sales WHERE coupon IS NOT NULL").fetchone()
        body = (
            "<h2>Coupons</h2><p class='sub'>Discount codes · buyers enter the code in the quantity window when buying</p>"
            + stat_cards((("Coupon uses", f"{used_total[0]}"), ("Total discounts", f"₩{used_total[1]:,}")))
            + "<div class='card' style='margin-top:16px'><b>Create coupon</b>"
            + form("add_coupon", page, fields, "+ Add coupon")
            + "<p class='mut'>% off takes 1–100; ₩ off takes an amount. A coupon works until midnight (KST) on its expiry date.</p></div><div class='card'>"
            + table(["Code", "Discount", "Used", "Min. purchase", "Product", "Expires", "Per user", "Status", ""], rows, "No coupons yet")
            + "</div>"
        )
    elif page == "orders":
        rows = "".join(
            f"<tr><td>{at}</td><td>{u}</td><td>{e(n)}</td><td>{qn}</td><td>₩{p:,}</td>"
            f"<td>{(e(c) + ' -₩' + format(d, ',')) if c else '-'}</td></tr>"
            for at, u, n, qn, p, d, c in db.execute(
                "SELECT datetime(at, '+9 hours'), user_id, product_name, qty, price, discount, coupon "
                "FROM sales ORDER BY id DESC LIMIT 100"
            )
        )
        body = (
            "<h2>Orders</h2><p class='sub'>Last 100 (KST)</p><div class='card'>"
            + table(["Time", "User ID", "Product", "Qty", "Paid", "Coupon"], rows, "No orders yet") + "</div>"
        )
    elif page == "users":
        uid_q = str(q.get("uid", "")).strip()
        if uid_q.isdigit():
            data = db.execute(
                "SELECT user_id, balance, referrer FROM users WHERE user_id = ?", (int(uid_q),)
            ).fetchall()
        else:
            data = db.execute("SELECT user_id, balance, referrer FROM users ORDER BY balance DESC LIMIT 200").fetchall()
        rows = ""
        for u, b, r in data:
            spent = db.execute("SELECT COALESCE(SUM(price), 0) FROM sales WHERE user_id = ?", (u,)).fetchone()[0]
            edit = form(
                "set_balance", page,
                f"<input type=hidden name=user_id value={u}><input name=balance type=number value={b} style='width:130px'>",
                "Save", "s",
            )
            rows += f"<tr><td>{u}</td><td>₩{b:,}</td><td>₩{spent:,}</td><td>{r or '-'}</td><td>{edit}</td></tr>"
        search = (
            "<form method='get'><input type=hidden name=p value=users>"
            f"<input name=uid placeholder='Search user ID' value='{e(uid_q)}'><button class='s'>Search</button>"
            + ("<a href='/admin?p=users'>Show all</a>" if uid_q else "") + "</form>"
        )
        body = (
            "<h2>Users / Balance</h2><p class='sub'>Sets the balance to the value you enter</p><div class='card'>"
            + form("set_balance", page, "<input name=user_id placeholder='Discord user ID' required><input name=balance type=number placeholder=Balance required>", "Add / update user")
            + search
            + table(["User ID", "Balance", "Total spent", "Referrer", "Edit"], rows, "No users yet") + "</div>"
        )
    elif page == "charges":
        st = {"paid": "Paid", "pending": "Pending", "expired": "Expired"}
        rows = ""
        for oid, u, m, k, d, x, s, tx in db.execute(
            "SELECT id, user_id, method, krw, depositor, expected, status, tx_id FROM orders ORDER BY id DESC LIMIT 100"
        ):
            txid = (tx or "").split(":")[1] if (tx or "").startswith("ltc:") else (tx or "")
            if txid and m == "ltc":
                link = f"<a href='https://litecoinspace.org/tx/{e(txid)}' target=_blank>{e(txid[:10])}…</a>"
            elif txid:
                link = f"<a href='https://tronscan.org/#/transaction/{e(txid)}' target=_blank>{e(txid[:10])}…</a>"
            else:
                link = "-"
            if m == "ltc" and x:
                who = f"{x / 1e8:.5f} LTC"
            elif m == "crypto" and x:
                who = f"{x / 1e6:.3f} USDT"
            else:
                who = e(d or "-")
            m = {"crypto": "USDT", "ltc": "LTC", "bank": "Bank"}.get(m, m)
            btn = form("approve_order", page, f"<input type=hidden name=order_id value={oid}>", "Approve manually", "s") if s in ("pending", "expired") else ""
            rows += f"<tr><td>{oid}</td><td>{u}</td><td>{m}</td><td>₩{k:,}</td><td>{who}</td><td>{st.get(s, s)}</td><td>{link}</td><td>{btn}</td></tr>"
        body = (
            f"<h2>Charges</h2><p class='sub'>USDT: {'ON' if CRYPTO_ON else 'OFF (config.txt)'} · LTC: {'ON' if LTC_ON else 'OFF (config.txt)'} · Bank: {'ON' if bank_on() else 'OFF'}</p>"
            "<div class='card'>" + table(["#", "User", "Method", "Amount", "Depositor / quote", "Status", "TXID", ""], rows, "No charge requests yet") + "</div>"
        )
    elif page == "referral":
        pct, first = ref_percent(), S("ref_first_only") == "1"
        fields = (
            f"<label>Reward <input name=ref_percent type=number min=0 max=100 value={pct} style='width:90px'> %</label>"
            f"<label class='row' style='align-items:center'><input type=checkbox name=ref_first_only value=1 {'checked' if first else ''}> First charge only</label>"
        )
        top = "".join(
            f"<tr><td>{r}</td><td>{c}</td><td>₩{s:,}</td></tr>"
            for r, c, s in db.execute(
                "SELECT u.referrer, COUNT(*), COALESCE((SELECT SUM(amount) FROM ref_rewards WHERE referrer = u.referrer), 0) "
                "FROM users u WHERE u.referrer IS NOT NULL GROUP BY u.referrer ORDER BY 2 DESC LIMIT 50"
            )
        )
        recent = "".join(
            f"<tr><td>{at}</td><td>{r}</td><td>{f}</td><td>{o}</td><td>₩{a:,}</td></tr>"
            for at, r, f, o, a in db.execute(
                "SELECT datetime(at, '+9 hours'), referrer, referee, order_id, amount FROM ref_rewards ORDER BY id DESC LIMIT 50"
            )
        )
        total = db.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM ref_rewards").fetchone()
        linked = db.execute("SELECT COUNT(*) FROM users WHERE referrer IS NOT NULL").fetchone()[0]
        body = (
            "<h2>Referrals</h2><p class='sub'>When a referred user charges, their referrer gets a share of it as balance</p>"
            + stat_cards((("Status", "ON" if pct else "OFF"), ("Referred users", f"{linked}"), ("Rewards paid", f"{total[0]}"), ("Reward total", f"₩{total[1]:,}")))
            + "<div class='card' style='margin-top:16px'><b>Settings</b>" + form("save_ref", page, fields, "Save")
            + "<p class='mut'>0% turns it off. Users register a referrer ID once, before their first charge, with the Referral button on the shop. "
            "Balance added by an admin (/charge or Users / Balance) does not pay a reward.</p></div>"
            "<div class='card'><b>Top referrers</b>" + table(["Referrer ID", "Users referred", "Rewards earned"], top, "None yet") + "</div>"
            "<div class='card'><b>Recent rewards</b>" + table(["Time (KST)", "Referrer", "Referred user", "Charge #", "Reward"], recent, "None yet") + "</div>"
        )
    elif page == "stats":
        rev, cnt, qty_sum, disc = db.execute(
            "SELECT COALESCE(SUM(price), 0), COUNT(*), COALESCE(SUM(qty), 0), COALESCE(SUM(discount), 0) FROM sales"
        ).fetchone()
        today = kst_today()
        rev_today = db.execute("SELECT COALESCE(SUM(price), 0) FROM sales WHERE date(at, '+9 hours') = ?", (today,)).fetchone()[0]
        rev_month = db.execute(
            "SELECT COALESCE(SUM(price), 0) FROM sales WHERE strftime('%Y-%m', at, '+9 hours') = ?", (today[:7],)
        ).fetchone()[0]
        charged = db.execute("SELECT COALESCE(SUM(krw), 0) FROM orders WHERE status = 'paid'").fetchone()[0]
        owed = db.execute("SELECT COALESCE(SUM(balance), 0) FROM users").fetchone()[0]
        left = db.execute("SELECT COUNT(*) FROM items WHERE sold_to IS NULL").fetchone()[0]
        refs = db.execute("SELECT COALESCE(SUM(amount), 0) FROM ref_rewards").fetchone()[0]
        cards = stat_cards((
            ("Today's revenue", f"₩{rev_today:,}"), ("This month", f"₩{rev_month:,}"), ("All-time revenue", f"₩{rev:,}"),
            ("Items / orders", f"{qty_sum} / {cnt}"), ("Total charged", f"₩{charged:,}"), ("User balances", f"₩{owed:,}"),
            ("Coupon discounts", f"₩{disc:,}"), ("Referral rewards", f"₩{refs:,}"), ("Stock left", f"{left}"),
        ))
        daily = db.execute(
            "SELECT date(at, '+9 hours') d, COALESCE(SUM(price), 0), COUNT(*), COALESCE(SUM(qty), 0) FROM sales "
            "WHERE at >= datetime('now', '-30 days') GROUP BY d ORDER BY d DESC"
        ).fetchall()
        peak = max((r[1] for r in daily), default=0) or 1
        drows = "".join(
            f"<tr><td style='white-space:nowrap'>{d}</td><td style='width:45%'><div class='bar' style='width:{s * 100 // peak}%'></div></td>"
            f"<td>₩{s:,}</td><td>{c}</td><td>{qn}</td></tr>"
            for d, s, c, qn in daily
        )
        prows = "".join(
            f"<tr><td>{e(n)}</td><td>{qn}</td><td>{c}</td><td>₩{s:,}</td></tr>"
            for n, qn, c, s in db.execute(
                "SELECT product_name, SUM(qty), COUNT(*), SUM(price) FROM sales GROUP BY product_name ORDER BY 4 DESC LIMIT 10"
            )
        )
        crow = "".join(
            f"<tr><td>{ {'crypto': 'USDT', 'ltc': 'LTC', 'bank': 'Bank'}.get(m, m) }</td><td>{c}</td><td>₩{s:,}</td></tr>"
            for m, c, s in db.execute(
                "SELECT method, COUNT(*), SUM(krw) FROM orders WHERE status = 'paid' GROUP BY method ORDER BY 3 DESC"
            )
        )
        buyers = "".join(
            f"<tr><td>{u}</td><td>{c}</td><td>₩{s:,}</td></tr>"
            for u, c, s in db.execute(
                "SELECT user_id, COUNT(*), SUM(price) FROM sales GROUP BY user_id ORDER BY 3 DESC LIMIT 10"
            )
        )
        body = (
            "<h2>Statistics</h2><p class='sub'>Revenue, charges and stock (KST)</p>" + cards
            + "<div class='card' style='margin-top:16px'><b>Daily revenue, last 30 days</b>"
            + table(["Date", "", "Revenue", "Orders", "Items"], drows, "No sales in the last 30 days") + "</div>"
            "<div class='grid' style='grid-template-columns:repeat(auto-fit,minmax(300px,1fr))'>"
            "<div class='card'><b>Top 10 products</b>" + table(["Product", "Items", "Orders", "Revenue"], prows, "None yet") + "</div>"
            "<div class='card'><b>Top 10 buyers</b>" + table(["User ID", "Orders", "Spent"], buyers, "None yet") + "</div>"
            "<div class='card'><b>By payment method</b>" + table(["Method", "Count", "Amount"], crow, "None yet") + "</div></div>"
        )
    elif page == "bank":
        url = f"{base}/api/deposit?token={deposit_token()}"
        textarea = f"<textarea name=bank_info placeholder='e.g. KakaoBank 3333-00-0000000 Hong Gildong'>{e(bank_info())}</textarea>"
        body = (
            "<h2>Bank Settings</h2><p class='sub'>Bank account details and the auto-charge webhook</p><div class='card'><b>Bank account details</b>"
            + form("save_bank", page, textarea, "Save") + "</div>"
            f"<div class='card'><b>Auto-charge webhook</b> {form('regen_token', page, '', 'New token', 's')}"
            "<p class='mut'>POST bank deposit texts to this URL to charge automatically (JSON body <code>{\"text\":\"the full message\"}</code>)</p>"
            f"<code>{e(url)}</code>"
            "<p class='mut'>iOS: Shortcuts app → Automation → Personal Automation → \"Message\" (contains 입금) → Get Contents of URL (POST, JSON)<br>"
            "Android: MacroDroid/Tasker/Automate → bank notification trigger → HTTP POST<br>"
            "The bot only charges when the text contains \"입금\" (Korean for \"deposit\") and also contains both the amount and the depositor name of a pending request.</p></div>"
        )
    elif page == "panel":
        t, d, c, img = S("panel_title", SHOP_TITLE), S("panel_desc", DEFAULT_DESC), S("panel_color", "#DB9509"), S("panel_image")
        fields = (
            f"<input id=t name=title value='{e(t)}' style='width:100%'><textarea id=d name=desc>{e(d)}</textarea>"
            f"<input name=image value='{e(img)}' placeholder='Image URL' style='width:100%'><input id=c name=color type=color value='{e(c)}'>"
        )
        body = (
            "<h2>Panel Settings</h2><p class='sub'>The shop embed in Discord</p><div class='grid'><div class='card'>"
            + form("save_panel", page, fields, "Save")
            + f"</div><div class='card'><b>Preview</b><div class='pv' id=pv style='border-color:{e(c)}'><b id=pt>{e(t)}</b><br><span id=pd style='white-space:pre-wrap'>{e(d)}</span></div></div></div>"
            "<script>const $=i=>document.getElementById(i);const u=()=>{$('pt').textContent=$('t').value;$('pd').textContent=$('d').value;$('pv').style.borderColor=$('c').value};"
            "['t','d','c'].forEach(i=>$(i).addEventListener('input',u))</script>"
            "<p class='mut'>After saving, run /shop again in Discord to apply it.</p>"
        )
    elif page == "logs":
        fields = (
            f"<input name=log_purchase placeholder='Purchase log channel ID' value='{e(S('log_purchase'))}' style='width:100%'>"
            f"<input name=log_charge placeholder='Charge log channel ID (includes referral rewards)' value='{e(S('log_charge'))}' style='width:100%'>"
        )
        body = (
            "<h2>Log Settings</h2><p class='sub'>Channel IDs that receive logs (leave blank to turn off)</p><div class='card'>"
            + form("save_logs", page, fields, "Save")
            + "</div><p class='mut'>The bot must be able to see and send messages in these channels.</p>"
        )
    else:
        rev, cnt, cust = db.execute("SELECT COALESCE(SUM(price), 0), COUNT(*), COUNT(DISTINCT user_id) FROM sales").fetchone()
        rev_today = db.execute(
            "SELECT COALESCE(SUM(price), 0) FROM sales WHERE date(at, '+9 hours') = ?", (kst_today(),)
        ).fetchone()[0]
        cards = stat_cards((
            ("Today's revenue", f"₩{rev_today:,}"), ("All-time revenue", f"₩{rev:,}"), ("Orders", f"{cnt}"), ("Customers", f"{cust}"),
            ("Average order", f"₩{rev // cnt if cnt else 0:,}"),
        ))
        rows = "".join(
            f"<tr><td>{at}</td><td>{u}</td><td>{e(n)}</td><td>{qn}</td><td>₩{p:,}</td></tr>"
            for at, u, n, qn, p in db.execute(
                "SELECT datetime(at, '+9 hours'), user_id, product_name, qty, price FROM sales ORDER BY id DESC LIMIT 10"
            )
        )
        body = (
            "<h2>Dashboard</h2><p class='sub'>Sales overview · more in <a href='/admin?p=stats'>Statistics</a></p>" + cards
            + "<div class='card' style='margin-top:16px'><b>Recent orders</b>"
            + table(["Time (KST)", "User ID", "Product", "Qty", "Amount"], rows, "No orders yet") + "</div>"
        )
    return body


def layout(page: str, body: str) -> str:
    active = HIDDEN.get(page, page)
    nav = "".join(f"<a href='/admin?p={k}' class='{'on' if k == active else ''}'>{v}</a>" for k, v in NAV)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(SHOP_TITLE)}</title><style>{CSS}</style></head><body><aside><h1>{escape(SHOP_TITLE)}</h1>"
        f"<small>VENDING PANEL</small>{nav}</aside><main>{body}</main></body></html>"
    )


def base_url(request) -> str:
    return f"{request.headers.get('X-Forwarded-Proto', 'https')}://{request.host}"


def valid_page(page) -> str:
    return page if page in dict(NAV) or page in HIDDEN else "dashboard"


async def admin_page(request: web.Request):
    if not check_admin(request):
        return unauthorized()
    page = valid_page(request.query.get("p", "dashboard"))
    html = layout(page, render_page(page, base_url(request), request.query))
    return web.Response(text=html, content_type="text/html", charset="utf-8")


class BadInput(Exception):
    pass


def to_int(value, low=None, high=None) -> int:
    n = int(str(value).strip())
    if (low is not None and n < low) or (high is not None and n > high):
        raise ValueError
    return n


def handle_admin_action(action: str, data):
    """Applies one admin form action. Returns a finished charge order to notify about, if any."""
    done = None
    if action == "add_product":
        db.execute("INSERT INTO products (name, price) VALUES (?, ?)", (data["name"].strip()[:60], to_int(data["price"], 0)))
    elif action == "edit_product":
        name = data["name"].strip()[:60]
        if not name:
            raise BadInput("Enter a product name.")
        db.execute(
            "UPDATE products SET name = ?, price = ? WHERE id = ?", (name, to_int(data["price"], 0), int(data["product_id"]))
        )
    elif action == "delete_product":
        pid = int(data["product_id"])
        db.execute("DELETE FROM items WHERE product_id = ? AND sold_to IS NULL", (pid,))
        db.execute("DELETE FROM products WHERE id = ?", (pid,))
        db.execute("UPDATE coupons SET active = 0 WHERE product_id = ?", (pid,))
    elif action == "add_stock":
        pid = int(data["product_id"])
        if not db.execute("SELECT 1 FROM products WHERE id = ?", (pid,)).fetchone():
            raise BadInput("Add the product first.")
        lines = [ln.strip() for ln in data["lines"].splitlines() if ln.strip()]
        db.executemany("INSERT INTO items (product_id, content) VALUES (?, ?)", [(pid, ln) for ln in lines])
    elif action == "delete_items":
        pid = int(data["id"])
        ids = [int(x) for x in data.getall("item_id", [])]
        db.executemany(
            "DELETE FROM items WHERE id = ? AND product_id = ? AND sold_to IS NULL", [(i, pid) for i in ids]
        )
    elif action == "delete_all_stock":
        db.execute("DELETE FROM items WHERE product_id = ? AND sold_to IS NULL", (int(data["product_id"]),))
    elif action == "set_balance":
        db.execute(
            "INSERT INTO users (user_id, balance) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET balance = excluded.balance",
            (int(data["user_id"]), int(data["balance"])),
        )
    elif action == "approve_order":
        done = complete_order(int(data["order_id"]))
    elif action == "add_coupon":
        code = data.get("code", "").strip().upper()
        if not code:
            code = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        if not re.fullmatch(r"[A-Z0-9_-]{2,30}", code):
            raise BadInput("Coupon codes must be 2-30 letters, numbers, - or _.")
        kind = data.get("kind")
        if kind not in ("percent", "fixed"):
            raise ValueError
        value = to_int(data["value"], 1, 100 if kind == "percent" else None)
        expires = data.get("expires", "").strip()
        if expires and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", expires):
            raise ValueError
        pid = data.get("product_id", "")
        try:
            db.execute(
                "INSERT INTO coupons (code, kind, value, max_uses, min_amount, product_id, expires, per_user) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    code, kind, value, to_int(data.get("max_uses") or 0, 0), to_int(data.get("min_amount") or 0, 0),
                    int(pid) if pid.isdigit() else None, expires or None, 1 if data.get("per_user") else 0,
                ),
            )
        except sqlite3.IntegrityError:
            raise BadInput("That coupon code already exists.")
    elif action == "toggle_coupon":
        db.execute("UPDATE coupons SET active = 1 - active WHERE id = ?", (int(data["coupon_id"]),))
    elif action == "delete_coupon":
        db.execute("DELETE FROM coupons WHERE id = ?", (int(data["coupon_id"]),))
    elif action == "save_ref":
        set_S("ref_percent", str(to_int(data.get("ref_percent") or 0, 0, 100)))
        set_S("ref_first_only", "1" if data.get("ref_first_only") else "0")
    elif action == "save_bank":
        set_S("bank_info", data.get("bank_info", "").strip()[:300])
    elif action == "regen_token":
        set_S("deposit_token", secrets.token_hex(24))
    elif action == "save_panel":
        color = data.get("color", "")
        set_S("panel_title", data.get("title", "").strip()[:100])
        set_S("panel_desc", data.get("desc", "").strip()[:500])
        set_S("panel_image", data.get("image", "").strip()[:500])
        set_S("panel_color", color if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else "#DB9509")
    elif action == "save_logs":
        for k in ("log_purchase", "log_charge"):
            v = data.get(k, "").strip()
            set_S(k, v if v.isdigit() else "")
    db.commit()
    return done


async def admin_post(request: web.Request):
    if not check_admin(request):
        return unauthorized()
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc != request.host:  # block cross-site form posts
        return web.Response(status=403, text="Bad origin")
    data = await request.post()
    try:
        done = handle_admin_action(data.get("action", ""), data)
    except BadInput as err:
        db.rollback()
        return web.Response(status=400, text=f"{err}\n\nGo back and try again.")
    except (KeyError, ValueError):
        db.rollback()
        return web.Response(status=400, text="Invalid input. Go back and try again.")
    if done:
        await notify_paid(*done)
    url = f"/admin?p={valid_page(data.get('p', 'dashboard'))}"
    if str(data.get("id", "")).isdigit():
        url += f"&id={data['id']}"
    raise web.HTTPFound(url)


async def start_web():
    app = web.Application()
    app.add_routes([web.get("/admin", admin_page), web.post("/admin", admin_post), web.post("/api/deposit", deposit_hook)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, WEB_HOST, WEB_PORT).start()
    print(f"Web panel running on http://{WEB_HOST}:{WEB_PORT}/admin")


# ---------- Bot ----------
class MyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(Shop())
        await self.tree.sync()
        await start_web()
        if CRYPTO_ON or LTC_ON:
            self.crypto_task = asyncio.create_task(crypto_loop())


bot = MyBot()


@bot.tree.command(name="shop", description="Post (or refresh) the shop panel in this channel")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def shop(interaction: discord.Interaction):
    try:
        color = int(S("panel_color", "#DB9509").lstrip("#"), 16)
    except ValueError:
        color = 0xDB9509
    embed = discord.Embed(title=S("panel_title", SHOP_TITLE), description=S("panel_desc", DEFAULT_DESC), color=color)
    if S("panel_image").startswith("http"):
        embed.set_image(url=S("panel_image"))
    await interaction.response.send_message(embed=embed, view=Shop())


@bot.tree.command(name="charge", description="Add (or subtract) balance for a user")
@app_commands.describe(user="The user", amount="Amount to add (negative to deduct)")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def charge(interaction: discord.Interaction, user: discord.Member, amount: int):
    add_balance(user.id, amount)
    await interaction.response.send_message(f"✅ {user.mention} balance is now ₩{get_balance(user.id):,}.", ephemeral=True)


bot.run(TOKEN)
