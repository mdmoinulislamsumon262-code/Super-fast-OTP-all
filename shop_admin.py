"""
SHOP ADMIN MODULE — isolated admin system for the shop extension.

Nothing here touches the existing bot's Admin Panel, states, callbacks or
tables.  All authorization goes through ``shop.is_shop_admin`` (which reuses
``main.is_admin``), every callback lives in the ``shop_admin`` / ``shop_ap:``
/ ``shop_order:`` / ``shop_dlv:`` / ``shop_topup:`` namespace and every table
is ``shop_*``.
"""

import logging

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

import shop as S

logger = logging.getLogger("shop_admin")

L = S.L
BACK = S.BACK


def st(t):
    return S.st(t)


def send(chat_id, text, **kw):
    return S.send(chat_id, text, **kw)


def conn():
    return S.conn()


def raw_conn():
    return S.raw_conn()


def esc(v):
    return S.esc(v)


def money(v):
    return S.money(v)


def sep(c="━", n=28):
    return S.sep(c, n)


def CANCEL():
    return L("⛔", "Cancel")


def ask(chat_id, user_id, step, prompt, data=None):
    S.shop_states[S._skey(chat_id, user_id)] = {"step": step, "data": dict(data or {})}
    send(chat_id, prompt)


def guard(user_id, chat_id) -> bool:
    if S.is_shop_admin(user_id):
        return True
    send(chat_id, f"❌ {st('Access Denied')}")
    return False


# ═══════════════════════════════════════════════════════════════════════════
# MENUS
# ═══════════════════════════════════════════════════════════════════════════
ADMIN_MENUS = {
    "admin": lambda admin: [
        [L("📦", "Product Management"), L("🗃️", "Stock Management")],
        [L("🧾", "Order Management"), L("🚚", "Delivery Management")],
        [L("🎟️", "Coupons & Offers"), L("👥", "Users & Balance")],
        [L("📊", "Reports"), L("⚙️", "Shop Settings")],
        [L("🗂️", "Shop Logs")],
        [BACK()],
    ],
    "admin_products": lambda admin: [
        [L("➕", "Add Product"), L("✏️", "Edit Product")],
        [L("🗑️", "Delete Product"), L("📃", "Product List")],
        [L("🔁", "Enable / Disable"), L("⭐", "Featured Toggle")],
        [L("🔥", "Popular Toggle"), L("🖼️", "Set Product Image")],
        [BACK()],
    ],
    "admin_stock": lambda admin: [
        [L("➕", "Add Stock"), L("🔢", "Set Stock")],
        [L("⚠️", "Low Stock Report"), L("🤖", "Auto Delivery Items")],
        [L("📜", "Stock Logs")],
        [BACK()],
    ],
    "admin_orders": lambda admin: [
        [L("⏳", "Pending Orders"), L("✅", "Confirmed Orders")],
        [L("🚚", "Processing Orders"), L("📤", "Delivered Orders")],
        [L("❌", "Cancelled Orders"), L("🔎", "Find Order")],
        [BACK()],
    ],
    "admin_delivery": lambda admin: [
        [L("📤", "Deliver Order"), L("♻️", "Resend Delivery")],
        [L("🤖", "Auto Deliver"), L("📜", "Delivery Logs")],
        [BACK()],
    ],
    "admin_promos": lambda admin: [
        [L("➕", "Add Coupon"), L("📃", "Coupon List")],
        [L("🗑️", "Delete Coupon"), L("🎁", "Add Offer")],
        [L("📃", "Offer List"), L("🗑️", "Delete Offer")],
        [BACK()],
    ],
    "admin_users": lambda admin: [
        [L("➕", "Add User Balance"), L("➖", "Remove User Balance")],
        [L("💳", "Top-up Requests"), L("🔎", "User Lookup")],
        [L("📢", "Send Notice")],
        [BACK()],
    ],
    "admin_reports": lambda admin: [
        [L("📈", "Sales Report"), L("🏆", "Top Products")],
        [L("💰", "Revenue Summary"), L("📊", "Order Stats")],
        [BACK()],
    ],
    "admin_settings": lambda admin: [
        [L("🏪", "Shop Name"), L("💱", "Currency")],
        [L("🧮", "Minimum Order"), L("💳", "Payment Info")],
        [L("🛠️", "Maintenance Mode"), L("⚠️", "Low Stock Limit")],
        [L("❓", "FAQ Text"), L("📖", "How To Buy Text")],
        [L("💬", "Support Contact")],
        [BACK()],
    ],
    "admin_logs": lambda admin: [
        [L("🗂️", "Activity Logs"), L("📜", "Stock Log List")],
        [L("💸", "Shop Transactions")],
        [BACK()],
    ],
}

ADMIN_TITLES = {
    "admin": lambda: f"👑 <b>{st('SHOP ADMIN PANEL')}</b>\n{sep()}\n{_admin_overview()}",
    "admin_products": lambda: f"📦 <b>{st('PRODUCT MANAGEMENT')}</b>",
    "admin_stock": lambda: f"🗃️ <b>{st('STOCK MANAGEMENT')}</b>",
    "admin_orders": lambda: f"🧾 <b>{st('ORDER MANAGEMENT')}</b>",
    "admin_delivery": lambda: f"🚚 <b>{st('DELIVERY MANAGEMENT')}</b>",
    "admin_promos": lambda: f"🎟️ <b>{st('COUPONS & OFFERS')}</b>",
    "admin_users": lambda: f"👥 <b>{st('USERS & BALANCE')}</b>",
    "admin_reports": lambda: f"📊 <b>{st('REPORTS')}</b>",
    "admin_settings": lambda: f"⚙️ <b>{st('SHOP SETTINGS')}</b>",
    "admin_logs": lambda: f"🗂️ <b>{st('SHOP LOGS')}</b>",
}


def _admin_overview() -> str:
    try:
        with conn() as c:
            prod = c.execute("SELECT COUNT(*) n FROM shop_products WHERE is_deleted=0").fetchone()["n"]
            pend = c.execute("SELECT COUNT(*) n FROM shop_orders WHERE order_status='PENDING'").fetchone()["n"]
            rev = c.execute(
                "SELECT COALESCE(SUM(total),0) t FROM shop_orders WHERE order_status IN ('CONFIRMED','PROCESSING','DELIVERED')"
            ).fetchone()["t"]
            low = c.execute(
                "SELECT COUNT(*) n FROM shop_products WHERE is_deleted=0 AND stock<=?",
                (S.low_stock_threshold(),)).fetchone()["n"]
        return (
            f"<blockquote>"
            f"📦 {st('Products')}: <b>{prod}</b>\n"
            f"⏳ {st('Pending Orders')}: <b>{pend}</b>\n"
            f"💰 {st('Revenue')}: <b>{money(rev)}</b>\n"
            f"⚠️ {st('Low Stock')}: <b>{low}</b>"
            f"</blockquote>"
        )
    except Exception:
        return st("Choose an option below.")


# ═══════════════════════════════════════════════════════════════════════════
# MENU TEXT ROUTING
# ═══════════════════════════════════════════════════════════════════════════
def handle_menu_text(message, menu, text) -> bool:
    uid, chat_id = message.from_user.id, message.chat.id
    if not guard(uid, chat_id):
        return True

    if menu == "admin":
        target = {
            L("📦", "Product Management"): "admin_products",
            L("🗃️", "Stock Management"): "admin_stock",
            L("🧾", "Order Management"): "admin_orders",
            L("🚚", "Delivery Management"): "admin_delivery",
            L("🎟️", "Coupons & Offers"): "admin_promos",
            L("👥", "Users & Balance"): "admin_users",
            L("📊", "Reports"): "admin_reports",
            L("⚙️", "Shop Settings"): "admin_settings",
            L("🗂️", "Shop Logs"): "admin_logs",
        }.get(text)
        if not target:
            return False
        S.render_menu(chat_id, uid, target)
        return True

    if menu == "admin_products":
        return _products_menu(chat_id, uid, text)
    if menu == "admin_stock":
        return _stock_menu(chat_id, uid, text)
    if menu == "admin_orders":
        return _orders_menu(chat_id, uid, text)
    if menu == "admin_delivery":
        return _delivery_menu(chat_id, uid, text)
    if menu == "admin_promos":
        return _promos_menu(chat_id, uid, text)
    if menu == "admin_users":
        return _users_menu(chat_id, uid, text)
    if menu == "admin_reports":
        return _reports_menu(chat_id, uid, text)
    if menu == "admin_settings":
        return _settings_menu(chat_id, uid, text)
    if menu == "admin_logs":
        return _logs_menu(chat_id, uid, text)
    return False


# ── PRODUCTS ──────────────────────────────────────────────────────────────
def _products_menu(chat_id, uid, text) -> bool:
    if text == L("➕", "Add Product"):
        ask(chat_id, uid, "ap_add_name", f"📝 {st('Send the product name.')}")
    elif text == L("✏️", "Edit Product"):
        ask(chat_id, uid, "ap_edit_id", f"🆔 {st('Send the Product ID you want to edit.')}")
    elif text == L("🗑️", "Delete Product"):
        ask(chat_id, uid, "ap_del_id", f"🗑️ {st('Send the Product ID to delete.')}")
    elif text == L("📃", "Product List"):
        _product_list(chat_id)
    elif text == L("🔁", "Enable / Disable"):
        ask(chat_id, uid, "ap_toggle_enabled", f"🔁 {st('Send the Product ID to enable/disable.')}")
    elif text == L("⭐", "Featured Toggle"):
        ask(chat_id, uid, "ap_toggle_featured", f"⭐ {st('Send the Product ID to toggle Featured.')}")
    elif text == L("🔥", "Popular Toggle"):
        ask(chat_id, uid, "ap_toggle_popular", f"🔥 {st('Send the Product ID to toggle Popular.')}")
    elif text == L("🖼️", "Set Product Image"):
        ask(chat_id, uid, "ap_image_id", f"🖼️ {st('Send the Product ID, then send the photo.')}")
    else:
        return False
    return True


def _product_list(chat_id, limit=30):
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM shop_products WHERE is_deleted=0 ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()]
    if not rows:
        send(chat_id, f"📭 {st('No products yet.')}")
        return
    lines = [f"📃 <b>{st('PRODUCT LIST')}</b>\n{sep()}"]
    for p in rows:
        flag = "🟢" if p["enabled"] else "🔴"
        marks = ("⭐" if p["featured"] else "") + ("🔥" if p["popular"] else "")
        lines.append(
            f"{flag} <code>{esc(p['product_id'])}</code> — <b>{esc(p['name'])}</b> {marks}\n"
            f"    💰 {money(p['price'])} | 📦 {p['stock']} | 🗂️ {esc(p['category'])} | "
            f"🚚 {esc(p['delivery_type'])} | 🧾 {st('Sold')}: {p['sold']}"
        )
    send(chat_id, "\n".join(lines))


# ── STOCK ─────────────────────────────────────────────────────────────────
def _stock_menu(chat_id, uid, text) -> bool:
    if text == L("➕", "Add Stock"):
        ask(chat_id, uid, "as_add_id", f"🆔 {st('Send the Product ID.')}")
    elif text == L("🔢", "Set Stock"):
        ask(chat_id, uid, "as_set_id", f"🆔 {st('Send the Product ID.')}")
    elif text == L("⚠️", "Low Stock Report"):
        _low_stock(chat_id)
    elif text == L("🤖", "Auto Delivery Items"):
        ask(chat_id, uid, "as_auto_id",
            f"🤖 {st('Send the Product ID for auto-delivery items.')}")
    elif text == L("📜", "Stock Logs"):
        _stock_logs(chat_id)
    else:
        return False
    return True


def _low_stock(chat_id):
    lim = S.low_stock_threshold()
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM shop_products WHERE is_deleted=0 AND stock<=? ORDER BY stock ASC LIMIT 40",
            (lim,)).fetchall()]
    if not rows:
        send(chat_id, f"✅ {st('No low-stock products.')}")
        return
    body = "\n".join(
        f"⚠️ <code>{esc(p['product_id'])}</code> {esc(p['name'])} — 📦 <b>{p['stock']}</b>"
        for p in rows)
    send(chat_id, f"⚠️ <b>{st('LOW STOCK')}</b> ({st('limit')} {lim})\n{sep()}\n{body}")


def _stock_logs(chat_id, limit=20):
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM shop_stock_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
    if not rows:
        send(chat_id, f"📭 {st('No stock logs.')}")
        return
    body = "\n".join(
        f"📜 {S.fmt_ts(r['created_at'])} | <code>{esc(r['product_id'])}</code> | "
        f"{'+' if r['change'] >= 0 else ''}{r['change']} | {esc(r['reason'])}"
        for r in rows)
    send(chat_id, f"📜 <b>{st('STOCK LOGS')}</b>\n{sep()}\n{body}")


# ── ORDERS ────────────────────────────────────────────────────────────────
def _orders_menu(chat_id, uid, text) -> bool:
    mapping = {
        L("⏳", "Pending Orders"): "PENDING",
        L("✅", "Confirmed Orders"): "CONFIRMED",
        L("🚚", "Processing Orders"): "PROCESSING",
        L("📤", "Delivered Orders"): "DELIVERED",
        L("❌", "Cancelled Orders"): "CANCELLED",
    }
    if text in mapping:
        _admin_order_list(chat_id, mapping[text])
        return True
    if text == L("🔎", "Find Order"):
        ask(chat_id, uid, "ao_lookup", f"🔎 {st('Send the Order ID.')}")
        return True
    return False


def _admin_order_list(chat_id, status, limit=12):
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM shop_orders WHERE order_status=? ORDER BY created_at DESC LIMIT ?",
            (status, limit)).fetchall()]
    if not rows:
        send(chat_id, f"📭 {st('No orders with status')} <b>{status}</b>.")
        return
    send(chat_id, f"🧾 <b>{status} {st('ORDERS')}</b> — {len(rows)}\n{sep()}")
    for o in rows:
        kb = S.order_admin_keyboard(o["order_id"], o["order_status"]) or InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton(f"🔎 {st('Details')}",
                                    callback_data=f"shop_order:view:{o['order_id']}"))
        send(chat_id, S.order_text(o, admin=True), reply_markup=kb)


# ── DELIVERY ──────────────────────────────────────────────────────────────
def _delivery_menu(chat_id, uid, text) -> bool:
    if text == L("📤", "Deliver Order"):
        ask(chat_id, uid, "ad_deliver_id", f"🆔 {st('Send the Order ID to deliver.')}")
    elif text == L("♻️", "Resend Delivery"):
        ask(chat_id, uid, "ad_resend_id", f"🆔 {st('Send the Order ID to resend.')}")
    elif text == L("🤖", "Auto Deliver"):
        ask(chat_id, uid, "ad_auto_id", f"🤖 {st('Send the Order ID for auto delivery.')}")
    elif text == L("📜", "Delivery Logs"):
        _delivery_logs(chat_id)
    else:
        return False
    return True


def _delivery_logs(chat_id, limit=15):
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM shop_deliveries ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
    if not rows:
        send(chat_id, f"📭 {st('No deliveries yet.')}")
        return
    body = "\n".join(
        f"🚚 {S.fmt_ts(r['created_at'])} | <code>{esc(r['order_id'])}</code> | {esc(r['kind'])}"
        f"{' | ♻️ resend' if r['is_resend'] else ''}"
        for r in rows)
    send(chat_id, f"📜 <b>{st('DELIVERY LOGS')}</b>\n{sep()}\n{body}")


# ── COUPONS & OFFERS ──────────────────────────────────────────────────────
def _promos_menu(chat_id, uid, text) -> bool:
    if text == L("➕", "Add Coupon"):
        ask(chat_id, uid, "ac_code", f"🎟️ {st('Send the coupon code.')}")
    elif text == L("📃", "Coupon List"):
        _coupon_list(chat_id)
    elif text == L("🗑️", "Delete Coupon"):
        ask(chat_id, uid, "ac_delete", f"🗑️ {st('Send the coupon code to delete.')}")
    elif text == L("🎁", "Add Offer"):
        ask(chat_id, uid, "ao_offer_title", f"🎁 {st('Send the offer title.')}")
    elif text == L("📃", "Offer List"):
        _offer_list(chat_id)
    elif text == L("🗑️", "Delete Offer"):
        ask(chat_id, uid, "ao_offer_delete", f"🗑️ {st('Send the Offer ID to delete.')}")
    else:
        return False
    return True


def _coupon_list(chat_id):
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM shop_coupons ORDER BY id DESC LIMIT 30").fetchall()]
    if not rows:
        send(chat_id, f"📭 {st('No coupons yet.')}")
        return
    body = "\n".join(
        f"{'🟢' if r['enabled'] else '🔴'} <code>{esc(r['code'])}</code> — "
        f"{r['discount_value']:g}{'%' if r['discount_type'] == 'PERCENT' else ' ' + S.currency()} | "
        f"{st('Min')} {money(r['min_order'])} | {st('Used')} {r['used_count']}/"
        f"{r['max_usage'] or '∞'} | {st('Expires')} "
        f"{S.fmt_ts(r['expiry_at']) if r['expiry_at'] else '∞'}"
        for r in rows)
    send(chat_id, f"🎟️ <b>{st('COUPONS')}</b>\n{sep()}\n{body}")


def _offer_list(chat_id):
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM shop_offers ORDER BY id DESC LIMIT 30").fetchall()]
    if not rows:
        send(chat_id, f"📭 {st('No offers yet.')}")
        return
    body = "\n".join(
        f"{'🟢' if r['enabled'] else '🔴'} <code>{r['id']}</code> [{esc(r['kind'])}] "
        f"<b>{esc(r['title'])}</b>"
        for r in rows)
    send(chat_id, f"🎁 <b>{st('OFFERS')}</b>\n{sep()}\n{body}")


# ── USERS & BALANCE ───────────────────────────────────────────────────────
def _users_menu(chat_id, uid, text) -> bool:
    if text == L("➕", "Add User Balance"):
        ask(chat_id, uid, "au_add_id", f"🆔 {st('Send the User ID.')}")
    elif text == L("➖", "Remove User Balance"):
        ask(chat_id, uid, "au_rem_id", f"🆔 {st('Send the User ID.')}")
    elif text == L("💳", "Top-up Requests"):
        _topup_list(chat_id)
    elif text == L("🔎", "User Lookup"):
        ask(chat_id, uid, "au_lookup", f"🔎 {st('Send the User ID.')}")
    elif text == L("📢", "Send Notice"):
        ask(chat_id, uid, "au_notice", f"📢 {st('Send the notice text for all shop users.')}")
    else:
        return False
    return True


def _topup_list(chat_id, limit=15):
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM shop_topups ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
    if not rows:
        send(chat_id, f"📭 {st('No top-up requests.')}")
        return
    for r in rows:
        kb = None
        if r["status"] == "PENDING":
            kb = InlineKeyboardMarkup(row_width=2)
            kb.add(
                InlineKeyboardButton(f"✅ {st('Approve')}", callback_data=f"shop_topup:approve:{r['id']}"),
                InlineKeyboardButton(f"❌ {st('Reject')}", callback_data=f"shop_topup:reject:{r['id']}"),
            )
        send(chat_id,
             f"💳 <b>{st('TOP-UP')} #{r['id']}</b>\n{sep('─')}\n"
             f"🆔 {st('User')}: <code>{r['user_id']}</code>\n"
             f"💰 {money(r['amount'])}\n📝 {esc(r['reference'])}\n"
             f"📌 {st('Status')}: <b>{esc(r['status'])}</b>\n"
             f"🕒 {S.fmt_ts(r['created_at'])}",
             reply_markup=kb)


# ── REPORTS ───────────────────────────────────────────────────────────────
def _reports_menu(chat_id, uid, text) -> bool:
    if text == L("📈", "Sales Report"):
        _sales_report(chat_id)
    elif text == L("🏆", "Top Products"):
        _top_products(chat_id)
    elif text == L("💰", "Revenue Summary"):
        _revenue(chat_id)
    elif text == L("📊", "Order Stats"):
        _order_stats(chat_id)
    else:
        return False
    return True


def _sales_report(chat_id):
    q = ("SELECT COUNT(*) n, COALESCE(SUM(total),0) t FROM shop_orders "
         "WHERE order_status IN ('CONFIRMED','PROCESSING','DELIVERED') AND created_at>=?")
    now = S.now_ts()
    with conn() as c:
        d = c.execute(q, (now - 86400,)).fetchone()
        w = c.execute(q, (now - 7 * 86400,)).fetchone()
        m = c.execute(q, (now - 30 * 86400,)).fetchone()
    send(chat_id,
         f"📈 <b>{st('SALES REPORT')}</b>\n{sep()}\n<blockquote>"
         f"📅 {st('Today')}: <b>{d['n']}</b> — {money(d['t'])}\n"
         f"🗓️ {st('7 Days')}: <b>{w['n']}</b> — {money(w['t'])}\n"
         f"📆 {st('30 Days')}: <b>{m['n']}</b> — {money(m['t'])}"
         f"</blockquote>")


def _top_products(chat_id):
    with conn() as c:
        rows = [dict(r) for r in c.execute("""
            SELECT i.product_id, i.product_name, SUM(i.qty) q, SUM(i.subtotal) t
            FROM shop_order_items i JOIN shop_orders o ON o.order_id=i.order_id
            WHERE o.order_status IN ('CONFIRMED','PROCESSING','DELIVERED')
            GROUP BY i.product_id ORDER BY q DESC LIMIT 10
        """).fetchall()]
    if not rows:
        send(chat_id, f"📭 {st('No sales yet.')}")
        return
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    body = "\n".join(
        f"{medals[i]} <b>{esc(r['product_name'])}</b> — {r['q']} {st('sold')} | {money(r['t'])}"
        for i, r in enumerate(rows))
    send(chat_id, f"🏆 <b>{st('TOP PRODUCTS')}</b>\n{sep()}\n{body}")


def _revenue(chat_id):
    with conn() as c:
        rev = c.execute(
            "SELECT COALESCE(SUM(total),0) t FROM shop_orders "
            "WHERE order_status IN ('CONFIRMED','PROCESSING','DELIVERED')").fetchone()["t"]
        ref = c.execute(
            "SELECT COALESCE(SUM(amount),0) t FROM shop_transactions WHERE kind='REFUND'").fetchone()["t"]
        top = c.execute(
            "SELECT COALESCE(SUM(amount),0) t FROM shop_topups WHERE status='APPROVED'").fetchone()["t"]
    send(chat_id,
         f"💰 <b>{st('REVENUE SUMMARY')}</b>\n{sep()}\n<blockquote>"
         f"🧾 {st('Gross Sales')}: <b>{money(rev)}</b>\n"
         f"↩️ {st('Refunded')}: <b>{money(ref)}</b>\n"
         f"➕ {st('Approved Top-ups')}: <b>{money(top)}</b>\n"
         f"📊 {st('Net')}: <b>{money(float(rev) - float(ref))}</b>"
         f"</blockquote>")


def _order_stats(chat_id):
    with conn() as c:
        rows = c.execute(
            "SELECT order_status s, COUNT(*) n FROM shop_orders GROUP BY order_status").fetchall()
    if not rows:
        send(chat_id, f"📭 {st('No orders yet.')}")
        return
    body = "\n".join(f"• {esc(r['s'])}: <b>{r['n']}</b>" for r in rows)
    send(chat_id, f"📊 <b>{st('ORDER STATS')}</b>\n{sep()}\n<blockquote>{body}</blockquote>")


# ── SETTINGS ──────────────────────────────────────────────────────────────
def SETTING_PROMPTS():
    return {
    L("🏪", "Shop Name"): ("shop_name", "🏪 Send the new shop name."),
    L("💱", "Currency"): ("currency", "💱 Send the currency code (e.g. BDT)."),
    L("🧮", "Minimum Order"): ("min_order", "🧮 Send the minimum order amount."),
    L("💳", "Payment Info"): ("payment_info", "💳 Send the payment instructions shown to users."),
    L("⚠️", "Low Stock Limit"): ("low_stock", "⚠️ Send the low-stock threshold number."),
    L("❓", "FAQ Text"): ("faq_text", "❓ Send the FAQ text."),
    L("📖", "How To Buy Text"): ("how_to_buy", "📖 Send the How-To-Buy text."),
    L("💬", "Support Contact"): ("support_contact", "💬 Send the support contact (@username or link)."),
    }


def _settings_menu(chat_id, uid, text) -> bool:
    _prompts = SETTING_PROMPTS()
    if text in _prompts:
        key, prompt = _prompts[text]
        current = S.sget(key, "")
        ask(chat_id, uid, "aset_value",
            f"{st(prompt)}\n\n{st('Current')}: <code>{esc(current) or '—'}</code>",
            {"key": key})
        return True
    if text == L("🛠️", "Maintenance Mode"):
        new = "0" if S.maintenance_on() else "1"
        S.sset("maintenance", new)
        S.log_activity(uid, "MAINTENANCE", "", new)
        send(chat_id, f"🛠️ {st('Maintenance mode is now')} <b>{'ON' if new == '1' else 'OFF'}</b>.")
        return True
    return False


# ── LOGS ──────────────────────────────────────────────────────────────────
def _logs_menu(chat_id, uid, text) -> bool:
    if text == L("🗂️", "Activity Logs"):
        with conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM shop_activity_logs ORDER BY id DESC LIMIT 25").fetchall()]
        if not rows:
            send(chat_id, f"📭 {st('No activity yet.')}")
            return True
        body = "\n".join(
            f"🗂️ {S.fmt_ts(r['created_at'])} | <code>{r['admin_id']}</code> | "
            f"{esc(r['action'])} {esc(r['target'])} {esc(r['detail'])}"
            for r in rows)
        send(chat_id, f"🗂️ <b>{st('ACTIVITY LOGS')}</b>\n{sep()}\n{body}")
    elif text == L("📜", "Stock Log List"):
        _stock_logs(chat_id, 25)
    elif text == L("💸", "Shop Transactions"):
        with conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM shop_transactions ORDER BY id DESC LIMIT 25").fetchall()]
        if not rows:
            send(chat_id, f"📭 {st('No transactions yet.')}")
            return True
        body = "\n".join(
            f"💸 {S.fmt_ts(r['created_at'])} | <code>{r['user_id']}</code> | {esc(r['kind'])} | "
            f"{money(r['amount'])} | {esc(r['order_id'])}"
            for r in rows)
        send(chat_id, f"💸 <b>{st('SHOP TRANSACTIONS')}</b>\n{sep()}\n{body}")
    else:
        return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
# STATE MACHINE
# ═══════════════════════════════════════════════════════════════════════════
def _num(text, integer=False):
    try:
        v = int(float(text)) if integer else float(text)
        return v
    except Exception:
        return None


def _finish(chat_id, uid, msg, menu=None):
    S.shop_states.pop(S._skey(chat_id, uid), None)
    send(chat_id, msg)
    menu = menu or S.current_menu(uid)
    if menu:
        S.render_menu(chat_id, uid, menu, push=False)


def handle_state(message, state) -> bool:
    step = str(state.get("step") or "")
    if not step.startswith(("ap_", "as_", "ao_", "ad_", "ac_", "au_", "aset_")):
        return False
    uid, chat_id = message.from_user.id, message.chat.id
    if not guard(uid, chat_id):
        S.shop_states.pop(S._skey(chat_id, uid), None)
        return True
    text = (message.text or "").strip()
    data = state.setdefault("data", {})

    try:
        return _run_state(message, state, step, text, data, uid, chat_id)
    except Exception as exc:
        logger.error("shop admin state %s failed: %s", step, exc, exc_info=True)
        _finish(chat_id, uid, f"⛔ {st('Action failed. Please try again.')}")
        return True


def _run_state(message, state, step, text, data, uid, chat_id) -> bool:
    # ── ADD PRODUCT ───────────────────────────────────────────────────────
    if step == "ap_add_name":
        if len(text) < 2:
            send(chat_id, f"⛔ {st('Name is too short.')}")
            return True
        data["name"] = text[:80]
        state["step"] = "ap_add_category"
        send(chat_id, f"🗂️ {st('Send the category.')}")
        return True

    if step == "ap_add_category":
        data["category"] = text[:40] or "General"
        state["step"] = "ap_add_price"
        send(chat_id, f"💰 {st('Send the price.')}")
        return True

    if step == "ap_add_price":
        price = _num(text)
        if price is None or price <= 0:
            send(chat_id, f"⛔ {st('Send a valid price.')}")
            return True
        data["price"] = price
        state["step"] = "ap_add_stock"
        send(chat_id, f"📦 {st('Send the stock quantity.')}")
        return True

    if step == "ap_add_stock":
        stock = _num(text, True)
        if stock is None or stock < 0:
            send(chat_id, f"⛔ {st('Send a valid stock number.')}")
            return True
        data["stock"] = stock
        state["step"] = "ap_add_delivery"
        send(chat_id, f"🚚 {st('Send delivery type')}: <code>TEXT</code> / <code>FILE</code> / <code>AUTO</code>")
        return True

    if step == "ap_add_delivery":
        dt = text.upper()
        if dt not in ("TEXT", "FILE", "AUTO"):
            send(chat_id, f"⛔ {st('Send TEXT, FILE or AUTO.')}")
            return True
        data["delivery_type"] = dt
        state["step"] = "ap_add_desc"
        send(chat_id, f"📝 {st('Send the description (or send')} <code>-</code>{st(' to skip).')}")
        return True

    if step == "ap_add_desc":
        desc = "" if text == "-" else text[:900]
        pid = _new_product_id()
        with conn() as c:
            c.execute(
                "INSERT INTO shop_products (product_id, name, category, description, price, stock, "
                "delivery_type) VALUES (?,?,?,?,?,?,?)",
                (pid, data["name"], data["category"], desc, data["price"], data["stock"],
                 data["delivery_type"]),
            )
            c.execute(
                "INSERT INTO shop_stock_logs (product_id, admin_id, change, reason) VALUES (?,?,?,?)",
                (pid, uid, data["stock"], "initial stock"))
        S.log_activity(uid, "ADD_PRODUCT", pid, data["name"])
        _finish(chat_id, uid,
                f"✅ <b>{st('PRODUCT ADDED')}</b>\n{sep()}\n"
                f"🆔 <code>{pid}</code>\n🛍️ {esc(data['name'])}\n"
                f"💰 {money(data['price'])}\n📦 {data['stock']}\n🚚 {data['delivery_type']}",
                "admin_products")
        return True

    # ── EDIT PRODUCT ──────────────────────────────────────────────────────
    if step == "ap_edit_id":
        p = S.get_product(text, admin=True)
        if not p:
            _finish(chat_id, uid, f"⛔ {st('Product not found.')}")
            return True
        data["pid"] = p["product_id"]
        state["step"] = "ap_edit_field"
        send(chat_id,
             f"✏️ <b>{esc(p['name'])}</b>\n{sep('─')}\n"
             f"{st('Which field?')} <code>name</code>, <code>price</code>, <code>stock</code>, "
             f"<code>category</code>, <code>description</code>, <code>delivery</code>")
        return True

    if step == "ap_edit_field":
        field = text.lower()
        allowed = {"name": "name", "price": "price", "stock": "stock",
                   "category": "category", "description": "description",
                   "delivery": "delivery_type"}
        if field not in allowed:
            send(chat_id, f"⛔ {st('Unknown field.')}")
            return True
        data["field"] = allowed[field]
        state["step"] = "ap_edit_value"
        send(chat_id, f"📝 {st('Send the new value.')}")
        return True

    if step == "ap_edit_value":
        field, pid = data["field"], data["pid"]
        value = text
        if field == "price":
            value = _num(text)
            if value is None or value <= 0:
                send(chat_id, f"⛔ {st('Send a valid price.')}")
                return True
        elif field == "stock":
            value = _num(text, True)
            if value is None or value < 0:
                send(chat_id, f"⛔ {st('Send a valid stock number.')}")
                return True
        elif field == "delivery_type":
            value = text.upper()
            if value not in ("TEXT", "FILE", "AUTO"):
                send(chat_id, f"⛔ {st('Send TEXT, FILE or AUTO.')}")
                return True
        with conn() as c:
            c.execute(
                f"UPDATE shop_products SET {field}=?, updated_at=strftime('%s','now') "
                "WHERE product_id=?", (value, pid))
            if field == "stock":
                c.execute(
                    "INSERT INTO shop_stock_logs (product_id, admin_id, change, reason) VALUES (?,?,?,?)",
                    (pid, uid, 0, f"stock set to {value}"))
        S.log_activity(uid, "EDIT_PRODUCT", pid, f"{field}={value}")
        _finish(chat_id, uid, f"✅ {st('Updated')} <code>{esc(pid)}</code> — {field} → <b>{esc(value)}</b>",
                "admin_products")
        return True

    # ── DELETE / TOGGLES ──────────────────────────────────────────────────
    if step == "ap_del_id":
        p = S.get_product(text, admin=True)
        if not p:
            _finish(chat_id, uid, f"⛔ {st('Product not found.')}")
            return True
        with conn() as c:
            c.execute("UPDATE shop_products SET is_deleted=1, enabled=0 WHERE product_id=?",
                      (p["product_id"],))
            c.execute("DELETE FROM shop_cart_items WHERE product_id=?", (p["product_id"],))
        S.log_activity(uid, "DELETE_PRODUCT", p["product_id"], p["name"])
        _finish(chat_id, uid, f"🗑️ {st('Product deleted')}: <b>{esc(p['name'])}</b>", "admin_products")
        return True

    if step in ("ap_toggle_enabled", "ap_toggle_featured", "ap_toggle_popular"):
        column = {"ap_toggle_enabled": "enabled",
                  "ap_toggle_featured": "featured",
                  "ap_toggle_popular": "popular"}[step]
        p = S.get_product(text, admin=True)
        if not p:
            _finish(chat_id, uid, f"⛔ {st('Product not found.')}")
            return True
        new = 0 if int(p[column]) else 1
        with conn() as c:
            c.execute(f"UPDATE shop_products SET {column}=? WHERE product_id=?", (new, p["product_id"]))
        S.log_activity(uid, f"TOGGLE_{column.upper()}", p["product_id"], str(new))
        _finish(chat_id, uid,
                f"🔁 <b>{esc(p['name'])}</b> — {column} → <b>{'ON' if new else 'OFF'}</b>",
                "admin_products")
        return True

    if step == "ap_image_id":
        p = S.get_product(text, admin=True)
        if not p:
            _finish(chat_id, uid, f"⛔ {st('Product not found.')}")
            return True
        data["pid"] = p["product_id"]
        state["step"] = "ap_image_wait"
        send(chat_id, f"🖼️ {st('Now send the product photo.')}")
        return True

    if step == "ap_image_wait":
        send(chat_id, f"🖼️ {st('Please send a photo, not text.')}")
        return True

    # ── STOCK ─────────────────────────────────────────────────────────────
    if step in ("as_add_id", "as_set_id"):
        p = S.get_product(text, admin=True)
        if not p:
            _finish(chat_id, uid, f"⛔ {st('Product not found.')}")
            return True
        data["pid"] = p["product_id"]
        state["step"] = "as_add_qty" if step == "as_add_id" else "as_set_qty"
        send(chat_id, f"🔢 {st('Current stock')}: <b>{p['stock']}</b>\n{st('Send the quantity.')}")
        return True

    if step in ("as_add_qty", "as_set_qty"):
        qty = _num(text, True)
        if qty is None:
            send(chat_id, f"⛔ {st('Send a valid number.')}")
            return True
        pid = data["pid"]
        with conn() as c:
            if step == "as_add_qty":
                c.execute("UPDATE shop_products SET stock=MAX(0, stock+?) WHERE product_id=?", (qty, pid))
                change, reason = qty, "manual add"
            else:
                if qty < 0:
                    send(chat_id, f"⛔ {st('Stock cannot be negative.')}")
                    return True
                c.execute("UPDATE shop_products SET stock=? WHERE product_id=?", (qty, pid))
                change, reason = qty, "manual set"
            c.execute(
                "INSERT INTO shop_stock_logs (product_id, admin_id, change, reason) VALUES (?,?,?,?)",
                (pid, uid, change, reason))
            row = c.execute("SELECT stock FROM shop_products WHERE product_id=?", (pid,)).fetchone()
        S.log_activity(uid, "STOCK", pid, reason)
        _finish(chat_id, uid, f"📦 {st('Stock for')} <code>{esc(pid)}</code> → <b>{row['stock']}</b>",
                "admin_stock")
        return True

    if step == "as_auto_id":
        p = S.get_product(text, admin=True)
        if not p:
            _finish(chat_id, uid, f"⛔ {st('Product not found.')}")
            return True
        data["pid"] = p["product_id"]
        state["step"] = "as_auto_items"
        with conn() as c:
            n = c.execute("SELECT COUNT(*) n FROM shop_auto_items WHERE product_id=? AND used=0",
                          (p["product_id"],)).fetchone()["n"]
        send(chat_id,
             f"🤖 {st('Unused auto items')}: <b>{n}</b>\n"
             f"{st('Send items — one per line. Each line is delivered to one buyer.')}")
        return True

    if step == "as_auto_items":
        items = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not items:
            send(chat_id, f"⛔ {st('Send at least one line.')}")
            return True
        pid = data["pid"]
        with conn() as c:
            for it in items:
                c.execute("INSERT INTO shop_auto_items (product_id, content) VALUES (?,?)", (pid, it))
            c.execute("UPDATE shop_products SET stock=stock+? WHERE product_id=?", (len(items), pid))
            c.execute("INSERT INTO shop_stock_logs (product_id, admin_id, change, reason) VALUES (?,?,?,?)",
                      (pid, uid, len(items), "auto items import"))
        S.log_activity(uid, "AUTO_IMPORT", pid, str(len(items)))
        _finish(chat_id, uid, f"🤖 {st('Imported')} <b>{len(items)}</b> {st('auto items and stock updated.')}",
                "admin_stock")
        return True

    # ── ORDERS ────────────────────────────────────────────────────────────
    if step == "ao_lookup":
        o = S.get_order(text)
        S.shop_states.pop(S._skey(chat_id, uid), None)
        if not o:
            send(chat_id, f"⛔ {st('Order not found.')}")
            return True
        kb = S.order_admin_keyboard(o["order_id"], o["order_status"])
        send(chat_id, S.order_text(o, admin=True), reply_markup=kb)
        return True

    # ── DELIVERY ──────────────────────────────────────────────────────────
    if step in ("ad_deliver_id", "ad_resend_id"):
        o = S.get_order(text)
        if not o:
            _finish(chat_id, uid, f"⛔ {st('Order not found.')}")
            return True
        if step == "ad_deliver_id" and o["order_status"] in ("CANCELLED", "DELIVERED"):
            _finish(chat_id, uid, f"⛔ {st('This order cannot be delivered.')}")
            return True
        data["order_id"] = o["order_id"]
        data["resend"] = step == "ad_resend_id"
        state["step"] = "ad_content"
        send(chat_id, f"📤 {st('Send the delivery text, or send a file/photo now.')}")
        return True

    if step == "ad_content":
        if len(text) < 1:
            send(chat_id, f"⛔ {st('Send the delivery content.')}")
            return True
        ok, msg = S.deliver_text(data["order_id"], uid, text, is_resend=bool(data.get("resend")))
        _finish(chat_id, uid, (f"✅ {st(msg)}" if ok else f"⛔ {st(msg)}"), "admin_delivery")
        return True

    if step == "ad_auto_id":
        o = S.get_order(text)
        if not o:
            _finish(chat_id, uid, f"⛔ {st('Order not found.')}")
            return True
        ok, msg = S.try_auto_deliver(o["order_id"], uid)
        _finish(chat_id, uid, (f"🤖 {st(msg)}" if ok else f"⛔ {st(msg)}"), "admin_delivery")
        return True

    # ── COUPONS ───────────────────────────────────────────────────────────
    if step == "ac_code":
        code = text.upper().replace(" ", "")
        if len(code) < 3:
            send(chat_id, f"⛔ {st('Code is too short.')}")
            return True
        with conn() as c:
            exists = c.execute("SELECT id FROM shop_coupons WHERE code=?", (code,)).fetchone()
        if exists:
            send(chat_id, f"⛔ {st('This code already exists.')}")
            return True
        data["code"] = code
        state["step"] = "ac_type"
        send(chat_id, f"🎟️ {st('Send discount type')}: <code>PERCENT</code> / <code>FLAT</code>")
        return True

    if step == "ac_type":
        kind = text.upper()
        if kind not in ("PERCENT", "FLAT"):
            send(chat_id, f"⛔ {st('Send PERCENT or FLAT.')}")
            return True
        data["kind"] = kind
        state["step"] = "ac_value"
        send(chat_id, f"💯 {st('Send the discount value.')}")
        return True

    if step == "ac_value":
        val = _num(text)
        if val is None or val <= 0:
            send(chat_id, f"⛔ {st('Send a valid value.')}")
            return True
        if data["kind"] == "PERCENT" and val > 100:
            send(chat_id, f"⛔ {st('Percentage cannot exceed 100.')}")
            return True
        data["value"] = val
        state["step"] = "ac_min"
        send(chat_id, f"🧮 {st('Send the minimum order amount (0 for none).')}")
        return True

    if step == "ac_min":
        val = _num(text)
        if val is None or val < 0:
            send(chat_id, f"⛔ {st('Send a valid amount.')}")
            return True
        data["min"] = val
        state["step"] = "ac_usage"
        send(chat_id, f"🔢 {st('Send total usage limit (0 = unlimited).')}")
        return True

    if step == "ac_usage":
        val = _num(text, True)
        if val is None or val < 0:
            send(chat_id, f"⛔ {st('Send a valid number.')}")
            return True
        data["usage"] = val
        state["step"] = "ac_per_user"
        send(chat_id, f"👤 {st('Send per-user limit (0 = unlimited).')}")
        return True

    if step == "ac_per_user":
        val = _num(text, True)
        if val is None or val < 0:
            send(chat_id, f"⛔ {st('Send a valid number.')}")
            return True
        data["per_user"] = val
        state["step"] = "ac_days"
        send(chat_id, f"📅 {st('Valid for how many days? (0 = never expires)')}")
        return True

    if step == "ac_days":
        days = _num(text, True)
        if days is None or days < 0:
            send(chat_id, f"⛔ {st('Send a valid number of days.')}")
            return True
        expiry = S.now_ts() + days * 86400 if days else 0
        with conn() as c:
            c.execute(
                "INSERT INTO shop_coupons (code, discount_type, discount_value, min_order, "
                "max_usage, per_user_limit, start_at, expiry_at) VALUES (?,?,?,?,?,?,?,?)",
                (data["code"], data["kind"], data["value"], data["min"], data["usage"],
                 data["per_user"], S.now_ts(), expiry),
            )
        S.log_activity(uid, "ADD_COUPON", data["code"], data["kind"])
        _finish(chat_id, uid,
                f"✅ <b>{st('COUPON CREATED')}</b>\n{sep()}\n"
                f"🎟️ <code>{esc(data['code'])}</code>\n"
                f"💯 {data['value']:g} {data['kind']}\n"
                f"🧮 {st('Min')}: {money(data['min'])}\n"
                f"📅 {st('Expires')}: {S.fmt_ts(expiry) if expiry else '∞'}",
                "admin_promos")
        return True

    if step == "ac_delete":
        code = text.upper().replace(" ", "")
        with conn() as c:
            cur = c.execute("DELETE FROM shop_coupons WHERE code=?", (code,))
            gone = cur.rowcount
            c.execute("DELETE FROM shop_cart_coupon WHERE UPPER(code)=?", (code,))
        S.log_activity(uid, "DELETE_COUPON", code)
        _finish(chat_id, uid,
                (f"🗑️ {st('Coupon deleted')}: <code>{esc(code)}</code>" if gone
                 else f"⛔ {st('Coupon not found.')}"), "admin_promos")
        return True

    # ── OFFERS ────────────────────────────────────────────────────────────
    if step == "ao_offer_title":
        if len(text) < 2:
            send(chat_id, f"⛔ {st('Title is too short.')}")
            return True
        data["title"] = text[:120]
        state["step"] = "ao_offer_kind"
        send(chat_id, f"🏷️ {st('Send type')}: <code>OFFER</code> / <code>DISCOUNT</code> / <code>BONUS</code>")
        return True

    if step == "ao_offer_kind":
        kind = text.upper()
        if kind not in ("OFFER", "DISCOUNT", "BONUS"):
            send(chat_id, f"⛔ {st('Send OFFER, DISCOUNT or BONUS.')}")
            return True
        data["kind"] = kind
        state["step"] = "ao_offer_desc"
        send(chat_id, f"📝 {st('Send the offer description.')}")
        return True

    if step == "ao_offer_desc":
        data["desc"] = text[:900]
        state["step"] = "ao_offer_days"
        send(chat_id, f"📅 {st('Valid for how many days? (0 = no expiry)')}")
        return True

    if step == "ao_offer_days":
        days = _num(text, True)
        if days is None or days < 0:
            send(chat_id, f"⛔ {st('Send a valid number of days.')}")
            return True
        expiry = S.now_ts() + days * 86400 if days else 0
        with conn() as c:
            c.execute(
                "INSERT INTO shop_offers (title, description, kind, start_at, expiry_at) "
                "VALUES (?,?,?,?,?)",
                (data["title"], data["desc"], data["kind"], S.now_ts(), expiry))
        S.log_activity(uid, "ADD_OFFER", data["title"], data["kind"])
        _finish(chat_id, uid, f"🎁 {st('Offer created')}: <b>{esc(data['title'])}</b>", "admin_promos")
        return True

    if step == "ao_offer_delete":
        oid = _num(text, True)
        if oid is None:
            send(chat_id, f"⛔ {st('Send a valid Offer ID.')}")
            return True
        with conn() as c:
            gone = c.execute("DELETE FROM shop_offers WHERE id=?", (oid,)).rowcount
        S.log_activity(uid, "DELETE_OFFER", str(oid))
        _finish(chat_id, uid,
                (f"🗑️ {st('Offer deleted.')}" if gone else f"⛔ {st('Offer not found.')}"),
                "admin_promos")
        return True

    # ── USERS & BALANCE ───────────────────────────────────────────────────
    if step in ("au_add_id", "au_rem_id"):
        target = _num(text, True)
        if target is None:
            send(chat_id, f"⛔ {st('Send a numeric User ID.')}")
            return True
        data["target"] = target
        data["mode"] = "add" if step == "au_add_id" else "remove"
        state["step"] = "au_amount"
        send(chat_id, f"💰 {st('Balance')}: <b>{money(S.user_balance(target))}</b>\n"
                      f"{st('Send the amount.')}")
        return True

    if step == "au_amount":
        amount = _num(text)
        if amount is None or amount <= 0:
            send(chat_id, f"⛔ {st('Send a valid amount.')}")
            return True
        target, mode = data["target"], data["mode"]
        ok, msg = adjust_balance(target, amount if mode == "add" else -amount, uid,
                                "admin add" if mode == "add" else "admin remove")
        _finish(chat_id, uid, msg if not ok else
                f"✅ {st('Balance updated')}\n🆔 <code>{target}</code>\n"
                f"{'➕' if mode == 'add' else '➖'} {money(amount)}\n"
                f"🏦 {st('New balance')}: <b>{money(S.user_balance(target))}</b>",
                "admin_users")
        return True

    if step == "au_lookup":
        target = _num(text, True)
        if target is None:
            send(chat_id, f"⛔ {st('Send a numeric User ID.')}")
            return True
        with conn() as c:
            orders = c.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(total),0) t FROM shop_orders WHERE user_id=? "
                "AND order_status IN ('CONFIRMED','PROCESSING','DELIVERED')", (target,)).fetchone()
            pending = c.execute(
                "SELECT COUNT(*) n FROM shop_orders WHERE user_id=? AND order_status='PENDING'",
                (target,)).fetchone()["n"]
        _finish(chat_id, uid,
                f"👤 <b>{st('SHOP USER')}</b> <code>{target}</code>\n{sep()}\n<blockquote>"
                f"🏦 {st('Balance')}: <b>{money(S.user_balance(target))}</b>\n"
                f"🧾 {st('Paid Orders')}: <b>{orders['n']}</b>\n"
                f"💰 {st('Total Spent')}: <b>{money(orders['t'])}</b>\n"
                f"⏳ {st('Pending')}: <b>{pending}</b>"
                f"</blockquote>", "admin_users")
        return True

    if step == "au_notice":
        if len(text) < 2:
            send(chat_id, f"⛔ {st('Message is too short.')}")
            return True
        with conn() as c:
            rows = [r["user_id"] for r in c.execute(
                "SELECT DISTINCT user_id FROM shop_orders").fetchall()]
        sent = 0
        body = f"📢 <b>{st('SHOP NOTICE')}</b>\n{sep()}\n{esc(text)}"
        for target in rows:
            try:
                S.notify(target, "OFFER", body)
                sent += 1
            except Exception:
                pass
        S.log_activity(uid, "NOTICE", "", str(sent))
        _finish(chat_id, uid, f"📢 {st('Notice sent to')} <b>{sent}</b> {st('users.')}", "admin_users")
        return True

    # ── SETTINGS ──────────────────────────────────────────────────────────
    if step == "aset_value":
        key = data.get("key")
        if key in ("min_order", "low_stock"):
            val = _num(text, key == "low_stock")
            if val is None or val < 0:
                send(chat_id, f"⛔ {st('Send a valid number.')}")
                return True
            S.sset(key, str(val))
        else:
            S.sset(key, text[:2000])
        S.log_activity(uid, "SETTING", key or "", text[:60])
        _finish(chat_id, uid, f"✅ {st('Saved')}: <code>{esc(key)}</code>", "admin_settings")
        return True

    return False


def _new_product_id() -> str:
    with conn() as c:
        row = c.execute("SELECT COUNT(*) n FROM shop_products").fetchone()
    return f"P{1000 + int(row['n']) + 1}"


def adjust_balance(user_id, delta, admin_id, reason=""):
    """Atomic balance change on the EXISTING wallet table."""
    c = raw_conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT OR IGNORE INTO wallet (user_id) VALUES (?)", (user_id,))
        row = c.execute("SELECT balance FROM wallet WHERE user_id=?", (user_id,)).fetchone()
        balance = float(row["balance"] or 0)
        if delta < 0 and balance + delta < 0:
            c.execute("ROLLBACK")
            return False, f"⛔ {st('Insufficient user balance.')}"
        c.execute("UPDATE wallet SET balance=balance+? WHERE user_id=?", (delta, user_id))
        c.execute(
            "INSERT INTO shop_transactions (user_id, kind, amount, note) VALUES (?,?,?,?)",
            (user_id, "CREDIT" if delta > 0 else "DEBIT", abs(delta), reason))
        c.execute("COMMIT")
    except Exception as exc:
        logger.error("adjust_balance failed: %s", exc)
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        return False, f"⛔ {st('Balance update failed.')}"
    finally:
        try:
            c.close()
        except Exception:
            pass
    S.log_activity(admin_id, "BALANCE", str(user_id), f"{delta:+.2f} {reason}")
    try:
        S.notify(user_id, "ORDER",
                 f"{'➕' if delta > 0 else '➖'} <b>{st('BALANCE UPDATED')}</b>\n{sep()}\n"
                 f"{money(abs(delta))}\n🏦 {st('New balance')}: <b>{money(S.user_balance(user_id))}</b>")
    except Exception:
        pass
    return True, "ok"


# ═══════════════════════════════════════════════════════════════════════════
# MEDIA STATES (product image, file delivery)
# ═══════════════════════════════════════════════════════════════════════════
def handle_media_state(message, state) -> bool:
    step = str(state.get("step") or "")
    uid, chat_id = message.from_user.id, message.chat.id
    if step not in ("ap_image_wait", "ad_content"):
        return False
    if not guard(uid, chat_id):
        S.shop_states.pop(S._skey(chat_id, uid), None)
        return True
    data = state.setdefault("data", {})

    file_id, kind = None, "document"
    if getattr(message, "photo", None):
        file_id, kind = message.photo[-1].file_id, "photo"
    elif getattr(message, "document", None):
        file_id, kind = message.document.file_id, "document"
    if not file_id:
        send(chat_id, f"⛔ {st('Send a photo or a document.')}")
        return True

    if step == "ap_image_wait":
        if kind != "photo":
            send(chat_id, f"⛔ {st('Send a photo.')}")
            return True
        with conn() as c:
            c.execute("UPDATE shop_products SET image_file_id=?, updated_at=strftime('%s','now') "
                      "WHERE product_id=?", (file_id, data.get("pid")))
        S.log_activity(uid, "PRODUCT_IMAGE", str(data.get("pid")))
        _finish(chat_id, uid, f"🖼️ {st('Product image saved.')}", "admin_products")
        return True

    ok, msg = S.deliver_file(data["order_id"], uid, file_id, kind,
                             caption=(message.caption or ""),
                             is_resend=bool(data.get("resend")))
    _finish(chat_id, uid, (f"✅ {st(msg)}" if ok else f"⛔ {st(msg)}"), "admin_delivery")
    return True


# ═══════════════════════════════════════════════════════════════════════════
# CALLBACKS
# ═══════════════════════════════════════════════════════════════════════════
def handle_callback(call):
    data = call.data or ""
    uid = call.from_user.id
    chat_id = call.message.chat.id if call.message else uid

    if not S.is_shop_admin(uid):
        S._ack(call, st("Access Denied"), True)
        return

    if data.startswith("shop_order:"):
        _, action, order_id = data.split(":", 2)
        _order_action(call, action, order_id, uid, chat_id)
        return

    if data.startswith("shop_topup:"):
        _, action, tid = data.split(":", 2)
        _topup_action(call, action, tid, uid, chat_id)
        return

    if data.startswith("shop_dlv:"):
        _, mode, order_id = data.split(":", 2)
        S._ack(call)
        S.shop_states[S._skey(chat_id, uid)] = {
            "step": "ad_content",
            "data": {"order_id": order_id, "resend": mode == "resend"},
        }
        send(chat_id, f"📤 {st('Send the delivery text, file or photo for order')} "
                      f"<code>{esc(order_id)}</code>.")
        return

    S._ack(call)


def _order_action(call, action, order_id, uid, chat_id):
    o = S.get_order(order_id)
    if not o:
        S._ack(call, st("Order not found."), True)
        return

    if action == "view":
        S._ack(call)
        send(chat_id, S.order_text(o, admin=True),
             reply_markup=S.order_admin_keyboard(order_id, o["order_status"]))
        return

    if action == "confirm":
        target = "CONFIRMED"
    elif action == "proc":
        target = "PROCESSING"
    elif action == "cancel":
        target = "CANCELLED"
    elif action == "deliver":
        if o["order_status"] in ("CANCELLED", "DELIVERED"):
            S._ack(call, st("This order cannot be delivered."), True)
            return
        S._ack(call)
        S.shop_states[S._skey(chat_id, uid)] = {
            "step": "ad_content", "data": {"order_id": order_id, "resend": False}}
        send(chat_id, f"📤 {st('Send the delivery text, file or photo for order')} "
                      f"<code>{esc(order_id)}</code>.")
        return
    else:
        S._ack(call)
        return

    if not S.can_transition(o["order_status"], target):
        S._ack(call, st("This status change is not allowed."), True)
        return

    if target == "CANCELLED":
        ok, res = S.refund_order(order_id, "Cancelled by admin")
        if not ok:
            S._ack(call, st(str(res)), True)
            return
        S.log_activity(uid, "CANCEL_ORDER", order_id)
        S.notify(o["user_id"], "ORDER",
                 f"❌ <b>{st('ORDER CANCELLED')}</b>\n{sep()}\n"
                 f"🧾 <code>{esc(order_id)}</code>\n"
                 f"↩️ {st('Refunded')}: <b>{money(o['total'])}</b>\n"
                 f"🏦 {st('Balance')}: <b>{money(S.user_balance(o['user_id']))}</b>")
        S._ack(call, st("Cancelled and refunded."))
    else:
        S.set_order_status(order_id, target)
        S.log_activity(uid, f"ORDER_{target}", order_id)
        S.notify(o["user_id"], "ORDER",
                 f"📌 <b>{st('ORDER UPDATE')}</b>\n{sep()}\n"
                 f"🧾 <code>{esc(order_id)}</code>\n{st('Status')}: <b>{target}</b>")
        S._ack(call, f"{target}")

    S.refresh_group_card(order_id)
    try:
        fresh = S.get_order(order_id)
        S.bot().edit_message_text(
            S.order_text(fresh, admin=True), chat_id, call.message.message_id,
            reply_markup=S.order_admin_keyboard(order_id, fresh["order_status"]))
    except Exception:
        pass


def _topup_action(call, action, tid, uid, chat_id):
    with conn() as c:
        row = c.execute("SELECT * FROM shop_topups WHERE id=?", (tid,)).fetchone()
        r = dict(row) if row else None
    if not r:
        S._ack(call, st("Request not found."), True)
        return
    if r["status"] != "PENDING":
        S._ack(call, st("Already processed."), True)
        return

    if action == "approve":
        ok, msg = adjust_balance(r["user_id"], float(r["amount"]), uid, f"top-up #{tid}")
        if not ok:
            S._ack(call, st("Approve failed."), True)
            return
        with conn() as c:
            c.execute("UPDATE shop_topups SET status='APPROVED' WHERE id=?", (tid,))
        S.log_activity(uid, "TOPUP_APPROVE", str(tid), str(r["amount"]))
        S.notify(r["user_id"], "ORDER",
                 f"✅ <b>{st('TOP-UP APPROVED')}</b>\n{sep()}\n"
                 f"💰 {money(r['amount'])}\n"
                 f"🏦 {st('Balance')}: <b>{money(S.user_balance(r['user_id']))}</b>")
        S._ack(call, st("Approved."))
        final = "✅ APPROVED"
    else:
        with conn() as c:
            c.execute("UPDATE shop_topups SET status='REJECTED' WHERE id=?", (tid,))
        S.log_activity(uid, "TOPUP_REJECT", str(tid))
        S.notify(r["user_id"], "ORDER",
                 f"❌ <b>{st('TOP-UP REJECTED')}</b>\n{sep()}\n💰 {money(r['amount'])}")
        S._ack(call, st("Rejected."))
        final = "❌ REJECTED"

    try:
        S.bot().edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        S.bot().send_message(chat_id, f"💳 Top-up #{tid} → <b>{final}</b>")
    except Exception:
        pass
