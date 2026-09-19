"""
SHOP MODULE — modular extension for the existing Telegram bot.

This file NEVER modifies existing bot logic.  It is bound to the running
main module (``shop.bind(main_module)``) and reuses:

  * main.bot                  — the same TeleBot instance
  * main.get_conn             — the same SQLite database
  * main.wallet.balance       — the EXISTING user balance (no new wallet)
  * main.is_admin             — the EXISTING admin authorization
  * setting payment_forward_chat_id — the EXISTING admin group

All shop callbacks use the dedicated ``shop`` / ``shop_*`` namespace and all
shop tables are prefixed ``shop_`` so nothing can collide with the existing
Get Number / Temp Mail / Custom Range / Withdrawal / Admin Panel systems.
"""

import html as _html
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta

from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)

logger = logging.getLogger("shop")

_M = None                      # bound main module
shop_states: dict = {}         # composite key -> {"step":..., "data":{...}}
shop_nav: dict = {}            # user_id -> [menu names]
_checkout_locks: set = set()   # user ids currently checking out
_lock = threading.Lock()

SALES_STATUSES = ("CONFIRMED", "PROCESSING", "DELIVERED")
PAGE_SIZE = 8


# ═══════════════════════════════════════════════════════════════════════════
# BINDING / SMALL HELPERS
# ═══════════════════════════════════════════════════════════════════════════
def bind(main_module):
    """Attach the shop to the already running bot."""
    global _M
    _M = main_module
    init_shop_db()
    _register_callbacks()
    logger.info("Shop module bound and ready.")


def bot():
    return _M.bot


def st(text) -> str:
    if _M is None:
        return str(text)
    try:
        return _M.stylish(str(text))
    except Exception:
        return str(text)


def conn():
    return _M.get_conn()


def raw_conn():
    return _M.raw_conn()


def is_shop_admin(user_id: int) -> bool:
    """Server-side authorization — never trust callback data."""
    try:
        return bool(_M.is_admin(int(user_id)))
    except Exception:
        return False


def esc(value) -> str:
    return _html.escape(str(value if value is not None else ""))


def _skey(chat_id, user_id) -> str:
    return f"{chat_id}:{user_id}"


def now_ts() -> int:
    return int(time.time())


def fmt_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%d-%m-%Y %H:%M")
    except Exception:
        return "N/A"


def send(chat_id, text, **kwargs):
    try:
        return bot().send_message(chat_id, text, **kwargs)
    except Exception as exc:
        logger.warning("shop send failed chat=%s: %s", chat_id, exc)
        return None


def money(value) -> str:
    return f"{float(value or 0):.2f} {currency()}"


def sep(char="━", n=28) -> str:
    return char * n


# ═══════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════
def init_shop_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS shop_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            category TEXT DEFAULT 'General',
            description TEXT DEFAULT '',
            price REAL DEFAULT 0,
            stock INTEGER DEFAULT 0,
            image_file_id TEXT DEFAULT '',
            delivery_type TEXT DEFAULT 'TEXT',
            enabled INTEGER DEFAULT 1,
            featured INTEGER DEFAULT 0,
            popular INTEGER DEFAULT 0,
            is_deleted INTEGER DEFAULT 0,
            sold INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            updated_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS shop_cart_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product_id TEXT NOT NULL,
            qty INTEGER DEFAULT 1,
            added_at INTEGER DEFAULT (strftime('%s','now')),
            UNIQUE(user_id, product_id)
        );

        CREATE TABLE IF NOT EXISTS shop_cart_coupon (
            user_id INTEGER PRIMARY KEY,
            code TEXT NOT NULL,
            applied_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS shop_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            subtotal REAL DEFAULT 0,
            discount REAL DEFAULT 0,
            total REAL DEFAULT 0,
            coupon_code TEXT DEFAULT '',
            payment_status TEXT DEFAULT 'PAID',
            order_status TEXT DEFAULT 'PENDING',
            delivery_status TEXT DEFAULT 'NOT_DELIVERED',
            group_msg_id INTEGER,
            group_chat_id TEXT DEFAULT '',
            created_at INTEGER DEFAULT (strftime('%s','now')),
            updated_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS shop_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            product_name TEXT DEFAULT '',
            qty INTEGER DEFAULT 1,
            unit_price REAL DEFAULT 0,
            subtotal REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS shop_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL,
            admin_id INTEGER,
            kind TEXT DEFAULT 'TEXT',
            content TEXT DEFAULT '',
            file_id TEXT DEFAULT '',
            is_resend INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS shop_auto_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT NOT NULL,
            content TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            order_id TEXT DEFAULT '',
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS shop_coupons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            discount_type TEXT DEFAULT 'PERCENT',
            discount_value REAL DEFAULT 0,
            min_order REAL DEFAULT 0,
            max_usage INTEGER DEFAULT 0,
            per_user_limit INTEGER DEFAULT 1,
            start_at INTEGER DEFAULT 0,
            expiry_at INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            used_count INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS shop_coupon_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            order_id TEXT DEFAULT '',
            used_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS shop_offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            kind TEXT DEFAULT 'OFFER',
            start_at INTEGER DEFAULT 0,
            expiry_at INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS shop_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT DEFAULT 'ORDER',
            text TEXT DEFAULT '',
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS shop_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            order_id TEXT DEFAULT '',
            kind TEXT DEFAULT 'DEBIT',
            amount REAL DEFAULT 0,
            note TEXT DEFAULT '',
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS shop_topups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL DEFAULT 0,
            method TEXT DEFAULT '',
            reference TEXT DEFAULT '',
            status TEXT DEFAULT 'PENDING',
            group_msg_id INTEGER,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS shop_stock_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT NOT NULL,
            admin_id INTEGER,
            change INTEGER DEFAULT 0,
            reason TEXT DEFAULT '',
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS shop_activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            action TEXT DEFAULT '',
            target TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS shop_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_shop_products_cat ON shop_products(category);
        CREATE INDEX IF NOT EXISTS idx_shop_products_created ON shop_products(created_at);
        CREATE INDEX IF NOT EXISTS idx_shop_cart_user ON shop_cart_items(user_id);
        CREATE INDEX IF NOT EXISTS idx_shop_orders_user ON shop_orders(user_id);
        CREATE INDEX IF NOT EXISTS idx_shop_orders_status ON shop_orders(order_status);
        CREATE INDEX IF NOT EXISTS idx_shop_orders_created ON shop_orders(created_at);
        CREATE INDEX IF NOT EXISTS idx_shop_items_order ON shop_order_items(order_id);
        CREATE INDEX IF NOT EXISTS idx_shop_items_product ON shop_order_items(product_id);
        CREATE INDEX IF NOT EXISTS idx_shop_tx_user ON shop_transactions(user_id);
        CREATE INDEX IF NOT EXISTS idx_shop_notif_user ON shop_notifications(user_id, kind);
        CREATE INDEX IF NOT EXISTS idx_shop_delivery_order ON shop_deliveries(order_id);
        """)


def sget(key, default=""):
    try:
        with conn() as c:
            row = c.execute("SELECT value FROM shop_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    except Exception:
        return default


def sset(key, value):
    with conn() as c:
        c.execute(
            "INSERT INTO shop_settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def shop_name() -> str:
    return sget("shop_name", "Shop")


def currency() -> str:
    return sget("currency", "BDT")


def min_order() -> float:
    try:
        return float(sget("min_order", "0") or 0)
    except Exception:
        return 0.0


def maintenance_on() -> bool:
    return sget("maintenance", "0") == "1"


def low_stock_threshold() -> int:
    try:
        return int(sget("low_stock", "5") or 5)
    except Exception:
        return 5


def log_activity(admin_id, action, target="", detail=""):
    try:
        with conn() as c:
            c.execute(
                "INSERT INTO shop_activity_logs (admin_id, action, target, detail) VALUES (?,?,?,?)",
                (admin_id, action, str(target), str(detail)[:400]),
            )
    except Exception as exc:
        logger.warning("activity log failed: %s", exc)


def add_transaction(user_id, kind, amount, note="", order_id=""):
    with conn() as c:
        c.execute(
            "INSERT INTO shop_transactions (user_id, order_id, kind, amount, note) VALUES (?,?,?,?,?)",
            (user_id, order_id, kind, float(amount), note),
        )


def notify(user_id, kind, text, reply_markup=None):
    """Persist + push a shop notification."""
    try:
        with conn() as c:
            c.execute(
                "INSERT INTO shop_notifications (user_id, kind, text) VALUES (?,?,?)",
                (user_id, kind, text),
            )
    except Exception as exc:
        logger.warning("notification store failed: %s", exc)
    send(user_id, text, reply_markup=reply_markup)


def admin_group_id():
    """The EXISTING admin group used by the withdrawal/request system."""
    gid = _M.get_setting("payment_forward_chat_id", "") or ""
    return str(gid).strip()


def user_balance(user_id) -> float:
    stats = _M.get_wallet_stats(user_id)
    return float(stats.get("balance", 0.0))


# ═══════════════════════════════════════════════════════════════════════════
# LABELS  (styled exactly like the existing bot)
# ═══════════════════════════════════════════════════════════════════════════
def L(emoji, text) -> str:
    return f"{emoji} {st(text)}"


def SHOP_BUTTON() -> str:
    return L("🛒", "Shop")


BACK = lambda: L("🔙", "Back")

MENUS = {
    "main": lambda admin: [
        [L("🛍️", "Products"), L("🛒", "My Cart")],
        [L("📋", "My Orders"), L("📜", "History")],
        [L("💰", "Shop Balance"), L("🎁", "Offers & Bonus")],
        [L("🔔", "Notifications"), L("🆘", "Shop Support")],
    ] + ([[L("👑", "Shop Admin Panel")]] if admin else []) + [[BACK()]],
    "products": lambda admin: [
        [L("📦", "All Products"), L("🔥", "Popular Products")],
        [L("🆕", "New Products"), L("⭐", "Featured Products")],
        [L("🔎", "Search Product"), L("🗂️", "Categories")],
        [BACK()],
    ],
    "orders": lambda admin: [
        [L("🆕", "New Orders"), L("⏳", "Pending Orders")],
        [L("🚚", "Processing Delivery"), L("✅", "Confirmed Orders")],
        [L("❌", "Cancelled Orders"), L("🔎", "Order Details")],
        [BACK()],
    ],
    "history": lambda admin: [
        [L("✅", "Confirmation History"), L("⏳", "Pending History")],
        [L("❌", "Cancelled History")],
        [BACK()],
    ],
    "balance": lambda admin: [
        [L("💰", "Current Balance"), L("➕", "Add Balance")],
        [L("📜", "Transaction History")],
        [BACK()],
    ],
    "offers": lambda admin: [
        [L("🔥", "Special Offers"), L("🎟️", "Promo Code")],
        [L("💎", "Discount"), L("🎁", "Bonus Products")],
        [BACK()],
    ],
    "notifications": lambda admin: [
        [L("📦", "Order Updates"), L("🚚", "Delivery Updates")],
        [L("🎁", "Offer Alerts")],
        [BACK()],
    ],
    "support": lambda admin: [
        [L("💬", "Contact Support"), L("❓", "FAQ")],
        [L("📖", "How To Buy")],
        [BACK()],
    ],
}

MENU_TITLES = {
    "main": lambda: f"🛒 <b>{st(shop_name().upper())}</b>\n{sep()}\n{st('Choose an option below.')}",
    "products": lambda: f"🛍️ <b>{st('PRODUCTS')}</b>",
    "orders": lambda: f"📋 <b>{st('MY ORDERS')}</b>",
    "history": lambda: f"📜 <b>{st('HISTORY')}</b>",
    "balance": lambda: f"💰 <b>{st('SHOP BALANCE')}</b>",
    "offers": lambda: f"🎁 <b>{st('OFFERS & BONUS')}</b>",
    "notifications": lambda: f"🔔 <b>{st('NOTIFICATIONS')}</b>",
    "support": lambda: f"🆘 <b>{st('SHOP SUPPORT')}</b>",
}


def keyboard_for(menu: str, admin: bool):
    """Build a reply keyboard from a menu spec (2 columns, like the main bot)."""
    from shop_admin import ADMIN_MENUS
    spec = MENUS.get(menu) or ADMIN_MENUS.get(menu)
    if not spec:
        return None
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    for row in spec(admin):
        kb.add(*[KeyboardButton(b) for b in row])
    return kb


def render_menu(chat_id, user_id, menu, text=None, push=True):
    admin = is_shop_admin(user_id)
    if push:
        stack = shop_nav.setdefault(user_id, [])
        if menu == "main":
            shop_nav[user_id] = ["main"]
        elif not stack or stack[-1] != menu:
            stack.append(menu)
    from shop_admin import ADMIN_TITLES
    title_fn = MENU_TITLES.get(menu) or ADMIN_TITLES.get(menu)
    body = text or (title_fn() if title_fn else st(menu))
    send(chat_id, body, reply_markup=keyboard_for(menu, admin))


def current_menu(user_id):
    stack = shop_nav.get(user_id) or []
    return stack[-1] if stack else None


def go_back(chat_id, user_id):
    stack = shop_nav.get(user_id) or []
    if stack:
        stack.pop()
    if stack:
        render_menu(chat_id, user_id, stack[-1], push=False)
    else:
        exit_shop(chat_id, user_id)


def exit_shop(chat_id, user_id):
    shop_nav.pop(user_id, None)
    shop_states.pop(_skey(chat_id, user_id), None)
    send(
        chat_id,
        f"🏠 {st('Back to main menu.')}",
        reply_markup=_M.welcome_keyboard(is_admin_user=is_shop_admin(user_id)),
    )


def open_shop(chat_id, user_id):
    if maintenance_on() and not is_shop_admin(user_id):
        send(
            chat_id,
            f"🛠️ {st('Shop is temporarily under maintenance.')}",
            reply_markup=_M.welcome_keyboard(is_admin_user=is_shop_admin(user_id)),
        )
        return
    shop_states.pop(_skey(chat_id, user_id), None)
    render_menu(chat_id, user_id, "main")


# ═══════════════════════════════════════════════════════════════════════════
# PRODUCT QUERIES
# ═══════════════════════════════════════════════════════════════════════════
ACTIVE_SQL = "is_deleted=0 AND enabled=1"


def get_product(product_id, admin=False):
    q = "SELECT * FROM shop_products WHERE product_id=?"
    if not admin:
        q += " AND is_deleted=0"
    with conn() as c:
        row = c.execute(q, (str(product_id),)).fetchone()
    return dict(row) if row else None


def list_products(kind="all", arg="", limit=PAGE_SIZE, offset=0):
    base = f"SELECT * FROM shop_products WHERE {ACTIVE_SQL}"
    params = []
    if kind == "popular":
        base += " AND popular=1"
    elif kind == "featured":
        base += " AND featured=1"
    elif kind == "cat":
        base += " AND LOWER(category)=LOWER(?)"
        params.append(arg)
    elif kind == "search":
        like = f"%{arg.lower()}%"
        base += (" AND (LOWER(name) LIKE ? OR LOWER(product_id) LIKE ?"
                 " OR LOWER(category) LIKE ? OR LOWER(description) LIKE ?)")
        params += [like, like, like, like]
    order = " ORDER BY created_at DESC" if kind == "new" else " ORDER BY sold DESC, created_at DESC"
    base += order + " LIMIT ? OFFSET ?"
    params += [limit + 1, offset]
    with conn() as c:
        rows = [dict(r) for r in c.execute(base, params).fetchall()]
    has_more = len(rows) > limit
    return rows[:limit], has_more


def categories():
    with conn() as c:
        rows = c.execute(
            f"SELECT category, COUNT(*) AS n FROM shop_products WHERE {ACTIVE_SQL} "
            "GROUP BY LOWER(category) ORDER BY category"
        ).fetchall()
    return [(r["category"], r["n"]) for r in rows]


def product_card(p, with_flags=True) -> str:
    flags = []
    if with_flags and p.get("featured"):
        flags.append("⭐ Featured")
    if with_flags and p.get("popular"):
        flags.append("🔥 Popular")
    stock_line = (
        f"❌ {st('Out of Stock')}" if int(p["stock"]) <= 0
        else f"<b>{int(p['stock'])}</b>"
    )
    text = (
        f"🛍️ <b>{esc(p['name'])}</b>\n"
        f"{sep('─')}\n"
        f"<blockquote>"
        f"🆔 {st('Product ID')}: <code>{esc(p['product_id'])}</code>\n"
        f"🗂️ {st('Category')}: {esc(p['category'])}\n"
        f"💰 {st('Price')}: <b>{money(p['price'])}</b>\n"
        f"📦 {st('Stock')}: {stock_line}\n"
        f"🚚 {st('Delivery')}: {esc(p['delivery_type'])}"
        f"</blockquote>\n"
    )
    if p.get("description"):
        text += f"\n📝 {st('Description')}:\n<blockquote>{esc(p['description'])}</blockquote>\n"
    if flags:
        text += f"\n{' | '.join(flags)}"
    return text


def product_inline(p, back_to="products"):
    kb = InlineKeyboardMarkup(row_width=2)
    if int(p["stock"]) > 0 and p["enabled"] and not p["is_deleted"]:
        kb.add(
            InlineKeyboardButton(f"➕ {st('Add to Cart')}", callback_data=f"shop_qty:cart:{p['product_id']}:1"),
            InlineKeyboardButton(f"⚡ {st('Buy Now')}", callback_data=f"shop_qty:buy:{p['product_id']}:1"),
        )
    else:
        kb.add(InlineKeyboardButton(f"❌ {st('Out of Stock')}", callback_data="shop_noop"))
    kb.add(InlineKeyboardButton(f"🔙 {st('Back')}", callback_data=f"shop_list:{back_to}::0"))
    return kb


def send_product_list(chat_id, user_id, kind, arg="", page=0, title=None):
    rows, has_more = list_products(kind, arg, PAGE_SIZE, page * PAGE_SIZE)
    if not rows:
        send(chat_id, f"📭 {st('No products found.')}")
        return
    head = title or {
        "all": "ALL PRODUCTS", "popular": "POPULAR PRODUCTS",
        "new": "NEW PRODUCTS", "featured": "FEATURED PRODUCTS",
        "cat": f"CATEGORY: {arg}", "search": f"SEARCH: {arg}",
    }.get(kind, "PRODUCTS")
    lines = [f"🛍️ <b>{st(head)}</b>\n{sep()}"]
    kb = InlineKeyboardMarkup(row_width=1)
    for p in rows:
        stock = "❌ Out of Stock" if int(p["stock"]) <= 0 else f"📦 {int(p['stock'])}"
        tags = ("⭐" if p["featured"] else "") + ("🔥" if p["popular"] else "")
        lines.append(
            f"\n<b>{esc(p['name'])}</b> {tags}\n"
            f"🆔 <code>{esc(p['product_id'])}</code> | 💰 {money(p['price'])} | {stock}"
        )
        kb.add(InlineKeyboardButton(
            f"🛍️ {p['name'][:28]} — {float(p['price']):.0f}",
            callback_data=f"shop_view:{p['product_id']}",
        ))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"shop_list:{kind}:{arg}:{page-1}"))
    if has_more:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"shop_list:{kind}:{arg}:{page+1}"))
    if nav:
        kb.row(*nav)
    send(chat_id, "\n".join(lines), reply_markup=kb)


# ═══════════════════════════════════════════════════════════════════════════
# CART
# ═══════════════════════════════════════════════════════════════════════════
def cart_rows(user_id):
    with conn() as c:
        rows = c.execute(
            "SELECT ci.product_id, ci.qty, p.name, p.price, p.stock, p.enabled, p.is_deleted "
            "FROM shop_cart_items ci LEFT JOIN shop_products p ON p.product_id=ci.product_id "
            "WHERE ci.user_id=? ORDER BY ci.id",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def cart_add(user_id, product_id, qty):
    with conn() as c:
        c.execute(
            "INSERT INTO shop_cart_items (user_id, product_id, qty) VALUES (?,?,?) "
            "ON CONFLICT(user_id, product_id) DO UPDATE SET qty=qty+excluded.qty",
            (user_id, product_id, int(qty)),
        )


def cart_set_qty(user_id, product_id, qty):
    with conn() as c:
        if int(qty) <= 0:
            c.execute("DELETE FROM shop_cart_items WHERE user_id=? AND product_id=?", (user_id, product_id))
        else:
            c.execute("UPDATE shop_cart_items SET qty=? WHERE user_id=? AND product_id=?",
                      (int(qty), user_id, product_id))


def cart_clear(user_id):
    with conn() as c:
        c.execute("DELETE FROM shop_cart_items WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM shop_cart_coupon WHERE user_id=?", (user_id,))


def cart_coupon(user_id):
    with conn() as c:
        row = c.execute("SELECT code FROM shop_cart_coupon WHERE user_id=?", (user_id,)).fetchone()
    return row["code"] if row else ""


def send_cart(chat_id, user_id):
    rows = cart_rows(user_id)
    if not rows:
        send(chat_id, f"🛒 {st('Your cart is empty.')}")
        return
    lines = [f"🛒 <b>{st('MY CART')}</b>\n{sep()}"]
    kb = InlineKeyboardMarkup(row_width=3)
    total = 0.0
    for r in rows:
        if r["name"] is None or r["is_deleted"]:
            lines.append(f"\n⚠️ <code>{esc(r['product_id'])}</code> — {st('no longer available')}")
            kb.add(InlineKeyboardButton(f"🗑️ {esc(r['product_id'])}",
                                        callback_data=f"shop_cart:del:{r['product_id']}"))
            continue
        sub = float(r["price"]) * int(r["qty"])
        total += sub
        lines.append(
            f"\n<b>{esc(r['name'])}</b>\n"
            f"💰 {money(r['price'])} × {int(r['qty'])} = <b>{money(sub)}</b>"
        )
        kb.row(
            InlineKeyboardButton("➖", callback_data=f"shop_cart:dec:{r['product_id']}"),
            InlineKeyboardButton(f"{r['name'][:14]} ({int(r['qty'])})", callback_data="shop_noop"),
            InlineKeyboardButton("➕", callback_data=f"shop_cart:inc:{r['product_id']}"),
        )
        kb.add(InlineKeyboardButton(f"🗑️ {st('Remove')} {r['name'][:16]}",
                                    callback_data=f"shop_cart:del:{r['product_id']}"))
    code = cart_coupon(user_id)
    disc = 0.0
    if code:
        ok, _msg, disc = validate_coupon(code, user_id, total)
        if not ok:
            disc = 0.0
    lines.append(f"\n{sep()}\n💰 {st('Cart Total')}: <b>{money(total)}</b>")
    if code:
        lines.append(f"🎟️ {st('Coupon')}: <code>{esc(code)}</code> (−{money(disc)})")
        lines.append(f"💵 {st('Payable')}: <b>{money(max(0.0, total - disc))}</b>")
    kb.add(InlineKeyboardButton(f"✅ {st('Checkout')}", callback_data="shop_cart:checkout"))
    kb.add(InlineKeyboardButton(f"🗑️ {st('Clear Cart')}", callback_data="shop_cart:clear"))
    send(chat_id, "\n".join(lines), reply_markup=kb)


# ═══════════════════════════════════════════════════════════════════════════
# COUPONS
# ═══════════════════════════════════════════════════════════════════════════
def validate_coupon(code, user_id, subtotal):
    code = str(code or "").strip().upper()
    if not code:
        return False, st("No coupon applied."), 0.0
    with conn() as c:
        row = c.execute("SELECT * FROM shop_coupons WHERE UPPER(code)=?", (code,)).fetchone()
        if not row:
            return False, st("Invalid coupon code."), 0.0
        cp = dict(row)
        used_by_user = c.execute(
            "SELECT COUNT(*) AS n FROM shop_coupon_usage WHERE UPPER(code)=? AND user_id=?",
            (code, user_id),
        ).fetchone()["n"]
    if not cp["enabled"]:
        return False, st("This coupon is disabled."), 0.0
    ts = now_ts()
    if cp["start_at"] and ts < int(cp["start_at"]):
        return False, st("This coupon is not active yet."), 0.0
    if cp["expiry_at"] and ts > int(cp["expiry_at"]):
        return False, st("This coupon has expired."), 0.0
    if cp["min_order"] and float(subtotal) < float(cp["min_order"]):
        return False, f"{st('Minimum order')}: {money(cp['min_order'])}", 0.0
    if cp["max_usage"] and int(cp["used_count"]) >= int(cp["max_usage"]):
        return False, st("This coupon usage limit is finished."), 0.0
    if cp["per_user_limit"] and used_by_user >= int(cp["per_user_limit"]):
        return False, st("You already used this coupon."), 0.0
    if cp["discount_type"].upper() == "PERCENT":
        disc = float(subtotal) * float(cp["discount_value"]) / 100.0
    else:
        disc = float(cp["discount_value"])
    disc = round(min(disc, float(subtotal)), 2)
    return True, f"{st('Coupon applied')}: −{money(disc)}", disc


# ═══════════════════════════════════════════════════════════════════════════
# CHECKOUT  (atomic balance + stock)
# ═══════════════════════════════════════════════════════════════════════════
def new_order_id() -> str:
    return "SO" + datetime.now().strftime("%y%m%d") + secrets.token_hex(2).upper()


def build_checkout(user_id):
    """Validate cart and return (ok, message, payload)."""
    rows = cart_rows(user_id)
    if not rows:
        return False, st("Your cart is empty."), None
    items, subtotal = [], 0.0
    for r in rows:
        if r["name"] is None or r["is_deleted"]:
            return False, f"{st('A product in your cart no longer exists')}: {r['product_id']}", None
        if not r["enabled"]:
            return False, f"{st('Product is disabled')}: {r['name']}", None
        if int(r["stock"]) <= 0:
            return False, f"{st('Out of stock')}: {r['name']}", None
        if int(r["qty"]) <= 0:
            return False, st("Invalid quantity in cart."), None
        if int(r["qty"]) > int(r["stock"]):
            return False, f"{st('Not enough stock for')} {r['name']} ({st('available')}: {int(r['stock'])})", None
        sub = round(float(r["price"]) * int(r["qty"]), 2)
        subtotal += sub
        items.append({
            "product_id": r["product_id"], "name": r["name"],
            "qty": int(r["qty"]), "unit_price": float(r["price"]), "subtotal": sub,
        })
    subtotal = round(subtotal, 2)
    mo = min_order()
    if mo and subtotal < mo:
        return False, f"{st('Minimum order amount')}: {money(mo)}", None
    code = cart_coupon(user_id)
    discount = 0.0
    if code:
        ok, _msg, discount = validate_coupon(code, user_id, subtotal)
        if not ok:
            code, discount = "", 0.0
    total = round(max(0.0, subtotal - discount), 2)
    balance = user_balance(user_id)
    if balance < total:
        return False, (f"⛔ {st('Insufficient balance')}\n{st('Needed')}: {money(total)}\n"
                       f"{st('Your balance')}: {money(balance)}"), None
    return True, "", {
        "items": items, "subtotal": subtotal, "discount": round(discount, 2),
        "total": total, "coupon": code, "balance": balance,
    }


def send_order_summary(chat_id, user_id):
    ok, msg, payload = build_checkout(user_id)
    if not ok:
        send(chat_id, msg)
        return
    token = secrets.token_hex(4)
    shop_states[_skey(chat_id, user_id)] = {
        "step": "await_confirm", "data": {"token": token, "payload": payload},
    }
    lines = [f"🧾 <b>{st('ORDER SUMMARY')}</b>\n{sep()}"]
    for it in payload["items"]:
        lines.append(f"• <b>{esc(it['name'])}</b> × {it['qty']} = {money(it['subtotal'])}")
    lines.append(
        f"\n{sep('─')}\n"
        f"{st('Subtotal')}: <b>{money(payload['subtotal'])}</b>\n"
        f"{st('Discount')}: <b>{money(payload['discount'])}</b>\n"
        f"{st('Total')}: <b>{money(payload['total'])}</b>\n\n"
        f"💰 {st('Current Balance')}: <b>{money(payload['balance'])}</b>\n"
        f"🏦 {st('Remaining Balance')}: <b>{money(payload['balance'] - payload['total'])}</b>"
    )
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton(f"✅ {st('CONFIRM ORDER')}", callback_data=f"shop_confirm:{token}"))
    kb.add(InlineKeyboardButton(f"❌ {st('CANCEL')}", callback_data="shop_confirm_cancel"))
    send(chat_id, "\n".join(lines), reply_markup=kb)


def place_order(user, chat_id, payload):
    """Atomic: validate + deduct balance + deduct stock + create order."""
    user_id = user.id
    with _lock:
        if user_id in _checkout_locks:
            return None, st("Your previous checkout is still processing.")
        _checkout_locks.add(user_id)
    c = None
    try:
        _M.get_wallet_stats(user_id)  # make sure wallet row exists
        order_id = new_order_id()
        total = float(payload["total"])
        c = raw_conn()
        c.execute("BEGIN IMMEDIATE")
        # re-validate stock and price inside the transaction
        for it in payload["items"]:
            row = c.execute(
                f"SELECT price, stock, enabled, is_deleted, name FROM shop_products WHERE product_id=?",
                (it["product_id"],),
            ).fetchone()
            if not row or row["is_deleted"] or not row["enabled"]:
                c.execute("ROLLBACK")
                return None, f"{st('Product unavailable')}: {it['name']}"
            if int(row["stock"]) < it["qty"]:
                c.execute("ROLLBACK")
                return None, f"{st('Not enough stock for')} {row['name']}"
            if round(float(row["price"]), 2) != round(it["unit_price"], 2):
                c.execute("ROLLBACK")
                return None, f"{st('Price changed for')} {row['name']}. {st('Please checkout again.')}"
        # balance deduction — guarded so it can never go negative
        cur = c.execute(
            "UPDATE wallet SET balance=balance-? WHERE user_id=? AND balance>=?",
            (total, user_id, total),
        )
        if cur.rowcount != 1:
            c.execute("ROLLBACK")
            return None, st("Insufficient balance.")
        for it in payload["items"]:
            cur = c.execute(
                "UPDATE shop_products SET stock=stock-?, sold=sold+?, updated_at=strftime('%s','now') "
                "WHERE product_id=? AND stock>=?",
                (it["qty"], it["qty"], it["product_id"], it["qty"]),
            )
            if cur.rowcount != 1:
                c.execute("ROLLBACK")
                return None, f"{st('Stock ran out for')} {it['name']}"
        uname = f"@{user.username}" if getattr(user, "username", None) else ""
        c.execute(
            "INSERT INTO shop_orders (order_id, user_id, username, subtotal, discount, total, "
            "coupon_code, payment_status, order_status, delivery_status) "
            "VALUES (?,?,?,?,?,?,?,'PAID','PENDING','NOT_DELIVERED')",
            (order_id, user_id, uname, payload["subtotal"], payload["discount"],
             total, payload["coupon"]),
        )
        for it in payload["items"]:
            c.execute(
                "INSERT INTO shop_order_items (order_id, product_id, product_name, qty, unit_price, subtotal) "
                "VALUES (?,?,?,?,?,?)",
                (order_id, it["product_id"], it["name"], it["qty"], it["unit_price"], it["subtotal"]),
            )
        if payload["coupon"]:
            c.execute("INSERT INTO shop_coupon_usage (code, user_id, order_id) VALUES (?,?,?)",
                      (payload["coupon"].upper(), user_id, order_id))
            c.execute("UPDATE shop_coupons SET used_count=used_count+1 WHERE UPPER(code)=?",
                      (payload["coupon"].upper(),))
        c.execute("DELETE FROM shop_cart_items WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM shop_cart_coupon WHERE user_id=?", (user_id,))
        c.execute(
            "INSERT INTO shop_transactions (user_id, order_id, kind, amount, note) VALUES (?,?,?,?,?)",
            (user_id, order_id, "DEBIT", total, "Shop order payment"),
        )
        c.execute("COMMIT")
        return order_id, ""
    except Exception as exc:
        logger.error("place_order failed uid=%s: %s", user_id, exc)
        try:
            if c is not None:
                c.execute("ROLLBACK")
        except Exception:
            pass
        return None, st("Something went wrong. No balance was deducted.")
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
        with _lock:
            _checkout_locks.discard(user_id)


def order_items(order_id):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM shop_order_items WHERE order_id=? ORDER BY id", (order_id,)).fetchall()]


def get_order(order_id):
    with conn() as c:
        row = c.execute("SELECT * FROM shop_orders WHERE order_id=?", (str(order_id),)).fetchone()
    return dict(row) if row else None


def order_text(o, admin=False) -> str:
    items = order_items(o["order_id"])
    lines = [
        f"🧾 <b>{st('ORDER')} <code>{esc(o['order_id'])}</code></b>\n{sep()}",
        f"<blockquote>",
    ]
    for it in items:
        lines.append(f"• {esc(it['product_name'])} × {it['qty']} = {money(it['subtotal'])}")
    lines.append("</blockquote>")
    lines.append(
        f"{st('Subtotal')}: {money(o['subtotal'])}\n"
        f"{st('Discount')}: {money(o['discount'])}\n"
        f"{st('Total')}: <b>{money(o['total'])}</b>\n"
        f"{st('Payment')}: <b>{esc(o['payment_status'])}</b>\n"
        f"{st('Status')}: <b>{esc(o['order_status'])}</b>\n"
        f"{st('Delivery')}: <b>{esc(o['delivery_status'])}</b>\n"
        f"{st('Date')}: {fmt_ts(o['created_at'])}"
    )
    if admin:
        lines.append(f"\n👤 {st('User')}: <code>{o['user_id']}</code> {esc(o['username'])}")
    return "\n".join(lines)


def order_admin_keyboard(order_id, status):
    kb = InlineKeyboardMarkup(row_width=2)
    buttons = []
    if status == "PENDING":
        buttons.append(InlineKeyboardButton(f"✅ {st('Confirm')}", callback_data=f"shop_order:confirm:{order_id}"))
    if status in ("PENDING", "CONFIRMED"):
        buttons.append(InlineKeyboardButton(f"🚚 {st('Processing')}", callback_data=f"shop_order:proc:{order_id}"))
    if status in ("CONFIRMED", "PROCESSING"):
        buttons.append(InlineKeyboardButton(f"📤 {st('Deliver')}", callback_data=f"shop_order:deliver:{order_id}"))
    if status in ("PENDING", "CONFIRMED", "PROCESSING"):
        buttons.append(InlineKeyboardButton(f"❌ {st('Cancel')}", callback_data=f"shop_order:cancel:{order_id}"))
    if not buttons:
        return None
    kb.add(*buttons)
    return kb


def push_order_to_admin_group(order_id):
    o = get_order(order_id)
    if not o:
        return
    gid = admin_group_id()
    items = order_items(order_id)
    item_lines = "\n".join(
        f"• {esc(i['product_name'])} (<code>{esc(i['product_id'])}</code>) × {i['qty']} @ {money(i['unit_price'])}"
        for i in items
    )
    text = (
        f"🛒 <b>NEW SHOP ORDER</b>\n{sep()}\n"
        f"🧾 Order ID: <code>{esc(order_id)}</code>\n"
        f"👤 User: {esc(o['username'] or 'N/A')}\n"
        f"🆔 User ID: <code>{o['user_id']}</code>\n\n"
        f"{item_lines}\n\n"
        f"Subtotal: <b>{money(o['subtotal'])}</b>\n"
        f"Discount: <b>{money(o['discount'])}</b>\n"
        f"Total: <b>{money(o['total'])}</b>\n"
        f"Payment: <b>{esc(o['payment_status'])}</b>\n"
        f"Status: <b>{esc(o['order_status'])}</b>"
    )
    if not gid:
        logger.warning("Shop order %s not forwarded — admin group is not configured.", order_id)
        return
    try:
        msg = bot().send_message(int(gid), text, reply_markup=order_admin_keyboard(order_id, o["order_status"]))
        with conn() as c:
            c.execute("UPDATE shop_orders SET group_msg_id=?, group_chat_id=? WHERE order_id=?",
                      (msg.message_id, str(gid), order_id))
    except Exception as exc:
        logger.warning("Shop order group forward failed: %s", exc)


def refresh_group_card(order_id):
    o = get_order(order_id)
    if not o or not o.get("group_msg_id") or not o.get("group_chat_id"):
        return
    try:
        bot().edit_message_reply_markup(
            int(o["group_chat_id"]), int(o["group_msg_id"]),
            reply_markup=order_admin_keyboard(order_id, o["order_status"]),
        )
    except Exception:
        pass


def set_order_status(order_id, status, delivery_status=None):
    with conn() as c:
        if delivery_status:
            c.execute(
                "UPDATE shop_orders SET order_status=?, delivery_status=?, "
                "updated_at=strftime('%s','now') WHERE order_id=?",
                (status, delivery_status, order_id),
            )
        else:
            c.execute(
                "UPDATE shop_orders SET order_status=?, updated_at=strftime('%s','now') WHERE order_id=?",
                (status, order_id),
            )


VALID_TRANSITIONS = {
    "PENDING": ("CONFIRMED", "PROCESSING", "CANCELLED"),
    "CONFIRMED": ("PROCESSING", "DELIVERED", "CANCELLED"),
    "PROCESSING": ("DELIVERED", "CANCELLED"),
    "DELIVERED": (),
    "CANCELLED": (),
}


def can_transition(current, target) -> bool:
    return target in VALID_TRANSITIONS.get(str(current).upper(), ())


def refund_order(order_id, reason="Order cancelled"):
    """Atomic refund + stock restore."""
    c = raw_conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM shop_orders WHERE order_id=?", (order_id,)).fetchone()
        if not row:
            c.execute("ROLLBACK")
            return False, "Order not found."
        o = dict(row)
        if o["order_status"] in ("CANCELLED", "DELIVERED"):
            c.execute("ROLLBACK")
            return False, "Order can no longer be cancelled."
        items = [dict(r) for r in c.execute(
            "SELECT * FROM shop_order_items WHERE order_id=?", (order_id,)).fetchall()]
        c.execute("UPDATE wallet SET balance=balance+? WHERE user_id=?", (o["total"], o["user_id"]))
        for it in items:
            c.execute(
                "UPDATE shop_products SET stock=stock+?, sold=MAX(0, sold-?) WHERE product_id=?",
                (it["qty"], it["qty"], it["product_id"]),
            )
        c.execute(
            "UPDATE shop_orders SET order_status='CANCELLED', payment_status='REFUNDED', "
            "updated_at=strftime('%s','now') WHERE order_id=?", (order_id,))
        c.execute(
            "INSERT INTO shop_transactions (user_id, order_id, kind, amount, note) VALUES (?,?,?,?,?)",
            (o["user_id"], order_id, "REFUND", o["total"], reason),
        )
        c.execute("COMMIT")
        return True, o
    except Exception as exc:
        logger.error("refund failed %s: %s", order_id, exc)
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        return False, "Refund failed."
    finally:
        try:
            c.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# DELIVERY
# ═══════════════════════════════════════════════════════════════════════════
def deliver_text(order_id, admin_id, content, is_resend=False):
    o = get_order(order_id)
    if not o:
        return False, "Order not found."
    try:
        send(
            o["user_id"],
            f"✅ <b>{st('ORDER DELIVERED')}</b>\n{sep()}\n"
            f"🧾 {st('Order ID')}: <code>{esc(order_id)}</code>\n"
            f"{st('Status')}: <b>DELIVERED</b>\n\n"
            f"📦 {st('Your product data')}:\n<blockquote>{esc(content)}</blockquote>",
        )
    except Exception as exc:
        logger.error("text delivery failed %s: %s", order_id, exc)
        return False, "Telegram delivery failed."
    with conn() as c:
        c.execute(
            "INSERT INTO shop_deliveries (order_id, admin_id, kind, content, is_resend) VALUES (?,?,?,?,?)",
            (order_id, admin_id, "TEXT", content, 1 if is_resend else 0),
        )
    if not is_resend:
        set_order_status(order_id, "DELIVERED", "DELIVERED")
        notify(o["user_id"], "DELIVERY", f"🚚 {st('Order')} {order_id} — {st('delivered.')}")
    log_activity(admin_id, "DELIVER_TEXT", order_id, "resend" if is_resend else "")
    refresh_group_card(order_id)
    return True, "Delivered."


def deliver_file(order_id, admin_id, file_id, kind="document", caption="", is_resend=False):
    o = get_order(order_id)
    if not o:
        return False, "Order not found."
    cap = (
        f"✅ <b>{st('ORDER DELIVERED')}</b>\n"
        f"🧾 {st('Order ID')}: <code>{esc(order_id)}</code>\n"
        f"📁 {st('Your product file is attached.')}"
    )
    try:
        if kind == "photo":
            bot().send_photo(o["user_id"], file_id, caption=cap)
        else:
            bot().send_document(o["user_id"], file_id, caption=cap)
    except Exception as exc:
        logger.error("file delivery failed %s: %s", order_id, exc)
        return False, "File delivery failed."
    with conn() as c:
        c.execute(
            "INSERT INTO shop_deliveries (order_id, admin_id, kind, content, file_id, is_resend) "
            "VALUES (?,?,?,?,?,?)",
            (order_id, admin_id, "FILE", caption, file_id, 1 if is_resend else 0),
        )
    if not is_resend:
        set_order_status(order_id, "DELIVERED", "DELIVERED")
        notify(o["user_id"], "DELIVERY", f"🚚 {st('Order')} {order_id} — {st('delivered.')}")
    log_activity(admin_id, "DELIVER_FILE", order_id, "resend" if is_resend else "")
    refresh_group_card(order_id)
    return True, "Delivered."


def try_auto_deliver(order_id, admin_id):
    """Deliver AUTO products from the auto-delivery inventory."""
    items = order_items(order_id)
    payloads = []
    with conn() as c:
        for it in items:
            p = c.execute("SELECT delivery_type FROM shop_products WHERE product_id=?",
                          (it["product_id"],)).fetchone()
            if not p or str(p["delivery_type"]).upper() != "AUTO":
                return False, "Order has non-auto products."
            rows = c.execute(
                "SELECT id, content FROM shop_auto_items WHERE product_id=? AND used=0 LIMIT ?",
                (it["product_id"], it["qty"]),
            ).fetchall()
            if len(rows) < it["qty"]:
                return False, "Auto delivery inventory is not enough."
            for r in rows:
                payloads.append((r["id"], it["product_name"], r["content"]))
        for row_id, _n, _c in payloads:
            c.execute("UPDATE shop_auto_items SET used=1, order_id=? WHERE id=?", (order_id, row_id))
    content = "\n\n".join(f"{name}:\n{val}" for _i, name, val in payloads)
    return deliver_text(order_id, admin_id, content)


# ═══════════════════════════════════════════════════════════════════════════
# USER TEXT ROUTER
# ═══════════════════════════════════════════════════════════════════════════
def handle_text(message) -> bool:
    """Return True when the shop consumed this message."""
    if _M is None:
        return False
    try:
        return _route_text(message)
    except Exception as exc:
        logger.error("shop handle_text error: %s", exc, exc_info=True)
        try:
            send(message.chat.id, f"⛔ {st('Something went wrong. Please try again.')}")
        except Exception:
            pass
        return True


def _route_text(message) -> bool:
    import shop_admin
    user = message.from_user
    uid = user.id
    chat_id = message.chat.id
    text = (message.text or "").strip()
    key = _skey(chat_id, uid)

    # 1) active shop input state (works in private chat and in the admin group)
    state = shop_states.get(key)
    if state:
        if text == BACK() or text == L("⛔", "Cancel"):
            shop_states.pop(key, None)
            menu = current_menu(uid)
            if menu:
                render_menu(chat_id, uid, menu, push=False)
            return True
        if shop_admin.handle_state(message, state):
            return True
        if _handle_user_state(message, state):
            return True

    if message.chat.type != "private":
        return False

    # 2) shop entry button
    if text == SHOP_BUTTON():
        open_shop(chat_id, uid)
        return True

    menu = current_menu(uid)
    if menu is None:
        return False

    if text == BACK():
        go_back(chat_id, uid)
        return True

    if menu.startswith("admin"):
        if shop_admin.handle_menu_text(message, menu, text):
            return True

    if _handle_user_menu_text(message, menu, text):
        return True

    # unknown text while inside the shop — hand back to the existing bot
    shop_nav.pop(uid, None)
    return False


def _handle_user_menu_text(message, menu, text) -> bool:
    import shop_admin
    user = message.from_user
    uid, chat_id = user.id, message.chat.id

    if menu == "main":
        if text == L("🛍️", "Products"):
            render_menu(chat_id, uid, "products")
        elif text == L("🛒", "My Cart"):
            send_cart(chat_id, uid)
        elif text == L("📋", "My Orders"):
            render_menu(chat_id, uid, "orders")
        elif text == L("📜", "History"):
            render_menu(chat_id, uid, "history")
        elif text == L("💰", "Shop Balance"):
            render_menu(chat_id, uid, "balance")
        elif text == L("🎁", "Offers & Bonus"):
            render_menu(chat_id, uid, "offers")
        elif text == L("🔔", "Notifications"):
            render_menu(chat_id, uid, "notifications")
        elif text == L("🆘", "Shop Support"):
            render_menu(chat_id, uid, "support")
        elif text == L("👑", "Shop Admin Panel"):
            if not is_shop_admin(uid):
                send(chat_id, f"❌ {st('Access Denied')}")
                return True
            render_menu(chat_id, uid, "admin")
        else:
            return False
        return True

    if menu == "products":
        if text == L("📦", "All Products"):
            send_product_list(chat_id, uid, "all")
        elif text == L("🔥", "Popular Products"):
            send_product_list(chat_id, uid, "popular")
        elif text == L("🆕", "New Products"):
            send_product_list(chat_id, uid, "new")
        elif text == L("⭐", "Featured Products"):
            send_product_list(chat_id, uid, "featured")
        elif text == L("🔎", "Search Product"):
            shop_states[_skey(chat_id, uid)] = {"step": "user_search", "data": {}}
            send(chat_id, f"🔎 {st('Send a product name, ID, category or keyword.')}")
        elif text == L("🗂️", "Categories"):
            cats = categories()
            if not cats:
                send(chat_id, f"📭 {st('No categories yet.')}")
                return True
            kb = InlineKeyboardMarkup(row_width=2)
            for name, n in cats:
                kb.add(InlineKeyboardButton(f"🗂️ {name} ({n})", callback_data=f"shop_list:cat:{name}:0"))
            send(chat_id, f"🗂️ <b>{st('CATEGORIES')}</b>", reply_markup=kb)
        else:
            return False
        return True

    if menu == "orders":
        mapping = {
            L("🆕", "New Orders"): ("PENDING", "NEW ORDERS", 1),
            L("⏳", "Pending Orders"): ("PENDING", "PENDING ORDERS", 0),
            L("🚚", "Processing Delivery"): ("PROCESSING", "PROCESSING / DELIVERY", 0),
            L("✅", "Confirmed Orders"): ("CONFIRMED", "CONFIRMED ORDERS", 0),
            L("❌", "Cancelled Orders"): ("CANCELLED", "CANCELLED ORDERS", 0),
        }
        if text in mapping:
            status, title, recent = mapping[text]
            send_user_orders(chat_id, uid, [status], title, recent_only=bool(recent))
            return True
        if text == L("🔎", "Order Details"):
            shop_states[_skey(chat_id, uid)] = {"step": "user_order_lookup", "data": {}}
            send(chat_id, f"🔎 {st('Send your Order ID.')}")
            return True
        return False

    if menu == "history":
        if text == L("✅", "Confirmation History"):
            send_user_orders(chat_id, uid, ["DELIVERED"], "CONFIRMATION HISTORY")
        elif text == L("⏳", "Pending History"):
            send_user_orders(chat_id, uid, ["PENDING", "CONFIRMED", "PROCESSING"], "PENDING HISTORY")
        elif text == L("❌", "Cancelled History"):
            send_user_orders(chat_id, uid, ["CANCELLED"], "CANCELLED HISTORY")
        else:
            return False
        return True

    if menu == "balance":
        if text == L("💰", "Current Balance"):
            bal = user_balance(uid)
            send(
                chat_id,
                f"💰 <b>{st('SHOP BALANCE')}</b>\n{sep()}\n"
                f"<blockquote>💰 {st('Current Balance')}: <b>{money(bal)}</b></blockquote>\n"
                f"<i>{st('This is your main bot balance.')}</i>",
            )
        elif text == L("➕", "Add Balance"):
            start_topup(chat_id, uid)
        elif text == L("📜", "Transaction History"):
            send_transactions(chat_id, uid)
        else:
            return False
        return True

    if menu == "offers":
        if text == L("🔥", "Special Offers"):
            send_offers(chat_id, "OFFER", "SPECIAL OFFERS")
        elif text == L("🎟️", "Promo Code"):
            shop_states[_skey(chat_id, uid)] = {"step": "user_promo", "data": {}}
            send(chat_id, f"🎟️ {st('Send your promo code.')}")
        elif text == L("💎", "Discount"):
            send_active_coupons(chat_id)
        elif text == L("🎁", "Bonus Products"):
            send_offers(chat_id, "BONUS", "BONUS PRODUCTS")
        else:
            return False
        return True

    if menu == "notifications":
        kinds = {
            L("📦", "Order Updates"): ("ORDER", "ORDER UPDATES"),
            L("🚚", "Delivery Updates"): ("DELIVERY", "DELIVERY UPDATES"),
            L("🎁", "Offer Alerts"): ("OFFER", "OFFER ALERTS"),
        }
        if text in kinds:
            kind, title = kinds[text]
            send_notifications(chat_id, uid, kind, title)
            return True
        return False

    if menu == "support":
        if text == L("💬", "Contact Support"):
            link = sget("support_link", "") or _M.get_setting("support_link", "")
            body = f"💬 <b>{st('CONTACT SUPPORT')}</b>\n{sep()}\n"
            if link:
                kb = InlineKeyboardMarkup()
                kb.add(InlineKeyboardButton(f"🛡️ {st('Contact Support')}", url=link))
                send(chat_id, body + esc(link), reply_markup=kb)
            else:
                send(chat_id, body + st("Support contact is not configured yet."))
        elif text == L("❓", "FAQ"):
            send(chat_id, f"❓ <b>{st('FAQ')}</b>\n{sep()}\n"
                          f"<blockquote>{esc(sget('faq', _default_faq()))}</blockquote>")
        elif text == L("📖", "How To Buy"):
            send(chat_id, f"📖 <b>{st('HOW TO BUY')}</b>\n{sep()}\n"
                          f"<blockquote>{esc(sget('how_to_buy', _default_how_to_buy()))}</blockquote>")
        else:
            return False
        return True

    return False


def _default_faq() -> str:
    return ("1. Balance: shop uses your main bot balance.\n"
            "2. Payment is taken only after you confirm an order.\n"
            "3. Delivery is done by admin as text or file.\n"
            "4. Cancelled orders are refunded automatically.")


def _default_how_to_buy() -> str:
    return ("1. Shop -> Products -> pick a product\n"
            "2. Choose quantity -> Add to Cart\n"
            "3. My Cart -> Checkout\n"
            "4. Confirm Order (balance is deducted)\n"
            "5. Wait for admin delivery")


def _handle_user_state(message, state) -> bool:
    user = message.from_user
    uid, chat_id = user.id, message.chat.id
    text = (message.text or "").strip()
    step = state.get("step")
    key = _skey(chat_id, uid)

    if step == "user_search":
        shop_states.pop(key, None)
        if len(text) < 2:
            send(chat_id, f"⛔ {st('Please send at least 2 characters.')}")
            return True
        send_product_list(chat_id, uid, "search", text)
        return True

    if step == "user_order_lookup":
        shop_states.pop(key, None)
        o = get_order(text)
        if not o or int(o["user_id"]) != uid:
            send(chat_id, f"⛔ {st('Order not found.')}")
            return True
        send(chat_id, order_text(o))
        return True

    if step == "user_promo":
        shop_states.pop(key, None)
        rows = cart_rows(uid)
        subtotal = sum(float(r["price"] or 0) * int(r["qty"]) for r in rows if r["name"])
        ok, msg, disc = validate_coupon(text, uid, subtotal)
        if not ok:
            send(chat_id, f"⛔ {msg}")
            return True
        with conn() as c:
            c.execute(
                "INSERT INTO shop_cart_coupon (user_id, code) VALUES (?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET code=excluded.code",
                (uid, text.strip().upper()),
            )
        send(chat_id, f"✅ {msg}\n{st('It will be applied at checkout.')}")
        return True

    if step == "user_topup_amount":
        try:
            amount = float(text)
        except Exception:
            send(chat_id, f"⛔ {st('Send a valid amount.')}")
            return True
        if amount <= 0:
            send(chat_id, f"⛔ {st('Amount must be greater than 0.')}")
            return True
        state["data"]["amount"] = amount
        state["step"] = "user_topup_ref"
        send(chat_id, f"🧾 {st('Send the payment method and transaction ID (one message).')}")
        return True

    if step == "user_topup_ref":
        shop_states.pop(key, None)
        submit_topup(user, chat_id, state["data"].get("amount", 0), text)
        return True

    return False


def send_user_orders(chat_id, user_id, statuses, title, recent_only=False, limit=10):
    q = ("SELECT * FROM shop_orders WHERE user_id=? AND order_status IN (%s) "
         "ORDER BY created_at DESC LIMIT ?" % ",".join("?" * len(statuses)))
    params = [user_id] + list(statuses) + [limit]
    with conn() as c:
        rows = [dict(r) for r in c.execute(q, params).fetchall()]
    if recent_only:
        cutoff = now_ts() - 86400
        rows = [r for r in rows if int(r["created_at"]) >= cutoff]
    if not rows:
        send(chat_id, f"📭 {st('No orders here yet.')}")
        return
    lines = [f"📋 <b>{st(title)}</b>\n{sep()}"]
    kb = InlineKeyboardMarkup(row_width=1)
    for o in rows:
        items = order_items(o["order_id"])
        names = ", ".join(f"{esc(i['product_name'])}×{i['qty']}" for i in items) or "-"
        lines.append(
            f"\n🧾 <code>{esc(o['order_id'])}</code>\n"
            f"🛍️ {names}\n"
            f"💰 {money(o['total'])} | 📅 {fmt_ts(o['created_at'])}\n"
            f"📌 {esc(o['order_status'])} / {esc(o['delivery_status'])}"
        )
        kb.add(InlineKeyboardButton(f"🔎 {o['order_id']}", callback_data=f"shop_myorder:{o['order_id']}"))
    send(chat_id, "\n".join(lines), reply_markup=kb)


def send_transactions(chat_id, user_id, limit=15):
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM shop_transactions WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit)).fetchall()]
    if not rows:
        send(chat_id, f"📭 {st('No shop transactions yet.')}")
        return
    icons = {"DEBIT": "➖", "REFUND": "↩️", "TOPUP": "➕"}
    lines = [f"📜 <b>{st('TRANSACTION HISTORY')}</b>\n{sep()}"]
    for t in rows:
        lines.append(
            f"\n{icons.get(t['kind'], '•')} <b>{esc(t['kind'])}</b> {money(t['amount'])}"
            f"\n🧾 {esc(t['order_id'] or '-')} | 📅 {fmt_ts(t['created_at'])}"
            f"\n📝 {esc(t['note'])}"
        )
    send(chat_id, "\n".join(lines))


def send_notifications(chat_id, user_id, kind, title, limit=12):
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM shop_notifications WHERE user_id=? AND kind=? "
            "ORDER BY created_at DESC LIMIT ?", (user_id, kind, limit)).fetchall()]
    if kind == "OFFER" and not rows:
        with conn() as c:
            rows = [{"text": f"🎁 {r['title']}", "created_at": r["created_at"]} for r in c.execute(
                "SELECT title, created_at FROM shop_offers WHERE enabled=1 "
                "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]
    if not rows:
        send(chat_id, f"📭 {st('No notifications yet.')}")
        return
    lines = [f"🔔 <b>{st(title)}</b>\n{sep()}"]
    for n in rows:
        lines.append(f"\n📅 {fmt_ts(n['created_at'])}\n{n['text']}")
    send(chat_id, "\n".join(lines))


def send_offers(chat_id, kind, title):
    ts = now_ts()
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM shop_offers WHERE enabled=1 AND kind=? "
            "AND (start_at=0 OR start_at<=?) AND (expiry_at=0 OR expiry_at>=?) "
            "ORDER BY created_at DESC LIMIT 15", (kind, ts, ts)).fetchall()]
    if not rows:
        send(chat_id, f"📭 {st('No active offers right now.')}")
        return
    lines = [f"🎁 <b>{st(title)}</b>\n{sep()}"]
    for o in rows:
        exp = fmt_ts(o["expiry_at"]) if o["expiry_at"] else st("No expiry")
        lines.append(f"\n🔥 <b>{esc(o['title'])}</b>\n{esc(o['description'])}\n⏳ {exp}")
    send(chat_id, "\n".join(lines))


def send_active_coupons(chat_id):
    ts = now_ts()
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM shop_coupons WHERE enabled=1 "
            "AND (start_at=0 OR start_at<=?) AND (expiry_at=0 OR expiry_at>=?) "
            "ORDER BY created_at DESC LIMIT 15", (ts, ts)).fetchall()]
    if not rows:
        send(chat_id, f"📭 {st('No discount codes available right now.')}")
        return
    lines = [f"💎 <b>{st('DISCOUNTS')}</b>\n{sep()}"]
    for cp in rows:
        val = f"{cp['discount_value']:.0f}%" if cp["discount_type"].upper() == "PERCENT" else money(cp["discount_value"])
        lines.append(
            f"\n🎟️ <code>{esc(cp['code'])}</code> — <b>{val}</b>"
            f"\n{st('Min order')}: {money(cp['min_order'])}"
            f" | ⏳ {fmt_ts(cp['expiry_at']) if cp['expiry_at'] else st('No expiry')}"
        )
    send(chat_id, "\n".join(lines))


# ═══════════════════════════════════════════════════════════════════════════
# TOP-UP (shop feature, uses the existing admin group + existing wallet)
# ═══════════════════════════════════════════════════════════════════════════
def start_topup(chat_id, user_id):
    info = sget("payment_info", "")
    text = f"➕ <b>{st('ADD BALANCE')}</b>\n{sep()}\n"
    if info:
        text += f"<blockquote>{esc(info)}</blockquote>\n"
    text += st("Send the amount you have paid (numbers only).")
    shop_states[_skey(chat_id, user_id)] = {"step": "user_topup_amount", "data": {}}
    send(chat_id, text)


def submit_topup(user, chat_id, amount, reference):
    with conn() as c:
        c.execute(
            "INSERT INTO shop_topups (user_id, amount, method, reference) VALUES (?,?,?,?)",
            (user.id, float(amount), "MANUAL", reference[:300]),
        )
        tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    send(chat_id, f"✅ {st('Top-up request submitted. Waiting for admin approval.')}\n"
                  f"🧾 {st('Request ID')}: <code>{tid}</code>\n💰 {money(amount)}")
    gid = admin_group_id()
    if not gid:
        return
    uname = f"@{user.username}" if user.username else "N/A"
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(f"✅ {st('Approve')}", callback_data=f"shop_topup:approve:{tid}"),
        InlineKeyboardButton(f"❌ {st('Reject')}", callback_data=f"shop_topup:reject:{tid}"),
    )
    try:
        msg = bot().send_message(
            int(gid),
            f"➕ <b>SHOP TOP-UP REQUEST</b>\n{sep()}\n"
            f"🧾 Request ID: <code>{tid}</code>\n"
            f"👤 User: {esc(uname)}\n🆔 User ID: <code>{user.id}</code>\n"
            f"💰 Amount: <b>{money(amount)}</b>\n"
            f"📝 Reference: {esc(reference)}",
            reply_markup=kb,
        )
        with conn() as c:
            c.execute("UPDATE shop_topups SET group_msg_id=? WHERE id=?", (msg.message_id, tid))
    except Exception as exc:
        logger.warning("topup forward failed: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════
# MEDIA HOOKS (product image, file delivery, auto import)
# ═══════════════════════════════════════════════════════════════════════════
def handle_media(message) -> bool:
    if _M is None:
        return False
    import shop_admin
    key = _skey(message.chat.id, message.from_user.id)
    state = shop_states.get(key)
    if not state:
        return False
    try:
        return shop_admin.handle_media_state(message, state)
    except Exception as exc:
        logger.error("shop media error: %s", exc, exc_info=True)
        send(message.chat.id, f"⛔ {st('Could not process that file.')}")
        return True


# ═══════════════════════════════════════════════════════════════════════════
# CALLBACKS
# ═══════════════════════════════════════════════════════════════════════════
def _register_callbacks():
    b = bot()

    @b.callback_query_handler(func=lambda c: bool(c.data) and c.data.startswith("shop"))
    def _shop_callbacks(call):
        try:
            _dispatch_callback(call)
        except Exception as exc:
            logger.error("shop callback error (%s): %s", call.data, exc, exc_info=True)
            try:
                b.answer_callback_query(call.id, st("Something went wrong."), show_alert=True)
            except Exception:
                pass


def _ack(call, text=None, alert=False):
    try:
        bot().answer_callback_query(call.id, text, show_alert=alert)
    except Exception:
        pass


def _dispatch_callback(call):
    import shop_admin
    data = call.data or ""
    user = call.from_user
    uid = user.id
    chat_id = call.message.chat.id if call.message else uid

    if data == "shop_noop":
        _ack(call)
        return

    if data.startswith("shop_admin") or data.startswith("shop_ap:"):
        shop_admin.handle_callback(call)
        return

    if data.startswith("shop_order:") or data.startswith("shop_dlv:") or data.startswith("shop_topup:"):
        shop_admin.handle_callback(call)
        return

    if data.startswith("shop_list:"):
        _, kind, arg, page = data.split(":", 3)
        _ack(call)
        if kind == "products":
            render_menu(chat_id, uid, "products", push=False)
        else:
            send_product_list(chat_id, uid, kind, arg, int(page or 0))
        return

    if data.startswith("shop_view:"):
        pid = data.split(":", 1)[1]
        p = get_product(pid)
        _ack(call)
        if not p or not p["enabled"]:
            send(chat_id, f"⛔ {st('This product is not available.')}")
            return
        if p.get("image_file_id"):
            try:
                bot().send_photo(chat_id, p["image_file_id"], caption=product_card(p),
                                 reply_markup=product_inline(p))
                return
            except Exception:
                pass
        send(chat_id, product_card(p), reply_markup=product_inline(p))
        return

    if data.startswith("shop_qty:"):
        _, mode, pid, qty = data.split(":", 3)
        qty = max(1, int(qty or 1))
        p = get_product(pid)
        if not p or not p["enabled"] or p["is_deleted"]:
            _ack(call, st("Product unavailable."), True)
            return
        stock = int(p["stock"])
        if stock <= 0:
            _ack(call, st("Out of stock."), True)
            return
        qty = min(qty, stock)
        _ack(call)
        unit = float(p["price"])
        kb = InlineKeyboardMarkup(row_width=3)
        kb.row(
            InlineKeyboardButton("➖", callback_data=f"shop_qty:{mode}:{pid}:{max(1, qty-1)}"),
            InlineKeyboardButton(f"{qty}", callback_data="shop_noop"),
            InlineKeyboardButton("➕", callback_data=f"shop_qty:{mode}:{pid}:{min(stock, qty+1)}"),
        )
        label = f"⚡ {st('Buy Now')}" if mode == "buy" else f"➕ {st('Add to Cart')}"
        kb.add(InlineKeyboardButton(label, callback_data=f"shop_do:{mode}:{pid}:{qty}"))
        kb.add(InlineKeyboardButton(f"🔙 {st('Back')}", callback_data=f"shop_view:{pid}"))
        text = (
            f"🛍️ <b>{esc(p['name'])}</b>\n{sep('─')}\n"
            f"<blockquote>"
            f"💰 {st('Unit Price')}: <b>{money(unit)}</b>\n"
            f"🔢 {st('Quantity')}: <b>{qty}</b>\n"
            f"🧮 {st('Subtotal')}: <b>{money(unit * qty)}</b>\n"
            f"📦 {st('Available')}: <b>{stock}</b>"
            f"</blockquote>"
        )
        try:
            bot().edit_message_text(text, chat_id, call.message.message_id, reply_markup=kb)
        except Exception:
            send(chat_id, text, reply_markup=kb)
        return

    if data.startswith("shop_do:"):
        _, mode, pid, qty = data.split(":", 3)
        qty = int(qty)
        p = get_product(pid)
        if not p or not p["enabled"] or p["is_deleted"]:
            _ack(call, st("Product unavailable."), True)
            return
        if qty <= 0 or qty > int(p["stock"]):
            _ack(call, st("Invalid quantity."), True)
            return
        cart_add(uid, pid, qty)
        _ack(call, st("Added to cart."))
        if mode == "buy":
            send_order_summary(chat_id, uid)
        else:
            send_cart(chat_id, uid)
        return

    if data.startswith("shop_cart:"):
        action = data.split(":", 1)[1]
        if action == "clear":
            cart_clear(uid)
            _ack(call, st("Cart cleared."))
            send(chat_id, f"🗑️ {st('Your cart is now empty.')}")
            return
        if action == "checkout":
            _ack(call)
            send_order_summary(chat_id, uid)
            return
        op, pid = action.split(":", 1)
        rows = {r["product_id"]: r for r in cart_rows(uid)}
        row = rows.get(pid)
        if not row:
            _ack(call, st("Item not in cart."), True)
            return
        if op == "del":
            cart_set_qty(uid, pid, 0)
        elif op == "inc":
            stock = int(row["stock"] or 0)
            if int(row["qty"]) + 1 > stock:
                _ack(call, st("No more stock available."), True)
                return
            cart_set_qty(uid, pid, int(row["qty"]) + 1)
        elif op == "dec":
            cart_set_qty(uid, pid, int(row["qty"]) - 1)
        _ack(call)
        send_cart(chat_id, uid)
        return

    if data == "shop_confirm_cancel":
        shop_states.pop(_skey(chat_id, uid), None)
        _ack(call, st("Cancelled."))
        send(chat_id, f"❌ {st('Checkout cancelled. Your cart is unchanged.')}")
        return

    if data.startswith("shop_confirm:"):
        token = data.split(":", 1)[1]
        key = _skey(chat_id, uid)
        state = shop_states.get(key)
        if not state or state.get("step") != "await_confirm" or state["data"].get("token") != token:
            _ack(call, st("This checkout has expired. Please try again."), True)
            return
        shop_states.pop(key, None)      # single-use — blocks double clicks
        _ack(call, st("Processing..."))
        try:
            bot().edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        # re-validate everything from scratch, then pay atomically
        ok, msg, payload = build_checkout(uid)
        if not ok:
            send(chat_id, msg)
            return
        order_id, err = place_order(user, chat_id, payload)
        if not order_id:
            send(chat_id, f"⛔ {err}")
            return
        o = get_order(order_id)
        notify(
            uid, "ORDER",
            f"✅ <b>{st('ORDER PLACED')}</b>\n{sep()}\n"
            f"🧾 {st('Order ID')}: <code>{order_id}</code>\n"
            f"💰 {st('Paid')}: <b>{money(o['total'])}</b>\n"
            f"🏦 {st('Balance')}: <b>{money(user_balance(uid))}</b>\n"
            f"📌 {st('Status')}: <b>PENDING</b>\n\n"
            f"⌛ {st('Waiting for admin confirmation.')}",
        )
        push_order_to_admin_group(order_id)
        for admin_id in _admin_ids():
            send(admin_id, f"🔔 {st('New shop order')}: <code>{order_id}</code> — {money(o['total'])}")
        render_menu(chat_id, uid, "main")
        return

    if data.startswith("shop_myorder:"):
        oid = data.split(":", 1)[1]
        o = get_order(oid)
        _ack(call)
        if not o or int(o["user_id"]) != uid:
            send(chat_id, f"⛔ {st('Order not found.')}")
            return
        send(chat_id, order_text(o))
        return

    _ack(call)


def _admin_ids():
    ids = set()
    try:
        ids.add(int(_M.ADMIN_ID))
        with conn() as c:
            for r in c.execute("SELECT user_id FROM admins").fetchall():
                ids.add(int(r["user_id"]))
    except Exception:
        pass
    return ids


# ═══════════════════════════════════════════════════════════════════════════
# PROFILE KEYBOARD HELPER (referral moved inside Profile)
# ═══════════════════════════════════════════════════════════════════════════
def profile_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton(L("🎁", "Referral")), KeyboardButton(L("🔙", "Back")))
    return kb
