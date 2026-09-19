"""
SUPER FAST OTP SHOP — single-file deployment bundle (Render ready).

Everything (bot core + shop + shop admin + temp-mail engine) lives in this one
file. The three former modules are registered as real Python modules before the
bot code runs, so every existing import, callback and table keeps working
exactly as before.

Run:  python main.py      Env: BOT_TOKEN, ADMIN_ID, DATA_DIR (optional)
"""

import sys as _sys
import types as _types


def _install_module(_name, _source, _extra=None):
    """Register an embedded module so `import <name>` keeps working."""
    _mod = _types.ModuleType(_name)
    _mod.__file__ = __file__
    _mod.__dict__.update(_extra or {})
    _sys.modules[_name] = _mod
    exec(compile(_source, "<%s>" % _name, "exec"), _mod.__dict__)
    return _mod


_SRC_TEMP_MAIL_ENGINE = r'''"""Temp Mail engine with multiple free providers + admin custom domains.

Providers supported
-------------------
* ``mailtm``     -> https://api.mail.tm      (free, stable)
* ``mailgw``     -> https://api.mail.gw      (free, sometimes down -> fallback)
* ``tempmailio`` -> https://api.internal.temp-mail.io/api/v3
* ``guerrilla``  -> https://api.guerrillamail.com
* ``imap``       -> ANY domain the admin owns (catch-all mailbox over IMAP)

Every provider exposes the same two operations:

    state = await create_account(provider, domain=None, imap_config=None)
    messages = await fetch_messages(state)

``state`` is a plain JSON-serialisable dict, so it can be stored in the
existing ``temp_mail_users.json`` file without any extra work.

``messages`` is a list of dicts: ``{"id", "from", "subject", "text"}``.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import re
import secrets
import ssl
import time
from email.header import decode_header, make_header

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

MAILTM_BASE = "https://api.mail.tm"
MAILGW_BASE = "https://api.mail.gw"
TEMPMAILIO_BASE = "https://api.internal.temp-mail.io/api/v3"
GUERRILLA_BASE = "https://api.guerrillamail.com/ajax.php"
TEMPMAILPLUS_BASE = "https://tempmail.plus/api"
INBOXES_BASE = "https://inboxes.com/api/v2"
TEMPMAILLOL_BASE = "https://api.tempmail.lol"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

TEMPMAILPLUS_DOMAINS = (
    "mailto.plus", "fexpost.com", "fexbox.org", "rover.info",
    "chitthi.in", "fextemp.com", "any.pink", "merepost.com",
)

INBOXES_FALLBACK_DOMAINS = (
    "blondmail.com", "chapsmail.com", "clowmail.com", "dropjar.com",
    "fivermail.com", "getairmail.com", "givmail.com", "inboxbear.com",
    "vomoto.com", "zlorkun.com",
)

# Order matters: unlimited (no-signup) providers first, dead ones last.
PUBLIC_PROVIDERS = (
    "tempmailplus", "inboxes", "tempmaillol",
    "mailtm", "tempmailio", "guerrilla", "mailgw",
)

# These never run out: any local part on their domains works instantly.
UNLIMITED_PROVIDERS = ("tempmailplus", "inboxes")

PROVIDER_LABELS = {
    "tempmailplus": "TempMail.Plus",
    "inboxes": "Inboxes.com",
    "tempmaillol": "TempMail.LOL",
    "mailtm": "Mail.tm",
    "mailgw": "Mail.gw",
    "tempmailio": "Temp-Mail.io",
    "guerrilla": "GuerrillaMail",
    "imap": "Own Domain (IMAP)",
}


def _random_local() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(12))


def _members(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("hydra:member", "messages", "data", "domains", "list"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _address(value):
    if isinstance(value, dict):
        return str(value.get("address") or value.get("email") or "").strip()
    if isinstance(value, list) and value:
        return _address(value[0])
    return str(value or "").strip()


_TAG_RE = re.compile(r"<[^>]+>")
_STYLE_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")


def html_to_text(value) -> str:
    """Turn an HTML (or plain) mail body into readable text so OTP codes are visible."""
    if isinstance(value, (list, tuple)):
        value = "\n".join(str(item) for item in value)
    text = str(value or "")
    if "<" in text and ">" in text:
        text = _STYLE_RE.sub(" ", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
        text = _TAG_RE.sub(" ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    text = re.sub(r"[ \t\x00-\x08\x0b\x0c]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ─── MAIL.TM / MAIL.GW (identical API) ────────────────────────────────────────
async def _mailbase_domains(client: httpx.AsyncClient, base: str):
    response = await client.get(f"{base}/domains")
    response.raise_for_status()
    domains = []
    for item in _members(response.json()):
        domain = item.get("domain") or item.get("name") if isinstance(item, dict) else item
        if domain:
            domains.append(str(domain).strip().lstrip("@"))
    return domains


async def _mailbase_create(provider: str, base: str, domain: str | None):
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        domains = await _mailbase_domains(client, base)
        if domain and domain.lower() in {d.lower() for d in domains}:
            domains = [domain]
        if not domains:
            raise RuntimeError(f"{provider}: no domains available")

        last_error = None
        for _ in range(3):
            chosen = secrets.choice(domains)
            address = f"{_random_local()}@{chosen}"
            password = secrets.token_urlsafe(18)
            try:
                created = await client.post(
                    f"{base}/accounts", json={"address": address, "password": password}
                )
                created.raise_for_status()
                token_response = await client.post(
                    f"{base}/token", json={"address": address, "password": password}
                )
                token_response.raise_for_status()
                token = str(token_response.json().get("token") or "").strip()
                if not token:
                    raise RuntimeError(f"{provider}: no token returned")
                return {
                    "provider": provider,
                    "email": address,
                    "password": password,
                    "token": token,
                }
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exc.response.status_code != 422:
                    raise
        raise last_error or RuntimeError(f"{provider}: account creation failed")


async def _mailbase_fetch(state: dict, base: str):
    headers = {"Authorization": f"Bearer {state.get('token', '')}"}
    out = []
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        listing = await client.get(f"{base}/messages", headers=headers)
        listing.raise_for_status()
        for message in _members(listing.json()):
            if not isinstance(message, dict):
                continue
            message_id = message.get("id")
            if message_id is None:
                continue
            details = message
            try:
                detail_response = await client.get(
                    f"{base}/messages/{message_id}", headers=headers
                )
                detail_response.raise_for_status()
                payload = detail_response.json()
                if isinstance(payload, dict):
                    details = payload
            except Exception as exc:  # detail is optional
                logger.debug("mail detail fetch failed: %s", exc)
            out.append(
                {
                    "id": str(message_id),
                    "from": _address(details.get("from") or message.get("from")),
                    "subject": str(details.get("subject") or "No subject"),
                    "text": html_to_text(
                        details.get("text")
                        or details.get("html")
                        or details.get("intro")
                        or message.get("intro")
                        or ""
                    ),
                }
            )
    return out


# ─── TEMP-MAIL.IO ─────────────────────────────────────────────────────────────
async def _tempmailio_create(domain: str | None):
    body: dict = {}
    if domain:
        body = {"name": _random_local(), "domain": domain}
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        response = await client.post(f"{TEMPMAILIO_BASE}/email/new", json=body)
        if response.status_code >= 400 and body:
            response = await client.post(f"{TEMPMAILIO_BASE}/email/new", json={})
        response.raise_for_status()
        payload = response.json()
        address = str(payload.get("email") or "").strip()
        if not address:
            raise RuntimeError("tempmailio: no address returned")
        return {
            "provider": "tempmailio",
            "email": address,
            "password": "",
            "token": str(payload.get("token") or ""),
        }


async def _tempmailio_fetch(state: dict):
    address = state.get("email", "")
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        response = await client.get(f"{TEMPMAILIO_BASE}/email/{address}/messages")
        response.raise_for_status()
        payload = response.json()
    out = []
    for message in payload if isinstance(payload, list) else _members(payload):
        if not isinstance(message, dict):
            continue
        out.append(
            {
                "id": str(message.get("id") or message.get("_id") or ""),
                "from": _address(message.get("from")),
                "subject": str(message.get("subject") or "No subject"),
                "text": html_to_text(message.get("body_text") or message.get("body_html") or ""),
            }
        )
    return [m for m in out if m["id"]]


# ─── GUERRILLAMAIL ────────────────────────────────────────────────────────────
async def _guerrilla_create(domain: str | None):
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        response = await client.get(GUERRILLA_BASE, params={"f": "get_email_address"})
        response.raise_for_status()
        payload = response.json()
        sid = str(payload.get("sid_token") or "")
        address = str(payload.get("email_addr") or "")
        if domain:
            try:
                renamed = await client.get(
                    GUERRILLA_BASE,
                    params={
                        "f": "set_email_user",
                        "email_user": _random_local(),
                        "domain": domain,
                        "sid_token": sid,
                    },
                )
                renamed.raise_for_status()
                address = str(renamed.json().get("email_addr") or address)
            except Exception as exc:
                logger.debug("guerrilla domain switch failed: %s", exc)
        if not address or not sid:
            raise RuntimeError("guerrilla: no address returned")
        return {"provider": "guerrilla", "email": address, "password": "", "token": sid}


async def _guerrilla_fetch(state: dict):
    sid = state.get("token", "")
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        listing = await client.get(
            GUERRILLA_BASE, params={"f": "check_email", "seq": 0, "sid_token": sid}
        )
        listing.raise_for_status()
        payload = listing.json()
        out = []
        for message in payload.get("list", []) if isinstance(payload, dict) else []:
            if not isinstance(message, dict):
                continue
            message_id = str(message.get("mail_id") or "")
            if not message_id:
                continue
            text = str(message.get("mail_excerpt") or "")
            try:
                detail = await client.get(
                    GUERRILLA_BASE,
                    params={"f": "fetch_email", "email_id": message_id, "sid_token": sid},
                )
                detail.raise_for_status()
                detail_payload = detail.json()
                if isinstance(detail_payload, dict):
                    text = str(detail_payload.get("mail_body") or text)
            except Exception as exc:
                logger.debug("guerrilla detail fetch failed: %s", exc)
            out.append(
                {
                    "id": message_id,
                    "from": str(message.get("mail_from") or ""),
                    "subject": str(message.get("mail_subject") or "No subject"),
                    "text": html_to_text(text),
                }
            )
    return out


# ─── OWN DOMAIN OVER IMAP (catch-all) ─────────────────────────────────────────
def _decode(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value)


def _body_text(message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "ignore"
                    )
                except Exception:
                    continue
        for part in message.walk():
            if part.get_content_type() == "text/html":
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "ignore"
                    )
                except Exception:
                    continue
        return ""
    try:
        return message.get_payload(decode=True).decode(
            message.get_content_charset() or "utf-8", "ignore"
        )
    except Exception:
        return str(message.get_payload() or "")


def _imap_connect(config: dict):
    host = str(config.get("imap_host") or "").strip()
    port = int(config.get("imap_port") or 993)
    user = str(config.get("imap_user") or "").strip()
    password = str(config.get("imap_pass") or "")
    if not host or not user:
        raise RuntimeError("IMAP host/user missing")
    if port == 143:
        connection = imaplib.IMAP4(host, port)
        try:
            connection.starttls(ssl.create_default_context())
        except Exception:
            pass
    else:
        connection = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context())
    connection.login(user, password)
    return connection


def _imap_fetch_sync(config: dict, address: str):
    connection = _imap_connect(config)
    out = []
    try:
        connection.select(str(config.get("imap_folder") or "INBOX"))
        status, data = connection.search(None, "TO", f'"{address}"')
        if status != "OK":
            return out
        ids = (data[0] or b"").split()[-30:]
        for raw_id in ids:
            status, payload = connection.fetch(raw_id, "(RFC822)")
            if status != "OK" or not payload or not payload[0]:
                continue
            message = email.message_from_bytes(payload[0][1])
            message_id = _decode(message.get("Message-ID")) or raw_id.decode()
            out.append(
                {
                    "id": message_id,
                    "from": _decode(message.get("From")),
                    "subject": _decode(message.get("Subject")) or "No subject",
                    "text": _body_text(message),
                }
            )
    finally:
        try:
            connection.logout()
        except Exception:
            pass
    return out


def imap_test_sync(config: dict):
    """Validate IMAP credentials; raises on failure."""
    connection = _imap_connect(config)
    try:
        connection.select(str(config.get("imap_folder") or "INBOX"))
    finally:
        try:
            connection.logout()
        except Exception:
            pass
    return True


async def _imap_create(domain: str, config: dict):
    await asyncio.to_thread(imap_test_sync, config)
    return {
        "provider": "imap",
        "email": f"{_random_local()}@{domain}",
        "password": "",
        "token": "",
        "imap": {
            "imap_host": config.get("imap_host"),
            "imap_port": config.get("imap_port") or 993,
            "imap_user": config.get("imap_user"),
            "imap_pass": config.get("imap_pass"),
            "imap_folder": config.get("imap_folder") or "INBOX",
        },
    }


# ─── TEMPMAIL.PLUS (unlimited, no signup) ─────────────────────────────────────
async def _tempmailplus_create(domain: str | None):
    chosen = domain if domain and domain in TEMPMAILPLUS_DOMAINS else secrets.choice(
        list(TEMPMAILPLUS_DOMAINS)
    )
    address = f"{_random_local()}@{chosen}"
    return {"provider": "tempmailplus", "email": address, "password": "", "token": ""}


async def _tempmailplus_fetch(state: dict):
    address = state.get("email", "")
    out = []
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True,
                                 headers=BROWSER_HEADERS) as client:
        listing = await client.get(
            f"{TEMPMAILPLUS_BASE}/mails",
            params={"email": address, "limit": 20, "epin": ""},
        )
        listing.raise_for_status()
        payload = listing.json()
        for message in (payload.get("mail_list") or []) if isinstance(payload, dict) else []:
            if not isinstance(message, dict):
                continue
            message_id = str(message.get("mail_id") or "")
            if not message_id:
                continue
            text = str(message.get("subject") or "")
            try:
                detail = await client.get(
                    f"{TEMPMAILPLUS_BASE}/mails/{message_id}",
                    params={"email": address, "epin": ""},
                )
                detail.raise_for_status()
                body = detail.json()
                if isinstance(body, dict):
                    text = body.get("text") or body.get("html") or text
            except Exception as exc:
                logger.debug("tempmailplus detail failed: %s", exc)
            out.append(
                {
                    "id": message_id,
                    "from": _address(message.get("from_mail") or message.get("from")),
                    "subject": str(message.get("subject") or "No subject"),
                    "text": html_to_text(text),
                }
            )
    return out


# ─── INBOXES.COM (unlimited, no signup) ───────────────────────────────────────
async def _inboxes_domains(client: httpx.AsyncClient):
    try:
        response = await client.get(f"{INBOXES_BASE}/domain")
        response.raise_for_status()
        domains = []
        for item in _members(response.json()) or response.json():
            if isinstance(item, dict):
                value = item.get("name") or item.get("domain")
            else:
                value = item
            if value:
                domains.append(str(value).strip().lstrip("@"))
        if domains:
            return domains
    except Exception as exc:
        logger.debug("inboxes domains failed: %s", exc)
    return list(INBOXES_FALLBACK_DOMAINS)


async def _inboxes_create(domain: str | None):
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True,
                                 headers=BROWSER_HEADERS) as client:
        domains = await _inboxes_domains(client)
        chosen = domain if domain and domain in domains else secrets.choice(domains)
        address = f"{_random_local()}@{chosen}"
        try:
            await client.get(f"{INBOXES_BASE}/inbox/{address}")
        except Exception as exc:
            logger.debug("inboxes warmup failed: %s", exc)
    return {"provider": "inboxes", "email": address, "password": "", "token": ""}


async def _inboxes_fetch(state: dict):
    address = state.get("email", "")
    out = []
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True,
                                 headers=BROWSER_HEADERS) as client:
        listing = await client.get(f"{INBOXES_BASE}/inbox/{address}")
        listing.raise_for_status()
        payload = listing.json()
        messages = payload.get("msgs") if isinstance(payload, dict) else payload
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            uid = str(message.get("uid") or message.get("id") or "")
            if not uid:
                continue
            text = str(message.get("snippet") or "")
            try:
                detail = await client.get(f"{INBOXES_BASE}/message/{uid}")
                detail.raise_for_status()
                body = detail.json()
                if isinstance(body, dict):
                    text = body.get("text") or body.get("html") or text
            except Exception as exc:
                logger.debug("inboxes detail failed: %s", exc)
            out.append(
                {
                    "id": uid,
                    "from": _address(message.get("f") or message.get("from")),
                    "subject": str(message.get("s") or message.get("subject") or "No subject"),
                    "text": html_to_text(text),
                }
            )
    return out


# ─── TEMPMAIL.LOL ─────────────────────────────────────────────────────────────
async def _tempmaillol_create(domain: str | None):
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True,
                                 headers=BROWSER_HEADERS) as client:
        response = await client.get(f"{TEMPMAILLOL_BASE}/generate")
        response.raise_for_status()
        payload = response.json()
    address = str(payload.get("address") or payload.get("email") or "").strip()
    token = str(payload.get("token") or "").strip()
    if not address or not token:
        raise RuntimeError("tempmaillol: no address returned")
    return {"provider": "tempmaillol", "email": address, "password": "", "token": token}


async def _tempmaillol_fetch(state: dict):
    token = state.get("token", "")
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True,
                                 headers=BROWSER_HEADERS) as client:
        response = await client.get(f"{TEMPMAILLOL_BASE}/auth/{token}")
        response.raise_for_status()
        payload = response.json()
    out = []
    messages = payload.get("email") if isinstance(payload, dict) else payload
    for index, message in enumerate(messages or []):
        if not isinstance(message, dict):
            continue
        out.append(
            {
                "id": str(message.get("id") or f"{message.get('date', '')}-{index}"),
                "from": _address(message.get("from")),
                "subject": str(message.get("subject") or "No subject"),
                "text": html_to_text(message.get("body") or message.get("html") or ""),
            }
        )
    return out


# ─── PUBLIC API ───────────────────────────────────────────────────────────────
async def create_account(provider: str, domain: str | None = None, imap_config: dict | None = None):
    if provider == "tempmailplus":
        state = await _tempmailplus_create(domain)
    elif provider == "inboxes":
        state = await _inboxes_create(domain)
    elif provider == "tempmaillol":
        state = await _tempmaillol_create(domain)
    elif provider == "mailtm":
        state = await _mailbase_create("mailtm", MAILTM_BASE, domain)
    elif provider == "mailgw":
        state = await _mailbase_create("mailgw", MAILGW_BASE, domain)
    elif provider == "tempmailio":
        state = await _tempmailio_create(domain)
    elif provider == "guerrilla":
        state = await _guerrilla_create(domain)
    elif provider == "imap":
        if not domain:
            raise RuntimeError("imap provider needs a domain")
        state = await _imap_create(domain, imap_config or {})
    else:
        raise RuntimeError(f"Unknown mail provider: {provider}")
    state["seen_messages"] = []
    state["created_at"] = time.time()
    return state


async def fetch_messages(state: dict):
    provider = str(state.get("provider") or "mailgw")
    if provider == "tempmailplus":
        return await _tempmailplus_fetch(state)
    if provider == "inboxes":
        return await _inboxes_fetch(state)
    if provider == "tempmaillol":
        return await _tempmaillol_fetch(state)
    if provider == "mailtm":
        return await _mailbase_fetch(state, MAILTM_BASE)
    if provider == "mailgw":
        return await _mailbase_fetch(state, MAILGW_BASE)
    if provider == "tempmailio":
        return await _tempmailio_fetch(state)
    if provider == "guerrilla":
        return await _guerrilla_fetch(state)
    if provider == "imap":
        return await asyncio.to_thread(
            _imap_fetch_sync, state.get("imap") or {}, state.get("email", "")
        )
    raise RuntimeError(f"Unknown mail provider: {provider}")


async def provider_health():
    """Quick reachability probe used by the admin panel."""
    results = {}
    for provider in PUBLIC_PROVIDERS:
        try:
            if provider == "tempmailplus":
                async with httpx.AsyncClient(timeout=TIMEOUT, headers=BROWSER_HEADERS) as client:
                    response = await client.get(
                        f"{TEMPMAILPLUS_BASE}/mails",
                        params={"email": f"{_random_local()}@mailto.plus", "limit": 5, "epin": ""},
                    )
                    response.raise_for_status()
            elif provider == "inboxes":
                async with httpx.AsyncClient(timeout=TIMEOUT, headers=BROWSER_HEADERS) as client:
                    response = await client.get(f"{INBOXES_BASE}/domain")
                    response.raise_for_status()
            elif provider == "tempmaillol":
                async with httpx.AsyncClient(timeout=TIMEOUT, headers=BROWSER_HEADERS) as client:
                    response = await client.get(f"{TEMPMAILLOL_BASE}/generate")
                    response.raise_for_status()
            elif provider in ("mailtm", "mailgw"):
                base = MAILTM_BASE if provider == "mailtm" else MAILGW_BASE
                async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                    response = await client.get(f"{base}/domains")
                    response.raise_for_status()
            elif provider == "tempmailio":
                async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                    response = await client.get(f"{TEMPMAILIO_BASE}/domains")
                    response.raise_for_status()
            else:
                async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                    response = await client.get(
                        GUERRILLA_BASE, params={"f": "get_email_address"}
                    )
                    response.raise_for_status()
            results[provider] = True
        except Exception as exc:
            logger.debug("provider %s down: %s", provider, exc)
            results[provider] = False
    return results
'''

_SRC_SHOP = r'''"""
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
import re as _re
import secrets
import threading
import time
from datetime import datetime, timedelta

import pyotp
import requests as _requests

from telebot.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
# InlineKeyboardButton / KeyboardButton are the main bot's styled factories,
# injected by the bundle so shop buttons behave exactly like main bot buttons.

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
        [L("🔑", "Get Code")],
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
        elif text == L("🔑", "Get Code"):
            gc_open_menu(chat_id)
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

    if step == "gc_mail_data":
        return _gc_receive_mail_data(message, state)

    if step == "gc_2fa_key":
        return _gc_receive_2fa_key(message, state)

    return False


# ═══════════════════════════════════════════════════════════════════════════
# GET CODE CENTER — Mail Code (Hotmail/Outlook OTP) + 2FA Code
# ═══════════════════════════════════════════════════════════════════════════
_gc_sessions: dict = {}   # user_id -> {"email", "r_token", "c_id"} or {"2fa_key"}


def _gc_progress_bar(remaining, total=30) -> str:
    filled = int((remaining / total) * 10)
    return "█" * filled + "░" * (10 - filled)


def _gc_otp_extract(body, subject):
    """Smart OTP extractor — works for any service (FB, Google, etc.)."""
    clean = _re.sub(r"<[^<]+?>", " ", str(body or ""))
    full = f"{subject or ''} {clean}"

    special = _re.findall(r"[A-Z0-9]+-([\d]{4,8})", full)
    if special:
        return special[0]

    for word in ("code", "otp", "verification", "confirmation", "pin", "password"):
        match = _re.search(rf"{word}.*?(\d{{4,8}})", full, _re.IGNORECASE | _re.DOTALL)
        if match:
            return match.group(1)

    for digit in _re.findall(r"\b\d{4,8}\b", full):
        if not (2000 <= int(digit) <= 2030):
            return digit
    return None


def _gc_hotmail_otp(refresh_token, client_id):
    """Check the latest Hotmail/Outlook inbox mail via Microsoft Graph API."""
    try:
        token_res = _requests.post(
            "https://login.live.com/oauth20_token.srf",
            data={
                "client_id": client_id,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": "https://graph.microsoft.com/Mail.Read offline_access",
            },
            timeout=15,
        )
        token_data = token_res.json()
        if "access_token" not in token_data:
            return False, st("Access Token Failed! Client ID or Refresh Token is invalid.")

        mail_res = _requests.get(
            "https://graph.microsoft.com/v1.0/me/messages?$top=1&$orderby=receivedDateTime desc",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
            timeout=15,
        )
        mail_data = mail_res.json()

        if mail_data.get("value"):
            msg = mail_data["value"][0]
            body = msg.get("body", {}).get("content", "")
            subject = msg.get("subject", "No Subject")
            sender = msg.get("from", {}).get("emailAddress", {}).get("name", "Unknown")
            otp = _gc_otp_extract(body, subject)
            if otp:
                return True, {"otp": otp, "sender": sender, "subject": subject}
            return False, st("Mail found, but no OTP detected in the message!")
        return False, st("No new mail found in the inbox!")
    except Exception as exc:
        return False, f"{st('System Error')}: <code>{esc(exc)}</code>"


def gc_open_menu(chat_id):
    """Get Code submenu — shown from the Shop main menu."""
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(f"📧 {st('Mail Code')}", callback_data="shop_gc:mail"),
        InlineKeyboardButton(f"🛡️ {st('2FA Code')}", callback_data="shop_gc:2fa"),
    )
    send(
        chat_id,
        f"🔑 <b>{st('GET CODE CENTER')}</b>\n{sep()}\n"
        f"⚡ {st('Which code do you want to get?')}\n"
        f"<blockquote>"
        f"📧 <b>{st('Mail Code')}</b> ➤ {st('OTP from Hotmail/Outlook inbox')}\n"
        f"🛡️ <b>{st('2FA Code')}</b> ➤ {st('Live OTP from your Secret Key')}"
        f"</blockquote>\n"
        f"👇 <i>{st('Select an option below.')}</i>",
        reply_markup=kb,
    )


def _gc_receive_mail_data(message, state) -> bool:
    user = message.from_user
    uid, chat_id = user.id, message.chat.id
    text = (message.text or "").strip()
    key = _skey(chat_id, uid)

    parts = text.split("|")
    if len(parts) < 4:
        send(
            chat_id,
            f"⛔ <b>{st('WRONG FORMAT')}</b>\n{sep()}\n"
            f"📝 {st('Send like this:')}\n<code>Email|Pass|RefreshToken|ClientID</code>",
        )
        return True

    email_addr = parts[0].strip()
    _gc_sessions[uid] = {
        "email": email_addr,
        "r_token": parts[2].strip(),
        "c_id": parts[3].strip(),
    }
    shop_states.pop(key, None)

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(f"📥 {st('Check Inbox (Latest OTP)')}", callback_data="shop_gc:mail_check"))
    send(
        chat_id,
        f"✅ <b>{st('DATA SAVED SUCCESSFULLY')}</b>\n{sep()}\n"
        f"<blockquote>📧 {st('Account')}: <code>{esc(email_addr)}</code></blockquote>\n"
        f"👇 <i>{st('Tap the button to check the latest OTP.')}</i>",
        reply_markup=kb,
    )
    return True


def _gc_receive_2fa_key(message, state) -> bool:
    user = message.from_user
    uid, chat_id = user.id, message.chat.id
    key = _skey(chat_id, uid)
    secret = (message.text or "").replace(" ", "").upper()

    try:
        otp = pyotp.TOTP(secret).now()
    except Exception:
        send(
            chat_id,
            f"⛔ <b>{st('INVALID SECRET KEY')}</b>\n{sep()}\n"
            f"🛡️ {st('Please send a valid Base-32 2FA Secret Key.')}",
        )
        return True

    _gc_sessions[uid] = {"2fa_key": secret}
    shop_states.pop(key, None)
    send(chat_id, _gc_2fa_text(otp), reply_markup=_gc_2fa_kb())
    return True


def _gc_2fa_text(otp) -> str:
    remaining = 30 - (int(time.time()) % 30)
    return (
        f"🛡️ <b>{st('YOUR 2FA OTP CODE')}</b>\n{sep()}\n"
        f"🔢 <b>{st('CODE')}:</b> <code>{otp}</code>\n\n"
        f"⏳ <b>{st('Expires in')}:</b> <code>{remaining}s</code>\n"
        f"📊 <code>{_gc_progress_bar(remaining)}</code>\n{sep()}\n"
        f"💡 <i>{st('Tap the code to copy. Refresh when it expires.')}</i>"
    )


def _gc_2fa_kb():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(f"♻️ {st('Refresh 2FA')}", callback_data="shop_gc:2fa_refresh"))
    return kb


def _gc_mail_result_text(success, result) -> str:
    if success:
        return (
            f"✨ <b>{st('OTP RECEIVED')}</b> ✨\n{sep()}\n"
            f"<blockquote>"
            f"🏷 <b>{st('Sender')}:</b> <code>{esc(result['sender'])}</code>\n"
            f"📌 <b>{st('Subject')}:</b> {esc(result['subject'])}"
            f"</blockquote>\n"
            f"🔑 <b>{st('OTP CODE')}:</b> <code>{esc(result['otp'])}</code>\n{sep()}\n"
            f"💡 <i>{st('Tap the code to copy. Refresh for a new one.')}</i>"
        )
    return (
        f"⚠️ <b>{st('NO OTP FOUND')}</b>\n{sep()}\n"
        f"<blockquote>{result}</blockquote>\n"
        f"👇 <i>{st('Wait a moment and tap Refresh again.')}</i>"
    )


def _gc_mail_kb():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(f"♻️ {st('Refresh Inbox')}", callback_data="shop_gc:mail_check"))
    return kb


def _gc_handle_callback(call) -> bool:
    """Handle every ``shop_gc:`` callback. Returns True when consumed."""
    data = call.data or ""
    uid = call.from_user.id
    chat_id = call.message.chat.id if call.message else uid
    msg_id = call.message.message_id if call.message else None
    action = data.split(":", 1)[1]

    if action == "mail":
        _ack(call)
        shop_states[_skey(chat_id, uid)] = {"step": "gc_mail_data", "data": {}}
        text = (
            f"📧 <b>{st('MAIL CODE')}</b>\n{sep()}\n"
            f"📥 {st('Send your mail data in this format:')}\n"
            f"<code>Email|Pass|RefreshToken|ClientID</code>\n{sep()}\n"
            f"💡 <i>{st('Copy-paste the full line like the example.')}</i>"
        )
        _gc_edit_or_send(chat_id, msg_id, text)
        return True

    if action == "2fa":
        _ack(call)
        shop_states[_skey(chat_id, uid)] = {"step": "gc_2fa_key", "data": {}}
        text = (
            f"🛡️ <b>{st('2FA CODE')}</b>\n{sep()}\n"
            f"🔐 {st('Send your 2FA Secret Key as text.')}\n"
            f"💡 <i>{st('Example:')} <code>JBSWY3DPEHPK3PXP</code></i>"
        )
        _gc_edit_or_send(chat_id, msg_id, text)
        return True

    if action == "mail_check":
        session = _gc_sessions.get(uid) or {}
        if "r_token" not in session:
            _ack(call, st("Data not found! Please send your mail data again."), True)
            return True
        _ack(call, st("Refreshing inbox..."))
        if msg_id:
            try:
                bot().edit_message_text(
                    f"🔄 <b>{st('Checking Mailbox... Please Wait!')}</b>", chat_id, msg_id
                )
            except Exception:
                pass
        success, result = _gc_hotmail_otp(session["r_token"], session["c_id"])
        _gc_edit_or_send(
            chat_id, msg_id, _gc_mail_result_text(success, result), reply_markup=_gc_mail_kb()
        )
        return True

    if action == "2fa_refresh":
        session = _gc_sessions.get(uid) or {}
        if "2fa_key" not in session:
            _ack(call, st("Key not found! Please send your Secret Key again."), True)
            return True
        try:
            otp = pyotp.TOTP(session["2fa_key"]).now()
        except Exception:
            _ack(call, st("Could not process the key!"), True)
            return True
        _gc_edit_or_send(chat_id, msg_id, _gc_2fa_text(otp), reply_markup=_gc_2fa_kb())
        _ack(call, st("Refreshed!"))
        return True

    return False


def _gc_edit_or_send(chat_id, msg_id, text, reply_markup=None):
    if msg_id:
        try:
            bot().edit_message_text(text, chat_id, msg_id, reply_markup=reply_markup)
            return
        except Exception:
            pass
    send(chat_id, text, reply_markup=reply_markup)


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

    if data.startswith("shop_gc:"):
        _gc_handle_callback(call)
        return

    if data.startswith("shop_admin") or data.startswith("shop_ap:"):
        shop_admin.handle_callback(call)
        return

    if data.startswith("shop_order:") or data.startswith("shop_dlv:") or data.startswith("shop_topup:"):
        shop_admin.handle_callback(call)
        return

    if data.startswith("shop_list:"):
        _, kind, rest = data.split(":", 2)
        arg, _sep, page = rest.rpartition(":")
        if not _sep:
            arg, page = rest, "0"
        if not page.strip().isdigit():
            arg, page = rest, "0"
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
        _, mode, rest = data.split(":", 2)
        pid, _sep, qty = rest.rpartition(":")
        if not _sep or not qty.strip().isdigit():
            pid, qty = rest, "1"
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
        _, mode, rest = data.split(":", 2)
        pid, _sep, qty = rest.rpartition(":")
        if not _sep or not qty.strip().isdigit():
            pid, qty = rest, "1"
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
'''

_SRC_SHOP_ADMIN = r'''"""
SHOP ADMIN MODULE — isolated admin system for the shop extension.

Nothing here touches the existing bot's Admin Panel, states, callbacks or
tables.  All authorization goes through ``shop.is_shop_admin`` (which reuses
``main.is_admin``), every callback lives in the ``shop_admin`` / ``shop_ap:``
/ ``shop_order:`` / ``shop_dlv:`` / ``shop_topup:`` namespace and every table
is ``shop_*``.
"""

import logging

from telebot.types import InlineKeyboardMarkup
# InlineKeyboardButton is the main bot's styled factory (injected by bundle).

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
        data["category"] = text.replace(":", " ").strip()[:40] or "General"
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
'''

temp_mail_engine = _install_module("temp_mail_engine", _SRC_TEMP_MAIL_ENGINE)
# NOTE: shop / shop_admin are installed further down, right after the bot core
# defines the styled button factories they share with the main bot.

# ══════════════════════════════════════════════════════════════════════════════
# BOT CORE
# ══════════════════════════════════════════════════════════════════════════════
import sqlite3
import threading
import time
import re
import os
import asyncio
import json
import html as _html
import io
import logging
import hashlib
import secrets
import requests
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import date, datetime
import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)
try:
    from telebot.types import CopyTextButton
except ImportError:
    CopyTextButton = None


_TelegramInlineKeyboardButton = InlineKeyboardButton
_TelegramKeyboardButton = KeyboardButton


def _button_style(text: str) -> str:
    label = str(text or "").lower()
    style_map = str.maketrans(
        "𝚊𝚋𝚌𝚍𝚎𝚏𝚐𝚑𝚒𝚓𝚔𝚕𝚖𝚗𝚘𝚙𝚚𝚛𝚜𝚝𝚞𝚟𝚠𝚡𝚢𝚣𝙰𝙱𝙲𝙳𝙴𝙵𝙶𝙷𝙸𝙹𝙺𝙻𝙼𝙽𝙾𝙿𝚀𝚁𝚂𝚃𝚄𝚅𝚆𝚇𝚈𝚉",
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    )
    label = label.translate(style_map)

    success_words = (
        "submit", "confirm", "save", "add", "approve", "yes", "enable",
        "unban", "get number", "backup file", "set", "import", "input",
        "force join: on",
    )
    danger_words = (
        "back", "cancel", "close", "delete", "reject", "no", "remove",
        "clear", "disable", "ban", "reset", "del", "force join: off",
    )

    if any(re.search(rf"\b{re.escape(word)}\b", label) for word in success_words):
        return "success"
    if any(re.search(rf"\b{re.escape(word)}\b", label) for word in danger_words):
        return "danger"
    if any(symbol in label for symbol in ("✔️", "➕")):
        return "success"
    if any(symbol in label for symbol in ("🧹", "⛔", "🛑", "↩")):
        return "danger"
    return "primary"


def _with_button_style(button, style: str):
    try:
        button.style = style
    except Exception:
        pass
    return button


def _number_values(number=None, numbers=None):
    values = list(numbers or ([] if number is None else [number]))
    return [str(value).strip().lstrip("+") for value in values if str(value).strip()]


def _number_from_api_data(num_data):
    return (
        num_data.get("no_plus_number")
        or normalize_number(num_data.get("full_number", ""))
        or num_data.get("national_number")
        or ""
    )


def fetch_api_numbers(rid: str, count: int = 1):
    """Collect `count` unique numbers, retrying a few times per slot."""
    numbers = []
    for _ in range(count):
        for _attempt in range(5):
            num_data = fetch_api_number(rid)
            if not num_data:
                continue
            full_number = _number_from_api_data(num_data)
            if full_number and full_number not in numbers:
                numbers.append(full_number)
                break
        else:
            return []
    return numbers


def InlineKeyboardButton(*args, **kwargs):
    text = args[0] if args else kwargs.get("text", "")
    style = kwargs.pop("style", None) or _button_style(text)
    try:
        return _TelegramInlineKeyboardButton(*args, style=style, **kwargs)
    except TypeError:
        return _with_button_style(_TelegramInlineKeyboardButton(*args, **kwargs), style)


def KeyboardButton(*args, **kwargs):
    text = args[0] if args else kwargs.get("text", "")
    style = kwargs.pop("style", None) or _button_style(text)
    try:
        return _TelegramKeyboardButton(*args, style=style, **kwargs)
    except TypeError:
        return _with_button_style(_TelegramKeyboardButton(*args, **kwargs), style)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ─── CREDENTIALS ───────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is required. Add it in Render -> Environment.")
if not ADMIN_ID_RAW or not ADMIN_ID_RAW.isdigit():
    raise SystemExit("ADMIN_ID must be a numeric Telegram user ID. Add it in Render -> Environment.")
ADMIN_ID = int(ADMIN_ID_RAW)

# Render's default filesystem is ephemeral. Set DATA_DIR to a mounted Disk
# path (for example /var/data) to keep the SQLite database across redeploys.
DATA_DIR = os.getenv("DATA_DIR", "./data").strip() or "./data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "voltx.db")

# ─── TEMP MAIL ────────────────────────────────────────────────────────────────
TEMP_MAIL_DOMAINS_API = "https://api.mail.gw/domains"
TEMP_MAIL_ACCOUNTS_API = "https://api.mail.gw/accounts"
TEMP_MAIL_TOKEN_API = "https://api.mail.gw/token"
TEMP_MAIL_MESSAGES_API = "https://api.mail.gw/messages"
TEMP_MAIL_MESSAGE_DETAILS_API = "https://api.mail.gw/messages/{message_id}"
TEMP_MAIL_DATA_PATH = os.path.join(DATA_DIR, "temp_mail_users.json")
TEMP_MAIL_TTL_SECONDS = 2 * 60 * 60
TEMP_MAIL_CHECK_INTERVAL_SECONDS = 5
TEMP_MAIL_FAST_POLL_SECONDS = 2
TEMP_MAIL_FAST_POLL_WINDOW = 180

# ─── EXTERNAL API (YesMS) ─────────────────────────────────────────────────────
YESMS_BASE = os.getenv("YESMS_BASE_URL", "https://yesms.online/api").rstrip("/")

# ─── PANEL BASE URLS ──────────────────────────────────────────────────────────
STEXSMS_BASE   = os.getenv("STEXSMS_BASE_URL", "https://api.2oo9.cloud/MXS47FLFX0U/tness/@public/api").rstrip("/")
FASTXOTPS_BASE = os.getenv("FASTXOTPS_BASE_URL", "https://2eee7.com/@Access/@Bot/2eee7/@public").rstrip("/")
VOLTXSMS_BASE  = os.getenv("VOLTXSMS_BASE_URL", "https://api.2oo9.cloud/MXS47FLFX0U/tnevs/@public/api").rstrip("/")
ZEBRASMS_BASE  = os.getenv("ZEBRASMS_BASE_URL", "https://api.zebrasms.com/api/v1").rstrip("/")

# ─── API CREDENTIALS (URLs only; keys managed entirely via admin panel) ────────
SMSHADI_URL = os.getenv("SMSHADI_URL", "http://147.135.212.197/crapi/had/viewstats").strip()
LAMIX_URL   = os.getenv("LAMIX_URL", "http://51.77.216.195/crapi/lamix/viewstats").strip()

# ─── API NAME DEFINITIONS ──────────────────────────────────────────────────────
API_DEFINITIONS = {
    "smshadi":   {"name": "SMShadi",    "url": SMSHADI_URL,    "default_key": ""},
    "lamix":     {"name": "Lamix",      "url": LAMIX_URL,      "default_key": ""},
    "yesms":     {"name": "YesMS API",  "url": YESMS_BASE,     "default_key": ""},
    "stexsms":   {"name": "StexSMS",    "url": STEXSMS_BASE,   "default_key": ""},
    "fastxotps": {"name": "FastXOTPs",  "url": FASTXOTPS_BASE, "default_key": ""},
    "voltxsms":  {"name": "VoltXSMS",   "url": VOLTXSMS_BASE,  "default_key": ""},
    "zebrasms":  {"name": "ZebraSMS",   "url": ZEBRASMS_BASE,  "default_key": ""},
}

# ─── PANEL ROTATION GLOBALS ────────────────────────────────────────────────────
_panel_alloc_idx       = 0
_panel_alloc_lock      = threading.Lock()
_traffic_panel_idx     = 0
_traffic_panel_last_sw = 0.0
_traffic_panel_lock    = threading.Lock()

# ─── SERVICE ABBREVIATION MAP ─────────────────────────────────────────────────
SERVICE_SHORT_MAP = {
    "facebook": "fb", "instagram": "ig", "whatsapp": "wa", "telegram": "tg",
    "twitter": "tw", "google": "gg", "gmail": "gm", "microsoft": "ms",
    "amazon": "amz", "netflix": "nf", "snapchat": "sc", "tiktok": "tt",
    "uber": "ub", "lyft": "lf", "paypal": "pp", "discord": "dc",
    "linkedin": "li", "reddit": "rd", "pinterest": "pt", "youtube": "yt",
    "apple": "ap", "yahoo": "yh", "ebay": "eb", "airbnb": "ab",
    "wechat": "wc", "viber": "vb", "line": "ln", "signal": "sg",
}


def get_service_short(sid: str) -> str:
    """Return short 2-letter abbreviation for a service name."""
    low = sid.lower()
    for key, short in SERVICE_SHORT_MAP.items():
        if key in low:
            return short
    return sid[:2].lower() if sid else "??"

def stylish(text: str) -> str:
    """Convert ASCII letters and digits to mathematical monospace typewriter font."""
    result = []
    for ch in text:
        if 'A' <= ch <= 'Z':
            result.append(chr(0x1D670 + ord(ch) - ord('A')))
        elif 'a' <= ch <= 'z':
            result.append(chr(0x1D68A + ord(ch) - ord('a')))
        elif '0' <= ch <= '9':
            result.append(chr(0x1D7F6 + ord(ch) - ord('0')))
        else:
            result.append(ch)
    return ''.join(result)


def _bot_name() -> str:
    saved = get_setting("bot_name", "")
    if saved:
        return saved
    try:
        me = bot.get_me()
        return me.username or me.first_name or "BOT"
    except Exception:
        return "BOT"

def _Developer_By() -> str:
    return get_setting("Developer_By", "SUMON VAI")

def _join_prompt_text() -> str:
    bn = stylish(_bot_name())
    pw = stylish(_Developer_By())
    sep = "━" * 32
    return (
        f"❗️ {sep}\n"
        f"🔐 {stylish('JOIN REQUIRED')}\n"
        f"{sep}\n\n"
        f"🦾 {bn} ব্যবহার করতে হলে\n"
        f"নিচের চ্যানেল/গ্রুপে জয়েন করুন!\n\n"
        f"👇 Join করে <b>{stylish('CHECK JOIN')}</b> বাটনে চাপুন\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔷 {stylish('Developer_By')} <b>{pw}</b>"
    )


bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")



# ─── IN-MEMORY STATE ───────────────────────────────────────────────────────────
admin_states: dict = {}
user_states: dict = {}
admin_live_mode: set = set()   # admins who see live number notifications (/start)
admin_user_mode: set = set()   # admins currently acting as plain user (/user)
admin_live_msg_ids: dict = {}  # number -> {admin_id: msg_id}  (live panel tracking)


# ─── TEMP MAIL STATE ──────────────────────────────────────────────────────────
temp_mail_users: dict = {}
temp_mail_lock = threading.RLock()
_temp_mail_loop = None
_temp_mail_loop_thread = None
_temp_mail_loop_ready = threading.Event()
_temp_mail_loop_lock = threading.Lock()
_temp_mail_user_locks = {}


def _load_temp_mail_users():
    global temp_mail_users
    try:
        with open(TEMP_MAIL_DATA_PATH, "r", encoding="utf-8") as state_file:
            loaded = json.load(state_file)
        if isinstance(loaded, dict):
            temp_mail_users = loaded
    except FileNotFoundError:
        temp_mail_users = {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load Temp Mail state: %s", exc)
        temp_mail_users = {}


def _save_temp_mail_users_locked():
    temporary_path = f"{TEMP_MAIL_DATA_PATH}.tmp"
    try:
        with open(temporary_path, "w", encoding="utf-8") as state_file:
            json.dump(temp_mail_users, state_file, ensure_ascii=False, indent=2)
        os.replace(temporary_path, TEMP_MAIL_DATA_PATH)
        try:
            os.chmod(TEMP_MAIL_DATA_PATH, 0o600)
        except OSError:
            pass
    except OSError as exc:
        logger.warning("Could not save Temp Mail state: %s", exc)
        try:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
        except OSError:
            pass


def _cleanup_expired_temp_mail_users():
    now = time.time()
    changed = False
    with temp_mail_lock:
        for user_id, state in list(temp_mail_users.items()):
            created_at = state.get("created_at", 0) if isinstance(state, dict) else 0
            try:
                is_expired = now - float(created_at) >= TEMP_MAIL_TTL_SECONDS
            except (TypeError, ValueError):
                is_expired = True
            if is_expired:
                del temp_mail_users[user_id]
                changed = True
        if changed:
            _save_temp_mail_users_locked()


def _get_temp_mail_user(user_id: int):
    _cleanup_expired_temp_mail_users()
    with temp_mail_lock:
        state = temp_mail_users.get(str(user_id))
        return dict(state) if isinstance(state, dict) else None


def _temp_mail_extract_members(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("hydra:member", "messages", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _temp_mail_sender_email(sender):
    if isinstance(sender, dict):
        return str(sender.get("address") or sender.get("email") or "").strip()
    return str(sender or "").strip()


def _temp_mail_clean_body(message_text: str) -> str:
    try:
        return temp_mail_engine.html_to_text(message_text)
    except Exception:
        return str(message_text or "")


def _temp_mail_extract_otp(subject: str, message_text: str) -> str:
    """Pull the verification code out of a mail (digits or alphanumeric)."""
    body = _temp_mail_clean_body(message_text)
    searchable_text = "\n".join(
        part for part in (str(subject or ""), body) if part
    )
    patterns = (
        r"(?i)\b(?:otp|one[-\s]?time\s+(?:password|code|pin)|verification|verify|security|"
        r"confirm(?:ation)?|activation|access|login|auth(?:entication)?)"
        r"[^0-9A-Za-z]{0,20}(?:code|pin|number|is|:)?[^0-9A-Za-z]{0,10}([0-9]{4,8})\b",
        r"(?i)\b([0-9]{4,8})\b[^0-9A-Za-z]{0,20}(?:is\s+your\s+)?"
        r"(?:otp|one[-\s]?time\s+(?:password|code)|verification\s+code|security\s+code|code)",
        r"(?i)\b(?:code|pin|token)\s*[:#\-=]?\s*([0-9]{4,8})\b",
        r"\b(?:code|otp|CODE|OTP)\s*[:#\-=]\s*([A-Z0-9]{4,8})\b",
        r"(?<![0-9])([0-9]{6})(?![0-9])",
        r"(?<![0-9])([0-9]{4,8})(?![0-9])",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, searchable_text):
            candidate = match.group(1).strip()
            # skip obvious years / prices
            if candidate.isdigit() and len(candidate) == 4 and candidate.startswith(("19", "20")):
                continue
            return candidate
    return "Not found"


def _temp_mail_sms_text(mailbox_email, sender, subject, otp, preview) -> str:
    """Render an incoming mail as a pretty SMS-style card."""
    line = "━" * 22
    sender_name = _temp_mail_sender_email(sender) or str(sender or "Unknown")
    when = time.strftime("%d %b %Y, %I:%M %p")
    has_otp = bool(otp) and str(otp).lower() != "not found"
    head = "📲 <b>NEW SMS RECEIVED</b>" if has_otp else "📬 <b>NEW MAIL RECEIVED</b>"
    parts = [
        f"{head}\n{line}",
        f"📪 <b>To :</b> <code>{_html.escape(mailbox_email)}</code>",
        f"👤 <b>From:</b> <code>{_html.escape(sender_name)}</code>",
        f"🏷️ <b>Subject:</b> {_html.escape(str(subject))}",
        f"🕒 <b>Time:</b> {_html.escape(when)}",
        line,
    ]
    if has_otp:
        parts += [
            f"🔐 <b>{stylish('YOUR OTP CODE')}</b>",
            f"<code>{_html.escape(str(otp))}</code>",
            "<i>👆 কোডটিতে ট্যাপ করলেই কপি হয়ে যাবে।</i>",
        ]
    else:
        parts.append("🔎 <i>এই মেইলে কোনো OTP কোড পাওয়া যায়নি।</i>")
    parts += [
        line,
        f"🗨 <b>Message:</b>\n<blockquote>{_html.escape(preview)}</blockquote>",
        line,
        f"🔷 {stylish('POWERED BY')} <b>{stylish(_Developer_By())}</b>",
    ]
    return "\n".join(parts)


def _temp_mail_preview(message_text: str, limit: int = 600) -> str:
    body = _temp_mail_clean_body(message_text)
    if len(body) > limit:
        body = body[:limit].rstrip() + "…"
    return body or "(empty message)"


def _temp_mail_copy_button(label: str, value: str, callback_prefix: str):
    if CopyTextButton is not None:
        try:
            return InlineKeyboardButton(label, copy_text=CopyTextButton(text=value))
        except Exception as exc:
            logger.debug("Native copy button unavailable: %s", exc)
    callback_data = f"{callback_prefix}{value}"
    if len(callback_data.encode("utf-8")) <= 64:
        return InlineKeyboardButton(label, callback_data=callback_data)
    return InlineKeyboardButton(label, callback_data=f"{callback_prefix}unavailable")


def _temp_mail_email_keyboard(email: str):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(_temp_mail_copy_button("🧾 Copy Email", email, "temp_mail_copy_email:"))
    kb.add(
        InlineKeyboardButton("♻ Refresh Inbox", callback_data="temp_mail_check"),
        InlineKeyboardButton("➕ New Email", callback_data="temp_mail_new"),
    )
    return kb


def _temp_mail_message_keyboard(otp: str):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(_temp_mail_copy_button("🧾 Copy OTP", otp, "temp_mail_copy_otp:"))
    kb.add(InlineKeyboardButton("➕ New Email", callback_data="temp_mail_new"))
    return kb


def _temp_mail_random_credentials(domain: str):
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    local_part = "".join(secrets.choice(alphabet) for _ in range(12))
    email = f"{local_part}@{domain}"
    password = secrets.token_urlsafe(18)
    return email, password


_temp_mail_provider_cursor = 0


async def _create_temp_mail_account():
    """Create a mailbox.

    Priority order:
      1. Admin-added domains (Admin Panel -> Settings -> Temp Mail -> Add Domain)
      2. Public free providers, in fallback order, so one provider being down
         never breaks the Get Mail button.
    """
    attempts = []
    for row in get_temp_mail_domains(enabled_only=True):
        attempts.append(
            (
                row["provider"],
                row["domain"],
                {
                    "imap_host": row["imap_host"],
                    "imap_port": row["imap_port"],
                    "imap_user": row["imap_user"],
                    "imap_pass": row["imap_pass"],
                    "imap_folder": row["imap_folder"],
                },
            )
        )
    # Round-robin across every public provider so no single service hits its
    # per-IP / per-day limit, then always finish with the "never-ending" ones.
    global _temp_mail_provider_cursor
    providers = list(temp_mail_engine.PUBLIC_PROVIDERS)
    if providers:
        start = _temp_mail_provider_cursor % len(providers)
        _temp_mail_provider_cursor = (start + 1) % len(providers)
        providers = providers[start:] + providers[:start]
    for provider in providers:
        attempts.append((provider, None, None))
    for provider in temp_mail_engine.UNLIMITED_PROVIDERS:
        attempts.append((provider, None, None))

    last_error = None
    for provider, domain, imap_config in attempts:
        try:
            state = await temp_mail_engine.create_account(
                provider, domain=domain, imap_config=imap_config
            )
            state["expires_at"] = state["created_at"] + TEMP_MAIL_TTL_SECONDS
            logger.info("Temp Mail created via %s (%s)", provider, state["email"])
            return state
        except Exception as exc:
            last_error = exc
            logger.warning("Temp Mail provider %s failed: %s", provider, exc)
    raise last_error or RuntimeError("Temp Mail account creation failed")


async def _generate_temp_mail_for_user(user_id: int):
    state = await _create_temp_mail_account()
    with temp_mail_lock:
        temp_mail_users[str(user_id)] = state
        _save_temp_mail_users_locked()
    return state


async def _check_temp_mail_for_user(user_id: int):
    user_lock = _temp_mail_user_locks.get(user_id)
    if user_lock is None:
        user_lock = asyncio.Lock()
        _temp_mail_user_locks[user_id] = user_lock
    async with user_lock:
        state = _get_temp_mail_user(user_id)
        if not state or not state.get("email"):
            return 0

        mailbox_email = state["email"]
        delivered_count = 0
        try:
            messages = await temp_mail_engine.fetch_messages(state)
        except Exception as exc:
            logger.warning("Temp Mail inbox fetch failed (%s): %s", state.get("provider"), exc)
            return 0
        seen_messages = {str(value) for value in state.get("seen_messages", [])}

        for message in messages:
            message_id = str(message.get("id") or "")
            if not message_id or message_id in seen_messages:
                continue

            subject = str(message.get("subject") or "No subject")
            sender = str(message.get("from") or "") or "Unknown"
            message_text = str(message.get("text") or "")
            otp = _temp_mail_extract_otp(subject, message_text)
            preview = _temp_mail_preview(message_text)
            notification = _temp_mail_sms_text(
                mailbox_email, sender, subject, otp, preview
            )
            await asyncio.to_thread(
                bot.send_message,
                user_id,
                notification,
                reply_markup=_temp_mail_message_keyboard(otp),
            )

            with temp_mail_lock:
                current_state = temp_mail_users.get(str(user_id))
                if (
                    not isinstance(current_state, dict)
                    or current_state.get("email") != mailbox_email
                ):
                    return delivered_count
                current_seen = [
                    str(value) for value in current_state.get("seen_messages", [])
                ]
                if message_id not in current_seen:
                    current_seen.append(message_id)
                current_state["seen_messages"] = current_seen
                _save_temp_mail_users_locked()
            seen_messages.add(message_id)
            delivered_count += 1
        return delivered_count


async def _temp_mail_background_loop():
    logger.info("Temp Mail background checker started.")
    while True:
        try:
            _cleanup_expired_temp_mail_users()
            with temp_mail_lock:
                user_ids = [
                    int(user_id)
                    for user_id in temp_mail_users
                    if str(user_id).isdigit()
                ]
            if user_ids:
                results = await asyncio.gather(
                    *(_check_temp_mail_for_user(user_id) for user_id in user_ids),
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, Exception):
                        logger.warning("Temp Mail check failed: %s", result)
        except Exception as exc:
            logger.warning("Temp Mail background loop error: %s", exc)
        try:
            now = time.time()
            with temp_mail_lock:
                fresh = any(
                    isinstance(state, dict)
                    and now - float(state.get("created_at") or 0) <= TEMP_MAIL_FAST_POLL_WINDOW
                    for state in temp_mail_users.values()
                )
        except Exception:
            fresh = False
        await asyncio.sleep(
            TEMP_MAIL_FAST_POLL_SECONDS if fresh else TEMP_MAIL_CHECK_INTERVAL_SECONDS
        )


def _run_temp_mail_coroutine(coroutine, timeout=45):
    _start_temp_mail_background_loop()
    if _temp_mail_loop is None:
        raise RuntimeError("Temp Mail background loop is unavailable")
    future = asyncio.run_coroutine_threadsafe(coroutine, _temp_mail_loop)
    return future.result(timeout=timeout)


def _start_temp_mail_background_loop():
    global _temp_mail_loop_thread
    with _temp_mail_loop_lock:
        if _temp_mail_loop_thread and _temp_mail_loop_thread.is_alive():
            return
        _temp_mail_loop_ready.clear()

        def run_loop():
            global _temp_mail_loop
            _temp_mail_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_temp_mail_loop)
            _temp_mail_loop.create_task(_temp_mail_background_loop())
            _temp_mail_loop_ready.set()
            _temp_mail_loop.run_forever()

        _temp_mail_loop_thread = threading.Thread(
            target=run_loop,
            daemon=True,
            name="temp-mail-checker",
        )
        _temp_mail_loop_thread.start()
    if not _temp_mail_loop_ready.wait(timeout=5):
        raise RuntimeError("Temp Mail background loop did not start")


def _send_generated_temp_mail(chat_id, user_id):
    try:
        state = _run_temp_mail_coroutine(_generate_temp_mail_for_user(user_id))
        email = _html.escape(state["email"])
        bot.send_message(
            chat_id,
            f"✔️ Temp Mail Generated: <code>{email}</code>\n"
            f"🛰 Source: <b>{_html.escape(temp_mail_engine.PROVIDER_LABELS.get(state.get('provider', ''), 'Auto'))}</b>\n"
            f"⌛ Valid for {TEMP_MAIL_TTL_SECONDS // 3600} hours.",
            reply_markup=_temp_mail_email_keyboard(state["email"]),
        )
    except Exception as exc:
        logger.warning("Temp Mail generation failed for user=%s: %s", user_id, exc)
        bot.send_message(
            chat_id,
            "⛔ Temp Mail could not be generated right now. Please try again.",
        )


def _send_temp_mail_check_result(chat_id, user_id):
    if not _get_temp_mail_user(user_id):
        new_kb = InlineKeyboardMarkup(row_width=1)
        new_kb.add(InlineKeyboardButton("➕ New Email", callback_data="temp_mail_new"))
        bot.send_message(
            chat_id,
            "⛔ No active Temp Mail (it may have expired). Generate a new one.",
            reply_markup=new_kb,
        )
        return
    try:
        delivered_count = _run_temp_mail_coroutine(
            _check_temp_mail_for_user(user_id)
        )
    except Exception as exc:
        logger.warning("Manual Temp Mail check failed for user=%s: %s", user_id, exc)
        delivered_count = 0
    if not delivered_count:
        retry_kb = InlineKeyboardMarkup(row_width=2)
        retry_kb.add(
            InlineKeyboardButton("♻ Refresh Inbox", callback_data="temp_mail_check"),
            InlineKeyboardButton("➕ New Email", callback_data="temp_mail_new"),
        )
        bot.send_message(
            chat_id,
            "⌛ Code not found yet. Please wait...",
            reply_markup=retry_kb,
        )


_load_temp_mail_users()



# ─── DATABASE ──────────────────────────────────────────────────────────────────
import contextlib

@contextlib.contextmanager
def get_conn():
    """Open a SQLite connection that commits on success, rolls back on error,
    and ALWAYS closes afterwards (sqlite3's own context manager never closes,
    which leaked a connection on every one of the ~100 call sites)."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except Exception:
        pass
    try:
        with conn:  # commit on success / rollback on exception
            yield conn
    finally:
        conn.close()


def raw_conn():
    """Plain connection for code that drives BEGIN/COMMIT/ROLLBACK itself.
    Caller MUST close it (use try/finally)."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except Exception:
        pass
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            is_banned INTEGER DEFAULT 0,
            numbers_generated INTEGER DEFAULT 0,
            otps_received INTEGER DEFAULT 0,
            joined_at INTEGER DEFAULT (strftime('%s','now')),
            last_active_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS countries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL,
            flag TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            code TEXT NOT NULL,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            country_id INTEGER NOT NULL,
            number TEXT NOT NULL,
            assigned INTEGER DEFAULT 0,
            assigned_to INTEGER DEFAULT NULL,
            assigned_at INTEGER DEFAULT NULL,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            FOREIGN KEY (country_id) REFERENCES countries(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            number_id INTEGER,
            number TEXT NOT NULL,
            service_name TEXT DEFAULT '',
            country_name TEXT DEFAULT '',
            country_flag TEXT DEFAULT '',
            country_code TEXT DEFAULT '',
            message_id INTEGER,
            otp_received INTEGER DEFAULT 0,
            otp_text TEXT DEFAULT '',
            timed_out INTEGER DEFAULT 0,
            allocated_at INTEGER DEFAULT (strftime('%s','now')),
            rid TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS otps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_hash TEXT UNIQUE,
            user_id INTEGER,
            number TEXT,
            message TEXT,
            otp_code TEXT,
            cli TEXT DEFAULT '',
            received_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS wallet (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0.0,
            pending_balance REAL DEFAULT 0.0,
            total_income REAL DEFAULT 0.0,
            total_otp INTEGER DEFAULT 0,
            today_otp INTEGER DEFAULT 0,
            today_income REAL DEFAULT 0.0,
            last_reset_date TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS withdraw_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            method TEXT,
            number TEXT,
            amount REAL,
            status TEXT DEFAULT 'pending',
            group_msg_id INTEGER,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS delivered_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_hash TEXT UNIQUE NOT NULL,
            number TEXT,
            message TEXT,
            user_id INTEGER,
            delivered_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER,
            added_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS withdraw_methods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            is_enabled INTEGER DEFAULT 1,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS join_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            channel_name TEXT NOT NULL,
            channel_url TEXT NOT NULL,
            channel_type TEXT DEFAULT 'channel',
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS api_configs (
            api_id TEXT PRIMARY KEY,
            api_key TEXT NOT NULL DEFAULT '',
            is_enabled INTEGER DEFAULT 1,
            updated_at INTEGER DEFAULT (strftime('%s','now'))
        );

        -- Durable de-duplication for Auto SMS.  The old implementation kept
        -- this only in RAM, so a restart resent old messages and a failed
        -- Telegram send was permanently lost.
        CREATE TABLE IF NOT EXISTS auto_sms_deliveries (
            delivery_key TEXT PRIMARY KEY,
            panel_name TEXT NOT NULL DEFAULT '',
            number TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );

        -- Admin-managed Temp Mail domains.  Mail addresses are created on
        -- these domains first; public free providers are only a fallback.
        CREATE TABLE IF NOT EXISTS temp_mail_domains (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT UNIQUE NOT NULL,
            provider TEXT NOT NULL DEFAULT 'mailtm',
            imap_host TEXT DEFAULT '',
            imap_port INTEGER DEFAULT 993,
            imap_user TEXT DEFAULT '',
            imap_pass TEXT DEFAULT '',
            imap_folder TEXT DEFAULT 'INBOX',
            is_enabled INTEGER DEFAULT 1,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );
        """)

init_db()


# ─── TEMP MAIL DOMAIN STORE ───────────────────────────────────────────────────
def get_temp_mail_domains(enabled_only: bool = False):
    query = "SELECT * FROM temp_mail_domains"
    if enabled_only:
        query += " WHERE is_enabled=1"
    query += " ORDER BY id"
    try:
        with get_conn() as conn:
            return [dict(row) for row in conn.execute(query).fetchall()]
    except Exception as exc:
        logger.warning("Could not read temp mail domains: %s", exc)
        return []


def add_temp_mail_domain(
    domain: str,
    provider: str = "mailtm",
    imap_host: str = "",
    imap_port: int = 993,
    imap_user: str = "",
    imap_pass: str = "",
    imap_folder: str = "INBOX",
):
    domain = str(domain or "").strip().lstrip("@").lower()
    if not domain:
        return False
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO temp_mail_domains
               (domain, provider, imap_host, imap_port, imap_user, imap_pass, imap_folder)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(domain) DO UPDATE SET
                 provider=excluded.provider,
                 imap_host=excluded.imap_host,
                 imap_port=excluded.imap_port,
                 imap_user=excluded.imap_user,
                 imap_pass=excluded.imap_pass,
                 imap_folder=excluded.imap_folder,
                 is_enabled=1""",
            (domain, provider, imap_host, int(imap_port or 993), imap_user, imap_pass, imap_folder),
        )
    return True


def delete_temp_mail_domain(domain: str):
    domain = str(domain or "").strip().lstrip("@").lower()
    with get_conn() as conn:
        cursor = conn.execute("DELETE FROM temp_mail_domains WHERE domain=?", (domain,))
    return cursor.rowcount > 0


def toggle_temp_mail_domain(domain: str):
    domain = str(domain or "").strip().lstrip("@").lower()
    with get_conn() as conn:
        conn.execute(
            "UPDATE temp_mail_domains SET is_enabled = 1 - is_enabled WHERE domain=?",
            (domain,),
        )


def migrate_db():
    with get_conn() as conn:
        for col, typedef in [
            ("balance", "REAL DEFAULT 0.0"),
            ("pending_balance", "REAL DEFAULT 0.0"),
            ("total_income", "REAL DEFAULT 0.0"),
            ("total_otp", "INTEGER DEFAULT 0"),
            ("today_otp", "INTEGER DEFAULT 0"),
            ("today_income", "REAL DEFAULT 0.0"),
            ("last_reset_date", "TEXT DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE wallet ADD COLUMN {col} {typedef}")
            except Exception:
                pass
        conn.execute("INSERT OR IGNORE INTO wallet (user_id) SELECT id FROM users")
        for col, typedef in [
            ("number_id", "INTEGER"),
            ("service_name", "TEXT DEFAULT ''"),
            ("country_name", "TEXT DEFAULT ''"),
            ("country_flag", "TEXT DEFAULT ''"),
            ("country_code", "TEXT DEFAULT ''"),
            ("otp_text", "TEXT DEFAULT ''"),
            ("rid", "TEXT DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE allocations ADD COLUMN {col} {typedef}")
            except Exception:
                pass
        try:
            conn.execute("ALTER TABLE join_channels ADD COLUMN channel_type TEXT DEFAULT 'channel'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE countries ADD COLUMN range_id TEXT DEFAULT ''")
        except Exception:
            pass
        # referral support
        try:
            conn.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER DEFAULT NULL")
        except Exception:
            pass

migrate_db()


# ─── API CONFIG HELPERS ────────────────────────────────────────────────────────
def get_api_config(api_id: str) -> dict:
    """Return config for an API (key + enabled). Falls back to code defaults."""
    defn = API_DEFINITIONS.get(api_id, {})
    with get_conn() as conn:
        row = conn.execute(
            "SELECT api_key, is_enabled FROM api_configs WHERE api_id=?", (api_id,)
        ).fetchone()
    if row:
        key = row["api_key"] if row["api_key"] else defn.get("default_key", "")
        return {"key": key, "enabled": bool(row["is_enabled"]), "url": defn.get("url", "")}
    return {"key": defn.get("default_key", ""), "enabled": True, "url": defn.get("url", "")}


def set_api_key(api_id: str, key: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO api_configs (api_id, api_key, is_enabled, updated_at)
               VALUES (?, ?, 1, strftime('%s','now'))
               ON CONFLICT(api_id) DO UPDATE SET api_key=excluded.api_key, updated_at=strftime('%s','now')""",
            (api_id, key),
        )


def toggle_api_enabled(api_id: str):
    cfg = get_api_config(api_id)
    new_val = 0 if cfg["enabled"] else 1
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO api_configs (api_id, api_key, is_enabled, updated_at)
               VALUES (?, '', ?, strftime('%s','now'))
               ON CONFLICT(api_id) DO UPDATE SET is_enabled=excluded.is_enabled, updated_at=strftime('%s','now')""",
            (api_id, new_val),
        )
    return bool(new_val)


def remove_api_key(api_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM api_configs WHERE api_id=?", (api_id,))


# ─── ADMIN HELPERS ─────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    with get_conn() as conn:
        row = conn.execute("SELECT user_id FROM admins WHERE user_id=?", (user_id,)).fetchone()
    return row is not None


def get_sub_admins() -> list:
    with get_conn() as conn:
        return conn.execute("SELECT user_id FROM admins ORDER BY added_at").fetchall()


# ─── SETTINGS HELPERS ──────────────────────────────────────────────────────────
def get_setting(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value):
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, str(value)))


def get_otp_earn() -> float:
    return float(get_setting("otp_earn_bdt", 0.30))


def is_leaderboard_enabled() -> bool:
    return str(get_setting("leaderboard_enabled", "1")) == "1"


def is_auto_sms_enabled() -> bool:
    return str(get_setting("auto_sms_enabled", "0")) == "1"


def get_min_withdraw() -> float:
    return float(get_setting("min_withdraw_bdt", 100.0))


def delete_setting(key: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM settings WHERE key=?", (key,))


def get_active_withdraw_methods() -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM withdraw_methods WHERE is_enabled=1 ORDER BY name"
        ).fetchall()


def get_all_withdraw_methods() -> list:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM withdraw_methods ORDER BY name").fetchall()


def get_join_channels() -> list:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM join_channels ORDER BY id").fetchall()


def is_force_join_enabled() -> bool:
    return get_setting("force_join_enabled", "0") == "1"


# ─── WALLET HELPERS ────────────────────────────────────────────────────────────
def get_wallet(user_id: int) -> sqlite3.Row:
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO wallet (user_id) VALUES (?)", (user_id,))
        return conn.execute("SELECT * FROM wallet WHERE user_id=?", (user_id,)).fetchone()


def _today_str() -> str:
    return str(date.today())


def credit_otp_earn(user_id: int):
    today = _today_str()
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO wallet (user_id) VALUES (?)", (user_id,))
        w = conn.execute("SELECT * FROM wallet WHERE user_id=?", (user_id,)).fetchone()
        if w["last_reset_date"] != today:
            conn.execute(
                "UPDATE wallet SET today_otp=0, today_income=0.0, last_reset_date=? WHERE user_id=?",
                (today, user_id),
            )
        earn = get_otp_earn()
        conn.execute("""
            UPDATE wallet SET
                balance=balance+?, total_income=total_income+?,
                today_income=today_income+?, total_otp=total_otp+1,
                today_otp=today_otp+1, last_reset_date=?
            WHERE user_id=?
        """, (earn, earn, earn, today, user_id))


def get_wallet_stats(user_id: int) -> dict:
    today = _today_str()
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO wallet (user_id) VALUES (?)", (user_id,))
        w = conn.execute("SELECT * FROM wallet WHERE user_id=?", (user_id,)).fetchone()
    if w["last_reset_date"] != today:
        return dict(
            balance=w["balance"], pending_balance=w["pending_balance"],
            total_income=w["total_income"], total_otp=w["total_otp"],
            today_otp=0, today_income=0.0,
        )
    return dict(
        balance=w["balance"], pending_balance=w["pending_balance"],
        total_income=w["total_income"], total_otp=w["total_otp"],
        today_otp=w["today_otp"], today_income=w["today_income"],
    )


def has_pending_withdraw(user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM withdraw_requests WHERE user_id=? AND status='pending'",
            (user_id,),
        ).fetchone()
    return row is not None


# ─── GENERAL HELPERS ───────────────────────────────────────────────────────────
def upsert_user(user):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO users (id, username, first_name, last_active_at)
            VALUES (?, ?, ?, strftime('%s','now'))
            ON CONFLICT(id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_active_at=strftime('%s','now')
        """, (user.id, user.username, user.first_name))
        conn.execute("INSERT OR IGNORE INTO wallet (user_id) VALUES (?)", (user.id,))


def get_user(user_id):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def extract_otp(message_text):
    # Instagram-style spaced codes: "123 456" or "1234 5678"
    spaced = re.findall(r'(?<!\d)(\d{3,4}[ \-]\d{3,4})(?!\d)', message_text)
    if spaced:
        return re.sub(r'[ \-]', '', spaced[0])
    # Standard 4-8 digit OTP code
    matches = re.findall(r'(?<!\d)(\d{4,8})(?!\d)', message_text)
    return matches[0] if matches else ""


def msg_hash(num, dt, message):
    raw = f"{num}|{dt}|{message}"
    return hashlib.sha256(raw.encode()).hexdigest()


def parse_country_info(text):
    text = text.strip()
    parts = text.split()
    if len(parts) < 3:
        return None
    flag = parts[0]
    code_raw = parts[-1].lstrip("+")
    if not code_raw.isdigit():
        return None
    name = " ".join(parts[1:-1])
    return {"flag": flag, "name": name, "code": code_raw}


def normalize_number(num: str) -> str:
    return num.strip().lstrip("+")


def parse_channel_link(link: str):
    """
    Parse a Telegram channel/group link and return (channel_id, channel_name, channel_url).
    Supports:
      https://t.me/username  → @username
      https://t.me/+invite   → invite hash (can't resolve ID without bot join)
      @username              → @username
    """
    link = link.strip()
    # Already a username
    if link.startswith("@"):
        username = link.lstrip("@")
        return f"@{username}", username, f"https://t.me/{username}"
    # t.me link
    match = re.search(r't\.me/([^/\s?]+)', link)
    if match:
        slug = match.group(1)
        if slug.startswith("+"):
            return slug, slug.lstrip("+")[:12], link
        return f"@{slug}", slug, f"https://t.me/{slug}"
    # Raw ID
    if re.match(r'^-?\d+$', link):
        return link, f"Chat_{link}", link
    return None, None, None


# ─── USER KEYBOARDS ────────────────────────────────────────────────────────────
def _shop_button_label():
    """🛒 Shop button label (shop module is optional / loaded lazily)."""
    try:
        import shop as _shop
        return _shop.SHOP_BUTTON()
    except Exception:
        return f"🛒 {stylish('Shop')}"


def welcome_keyboard(is_admin_user=False):
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    # Row 1 — primary action + Temp Mail
    kb.add(
        KeyboardButton(f"📱 {stylish('Get Number')}"),
        KeyboardButton(f"✉️ {stylish('Temp Mail')}"),
    )
    # Row 2 — Balance + custom range
    kb.add(
        KeyboardButton(f"💰 {stylish('Balance')}"),
        KeyboardButton(f"🎯 {stylish('Custom Range')}"),
    )
    # Row 3 — Support + profile
    kb.add(
        KeyboardButton(f"💬 {stylish('Support')}"),
        KeyboardButton(f"👤 {stylish('Profile')}"),
    )
    # Row 4 — live traffic + shop (Referral now lives inside Profile)
    kb.add(
        KeyboardButton(f"📈 {stylish('Traffic')}"),
        KeyboardButton(_shop_button_label()),
    )
    # Row 5 — Leaderboard (admin controlled)
    if is_leaderboard_enabled():
        kb.add(KeyboardButton(f"🏆 {stylish('Leaderboard')}"))
    if is_admin_user:
        kb.add(KeyboardButton(f"👑 {stylish('Admin Panel')}"))
    return kb


def balance_inline_keyboard():
    """Withdraw is reachable from the Balance card only."""
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton(f"🪙 {stylish('Withdraw')}", callback_data="bal_withdraw"))
    return kb


def withdraw_methods_inline_keyboard():
    methods = get_active_withdraw_methods()
    if not methods:
        return None
    kb = InlineKeyboardMarkup(row_width=2)
    for m in methods:
        kb.add(InlineKeyboardButton(m["name"], callback_data=f"wd_method:{m['name']}"))
    kb.add(InlineKeyboardButton("❎️ Cancel", callback_data="wd_cancel"))
    return kb


def withdraw_confirm_inline_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✔️ Confirm", callback_data="wd_confirm"),
        InlineKeyboardButton("❎️ Cancel", callback_data="wd_cancel"),
    )
    return kb


def join_keyboard():
    channels = get_join_channels()
    kb = InlineKeyboardMarkup(row_width=2)
    channel_buttons = []
    if channels:
        for ch in channels:
            sname = stylish(ch["channel_name"].upper())
            if ch["channel_type"] == "group":
                label = f"👥 {sname}"
            else:
                label = f"📣 {sname}"
            channel_buttons.append(InlineKeyboardButton(label, url=ch["channel_url"]))
    else:
        fallback_url = get_setting("otp_group_link", "https://t.me/")
        channel_buttons.append(InlineKeyboardButton(f"👥 {stylish('JOIN GROUP')}", url=fallback_url))
    # Add channel buttons in rows of 2
    for i in range(0, len(channel_buttons), 2):
        kb.add(*channel_buttons[i:i+2])
    kb.add(InlineKeyboardButton(f"♻ {stylish('CHECK JOIN')}", callback_data="usr_check_join"))
    return kb


def number_card_inline_keyboard(number: str = "", numbers=None):
    """Inline buttons shown below the number card.
    Row 1: COPY NUMBER (full width, native copy button)
    Row 2: CHANGE (full width)
    Row 3: COUNTRY | OTP GROUP (50/50) or CHANGE COUNTRY (full width)
    """
    otp_link = get_setting("otp_group_link", "")
    kb = InlineKeyboardMarkup(row_width=2)
    # Row 1: COPY NUMBER — native copy button if available
    number_values = list(numbers or ([] if not number else [number]))
    for index, number_value in enumerate(number_values, 1):
        safe_num = str(number_value).strip().lstrip("+")
        display = f"+{safe_num}"
        label = f"🧾 {stylish('COPY NUMBER')} {index} • {display}"
        if CopyTextButton is not None:
            try:
                kb.row(InlineKeyboardButton(label, copy_text=CopyTextButton(text=display)))
            except Exception:
                cb = f"num_copy:{safe_num}"
                if len(cb.encode()) <= 64:
                    kb.row(InlineKeyboardButton(label, callback_data=cb))
        else:
            cb = f"num_copy:{safe_num}"
            if len(cb.encode()) <= 64:
                kb.row(InlineKeyboardButton(label, callback_data=cb))
    # Row 2: CHANGE — full width always
    kb.row(InlineKeyboardButton(f"♻ {stylish('CHANGE')}", callback_data="num_change"))
    if otp_link:
        kb.row(
            InlineKeyboardButton(f"🗺 {stylish('COUNTRY')}", callback_data="num_change_country"),
            InlineKeyboardButton(f"👥 {stylish('OTP GROUP')}", url=otp_link),
        )
    else:
        kb.row(InlineKeyboardButton(f"🗺 {stylish('CHANGE COUNTRY')}", callback_data="num_change_country"))
    return kb


def user_services_inline_keyboard():
    """Services shown as InlineKeyboard (admin services on top, Other Service at bottom)."""
    with get_conn() as conn:
        services = conn.execute("""
            SELECT DISTINCT s.id, s.name FROM services s
            WHERE EXISTS (
                SELECT 1 FROM countries c
                WHERE c.service_id = s.id
                AND (
                    (c.range_id IS NOT NULL AND c.range_id != '')
                    OR c.id IN (SELECT n.country_id FROM numbers n WHERE n.assigned = 0)
                )
            )
            ORDER BY s.name
        """).fetchall()
    kb = InlineKeyboardMarkup(row_width=2)
    for svc in services:
        kb.add(InlineKeyboardButton(f"📟 {svc['name']}", callback_data=f"sel_service:{svc['id']}"))
    kb.add(InlineKeyboardButton("◀️ Back", callback_data="sel_service_back"))
    return kb


def user_services_keyboard():
    with get_conn() as conn:
        services = conn.execute("""
            SELECT DISTINCT s.id, s.name FROM services s
            WHERE EXISTS (
                SELECT 1 FROM countries c
                WHERE c.service_id = s.id
                AND (
                    (c.range_id IS NOT NULL AND c.range_id != '')
                    OR c.id IN (SELECT n.country_id FROM numbers n WHERE n.assigned = 0)
                )
            )
            ORDER BY s.name
        """).fetchall()
    if not services:
        return None
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    for svc in services:
        kb.add(KeyboardButton(f"📟 {svc['name']}"))
    kb.add(KeyboardButton("◀️ Back"))
    return kb


def user_countries_inline_keyboard(service_id):
    """Countries shown as InlineKeyboard — 3 per row."""
    with get_conn() as conn:
        countries = conn.execute("""
            SELECT c.*,
                   (SELECT COUNT(*) FROM numbers n WHERE n.country_id = c.id AND n.assigned = 0) as avail
            FROM countries c
            WHERE c.service_id = ?
            AND (
                (c.range_id IS NOT NULL AND c.range_id != '')
                OR c.id IN (SELECT n.country_id FROM numbers n WHERE n.assigned = 0)
            )
            ORDER BY c.name
        """, (service_id,)).fetchall()
    kb = InlineKeyboardMarkup(row_width=3)
    buttons = [
        InlineKeyboardButton(
            f"{c['flag']} {c['name']}",
            callback_data=f"sel_country:{c['id']}",
        )
        for c in countries
    ]
    kb.add(*buttons)
    kb.add(InlineKeyboardButton("◀️ Back", callback_data="sel_country_back"))
    return kb


# ─── ADMIN KEYBOARDS ───────────────────────────────────────────────────────────
def admin_keyboard(is_main_admin=False):
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton(f"🛠️ {stylish('Manage Services')}"),
        KeyboardButton(f"📊 {stylish('Dashboard')}"),
    )
    kb.add(
        KeyboardButton(f"🛡️ {stylish('Ban Unban')}"),
        KeyboardButton(f"📣 {stylish('Broadcast')}"),
    )
    kb.add(
        KeyboardButton(f"👥 {stylish('Users')}"),
        KeyboardButton(f"💎 {stylish('Balance Mgmt')}"),
    )
    kb.add(
        KeyboardButton(f"🪙 {stylish('Withdraw Mgmt')}"),
        KeyboardButton(f"🔧 {stylish('Settings')}"),
    )
    kb.add(KeyboardButton(f"◀️ {stylish('Back to User Panel')}"))
    if is_main_admin:
        kb.add(KeyboardButton(f"👑 {stylish('Admin Management')}"))
    return kb


def admin_management_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton(f"➕ {stylish('Add Admin')}"),
        KeyboardButton(f"👥 {stylish('View Admins')}"),
    )
    kb.add(
        KeyboardButton(f"🧹 {stylish('Remove Admin')}"),
        KeyboardButton(f"◀️ {stylish('Back to Admin')}"),
    )
    return kb


def balance_management_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton(f"🎛️ {stylish('Set OTP Earn')}"),
        KeyboardButton(f"💲 {stylish('Add Balance')}"),
    )
    kb.add(
        KeyboardButton(f"➖ {stylish('Remove Balance')}"),
        KeyboardButton(f"◀️ {stylish('Back to Admin')}"),
    )
    return kb


def withdraw_management_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton(f"➕ {stylish('Add Method')}"),
        KeyboardButton(f"🧹 {stylish('Delete Method')}"),
    )
    kb.add(
        KeyboardButton(f"🧾 {stylish('List Methods')}"),
        KeyboardButton(f"🎛️ {stylish('Set Min Withdraw')}"),
    )
    kb.add(KeyboardButton(f"◀️ {stylish('Back to Admin')}"))
    return kb


def settings_keyboard(is_main_admin=False):
    """Main Settings keyboard (Task 2)."""
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton(f"🔏 {stylish('Force Join')}"),
        KeyboardButton(f"⛓ {stylish('Others Link')}"),
    )
    kb.add(KeyboardButton(f"🗝 {stylish('API Management')}"))
    lb = "ON" if is_leaderboard_enabled() else "OFF"
    kb.add(KeyboardButton(f"🏆 {stylish('Leaderboard')}: {lb}"))
    kb.add(KeyboardButton(f"🚀 {stylish('Auto SMS')}"))
    kb.add(KeyboardButton(f"📬 {stylish('Temp Mail Domains')}"))
    kb.add(KeyboardButton(f"🗄 {stylish('Backup')}"))
    kb.add(KeyboardButton(f"◀️ {stylish('Back to Admin')}"))
    return kb


def temp_mail_admin_keyboard():
    """Temp Mail domain management sub-menu (admin)."""
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton(f"➕ {stylish('Add Mail Domain')}"),
        KeyboardButton(f"🧾 {stylish('List Mail Domains')}"),
    )
    kb.add(
        KeyboardButton(f"🔁 {stylish('Toggle Mail Domain')}"),
        KeyboardButton(f"🧹 {stylish('Del Mail Domain')}"),
    )
    kb.add(KeyboardButton(f"🧪 {stylish('Mail Provider Status')}"))
    kb.add(KeyboardButton(f"↩ {stylish('Back to Settings')}"))
    return kb


def temp_mail_provider_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    kb.add(KeyboardButton("🛰 Own Domain (IMAP)"))
    kb.add(KeyboardButton("📪 Mail.tm"))
    kb.add(KeyboardButton("📪 Mail.gw"))
    kb.add(KeyboardButton("📪 Temp-Mail.io"))
    kb.add(KeyboardButton("📪 GuerrillaMail"))
    kb.add(KeyboardButton("❎️ Cancel"))
    return kb


TEMP_MAIL_PROVIDER_CHOICES = {
    "🛰 Own Domain (IMAP)": "imap",
    "📪 Mail.tm": "mailtm",
    "📪 Mail.gw": "mailgw",
    "📪 Temp-Mail.io": "tempmailio",
    "📪 GuerrillaMail": "guerrilla",
}


def _temp_mail_domains_text():
    rows = get_temp_mail_domains()
    if not rows:
        return (
            "📬 <b>Mail Domains</b>\n\n"
            "⛔ কোনো domain যোগ করা নেই।\n"
            "এখন Get Mail free public provider থেকে address দিচ্ছে।"
        )
    lines = ["📬 <b>Mail Domains</b>\n"]
    for row in rows:
        status = "🟩" if row["is_enabled"] else "🟥"
        label = temp_mail_engine.PROVIDER_LABELS.get(row["provider"], row["provider"])
        lines.append(f"{status} <code>{_html.escape(row['domain'])}</code> — {label}")
        if row["provider"] == "imap":
            lines.append(
                f"    📥 {_html.escape(str(row['imap_host']))}:{row['imap_port']} "
                f"({_html.escape(str(row['imap_user']))})"
            )
    return "\n".join(lines)


def _temp_mail_admin_status_text():
    return (
        "📬 <b>Temp Mail Settings</b>\n\n"
        f"{_temp_mail_domains_text()}\n\n"
        "ℹ️ যোগ করা domain গুলো আগে ব্যবহার হবে; কাজ না করলে free provider fallback হবে।"
    )


def auto_sms_keyboard():
    """Auto SMS sub-menu (admin) — forwards REAL panel SMS to a group/channel."""
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    state = "OFF" if is_auto_sms_enabled() else "ON"
    kb.add(KeyboardButton(f"🚀 {stylish('Auto SMS')}: {state}"))
    kb.add(KeyboardButton(f"🗨 {stylish('Set Forward Group ID')}"))
    kb.add(KeyboardButton(f"🧹 {stylish('Del Auto SMS Group')}"))
    demo_state = "OFF" if is_demo_sms_enabled() else "ON"
    kb.add(KeyboardButton(f"🧪 {stylish('Demo SMS')}: {demo_state}"))
    kb.add(KeyboardButton(f"⏱ {stylish('Set Demo Delay')}"))
    kb.add(KeyboardButton(f"📨 {stylish('Send Demo SMS Now')}"))
    kb.add(KeyboardButton(f"↩ {stylish('Back to Settings')}"))
    return kb


def backup_keyboard():
    """Backup sub-menu — take a backup file, or restore from an uploaded file."""
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton(f"📤 {stylish('Backup File')}"),
        KeyboardButton(f"📥 {stylish('Input File')}"),
    )
    kb.add(KeyboardButton(f"↩ {stylish('Back to Settings')}"))
    return kb


def api_management_keyboard():
    """API Management sub-menu."""
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    for api_id, defn in API_DEFINITIONS.items():
        cfg = get_api_config(api_id)
        status = "🟩" if cfg["enabled"] else "🟥"
        kb.add(KeyboardButton(f"{status} {stylish(defn['name'])}"))
    kb.add(KeyboardButton(f"↩ {stylish('Back to Settings')}"))
    return kb


def api_detail_keyboard(api_id: str):
    """Detail buttons for a specific API."""
    cfg = get_api_config(api_id)
    toggle_label = f"🟥 {stylish('Disable')}" if cfg["enabled"] else f"🟩 {stylish('Enable')}"
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton(f"🗝 {stylish('Set Key')} [{api_id}]"),
        KeyboardButton(f"🧹 {stylish('Remove Key')} [{api_id}]"),
    )
    kb.add(KeyboardButton(f"{toggle_label} [{api_id}]"))
    if api_id == "zebrasms":
        kb.add(KeyboardButton(f"📡 {stylish('Live Access')} [{api_id}]"))
    kb.add(KeyboardButton(f"↩ {stylish('API Management')}"))
    return kb


def developer_keyboard():
    """Developer Info sub-menu (main admin only)."""
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton(f"✏️ {stylish('Set Dev Info')}"),
        KeyboardButton(f"🧹 {stylish('Clear Dev Info')}"),
    )
    kb.add(KeyboardButton(f"↩ {stylish('Back to Settings')}"))
    return kb


def force_join_keyboard():
    """Force Join sub-menu (Task 3)."""
    fj_status = get_setting("force_join_enabled", "0")
    fj_label = f"🟩 {stylish('Force Join: ON')}" if fj_status == "1" else f"🟥 {stylish('Force Join: OFF')}"
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton(f"➕ {stylish('Add Channel')}"),
        KeyboardButton(f"➕ {stylish('Add Group')}"),
    )
    kb.add(
        KeyboardButton(f"🧹 {stylish('Delete Channel')}"),
        KeyboardButton(f"🧹 {stylish('Delete Group')}"),
    )
    kb.add(KeyboardButton(fj_label))
    kb.add(KeyboardButton(f"↩ {stylish('Back to Settings')}"))
    return kb


def others_link_keyboard():
    """Others Link sub-menu."""
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton(f"☎ {stylish('Support Btn')}"),
        KeyboardButton(f"🧹 {stylish('Del Support')}"),
    )
    kb.add(
        KeyboardButton(f"🗨 {stylish('OTP Group Btn')}"),
        KeyboardButton(f"🧹 {stylish('Del OTP Group')}"),
    )
    kb.add(
        KeyboardButton(f"👑 {stylish('Panel Link')}"),
        KeyboardButton(f"🧹 {stylish('Del Panel Link')}"),
    )
    kb.add(
        KeyboardButton(f"⚡ {stylish('Dev Link')}"),
        KeyboardButton(f"🧹 {stylish('Del Dev Link')}"),
    )
    kb.add(
        KeyboardButton(f"📣 {stylish('Main Channel')}"),
        KeyboardButton(f"🧹 {stylish('Del Main Channel')}"),
    )
    kb.add(
        KeyboardButton(f"🤖 {stylish('Auto SMS Bot Link')}"),
        KeyboardButton(f"🧹 {stylish('Del Auto Bot Link')}"),
    )
    kb.add(
        KeyboardButton(f"📢 {stylish('Auto SMS Channel Link')}"),
        KeyboardButton(f"🧹 {stylish('Del Auto Channel Link')}"),
    )
    kb.add(
        KeyboardButton(f"🪪 {stylish('Payment Request ID')}"),
        KeyboardButton(f"🧹 {stylish('Del Payment ID')}"),
    )
    kb.add(
        KeyboardButton(f"📨 {stylish('OTP Forward ID')}"),
        KeyboardButton(f"🧹 {stylish('Del OTP Fwd')}"),
    )
    kb.add(
        KeyboardButton(f"🦾 {stylish('Bot Name')}"),
        KeyboardButton(f"🧹 {stylish('Del Bot Name')}"),
    )
    kb.add(
        KeyboardButton(f"🕷 {stylish('Powered By')}"),
        KeyboardButton(f"🧹 {stylish('Del Powered By')}"),
    )
    kb.add(KeyboardButton(f"↩ {stylish('Back to Settings')}"))
    return kb


def ban_unban_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton(f"🛑 {stylish('Ban User')}"),
        KeyboardButton(f"✔️ {stylish('Unban User')}"),
    )
    kb.add(KeyboardButton(f"◀️ {stylish('Back to Admin')}"))
    return kb


def manage_services_keyboard():
    """Simplified Manage Services keyboard (Task 1)."""
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton(f"➕ {stylish('Add Service')}"),
        KeyboardButton(f"🧹 {stylish('Delete Service')}"),
    )
    kb.add(
        KeyboardButton(f"🧾 {stylish('View Services')}"),
        KeyboardButton(f"📊 {stylish('Service Statistics')}"),
    )
    kb.add(
        KeyboardButton(f"📥 {stylish('Import Numbers')}"),
        KeyboardButton(f"🧩 {stylish('Input Range')}"),
    )
    kb.add(KeyboardButton(f"♻ {stylish('Reset Numbers')}"))
    kb.add(KeyboardButton(f"◀️ {stylish('Back to Admin')}"))
    return kb


def cancel_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    kb.add(KeyboardButton(f"⛔ {stylish('Cancel')}"))
    return kb


def _services_buttons_keyboard(back_label="◀️ Back"):
    """Shared: list all services, two per row, with a back button."""
    with get_conn() as conn:
        services = conn.execute("SELECT * FROM services ORDER BY name").fetchall()
    if not services:
        return None
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [KeyboardButton(f"📟 {svc['name']}") for svc in services]
    for i in range(0, len(buttons), 2):
        kb.add(*buttons[i:i + 2])
    kb.add(KeyboardButton(back_label))
    return kb


def services_list_keyboard():
    """List all services as reply buttons."""
    return _services_buttons_keyboard()


def delete_service_options_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    kb.add(KeyboardButton(f"🧹 {stylish('Delete Entire Service')}"))
    kb.add(KeyboardButton(f"🗂 {stylish('Show Countries')}"))
    kb.add(KeyboardButton("◀️ Back"))
    return kb


def delete_country_list_keyboard(service_id):
    with get_conn() as conn:
        countries = conn.execute(
            "SELECT * FROM countries WHERE service_id=? ORDER BY name", (service_id,)
        ).fetchall()
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    for c in countries:
        kb.add(KeyboardButton(f"{c['flag']} {c['name']} +{c['code']}"))
    kb.add(KeyboardButton("◀️ Back"))
    return kb


def delete_country_options_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    kb.add(KeyboardButton(f"🧹 {stylish('Delete Country + Numbers')}"))
    kb.add(KeyboardButton(f"🧹 {stylish('Delete Numbers Only')}"))
    kb.add(KeyboardButton("◀️ Back"))
    return kb


def confirm_keyboard_reply():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton(f"✔️ {stylish('Yes, Confirm')}"), KeyboardButton(f"⛔ {stylish('No, Cancel')}"))
    return kb


def reset_services_keyboard():
    return _services_buttons_keyboard()


def reset_countries_keyboard(service_id):
    with get_conn() as conn:
        countries = conn.execute(
            "SELECT * FROM countries WHERE service_id=? ORDER BY name", (service_id,)
        ).fetchall()
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    for c in countries:
        kb.add(KeyboardButton(f"{c['flag']} {c['name']} +{c['code']}"))
    kb.add(KeyboardButton("◀️ Back"))
    return kb


def import_service_keyboard():
    """Services keyboard for number import."""
    return _services_buttons_keyboard()


def join_channels_list_keyboard(channel_type=None):
    """List channels/groups for deletion."""
    with get_conn() as conn:
        if channel_type:
            rows = conn.execute(
                "SELECT * FROM join_channels WHERE channel_type=? ORDER BY id", (channel_type,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM join_channels ORDER BY id").fetchall()
    if not rows:
        return None
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    for ch in rows:
        kb.add(KeyboardButton(ch["channel_name"]))
    kb.add(KeyboardButton(f"⛔ {stylish('Cancel')}"))
    return kb


def withdraw_action_keyboard(request_id: int):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✔️ Approve", callback_data=f"awd_approve:{request_id}"),
        InlineKeyboardButton("⛔ Reject", callback_data=f"awd_reject:{request_id}"),
    )
    return kb


# ─── MEMBERSHIP CHECK ──────────────────────────────────────────────────────────
def _check_chat_member(chat_id, user_id) -> bool:
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status not in ("left", "kicked", "banned")
    except Exception as e:
        err = str(e).lower()
        # User is explicitly not a participant — they haven't joined
        if "user_not_participant" in err or "participant" in err or "not found" in err:
            return False
        # Any other error (bot not admin yet, network, API error) — fail open
        # so we don't block users who actually joined
        return True


def is_member(user_id):
    """Check if user has joined all required channels (if force join is enabled)."""
    if not is_force_join_enabled():
        return True
    channels = get_join_channels()
    if channels:
        return all(_check_chat_member(ch["channel_id"], user_id) for ch in channels)
    gid = get_setting("group_id", "")
    if gid:
        return _check_chat_member(gid, user_id)
    return True


# ─── MESSAGE BUILDERS ──────────────────────────────────────────────────────────
def build_number_card(flag, code, name, number, service, numbers=None):
    pw = stylish(_Developer_By())
    sep = "━" * 32
    number_values = _number_values(number, numbers)
    number_boxes = []
    for index, num_str in enumerate(number_values, 1):
        number_boxes.append(
            f"☎ {stylish('YOUR NUMBER')} {index}\n"
            f"┌{'─'*30}┐\n"
            f"│  <code>+{num_str}</code>\n"
            f"└{'─'*30}┘"
        )
    number_section = "\n\n".join(number_boxes)
    return (
        f"🟩 {sep}\n"
        f"  ✔️  {stylish('NUMBER ALLOCATED')}\n"
        f"{sep}\n\n"
        f"<blockquote>"
        f"❖ 📟 {stylish('SERVICE')}  ➤  <b>{service}</b>\n"
        f"❖ 🗺 {stylish('COUNTRY')}  ➤  {flag} <b>{name}</b>\n"
        f"❖ ⌛ {stylish('STATUS')}   ➤  🕰 {stylish('WAITING FOR OTP...')}"
        f"</blockquote>\n\n"
        f"{number_section}\n"
        f"<i>👆 Tap either number above to copy it instantly</i>\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔷 {stylish('POWERED BY')} <b>{pw}</b>"
    )


def build_otp_card(flag, code, name, number, service, otp_msg, otp_code):
    safe_otp = _html.escape(str(otp_code)) if otp_code else "N/A"
    safe_msg = _html.escape(str(otp_msg)) if otp_msg else "N/A"
    pw = stylish(_Developer_By())
    sep = "━" * 32
    return (
        f"⚡ {sep}\n"
        f"  🧿  {stylish('NEW OTP RECEIVED')}\n"
        f"{sep}\n\n"
        f"<blockquote>"
        f"❖ 📟 {stylish('NUMBER')}   ➤  <code>+{number}</code>\n"
        f"❖ 🔢 {stylish('OTP CODE')} ➤  <code>{safe_otp}</code>"
        f"</blockquote>\n\n"
        f"📬 {stylish('FULL MESSAGE')}\n"
        f"<blockquote>{safe_msg}</blockquote>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔷 {stylish('POWERED BY')} <b>{pw}</b>"
    )


def build_otp_card_with_balance(flag, code, name, number, service, otp_msg, otp_code, balance: float):
    safe_otp = _html.escape(str(otp_code)) if otp_code else "N/A"
    safe_msg = _html.escape(str(otp_msg)) if otp_msg else "N/A"
    pw = stylish(_Developer_By())
    earn = get_otp_earn()
    sep = "━" * 32
    return (
        f"⚡ {sep}\n"
        f"  🧿  {stylish('NEW OTP RECEIVED')}\n"
        f"{sep}\n\n"
        f"<blockquote>"
        f"❖ 📟 {stylish('NUMBER')}    ➤  <code>+{number}</code>\n"
        f"❖ 🔢 {stylish('OTP CODE')}  ➤  <code>{safe_otp}</code>\n"
        f"❖ 💲 {stylish('EARNED')}    ➤  +{earn:.2f} BDT\n"
        f"❖ 🏦 {stylish('BALANCE')}   ➤  {balance:.2f} BDT"
        f"</blockquote>\n\n"
        f"📬 {stylish('FULL MESSAGE')}\n"
        f"<blockquote>{safe_msg}</blockquote>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔷 {stylish('POWERED BY')} <b>{pw}</b>"
    )


def build_timeout_card(flag, code, name, number, service, numbers=None):
    """Same visual design as build_number_card, but shows the NO OTP RECEIVED / timed-out state."""
    pw = stylish(_Developer_By())
    sep = "━" * 32
    number_values = _number_values(number, numbers)
    number_boxes = []
    for index, num_str in enumerate(number_values, 1):
        number_boxes.append(
            f"❖ ☎ {stylish('NUMBER')} {index} ➤ <code>+{num_str}</code>"
        )
    number_section = "\n".join(number_boxes)
    return (
        f"🟥 {sep}\n"
        f"  ⛔  {stylish('NO OTP RECEIVED')}\n"
        f"{sep}\n\n"
        f"<blockquote>"
        f"❖ 📟 {stylish('SERVICE')}  ➤  <b>{service}</b>\n"
        f"❖ 🗺 {stylish('COUNTRY')}  ➤  {flag} <b>{name}</b>\n"
        f"{number_section}\n"
        f"❖ ⌛ {stylish('STATUS')}   ➤  ❗️ {stylish('TIMED OUT')}"
        f"</blockquote>\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔷 {stylish('POWERED BY')} <b>{pw}</b>"
    )


def otp_card_keyboard(otp_code):
    """Inline keyboard below an OTP card sent to the USER — COPY OTP button."""
    kb = InlineKeyboardMarkup(row_width=1)
    safe = str(otp_code).strip() if otp_code else ""
    if safe and safe.isdigit() and len(safe) <= 8:
        label = f"🧾 {stylish('COPY OTP')} : {safe}"
        if CopyTextButton is not None:
            try:
                kb.add(InlineKeyboardButton(label, copy_text=CopyTextButton(text=safe)))
                return kb
            except Exception as e:
                logger.warning(f"CopyTextButton unsupported, falling back: {e}")
        cb = f"otp_copy:{safe}"
        if len(cb.encode()) <= 64:
            kb.add(InlineKeyboardButton(label, callback_data=cb))
            return kb
    return None


def normalize_link(raw: str) -> str:
    """Turn any admin-entered contact value into a valid Telegram-safe https URL."""
    link = str(raw or "").strip()
    if not link:
        return ""
    if link.startswith("@"):
        return "https://t.me/" + link[1:].strip()
    if link.startswith(("http://", "https://", "tg://")):
        return link
    if re.fullmatch(r"[A-Za-z0-9_]{4,64}", link):
        return "https://t.me/" + link
    return "https://" + link.lstrip("/")


def _send_support_message(chat_id):
    """Send the Support card. Always answers the user, even if no link is set."""
    link = normalize_link(get_setting("support_link", ""))
    admin_uname = str(get_setting("support_username", "") or "").strip()
    if not link and admin_uname:
        link = normalize_link(admin_uname)
    text_body = (
        f"☎ <b>{stylish('Support')}</b>\n\n"
        f"<blockquote>{stylish('Need help? Tap the button below to contact our support team.')}</blockquote>"
    )
    if not link:
        bot.send_message(
            chat_id,
            f"☎ <b>{stylish('Support')}</b>\n\n"
            f"<blockquote>{stylish('Support link is not configured yet. Please try again later.')}</blockquote>",
        )
        return
    kb = InlineKeyboardMarkup()
    try:
        kb.add(InlineKeyboardButton(f"🔵 🛡️ {stylish('Contact Support')}", url=link))
        bot.send_message(chat_id, text_body, reply_markup=kb)
    except Exception as e:
        logger.warning(f"Support button error: {e}")
        bot.send_message(chat_id, f"☎ <b>{stylish('Support')}:</b> {_html.escape(link)}")



def _resolve_panel_link():
    return normalize_link(get_setting("panel_link", "") or get_setting("number_panel_link", "") or get_setting("bot_link", "") or "https://t.me")

def _resolve_channel_link():
    return normalize_link(get_setting("main_channel_link", "") or get_setting("auto_sms_channel_link", "") or get_setting("channel_link", "") or "https://t.me")

def otp_group_keyboard():
    """Buttons shown on forwarded OTP cards: GO TO PANEL and GO TO CHANNEL."""
    panel_link = _resolve_panel_link()
    channel_link = _resolve_channel_link()
    kb = InlineKeyboardMarkup(row_width=2)
    btn_panel = InlineKeyboardButton("🌐 GO TO PANEL", url=panel_link if panel_link.startswith("http") else "https://t.me")
    btn_channel = InlineKeyboardButton("📢 GO TO CHANNEL", url=channel_link if channel_link.startswith("http") else "https://t.me")
    kb.row(btn_panel, btn_channel)
    return kb


def build_balance_text(stats: dict) -> str:
    earn = get_otp_earn()
    min_wd = get_min_withdraw()
    sep = "━" * 28
    return (
        f"💲 {sep}\n"
        f"   💎  {stylish('MY WALLET')}\n"
        f"{sep}\n\n"
        f"<blockquote>"
        f"❖ 📊 {stylish('Today OTP')}     ➤  <b>{stats['today_otp']}</b>\n"
        f"❖ 📈 {stylish('Total OTP')}     ➤  <b>{stats['total_otp']}</b>\n"
        f"❖ 💵 {stylish('Per OTP Earn')}  ➤  <b>{earn:.2f} BDT</b>\n\n"
        f"❖ 💲 {stylish('Today Income')}  ➤  <b>{stats['today_income']:.2f} BDT</b>\n"
        f"❖ 🏦 {stylish('Total Income')}  ➤  <b>{stats['total_income']:.2f} BDT</b>\n\n"
        f"❖ 🪪 {stylish('Balance')}       ➤  <b>{stats['balance']:.2f} BDT</b>\n"
        f"❖ 🔏 {stylish('Min Withdraw')}  ➤  <b>{min_wd:.0f} BDT</b>"
        f"</blockquote>\n"
        f"━━━━━━━━━━━━━━━━━━"
    )


def _row_value(row, key, default=None):
    """Safely read a column from a sqlite3.Row / dict / None."""
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def build_profile_text(user, db_user, stats):
    sep = "━" * 28
    joined_ts = _row_value(db_user, "joined_at", 0)
    try:
        joined = datetime.fromtimestamp(float(joined_ts)).strftime("%d %b %Y") if joined_ts else "N/A"
    except (ValueError, OSError, OverflowError, TypeError):
        joined = "N/A"
    return (
        f"🧑 {sep}\n"
        f"   🌟  {stylish('MY PROFILE')}\n"
        f"{sep}\n\n"
        f"<blockquote>"
        f"❖ 🏷 {stylish('Name')}       ➤  <b>{user.first_name or 'N/A'}</b>\n"
        f"❖ 🆔 {stylish('User ID')}    ➤  <code>{user.id}</code>\n"
        f"❖ 📛 {stylish('Username')}   ➤  @{user.username or 'N/A'}\n"
        f"❖ 📅 {stylish('Joined')}     ➤  {joined}\n\n"
        f"❖ ☎ {stylish('Numbers')}    ➤  <b>{_row_value(db_user, 'numbers_generated', 0)}</b>\n"
        f"❖ 🔢 {stylish('OTPs Got')}   ➤  <b>{_row_value(db_user, 'otps_received', 0)}</b>\n\n"
        f"❖ 🪪 {stylish('Balance')}    ➤  <b>{stats['balance']:.2f} BDT</b>\n"
        f"❖ 🏦 {stylish('Income')}     ➤  <b>{stats['total_income']:.2f} BDT</b>"
        f"</blockquote>\n"
        f"━━━━━━━━━━━━━━━━━━"
    )


# ─── SHARED ACTION HELPERS ─────────────────────────────────────────────────────
def _show_api_detail(chat_id, api_id: str):
    """Show detail info for one API with manage buttons."""
    defn = API_DEFINITIONS.get(api_id, {})
    cfg = get_api_config(api_id)
    status = "🟩 ON" if cfg["enabled"] else "🟥 OFF"
    key_preview = cfg["key"][:12] + "..." if len(cfg["key"]) > 12 else (cfg["key"] or "Using default key from code")
    bot.send_message(
        chat_id,
        f"🗝 <b>{defn.get('name', api_id)}</b>\n\nStatus: <b>{status}</b>\n"
        f"🗝 Key: <code>{key_preview}</code>",
        reply_markup=api_detail_keyboard(api_id),
    )


def _show_dashboard(chat_id, is_main_admin=False):
    with get_conn() as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_nums = conn.execute("SELECT SUM(numbers_generated) FROM users").fetchone()[0] or 0
        total_otps = conn.execute("SELECT COUNT(*) FROM otps").fetchone()[0] or 0
        active_24h = conn.execute(
            "SELECT COUNT(*) FROM users WHERE last_active_at >= strftime('%s','now') - 86400"
        ).fetchone()[0]
        top_users = conn.execute(
            "SELECT u.first_name, w.total_otp FROM users u "
            "LEFT JOIN wallet w ON u.id=w.user_id ORDER BY w.total_otp DESC LIMIT 3"
        ).fetchall()
        pending_wd = conn.execute(
            "SELECT COUNT(*) FROM withdraw_requests WHERE status='pending'"
        ).fetchone()[0]
    medals = ["🥇", "🥈", "🥉"]
    top_str = ""
    for i, u in enumerate(top_users):
        top_str += f"\n{medals[i]} {u['first_name'] or 'User'} — {u['total_otp'] or 0} OTPs"
    sep = "━" * 30
    bot.send_message(
        chat_id,
        f"📊 {sep}\n"
        f"   👑  {stylish('ADMIN DASHBOARD')}\n"
        f"{sep}\n\n"
        f"<blockquote>"
        f"❖ 👥 {stylish('Total Users')}    ➤  <b>{total_users}</b>\n"
        f"❖ ☎ {stylish('Numbers Given')}  ➤  <b>{total_nums}</b>\n"
        f"❖ 🔢 {stylish('OTPs Received')}  ➤  <b>{total_otps}</b>\n"
        f"❖ 🟩 {stylish('Active 24h')}     ➤  <b>{active_24h}</b>\n"
        f"❖ 🪙 {stylish('Pending WD')}     ➤  <b>{pending_wd}</b>"
        f"</blockquote>\n\n"
        f"🏆 <b>{stylish('TOP OTP EARNERS')}</b>{top_str}\n\n"
        f"━━━━━━━━━━━━━━━━━━",
        reply_markup=admin_keyboard(is_main_admin=is_main_admin),
    )


def _show_service_stats(chat_id):
    with get_conn() as conn:
        total_services = conn.execute("SELECT COUNT(*) FROM services").fetchone()[0]
        total_countries = conn.execute("SELECT COUNT(*) FROM countries").fetchone()[0]
        total_numbers = conn.execute("SELECT COUNT(*) FROM numbers").fetchone()[0]
        used_numbers = conn.execute("SELECT COUNT(*) FROM numbers WHERE assigned=1").fetchone()[0]
        avail_numbers = total_numbers - used_numbers
        active_alloc = conn.execute(
            "SELECT COUNT(*) FROM allocations WHERE otp_received=0 AND timed_out=0"
        ).fetchone()[0]
        total_otps = conn.execute("SELECT COUNT(*) FROM otps").fetchone()[0]
        services = conn.execute("SELECT * FROM services ORDER BY name").fetchall()
        breakdown = ""
        for svc in services:
            s_countries = conn.execute(
                "SELECT COUNT(*) FROM countries WHERE service_id=?", (svc["id"],)
            ).fetchone()[0]
            s_total = conn.execute(
                "SELECT COUNT(*) FROM numbers WHERE country_id IN (SELECT id FROM countries WHERE service_id=?)",
                (svc["id"],),
            ).fetchone()[0]
            s_used = conn.execute(
                "SELECT COUNT(*) FROM numbers WHERE assigned=1 AND country_id IN (SELECT id FROM countries WHERE service_id=?)",
                (svc["id"],),
            ).fetchone()[0]
            s_avail = s_total - s_used
            breakdown += (
                f"\n<b>{svc['name']}</b>: {s_countries} countries | "
                f"{s_avail} avail / {s_used} used / {s_total} total"
            )
    bot.send_message(
        chat_id,
        "<blockquote>📊 SERVICE STATISTICS</blockquote>\n\n"
        f"📟 <b>Total Services:</b> {total_services}\n"
        f"🗺 <b>Total Countries:</b> {total_countries}\n\n"
        f"☎ <b>Total Numbers:</b> {total_numbers}\n"
        f"✔️ <b>Used Numbers:</b> {used_numbers}\n"
        f"🟩 <b>Available Numbers:</b> {avail_numbers}\n\n"
        f"⌛ <b>Active Allocations:</b> {active_alloc}\n"
        f"🔐 <b>Total OTPs Delivered:</b> {total_otps}"
        f"\n\n{'─'*30}{breakdown}",
        reply_markup=manage_services_keyboard(),
    )


def _send_admin_live_panel(chat_id):
    """Send the live active-allocations panel to admin when they do /start."""
    sep = "━" * 30
    with get_conn() as conn:
        active = conn.execute("""
            SELECT a.number, a.service_name, a.country_flag, a.country_name,
                   a.allocated_at, a.otp_received, a.otp_text,
                   u.first_name, u.username, u.id as uid
            FROM allocations a
            LEFT JOIN users u ON u.id = a.user_id
            WHERE a.otp_received = 0 AND (a.timed_out IS NULL OR a.timed_out = 0)
            ORDER BY a.allocated_at DESC
        """).fetchall()
    lines = [
        f"📡 {sep}",
        f"   🟥 {stylish('LIVE ACTIVE NUMBERS')}",
        f"{sep}\n",
    ]
    if active:
        for a in active:
            fname = _html.escape(a["first_name"] or "User")
            uname = ("@" + a["username"]) if a["username"] else f"ID:{a['uid']}"
            flag = a["country_flag"] or "🛰"
            t = datetime.fromtimestamp(a["allocated_at"]).strftime("%d/%m %H:%M") if a["allocated_at"] else "N/A"
            num = str(a["number"])
            lines.append(
                f"🧑 <b>{fname}</b>  <i>({uname})</i>\n"
                f"   ☎ <code>+{num}</code>  {flag} {_html.escape(a['country_name'] or '')}\n"
                f"   📟 {_html.escape(a['service_name'] or 'N/A')}  🕰 {t}\n"
                f"   ⌛ <i>Waiting for OTP...</i>"
            )
    else:
        lines.append("   <i>এখন কোনো active number নেই।</i>")
    lines += [
        f"\n{sep}",
        f"🔷 {stylish('POWERED BY')} <b>{stylish(_Developer_By())}</b>",
        "\n<i>⚡ যখনই কেউ number নেবে, instant notification আসবে।</i>",
    ]
    try:
        bot.send_message(chat_id, "\n".join(lines))
    except Exception as e:
        logger.warning(f"Live panel send error: {e}")


def _notify_admin_live(user_id, fname, uname, number, service, flag, country):
    """Instant live notification to all admins in admin_live_mode when a number is taken."""
    if not admin_live_mode:
        return
    sep = "━" * 28
    num_str = str(number)
    uname_display = ("@" + uname) if uname else f"ID:{user_id}"
    text = (
        f"🚨 {sep}\n"
        f"   📲 {stylish('NUMBER TAKEN — LIVE')}\n"
        f"{sep}\n\n"
        f"<blockquote>"
        f"🧑 {stylish('USER')}     ➤  <b>{_html.escape(fname)}</b>  <i>({_html.escape(uname_display)})</i>\n"
        f"📟 {stylish('SERVICE')}  ➤  {_html.escape(service)}\n"
        f"🗺 {stylish('COUNTRY')} ➤  {flag} {_html.escape(country)}\n"
        f"⌛ {stylish('STATUS')}  ➤  🕰 Waiting for OTP..."
        f"</blockquote>\n\n"
        f"☎ {stylish('NUMBER')}\n"
        f"┌{'─'*26}┐\n"
        f"│  <code>+{num_str}</code>\n"
        f"└{'─'*26}┘\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    for admin_id in list(admin_live_mode):
        try:
            sent = bot.send_message(admin_id, text)
            # Track msg_id per number per admin for later OTP update
            if num_str not in admin_live_msg_ids:
                admin_live_msg_ids[num_str] = {}
            admin_live_msg_ids[num_str][admin_id] = sent.message_id
        except Exception as e:
            logger.warning(f"Admin live notify error (admin {admin_id}): {e}")


def _notify_admin_live_otp(number, otp_code, msg_text, fname, uname, user_id, service, flag, country):
    """When OTP arrives: delete old 'number taken' admin message, send new OTP notification."""
    if not admin_live_mode:
        return
    num_str = str(number).lstrip("+")
    sep = "━" * 28
    safe_otp = _html.escape(str(otp_code)) if otp_code else "N/A"
    safe_msg = _html.escape(str(msg_text)) if msg_text else "N/A"
    uname_display = ("@" + uname) if uname else f"ID:{user_id}"
    otp_text = (
        f"✔️ {sep}\n"
        f"   🧿 {stylish('OTP RECEIVED — LIVE')}\n"
        f"{sep}\n\n"
        f"<blockquote>"
        f"🧑 {stylish('USER')}     ➤  <b>{_html.escape(fname)}</b>  <i>({_html.escape(uname_display)})</i>\n"
        f"📟 {stylish('SERVICE')}  ➤  {_html.escape(service)}\n"
        f"🗺 {stylish('COUNTRY')} ➤  {flag} {_html.escape(country)}\n"
        f"☎ {stylish('NUMBER')}  ➤  <code>+{num_str}</code>"
        f"</blockquote>\n\n"
        f"🔢 {stylish('OTP CODE')}\n"
        f"┌{'─'*26}┐\n"
        f"│  <code>{safe_otp}</code>\n"
        f"└{'─'*26}┘\n\n"
        f"📬 {stylish('FULL SMS')}\n"
        f"<blockquote>{safe_msg}</blockquote>\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    prev_msgs = admin_live_msg_ids.pop(num_str, {})
    for admin_id in list(admin_live_mode):
        # Delete old "number taken" message for this admin
        old_msg_id = prev_msgs.get(admin_id)
        if old_msg_id:
            try:
                bot.delete_message(admin_id, old_msg_id)
            except Exception:
                pass
        # Send fresh OTP notification
        try:
            bot.send_message(admin_id, otp_text)
        except Exception as e:
            logger.warning(f"Admin live OTP notify error (admin {admin_id}): {e}")


def _send_welcome(chat_id, first_name, user_id):
    """Send welcome message with reply keyboard."""
    bn = stylish(_bot_name())
    pw = stylish(_Developer_By())
    sep = "━" * 32
    earn = get_otp_earn()
    bot.send_message(
        chat_id,
        f"🌟 {sep}\n"
        f"   👋  {stylish('WELCOME')} {stylish(first_name.upper())}\n"
        f"{sep}\n\n"
        f"<blockquote>"
        f"🦾 {stylish('BOT')}    ➤  {bn}\n"
        f"💲 {stylish('PER OTP')} ➤  {earn:.2f} BDT\n"
        f"📲 {stylish('GET NUM')} ➤  যেকোনো service এর নম্বর\n"
        f"🔢 {stylish('OTP')}    ➤  Auto deliver হবে"
        f"</blockquote>\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔷 {stylish('POWERED BY')} <b>{pw}</b>",
        reply_markup=welcome_keyboard(is_admin_user=is_admin(user_id)),
    )


# ─── NUMBER ASSIGNMENT ────────────────────────────────────────────────────────
def assign_number_to_user(user_id, chat_id, country_id):
    if not is_member(user_id):
        bot.send_message(
            chat_id,
            _join_prompt_text(),
            reply_markup=join_keyboard(),
        )
        return

    with get_conn() as conn:
        country = conn.execute("SELECT * FROM countries WHERE id=?", (country_id,)).fetchone()
        if not country:
            bot.send_message(
                chat_id, f"⛔ {stylish('Country not found.')}",
                reply_markup=welcome_keyboard(is_admin_user=is_admin(user_id)),
            )
            return
        service = conn.execute("SELECT * FROM services WHERE id=?", (country["service_id"],)).fetchone()

    range_id = (country["range_id"] or "").strip()
    if range_id:
        _assign_range_number_to_user(user_id, chat_id, country, service, range_id)
        return

    with get_conn() as conn:
        number_rows = conn.execute(
            "SELECT * FROM numbers WHERE country_id=? AND assigned=0 ORDER BY id LIMIT 1",
            (country_id,),
        ).fetchall()
        if len(number_rows) < 1:
            bot.send_message(
                chat_id, f"⛔ {stylish('No number is available for this country.')}",
                reply_markup=welcome_keyboard(is_admin_user=is_admin(user_id)),
            )
            return

        for number_row in number_rows:
            conn.execute(
                "UPDATE numbers SET assigned=1, assigned_to=?, assigned_at=strftime('%s','now') WHERE id=?",
                (user_id, number_row["id"]),
            )

        text = build_number_card(
            country["flag"], country["code"], country["name"],
            number_rows[0]["number"], service["name"],
            numbers=[row["number"] for row in number_rows],
        )
        msg = bot.send_message(
            chat_id,
            text,
            reply_markup=number_card_inline_keyboard(
                numbers=[row["number"] for row in number_rows]
            ),
        )

        for number_row in number_rows:
            conn.execute("""
                INSERT INTO allocations
                (user_id, number_id, number, service_name, country_name,
                 country_flag, country_code, message_id)
                VALUES (?,?,?,?,?,?,?,?)
            """, (
                user_id, number_row["id"], number_row["number"],
                service["name"], country["name"], country["flag"],
                country["code"], msg.message_id,
            ))
        conn.execute(
            "UPDATE users SET numbers_generated=numbers_generated+1 WHERE id=?",
            (user_id,),
        )
        _u = conn.execute("SELECT first_name, username FROM users WHERE id=?", (user_id,)).fetchone()
        _fn = (_u["first_name"] if _u else None) or "User"
        _un = (_u["username"] if _u else None)
    for number_row in number_rows:
        _notify_admin_live(
            user_id, _fn, _un,
            number_row["number"], service["name"],
            country["flag"], country["name"],
        )


def _assign_range_number_to_user(user_id, chat_id, country, service, range_id):
    """Allocate a number dynamically via YesMS for an admin-configured range-based country."""
    loading_msg = bot.send_message(
        chat_id, f"⌛ {stylish('Getting number for')} <b>{_html.escape(country['name'])}</b>...",
    )
    full_numbers = fetch_api_numbers(range_id)
    if len(full_numbers) < 1:
        retry_kb = InlineKeyboardMarkup()
        retry_kb.add(InlineKeyboardButton(f"♻ {stylish('Try Again')}", callback_data=f"custom_range_retry:{range_id}"))
        try:
            bot.edit_message_text(
                f"⛔ {stylish('No number is available for')} {_html.escape(country['name'])}. {stylish('Please try again later.')}",
                chat_id=chat_id,
                message_id=loading_msg.message_id,
                reply_markup=retry_kb,
            )
        except Exception:
            pass
        return

    full_number = full_numbers[0]
    text_card = build_number_card(
        country["flag"], country["code"], country["name"], full_number,
        service["name"], numbers=full_numbers,
    )
    final_message_id = loading_msg.message_id
    try:
        bot.edit_message_text(
            text_card, chat_id=chat_id, message_id=loading_msg.message_id,
            reply_markup=number_card_inline_keyboard(numbers=full_numbers),
        )
    except Exception:
        sent = bot.send_message(chat_id, text_card, reply_markup=number_card_inline_keyboard(numbers=full_numbers))
        final_message_id = sent.message_id

    with get_conn() as conn:
        for full_number in full_numbers:
            conn.execute(
                """INSERT INTO allocations
                   (user_id, number_id, number, service_name, country_name,
                    country_flag, country_code, message_id, rid)
                   VALUES (?,NULL,?,?,?,?,?,?,?)""",
                (
                    user_id, full_number, service["name"], country["name"],
                    country["flag"], country["code"], final_message_id, range_id,
                ),
            )
        conn.execute(
            "UPDATE users SET numbers_generated=numbers_generated+1 WHERE id=?",
            (user_id,),
        )
        _u2 = conn.execute("SELECT first_name, username FROM users WHERE id=?", (user_id,)).fetchone()
        _fn2 = (_u2["first_name"] if _u2 else None) or "User"
        _un2 = (_u2["username"] if _u2 else None)
    _notify_admin_live(
        user_id, _fn2, _un2,
        full_numbers[0], service["name"],
        country["flag"], country["name"],
    )


# ─── OTP DELIVERY ─────────────────────────────────────────────────────────────
def _deliver_otp(alloc, msg_text: str, msg_id: int):
    """Deliver OTP from a group message to the waiting user."""
    number = normalize_number(alloc["number"])
    mhash = msg_hash(number, str(msg_id), msg_text)

    with get_conn() as conn:
        dup = conn.execute(
            "SELECT id FROM delivered_messages WHERE msg_hash=?", (mhash,)
        ).fetchone()
        if dup:
            return False

    credit_otp_earn(alloc["user_id"])
    stats = get_wallet_stats(alloc["user_id"])
    otp_code = extract_otp(msg_text)
    otp_card = build_otp_card_with_balance(
        alloc["country_flag"], alloc["country_code"], alloc["country_name"],
        alloc["number"], alloc["service_name"], msg_text, otp_code, stats["balance"],
    )
    sent = False
    try:
        bot.send_message(alloc["user_id"], otp_card)
        sent = True
    except Exception as e2:
        logger.warning(f"OTP delivery error: {e2}")

    if sent:
        with get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO delivered_messages (msg_hash, number, message, user_id) VALUES (?,?,?,?)",
                (mhash, number, msg_text, alloc["user_id"]),
            )
            conn.execute(
                "INSERT OR IGNORE INTO otps (msg_hash, user_id, number, message, otp_code, cli) VALUES (?,?,?,?,?,?)",
                (mhash, alloc["user_id"], number, msg_text, otp_code, ""),
            )
            conn.execute("UPDATE allocations SET otp_received=1, otp_text=? WHERE id=?", (msg_text, alloc["id"]))
            conn.execute("UPDATE users SET otps_received=otps_received+1 WHERE id=?", (alloc["user_id"],))
            _u = conn.execute("SELECT first_name, username FROM users WHERE id=?", (alloc["user_id"],)).fetchone()
            _fn = (_u["first_name"] if _u else None) or "User"
            _un = (_u["username"] if _u else None)
        _forward_otp(msg_text, alloc["number"], dict(alloc))
        _notify_admin_live_otp(
            number, otp_code, msg_text,
            _fn, _un, alloc["user_id"],
            alloc.get("service_name", ""), alloc.get("country_flag", ""), alloc.get("country_name", ""),
        )
        return True
    return False


def _deliver_otp_api(alloc, msg_text: str, dt: str, mhash_val: str):
    """Deliver OTP received from API polling.

    Note: We no longer block delivery of the same OTP code — when a service
    resends the same code, the user should receive it again.
    """
    number = normalize_number(alloc["number"])

    credit_otp_earn(alloc["user_id"])
    stats = get_wallet_stats(alloc["user_id"])
    otp_code = extract_otp(msg_text)
    otp_card = build_otp_card_with_balance(
        alloc["country_flag"], alloc["country_code"], alloc["country_name"],
        alloc["number"], alloc["service_name"], msg_text, otp_code, stats["balance"],
    )
    sent = False
    try:
        bot.send_message(alloc["user_id"], otp_card)
        sent = True
    except Exception as e2:
        logger.warning(f"API OTP delivery error: {e2}")

    if sent:
        with get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO delivered_messages (msg_hash, number, message, user_id) VALUES (?,?,?,?)",
                (mhash_val, number, msg_text, alloc["user_id"]),
            )
            conn.execute(
                "INSERT OR IGNORE INTO otps (msg_hash, user_id, number, message, otp_code, cli) VALUES (?,?,?,?,?,?)",
                (mhash_val, alloc["user_id"], number, msg_text, otp_code, "api"),
            )
            conn.execute("UPDATE allocations SET otp_received=1, otp_text=? WHERE id=?", (msg_text, alloc["id"]))
            conn.execute("UPDATE users SET otps_received=otps_received+1 WHERE id=?", (alloc["user_id"],))
            _u = conn.execute("SELECT first_name, username FROM users WHERE id=?", (alloc["user_id"],)).fetchone()
            _fn = (_u["first_name"] if _u else None) or "User"
            _un = (_u["username"] if _u else None)
        _forward_otp(msg_text, alloc["number"], dict(alloc))
        _notify_admin_live_otp(
            number, otp_code, msg_text,
            _fn, _un, alloc["user_id"],
            alloc.get("service_name", ""), alloc.get("country_flag", ""), alloc.get("country_name", ""),
        )
        return True
    return False


def get_unified_forward_chat_id() -> str:
    """Return the single configured destination chat ID/username for both real OTP and Demo/Auto SMS."""
    fwd = str(get_setting("otp_forward_chat_id", "") or "").strip()
    if fwd and fwd != "Not set":
        return fwd
    auto = str(get_setting("auto_sms_chat_id", "") or "").strip()
    if auto and auto != "Not set":
        return auto
    return ""


def set_unified_forward_chat_id(chat_id_val: str):
    """Save forward chat ID to both settings so real OTP and Demo SMS share the exact same destination."""
    v = str(chat_id_val).strip()
    set_setting("otp_forward_chat_id", v)
    set_setting("auto_sms_chat_id", v)


def delete_unified_forward_chat_id():
    """Delete forward chat ID from all settings."""
    delete_setting("otp_forward_chat_id")
    delete_setting("auto_sms_chat_id")


_auto_sms_seen = set()
_auto_sms_lock = threading.Lock()


def _auto_sms_status_text() -> str:
    state = "🟩 ON" if is_auto_sms_enabled() else "🟥 OFF"
    chat = get_unified_forward_chat_id() or "Not set"
    demo = "🟩 ON" if is_demo_sms_enabled() else "🟥 OFF"
    delay = fmt_delay(get_demo_sms_delay())
    return (
        f"🚀 <b>{stylish('Auto SMS & Demo SMS')}</b>\n\n"
        f"Status: <b>{state}</b>\n"
        f"🗨 Forward Group/Channel ID: <code>{chat}</code>\n"
        f"🧪 Demo SMS: <b>{demo}</b>\n"
        f"⏱ Demo Delay: <b>{delay}</b>  (min 3 sec, max 60 min)\n\n"
        f"<i>Every real OTP and every Demo SMS will be forwarded to this exact same group with "
        f"GO TO PANEL and GO TO CHANNEL buttons.</i>"
    )


def auto_sms_inline_keyboard():
    """Buttons under an Auto SMS card: GO TO PANEL + GO TO CHANNEL."""
    panel_link = _resolve_panel_link()
    channel_link = _resolve_channel_link()
    kb = InlineKeyboardMarkup(row_width=2)
    btn_panel = InlineKeyboardButton("🌐 GO TO PANEL", url=panel_link if panel_link.startswith("http") else "https://t.me")
    btn_channel = InlineKeyboardButton("📢 GO TO CHANNEL", url=channel_link if channel_link.startswith("http") else "https://t.me")
    kb.row(btn_panel, btn_channel)
    return kb


# ─── AUTO SMS ENGINE (top range / top country of the connected panel) ─────────
_auto_sms_targets = {"ranges": [], "countries": set(), "ts": 0.0}
_auto_sms_targets_lock = threading.Lock()
AUTO_SMS_TARGET_TTL = 180          # refresh top range/country every 3 min
AUTO_SMS_ENGINE_INTERVAL = 45      # allocate on a top range this often


def _auto_sms_panel_ids() -> list:
    """Every SMS panel the admin actually added (key saved + enabled)."""
    return [
        pid for pid in ("zebrasms", "yesms", "stexsms", "fastxotps", "voltxsms")
        if get_api_config(pid)["enabled"] and get_api_config(pid)["key"]
    ]


def _refresh_auto_sms_targets(force: bool = False):
    """Work out the TOP ranges + TOP countries of the connected panels."""
    now = time.time()
    with _auto_sms_targets_lock:
        if not force and now - _auto_sms_targets["ts"] < AUTO_SMS_TARGET_TTL:
            return _auto_sms_targets["ranges"], _auto_sms_targets["countries"]
    range_counts, country_counts = {}, {}
    for panel_id in _auto_sms_panel_ids():
        try:
            for rng, country_raw in _build_traffic_rows_from_panel(panel_id):
                rng_digits = re.sub(r"[Xx\s]+$", "", str(rng or ""))
                if rng_digits:
                    range_counts[rng_digits] = range_counts.get(rng_digits, 0) + 1
                _flag, cname = extract_flag_from_name(str(country_raw or ""))
                if cname:
                    country_counts[cname] = country_counts.get(cname, 0) + 1
        except Exception as e:
            logger.warning("Auto SMS target scan failed (%s): %s", panel_id, e)
    top_ranges = [r for r, _c in sorted(range_counts.items(), key=lambda x: -x[1])[:12]]
    top_countries = {c for c, _n in sorted(country_counts.items(), key=lambda x: -x[1])[:6]}
    with _auto_sms_targets_lock:
        _auto_sms_targets["ranges"] = top_ranges
        _auto_sms_targets["countries"] = top_countries
        _auto_sms_targets["ts"] = now
    logger.info("Auto SMS targets → ranges=%s countries=%s", top_ranges[:5], list(top_countries)[:5])
    return top_ranges, top_countries


def _auto_sms_matches_top(number: str) -> bool:
    """True when the SMS belongs to a top range / top country of the panel."""
    ranges, countries = _refresh_auto_sms_targets()
    if not ranges and not countries:
        return True                      # nothing learned yet → forward everything
    num = normalize_number(number)
    for rng in ranges:
        if num.startswith(rng):
            return True
    try:
        _flag, cname = extract_flag_from_name(range_to_country_name(num))
        if cname and cname in countries:
            return True
    except Exception:
        pass
    return False


def _auto_sms_engine_loop():
    """Keep the Auto SMS group alive with REAL panel traffic.

    Every cycle the bot picks the currently hottest range of the connected
    panel and allocates a number on it through the normal allocation path, so
    genuine OTPs keep arriving and get forwarded to the group automatically.
    """
    logger.info("Auto SMS engine started.")
    idx = 0
    while True:
        try:
            if is_auto_sms_enabled() and get_unified_forward_chat_id():
                ranges, _countries = _refresh_auto_sms_targets()
                if ranges:
                    rid = ranges[idx % len(ranges)]
                    idx += 1
                    try:
                        data = fetch_api_number(rid)
                        if data:
                            logger.info("Auto SMS engine holding %s on top range %s",
                                        _number_from_api_data(data), rid)
                    except Exception as e:
                        logger.warning("Auto SMS engine allocation error (%s): %s", rid, e)
        except Exception as e:
            logger.warning("Auto SMS engine loop error: %s", e)
        time.sleep(AUTO_SMS_ENGINE_INTERVAL)


def _build_auto_sms_card(number: str, msg_text: str, panel_name: str = "") -> str:
    raw_num = str(number).strip().lstrip("+")
    masked = (raw_num[:-3] + "XXX") if len(raw_num) >= 4 else raw_num
    country_full = range_to_country_name(raw_num)
    flag, cname = extract_flag_from_name(country_full)
    otp_code = extract_otp(msg_text) or "N/A"
    service = detect_service_from_message(msg_text)
    sep = "━" * 28
    return (
        f"✨ {sep} ✨\n"
        f"        ⚡ <b>NEW OTP RECEIVED</b> ⚡\n"
        f"✨ {sep} ✨\n\n"
        f"<blockquote>"
        f"🌍 <b>Country:</b> {flag} {cname}\n"
        f"📱 <b>Service:</b> {service}\n"
        f"📡 <b>Range:</b> <code>{masked}</code>\n"
        f"🔑 <b>OTP Code:</b> <code>{otp_code}</code>"
        f"</blockquote>\n\n"
        f"💬 <b>Full SMS:</b>\n"
        f"<blockquote>{_html.escape(str(msg_text))}</blockquote>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 <b>Powered By:</b> <b>{_html.escape(_Developer_By())}</b>"
    )


def detect_service_from_message(msg_text: str) -> str:
    """Best-effort service name detection from the SMS body."""
    low = str(msg_text or "").lower()
    known = (
        ("whatsapp", "WhatsApp"), ("facebook", "Facebook"), ("instagram", "Instagram"),
        ("telegram", "Telegram"), ("tiktok", "TikTok"), ("google", "Google"),
        ("imo", "IMO"), ("viber", "Viber"), ("signal", "Signal"), ("twitter", "Twitter"),
        ("x.com", "X"), ("openai", "OpenAI"), ("chatgpt", "ChatGPT"), ("amazon", "Amazon"),
        ("paypal", "PayPal"), ("netflix", "Netflix"), ("uber", "Uber"), ("binance", "Binance"),
        ("microsoft", "Microsoft"), ("apple", "Apple"), ("snapchat", "Snapchat"),
        ("linkedin", "LinkedIn"), ("discord", "Discord"), ("yalla", "Yalla"),
    )
    for key, label in known:
        if key in low:
            return label
    return "OTHERS"


def _auto_forward_panel_sms(panel_name: str, number: str, msg_text: str, otp_id: str = ""):
    """Forward EVERY real SMS captured from a panel to the unified forward group."""
    if not is_auto_sms_enabled():
        return
    target = get_unified_forward_chat_id()
    if not target or not number or not msg_text:
        return
    try:
        if not _auto_sms_matches_top(number):
            logger.info("Auto SMS skip (not a top range/country): %s", number)
            return
    except Exception as e:
        logger.warning("Auto SMS top-range check failed: %s", e)
    key = hashlib.sha256(f"{normalize_number(number)}|{otp_id}|{msg_text}".encode()).hexdigest()
    with _auto_sms_lock:
        if key in _auto_sms_seen:
            return
        # Check the durable ledger too.  This prevents duplicate forwards
        # after a Railway restart, while still allowing a retry after a send
        # failure.
        with get_conn() as conn:
            if conn.execute(
                "SELECT 1 FROM auto_sms_deliveries WHERE delivery_key=?", (key,)
            ).fetchone():
                _auto_sms_seen.add(key)
                return
    try:
        kb = otp_group_keyboard()
        card = _build_auto_sms_card(number, msg_text, panel_name)
        chat_dest = int(target) if target.lstrip("-").isdigit() else target
        if kb:
            bot.send_message(chat_dest, card, reply_markup=kb)
        else:
            bot.send_message(chat_dest, card)
        # Only mark delivered after Telegram accepted the message.
        with _auto_sms_lock:
            with get_conn() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO auto_sms_deliveries
                       (delivery_key, panel_name, number, message)
                       VALUES (?,?,?,?)""",
                    (key, panel_name, normalize_number(number), msg_text),
                )
            _auto_sms_seen.add(key)
            if len(_auto_sms_seen) > 10000:
                _auto_sms_seen.clear()
    except Exception as e:
        logger.warning(f"Auto SMS forward error ({panel_name}): {e}")


# ─── DEMO SMS (admin-timed, looks exactly like a real panel OTP) ──────────────
DEMO_SMS_MIN_DELAY = 3          # seconds (hard minimum)
DEMO_SMS_MAX_DELAY = 3600       # seconds (max 60 minutes)
DEMO_SMS_DEFAULT_DELAY = 180    # 3 minutes


def is_demo_sms_enabled() -> bool:
    return str(get_setting("auto_sms_demo_enabled", "0")) == "1"


def get_demo_sms_delay() -> int:
    try:
        val = int(str(get_setting("auto_sms_demo_delay", DEMO_SMS_DEFAULT_DELAY)).strip())
    except Exception:
        val = DEMO_SMS_DEFAULT_DELAY
    return max(DEMO_SMS_MIN_DELAY, min(DEMO_SMS_MAX_DELAY, val))


def fmt_delay(sec) -> str:
    sec = int(sec)
    if sec >= 60 and sec % 60 == 0:
        return f"{sec // 60} min"
    if sec > 60:
        return f"{sec // 60} min {sec % 60} sec"
    return f"{sec} sec"


_DEMO_TEMPLATES = (
    "{code} is your WhatsApp code. Don't share this code with others",
    "<#> {code} is your WhatsApp code",
    "Login code: {code}. Do not give this code to anyone, even if they say they are from Telegram!",
    "{code} is your Facebook confirmation code",
    "{code} is your Instagram code. Don't share it.",
    "[TikTok] {code} is your verification code",
    "G-{code} is your Google verification code.",
    "<#> imo verification code: {code}",
    "Your Signal verification code: {code}",
    "Your Viber code is {code}. It expires in 10 minutes.",
    "{code} is your Snapchat code. Snapchat will never call or text you for this code.",
    "Your Discord verification code is {code}",
    "[Binance] Verification code: {code}. Never share this code.",
    "Your ChatGPT code is {code}",
    "Your Uber code is {code}. Reply STOP to unsubscribe.",
    "PayPal: {code} is your security code. Don't share it.",
    "Microsoft account security code: {code}",
    "{code} is your Amazon OTP. Do not share it with anyone.",
)


def _demo_number_from_targets() -> str:
    """Build a realistic number from the panels' own TOP ranges."""
    base = ""
    try:
        ranges, _countries = _refresh_auto_sms_targets()
        if ranges:
            base = re.sub(r"\D", "", secrets.choice(ranges))
    except Exception:
        base = ""
    if not base:
        try:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT number FROM numbers ORDER BY RANDOM() LIMIT 1"
                ).fetchone()
            if row:
                base = re.sub(r"\D", "", str(row[0]))[:7]
        except Exception:
            base = ""
    if not base:
        base = secrets.choice(("8801712", "2613880", "212612", "96650", "234701", "639171"))
    need = max(4, 12 - len(base))
    tail = "".join(secrets.choice("0123456789") for _ in range(need))
    return (base + tail)[:15]


def _build_demo_sms():
    """Return (number, message) that looks exactly like a real panel OTP."""
    number = _demo_number_from_targets()
    template = secrets.choice(_DEMO_TEMPLATES)
    digits = secrets.choice((4, 5, 6, 6, 6))
    code = "".join(secrets.choice("0123456789") for _ in range(digits))
    if digits == 6 and secrets.choice((0, 1)):
        code = f"{code[:3]}-{code[3:]}"
    return number, template.format(code=code)


def _send_demo_sms() -> bool:
    target = get_unified_forward_chat_id()
    if not target:
        return False
    number, msg_text = _build_demo_sms()
    try:
        kb = otp_group_keyboard()
        card = _build_auto_sms_card(number, msg_text, "")
        chat_dest = int(target) if target.lstrip("-").isdigit() else target
        if kb:
            bot.send_message(chat_dest, card, reply_markup=kb)
        else:
            bot.send_message(chat_dest, card)
        return True
    except Exception as e:
        logger.warning("Demo SMS send error: %s", e)
        return False


def _demo_sms_loop():
    """Send one demo OTP to the unified forward group every <admin delay> seconds."""
    logger.info("Demo SMS engine started.")
    next_at = 0.0
    while True:
        try:
            if is_demo_sms_enabled():
                target = get_unified_forward_chat_id()
                if target and time.time() >= next_at:
                    if _send_demo_sms():
                        logger.info("Demo SMS sent (next in %ss).", get_demo_sms_delay())
                    next_at = time.time() + get_demo_sms_delay()
            else:
                next_at = 0.0
        except Exception as e:
            logger.warning("Demo SMS loop error: %s", e)
        time.sleep(1)


def _forward_otp(msg_text: str, number: str, alloc: dict = None):
    """Forward OTP to the unified forward chat ID in stylish format."""
    fwd_id = get_unified_forward_chat_id()
    if not fwd_id:
        return
    try:
        otp_code = extract_otp(msg_text) if msg_text else ""
        if alloc:
            country_flag = alloc.get("country_flag", "")
            country_name = alloc.get("country_name", "")
            service_name = alloc.get("service_name", "")
            raw_num = str(alloc.get("number", number)).lstrip("+")
        else:
            country_flag = ""
            country_name = ""
            service_name = ""
            raw_num = str(number).lstrip("+")
        # Build masked range: show first digits then XXX for last 3
        if len(raw_num) >= 4:
            masked = raw_num[:-3] + "XXX"
        else:
            masked = raw_num
        country_display = f"{country_flag} {country_name}".strip() if country_name else raw_num
        sep = "━" * 28
        fwd_msg = (
            f"✨ {sep} ✨\n"
            f"        ⚡ <b>NEW OTP RECEIVED</b> ⚡\n"
            f"✨ {sep} ✨\n\n"
            f"<blockquote>"
            f"🌍 <b>Country:</b> {country_display}\n"
            f"📱 <b>Service:</b> {service_name.upper() if service_name else 'N/A'}\n"
            f"📡 <b>Range:</b> <code>{masked}</code>\n"
            f"🔑 <b>OTP Code:</b> <code>{otp_code if otp_code else 'N/A'}</code>"
            f"</blockquote>\n\n"
            f"💬 <b>Full SMS:</b>\n"
            f"<blockquote>{_html.escape(str(msg_text))}</blockquote>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        grp_kb = otp_group_keyboard()
        chat_dest = int(fwd_id) if fwd_id.lstrip("-").isdigit() else fwd_id
        if grp_kb:
            bot.send_message(chat_dest, fwd_msg, reply_markup=grp_kb)
        else:
            bot.send_message(chat_dest, fwd_msg)
    except Exception as e:
        logger.warning(f"OTP forward error: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMANDS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.message_handler(commands=["start"])
def cmd_start(message):
    # Only respond in private chats — prevents welcome message from going to groups
    if message.chat.type != "private":
        return
    user = message.from_user
    upsert_user(user)
    db_user = get_user(user.id)
    if db_user and db_user["is_banned"]:
        bot.send_message(message.chat.id, f"🛑 {stylish('You are banned from using this bot.')}")
        return
    # ── Handle referral link (/start ref_USERID) ─────────────────────
    args = message.text.split()[1] if len(message.text.split()) > 1 else ""
    if args.startswith("ref_"):
        try:
            referrer_id = int(args[4:])
            if referrer_id != user.id:
                with get_conn() as conn:
                    existing = conn.execute("SELECT referred_by FROM users WHERE id=?", (user.id,)).fetchone()
                    if existing and existing["referred_by"] is None:
                        conn.execute("UPDATE users SET referred_by=? WHERE id=?", (referrer_id, user.id))
        except Exception:
            pass
    if not is_member(user.id):
        bot.send_message(
            message.chat.id,
            _join_prompt_text(),
            reply_markup=join_keyboard(),
        )
        return
    first_name = user.first_name or "User"
    if is_admin(user.id) and user.id not in admin_user_mode:
        admin_user_mode.discard(user.id)   # exit user mode if active
        admin_live_mode.add(user.id)        # enable live notifications
        _send_welcome(message.chat.id, first_name, user.id)
        _send_admin_live_panel(message.chat.id)
    else:
        _send_welcome(message.chat.id, first_name, user.id)


@bot.message_handler(commands=["tempmail", "checkmail"])
def cmd_temp_mail(message):
    if message.chat.type != "private":
        return
    user = message.from_user
    upsert_user(user)
    db_user = get_user(user.id)
    if db_user and db_user["is_banned"]:
        return
    if message.text.split()[0].lower().startswith("/checkmail"):
        _send_temp_mail_check_result(message.chat.id, user.id)
        return
    _send_generated_temp_mail(message.chat.id, user.id)


@bot.message_handler(commands=["searchotp"])
def cmd_searchotp(message):
    """Search OTP by number. Usage: /searchotp 8801XXXXXXXX"""
    if message.chat.type != "private":
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(message.chat.id, "🔍 <b>Usage:</b> <code>/searchotp 8801XXXXXXXX</code>")
        return
    query = parts[1].strip().lstrip("+")
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT number, otp_code, message, received_at FROM otps WHERE number LIKE ? ORDER BY received_at DESC LIMIT 5",
            (f"%{query}%",)
        ).fetchall()
    sep = "━" * 30
    if not rows:
        bot.send_message(message.chat.id,
            f"🔍 {sep}\n   {stylish('SEARCH OTP')}\n{sep}\n\n"
            f"⛔ <b>{query}</b> এর জন্য কোনো OTP পাওয়া যায়নি।")
        return
    result_lines = ""
    for r in rows:
        t = datetime.fromtimestamp(r["received_at"]).strftime("%d/%m %H:%M") if r["received_at"] else "N/A"
        otp = _html.escape(str(r["otp_code"])) if r["otp_code"] else "N/A"
        result_lines += f"\n❖ 🔢 OTP: <code>{otp}</code>  🕰 {t}"
    bot.send_message(
        message.chat.id,
        f"🔍 {sep}\n"
        f"   {stylish('SEARCH OTP')}\n"
        f"{sep}\n\n"
        f"☎ Number: <code>+{query}</code>\n"
        f"<blockquote>{result_lines}</blockquote>\n"
        f"━━━━━━━━━━━━━━━━━━",
    )


@bot.message_handler(commands=["user"])
def cmd_user(message):
    """Admin enters user mode: no live feed, no admin panel."""
    if message.chat.type != "private":
        return
    user = message.from_user
    if not is_admin(user.id):
        cmd_start(message)
        return
    admin_live_mode.discard(user.id)
    admin_states.pop(user.id, None)
    admin_user_mode.add(user.id)
    first_name = user.first_name or "User"
    # Send exact same welcome panel as a normal user (no admin notice at all)
    _send_welcome(message.chat.id, first_name, user.id)


@bot.message_handler(commands=["ban"])
def cmd_ban(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, f"🛡️ {stylish('Admin access required.')}")
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, f"ℹ️ {stylish('Usage: /ban <user_id>')}")
        return
    try:
        uid = int(parts[1])
    except (TypeError, ValueError):
        bot.send_message(message.chat.id, f"❎️ {stylish('Invalid user ID. Use numbers only.')}")
        return
    with get_conn() as conn:
        conn.execute("UPDATE users SET is_banned=1 WHERE id=?", (uid,))
    bot.send_message(message.chat.id, f"✔️ User {uid} banned.")


@bot.message_handler(commands=["unban"])
def cmd_unban(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, f"🛡️ {stylish('Admin access required.')}")
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, f"ℹ️ {stylish('Usage: /unban <user_id>')}")
        return
    try:
        uid = int(parts[1])
    except (TypeError, ValueError):
        bot.send_message(message.chat.id, f"❎️ {stylish('Invalid user ID. Use numbers only.')}")
        return
    with get_conn() as conn:
        conn.execute("UPDATE users SET is_banned=0 WHERE id=?", (uid,))
    bot.send_message(message.chat.id, f"✔️ User {uid} unbanned.")


@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, f"🛡️ {stylish('Admin access required.')}")
        return
    text = message.text[len("/broadcast"):].strip()
    with get_conn() as conn:
        users = conn.execute("SELECT id FROM users WHERE is_banned=0").fetchall()
    sent = 0
    for u in users:
        try:
            if message.reply_to_message:
                bot.forward_message(u["id"], message.chat.id, message.reply_to_message.message_id)
            else:
                bot.send_message(u["id"], text or "📣 Broadcast message.")
            sent += 1
        except Exception:
            pass
    bot.send_message(message.chat.id, f"✔️ Broadcast sent to {sent} users.")


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if not is_admin(message.from_user.id):
        return
    _show_admin_monitor(message.chat.id, message.from_user.id)


def _show_admin_monitor(chat_id, admin_user_id):
    """Show admin monitoring panel: recent numbers taken + OTPs received."""
    now = int(time.time())
    with get_conn() as conn:
        recent_allocs = conn.execute("""
            SELECT a.id, a.number, a.service_name, a.country_flag, a.country_name,
                   a.allocated_at, a.otp_received, a.otp_text,
                   u.first_name, u.username, u.id as uid
            FROM allocations a
            LEFT JOIN users u ON u.id = a.user_id
            ORDER BY a.allocated_at DESC LIMIT 10
        """).fetchall()
        recent_otps = conn.execute("""
            SELECT o.number, o.otp_code, o.message, o.received_at,
                   u.first_name, u.username, u.id as uid
            FROM otps o
            LEFT JOIN users u ON u.id = o.user_id
            ORDER BY o.received_at DESC LIMIT 8
        """).fetchall()
        stats_row = conn.execute("""
            SELECT COUNT(*) as total_allocs,
                   SUM(CASE WHEN otp_received=1 THEN 1 ELSE 0 END) as got_otp,
                   SUM(CASE WHEN timed_out=1 AND otp_received=0 THEN 1 ELSE 0 END) as timed_out
            FROM allocations WHERE allocated_at > ? - 86400
        """, (now,)).fetchone()
    sep = "━" * 28
    lines = [
        "🔐 " + sep,
        "   👁️  " + stylish("ADMIN MONITOR"),
        sep + "\n",
        "📊 <b>" + stylish("LAST 24H STATS") + "</b>",
        "<blockquote>",
        "❖ ☎ " + stylish("Numbers Taken") + "  ➤  <b>" + str(stats_row["total_allocs"] or 0) + "</b>",
        "❖ ✔️ " + stylish("Got OTP") + "         ➤  <b>" + str(stats_row["got_otp"] or 0) + "</b>",
        "❖ ⏰ " + stylish("Timed Out") + "       ➤  <b>" + str(stats_row["timed_out"] or 0) + "</b>",
        "</blockquote>\n",
        "📟 <b>" + stylish("RECENT NUMBERS TAKEN") + "</b>",
        "<blockquote>",
    ]
    if recent_allocs:
        for a in recent_allocs[:8]:
            fname = _html.escape(a["first_name"] or "User")
            uname = ("@" + a["username"]) if a["username"] else ("ID:" + str(a["uid"]))
            t = datetime.fromtimestamp(a["allocated_at"]).strftime("%d/%m %H:%M") if a["allocated_at"] else "N/A"
            flag = a["country_flag"] or "🛰"
            otp_status = "✔️" if a["otp_received"] else "⌛"
            lines.append(
                otp_status + " <b>" + fname + "</b> (" + uname + ")\n"
                "   ☎ <code>+" + str(a["number"]) + "</code>  " + flag + "  📟 " + (a["service_name"] or "N/A") + "\n"
                "   🕰 " + t
            )
    else:
        lines.append("   <i>এখনো কোনো number নেওয়া হয়নি।</i>")
    lines += ["</blockquote>\n", "🔢 <b>" + stylish("RECENT OTPs RECEIVED") + "</b>", "<blockquote>"]
    if recent_otps:
        for o in recent_otps:
            fname = _html.escape(o["first_name"] or "User")
            uname = ("@" + o["username"]) if o["username"] else ("ID:" + str(o["uid"]))
            t = datetime.fromtimestamp(o["received_at"]).strftime("%d/%m %H:%M") if o["received_at"] else "N/A"
            otp_code = _html.escape(str(o["otp_code"])) if o["otp_code"] else "N/A"
            lines.append(
                "🧑 <b>" + fname + "</b> (" + uname + ")\n"
                "   ☎ <code>+" + str(o["number"]) + "</code>  🔢 <code>" + otp_code + "</code>\n"
                "   🕰 " + t
            )
    else:
        lines.append("   <i>এখনো কোনো OTP পাওয়া যায়নি।</i>")
    lines += [
        "</blockquote>",
        "\n" + "━" * 28,
        "🔷 " + stylish("POWERED BY") + " <b>" + stylish(_Developer_By()) + "</b>",
    ]
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("☎ সব Numbers", callback_data="monitor_all_numbers"),
        InlineKeyboardButton("🔢 সব OTPs", callback_data="monitor_all_otps"),
    )
    kb.add(InlineKeyboardButton("🔐 Admin Panel খুলুন", callback_data="monitor_open_admin"))
    try:
        bot.send_message(chat_id, "\n".join(lines), reply_markup=kb)
    except Exception as e:
        logger.warning("Monitor send error: " + str(e))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CALLBACK HANDLERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _send_monitor_rows(chat_id, title, rows):
    header = f"🧾 <b>{stylish(title)}</b>\n━━━━━━━━━━━━━━━━━━━━"
    if not rows:
        bot.send_message(chat_id, header + "\n\n<i>No records found.</i>")
        return
    blocks = []
    for row in rows:
        blocks.append(row)
    message = header
    for block in blocks:
        candidate = message + "\n\n" + block
        if len(candidate) > 3900:
            bot.send_message(chat_id, message)
            message = header + "\n\n" + block
        else:
            message = candidate
    bot.send_message(chat_id, message)


@bot.callback_query_handler(func=lambda c: c.data in ("monitor_all_numbers", "monitor_all_otps", "monitor_open_admin"))
def cb_admin_monitor_actions(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Admin access required.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    if call.data == "monitor_open_admin":
        admin_live_mode.discard(call.from_user.id)
        admin_states.pop(call.from_user.id, None)
        bot.send_message(
            call.message.chat.id,
            f"🎛️ <b>{stylish('ADMIN PANEL')}</b>",
            reply_markup=admin_keyboard(call.from_user.id == ADMIN_ID),
        )
        return
    with get_conn() as conn:
        if call.data == "monitor_all_numbers":
            rows = conn.execute("""
                SELECT a.number, a.service_name, a.country_flag, a.country_name,
                       a.allocated_at, a.otp_received, u.first_name, u.username, u.id AS uid
                FROM allocations a
                LEFT JOIN users u ON u.id = a.user_id
                ORDER BY a.allocated_at DESC LIMIT 100
            """).fetchall()
            items = []
            for row in rows:
                user_name = _html.escape(row["first_name"] or row["username"] or str(row["uid"]) or "User")
                number = _html.escape(str(row["number"] or "N/A"))
                service = _html.escape(str(row["service_name"] or "N/A"))
                country = _html.escape(str(row["country_name"] or "N/A"))
                when = datetime.fromtimestamp(row["allocated_at"]).strftime("%d/%m/%Y %H:%M") if row["allocated_at"] else "N/A"
                status = "✔️" if row["otp_received"] else "⌛"
                items.append(f"{status} <b>{user_name}</b> · <code>+{number}</code>\n{row['country_flag'] or '🛰'} {country} · {service} · 🕰 {when}")
            title = "LATEST 100 NUMBERS"
        else:
            rows = conn.execute("""
                SELECT o.number, o.otp_code, o.message, o.received_at,
                       u.first_name, u.username, u.id AS uid
                FROM otps o
                LEFT JOIN users u ON u.id = o.user_id
                ORDER BY o.received_at DESC LIMIT 100
            """).fetchall()
            items = []
            for row in rows:
                user_name = _html.escape(row["first_name"] or row["username"] or str(row["uid"]) or "User")
                number = _html.escape(str(row["number"] or "N/A"))
                otp = _html.escape(str(row["otp_code"] or "N/A"))
                when = datetime.fromtimestamp(row["received_at"]).strftime("%d/%m/%Y %H:%M") if row["received_at"] else "N/A"
                items.append(f"🔐 <b>{user_name}</b> · <code>+{number}</code>\n🔢 <code>{otp}</code> · 🕰 {when}")
            title = "LATEST 100 OTPs"
    _send_monitor_rows(call.message.chat.id, title, items)


@bot.callback_query_handler(func=lambda c: c.data == "usr_check_join")
def cb_check_join(call):
    user = call.from_user
    upsert_user(user)
    db_user = get_user(user.id)
    if db_user and db_user["is_banned"]:
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, f"🛑 {stylish('You are banned.')}")
        return
    if not is_member(user.id):
        bot.answer_callback_query(call.id, f"❗️ {stylish('You have not joined yet!')}", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    # Delete the join prompt message immediately after successful join
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    first_name = user.first_name or "User"
    _send_welcome(call.message.chat.id, first_name, user.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("sel_service:") or c.data == "sel_service_back")
def cb_select_service(call):
    """Handle inline service selection."""
    user = call.from_user
    upsert_user(user)
    db_user = get_user(user.id)
    if db_user and db_user["is_banned"]:
        bot.answer_callback_query(call.id)
        return

    if call.data == "sel_service_back":
        bot.answer_callback_query(call.id)
        user_states.pop(user.id, None)
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        return

    svc_id = int(call.data.split(":")[1])
    with get_conn() as conn:
        svc = conn.execute("SELECT * FROM services WHERE id=?", (svc_id,)).fetchone()
    if not svc:
        bot.answer_callback_query(call.id, stylish("Service not found."), show_alert=True)
        return

    ustate = user_states.get(user.id, {})
    ustate["service_id"] = svc["id"]
    ustate["service_name"] = svc["name"]
    ustate["step"] = "selecting_country"
    user_states[user.id] = ustate

    bot.answer_callback_query(call.id)
    kb = user_countries_inline_keyboard(svc["id"])
    try:
        bot.edit_message_text(
            f"🗺 <b>{stylish('Select a Country')}:</b>",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("sel_country:") or c.data == "sel_country_back")
def cb_select_country(call):
    """Handle inline country selection."""
    user = call.from_user
    upsert_user(user)
    db_user = get_user(user.id)
    if db_user and db_user["is_banned"]:
        bot.answer_callback_query(call.id)
        return

    if call.data == "sel_country_back":
        ustate = user_states.get(user.id, {})
        ustate["step"] = "selecting_service"
        user_states[user.id] = ustate
        bot.answer_callback_query(call.id)
        kb = user_services_inline_keyboard()
        if not kb:
            user_states.pop(user.id, None)
            try:
                bot.edit_message_text(
                    f"⛔ {stylish('No services available.')}",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=None,
                )
            except Exception:
                pass
            return
        try:
            bot.edit_message_text(
                f"📟 <b>{stylish('Select a Service')}:</b>",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=kb,
            )
        except Exception:
            pass
        return

    ustate = user_states.get(user.id)
    if not ustate or ustate.get("step") != "selecting_country":
        bot.answer_callback_query(call.id, stylish("Session expired. Please start again."), show_alert=True)
        return

    country_id = int(call.data.split(":")[1])
    bot.answer_callback_query(call.id)
    user_states.pop(user.id, None)
    # Delete the "Select a Country" message entirely
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    assign_number_to_user(user.id, call.message.chat.id, country_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("otp_copy:"))
def cb_otp_copy(call):
    """Fallback COPY OTP handler (used only if native copy_text buttons aren't supported).
    Shows the OTP in an alert popup so the user can select and copy it manually."""
    try:
        otp_val = call.data.split(":", 1)[1]
        bot.answer_callback_query(call.id, text=f"OTP: {otp_val}", show_alert=True)
    except Exception as e:
        logger.error(f"OTP copy error: {e}")


@bot.callback_query_handler(func=lambda c: c.data.startswith("num_copy:"))
def cb_num_copy(call):
    """Fallback COPY NUMBER handler — shows number in alert popup for manual copy."""
    try:
        num_val = call.data.split(":", 1)[1]
        bot.answer_callback_query(call.id, text=f"+{num_val}", show_alert=True)
    except Exception as e:
        logger.error(f"Number copy error: {e}")


@bot.callback_query_handler(func=lambda c: c.data == "num_change")
def cb_number_card_actions(call):
    """Handle CHANGE — delete old card and assign new number from same country/range."""
    user = call.from_user
    bot.answer_callback_query(call.id)

    with get_conn() as conn:
        alloc = conn.execute(
            "SELECT * FROM allocations WHERE user_id=? AND message_id=? ORDER BY id LIMIT 1",
            (user.id, call.message.message_id),
        ).fetchone()

    # Delete old number card message
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass

    if not alloc:
        kb = user_services_inline_keyboard()
        user_states[user.id] = {"step": "selecting_service"}
        bot.send_message(call.message.chat.id, f"📟 <b>{stylish('Select a Service')}:</b>", reply_markup=kb)
        return

    # Cancel old allocation — DB number stays assigned=1 permanently (never re-used)
    with get_conn() as conn:
        conn.execute(
            "UPDATE allocations SET timed_out=1 WHERE user_id=? AND message_id=?",
            (user.id, call.message.message_id),
        )

    # OTP Work number (rid stored) → re-fetch from same range, send as NEW message
    stored_rid = alloc["rid"] if alloc["rid"] else ""
    if not alloc["number_id"] and stored_rid:
        flag_prefix = alloc["country_flag"] or ""
        country_with_flag = f"{flag_prefix} {alloc['country_name']}".strip() if flag_prefix else alloc["country_name"]
        entry = {
            "country": country_with_flag,
            "service_sid": alloc["service_name"],
        }
        _otpwork_fetch_number(call, entry, stored_rid, send_new=True)
        return

    # DB number → same country
    with get_conn() as conn:
        country_row = conn.execute(
            """SELECT c.id FROM countries c
               JOIN services s ON s.id = c.service_id
               WHERE s.name=? AND c.name=? AND c.code=?
               LIMIT 1""",
            (alloc["service_name"], alloc["country_name"], alloc["country_code"]),
        ).fetchone()

    if country_row:
        assign_number_to_user(user.id, call.message.chat.id, country_row["id"])
    else:
        kb = user_services_inline_keyboard()
        user_states[user.id] = {"step": "selecting_service"}
        bot.send_message(call.message.chat.id, f"📟 <b>{stylish('Select a Service')}:</b>", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data == "num_change_range")
def cb_num_change_range(call):
    """Change Range — no longer available (live country/range browsing was removed)."""
    bot.answer_callback_query(call.id, f"❗️ {stylish('Change Range is no longer available.')}", show_alert=True)


def _submit_withdraw_request(user, chat_id, method, phone, amount):
    with get_conn() as conn:
        conn.execute(
            "UPDATE wallet SET balance=balance-?, pending_balance=pending_balance+? WHERE user_id=?",
            (amount, amount, user.id),
        )
        conn.execute(
            "INSERT INTO withdraw_requests (user_id, method, number, amount) VALUES (?,?,?,?)",
            (user.id, method, phone, amount),
        )
        req_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    new_stats = get_wallet_stats(user.id)
    bot.send_message(
        chat_id,
        f"✔️ <b>{stylish('Withdraw Request Submitted')}</b>\n\n{stylish('Method')}: <b>{method}</b>\n"
        f"{stylish('Number')}: <b>{phone}</b>\n{stylish('Amount')}: <b>{amount:.2f} BDT</b>\n\n⌛ {stylish('Waiting for admin approval...')}",
        reply_markup=balance_inline_keyboard(),
    )
    uname = f"@{user.username}" if user.username else "N/A"
    group_text = (
        "🔔 <b>NEW WITHDRAW REQUEST</b>\n\n"
        f"🧑 User ID: <code>{user.id}</code>\n"
        f"🧑 Name: {user.first_name or 'N/A'}\n"
        f"🧑 Username: {uname}\n\n"
        f"🪪 Method: <b>{method}</b>\n"
        f"📟 Number: <code>{phone}</code>\n\n"
        f"💲 Requested: <b>{amount:.2f} BDT</b>\n"
        f"🏦 Remaining Balance: <b>{new_stats['balance']:.2f} BDT</b>"
    )
    pmt_fwd = get_setting("payment_forward_chat_id")
    if pmt_fwd:
        try:
            g_msg = bot.send_message(
                int(pmt_fwd), group_text, reply_markup=withdraw_action_keyboard(req_id)
            )
            with get_conn() as conn:
                conn.execute(
                    "UPDATE withdraw_requests SET group_msg_id=? WHERE id=?",
                    (g_msg.message_id, req_id),
                )
        except Exception as e:
            logger.warning(f"Payment forward error: {e}")


@bot.callback_query_handler(func=lambda c: c.data in ("bal_withdraw", "bal_home"))
def cb_balance_actions(call):
    user = call.from_user
    upsert_user(user)
    db_user = get_user(user.id)
    if db_user and db_user["is_banned"]:
        bot.answer_callback_query(call.id)
        return
    if call.data == "bal_home":
        bot.answer_callback_query(call.id)
        user_states.pop(user.id, None)
        first_name = user.first_name or "User"
        _send_welcome(call.message.chat.id, first_name, user.id)
        return
    stats = get_wallet_stats(user.id)
    balance = stats["balance"]
    min_wd = get_min_withdraw()
    if balance < min_wd:
        bot.answer_callback_query(
            call.id,
            f"⛔ {stylish('Insufficient balance!')}\n{stylish('Minimum')}: {min_wd:.0f} BDT\n{stylish('Yours')}: {balance:.2f} BDT",
            show_alert=True,
        )
        return
    if has_pending_withdraw(user.id):
        bot.answer_callback_query(call.id, f"❗️ {stylish('You already have a pending withdrawal.')}", show_alert=True)
        return
    methods_kb = withdraw_methods_inline_keyboard()
    if not methods_kb:
        bot.answer_callback_query(call.id, f"⛔ {stylish('No payment methods configured. Contact admin.')}", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"🪙 <b>{stylish('Select Payment Method')}</b>\n\n{stylish('Balance')}: <b>{balance:.2f} BDT</b>",
        reply_markup=methods_kb,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("wd_method:"))
def cb_wd_method(call):
    user = call.from_user
    db_user = get_user(user.id)
    if db_user and db_user["is_banned"]:
        bot.answer_callback_query(call.id)
        return
    method = call.data.split(":", 1)[1]
    user_states[user.id] = {"step": "wd_enter_phone", "method": method}
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(
            f"✔️ <b>{method}</b> {stylish('selected.')}",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=None,
        )
    except Exception:
        pass
    bot.send_message(call.message.chat.id, f"📟 <b>{method}</b>\n\n{stylish('Enter your')} <b>{stylish('phone number')}:</b>")


@bot.callback_query_handler(func=lambda c: c.data in ("wd_confirm", "wd_cancel"))
def cb_wd_confirm(call):
    user = call.from_user
    db_user = get_user(user.id)
    if db_user and db_user["is_banned"]:
        bot.answer_callback_query(call.id)
        return
    if call.data == "wd_cancel":
        user_states.pop(user.id, None)
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        stats = get_wallet_stats(user.id)
        bot.send_message(
            call.message.chat.id,
            f"⛔ {stylish('Withdraw cancelled.')}\n\n" + build_balance_text(stats),
            reply_markup=balance_inline_keyboard(),
        )
        return
    wstate = user_states.get(user.id)
    if not wstate or wstate.get("step") != "wd_confirm":
        bot.answer_callback_query(call.id, stylish("Session expired. Please try again."), show_alert=True)
        return
    bot.answer_callback_query(call.id)
    user_states.pop(user.id, None)
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    _submit_withdraw_request(user, call.message.chat.id, wstate["method"], wstate["phone"], wstate["amount"])


@bot.callback_query_handler(func=lambda c: c.data.startswith("awd_approve:") or c.data.startswith("awd_reject:"))
def cb_withdraw_action(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, stylish("Unauthorized"), show_alert=True)
        return
    action, req_id_str = call.data.split(":", 1)
    req_id = int(req_id_str)
    with get_conn() as conn:
        req = conn.execute("SELECT * FROM withdraw_requests WHERE id=?", (req_id,)).fetchone()
    if not req:
        bot.answer_callback_query(call.id, stylish("Request not found."), show_alert=True)
        return
    if req["status"] != "pending":
        bot.answer_callback_query(call.id, stylish("Already processed."), show_alert=True)
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        return
    bot.answer_callback_query(call.id)
    if action == "awd_approve":
        with get_conn() as conn:
            conn.execute("UPDATE withdraw_requests SET status='approved' WHERE id=?", (req_id,))
            conn.execute(
                "UPDATE wallet SET pending_balance=MAX(0, pending_balance-?) WHERE user_id=?",
                (req["amount"], req["user_id"]),
            )
        try:
            bot.send_message(
                req["user_id"],
                f"✔️ <b>{stylish('Withdraw Successful')}</b>\n\n{stylish('Amount')}: <b>{req['amount']:.2f} BDT</b>\n"
                f"{stylish('Method')}: <b>{req['method']}</b>\n{stylish('Number')}: <b>{req['number']}</b>",
            )
        except Exception:
            pass
        try:
            bot.edit_message_text(
                call.message.text + "\n\n✔️ <b>APPROVED</b>",
                chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None,
            )
        except Exception:
            pass
    else:
        with get_conn() as conn:
            conn.execute("UPDATE withdraw_requests SET status='rejected' WHERE id=?", (req_id,))
            conn.execute(
                "UPDATE wallet SET balance=balance+?, pending_balance=MAX(0, pending_balance-?) WHERE user_id=?",
                (req["amount"], req["amount"], req["user_id"]),
            )
        try:
            bot.send_message(
                req["user_id"],
                f"⛔ <b>Withdraw Failed</b>\n\nAmount <b>{req['amount']:.2f} BDT</b> returned.",
            )
        except Exception:
            pass
        try:
            bot.edit_message_text(
                call.message.text + "\n\n⛔ <b>REJECTED — Balance Returned</b>",
                chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None,
            )
        except Exception:
            pass





# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# REFERRAL (now opened from inside Profile)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _send_referral_card(chat_id, user):
    sep = "━" * 30
    bot_info = bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user.id}"
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) as c FROM users WHERE referred_by=?", (user.id,)).fetchone()
        count = row["c"] if row else 0
    earn = get_otp_earn()
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(
        "⛓ Share Refer Link",
        url=f"https://t.me/share/url?url={ref_link}&text=Join+this+bot+%26+earn+BDT+per+OTP!",
    ))
    bot.send_message(
        chat_id,
        f"🎁 {sep}\n"
        f"   🤝  {stylish('REFER & EARN')}\n"
        f"{sep}\n\n"
        f"<blockquote>"
        f"❖ 👥 {stylish('Your Referrals')}  ➤  <b>{count}</b>\n"
        f"❖ 💲 {stylish('Per OTP Earn')}   ➤  <b>{earn:.2f} BDT</b>\n"
        f"❖ ⛓ {stylish('Your Link')}      ➤\n"
        f"<code>{ref_link}</code>"
        f"</blockquote>\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔷 {stylish('POWERED BY')} <b>{stylish(_Developer_By())}</b>",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data == "profile_referral")
def cb_profile_referral(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    try:
        _send_referral_card(call.message.chat.id, call.from_user)
    except Exception as e:
        logger.error(f"referral card error: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN TEXT HANDLER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.message_handler(content_types=["text"])
def handle_text(message):
    # ── SHOP HOOK (also serves the admin group, so it runs before the guard) ──
    try:
        import shop as _shop
        if _shop.handle_text(message):
            return
    except Exception as _shop_exc:
        logger.error(f"shop hook error: {_shop_exc}")
    if message.chat.type != "private":
        return
    user = message.from_user
    upsert_user(user)
    db_user = get_user(user.id)
    if db_user and db_user["is_banned"]:
        return

    text = message.text.strip()
    chat_id = message.chat.id
    _is_main_admin = (user.id == ADMIN_ID)
    logger.info(f"[MSG] uid={user.id} is_admin={is_admin(user.id)} state={admin_states.get(user.id,{}).get('step','NONE')} text={repr(text[:60])}")

    # ══════════════════════════════════════════════════════════════════════════
    # ADMIN PANEL
    # ══════════════════════════════════════════════════════════════════════════
    if is_admin(user.id):

        if user.id in admin_states:
            state = admin_states[user.id]
            step = state.get("step")

            # ── Cancel from anywhere ─────────────────────────────────────────
            if text == f"⛔ {stylish('Cancel')}":
                admin_states.pop(user.id, None)
                _bal_kb_steps = ("aset_otp_earn", "aadd_balance", "arem_balance")
                _wd_kb_steps = ("awm_add", "awm_del", "awm_set_min")
                _fj_kb_steps = ("aset_fj_del_ch", "aset_fj_del_gr")
                _ol_kb_steps = ("aset_ol_support", "aset_ol_otpgroup", "aset_ol_main_channel", "aset_ol_payment_id", "aset_ol_otp_fwd")
                _ban_kb_steps = ("aban_uid", "aunban_uid")
                if step in _bal_kb_steps:
                    kb = balance_management_keyboard()
                elif step in _wd_kb_steps:
                    kb = withdraw_management_keyboard()
                elif step in _fj_kb_steps:
                    kb = force_join_keyboard()
                elif step in _ol_kb_steps:
                    kb = others_link_keyboard()
                elif step in _ban_kb_steps:
                    kb = ban_unban_keyboard()
                elif step in ("amgmt_add_uid", "amgmt_remove_uid"):
                    kb = admin_management_keyboard()
                elif step == "abroadcast_wait":
                    kb = admin_keyboard(is_main_admin=_is_main_admin)
                elif step in ("asvc_add_name", "arange_country_info", "arange_range_input"):
                    kb = manage_services_keyboard()
                elif step == "adev_set_info":
                    kb = developer_keyboard()
                elif step in ("aapi_setkey_smshadi", "aapi_setkey_lamix", "aapi_setkey_yesms",
                              "aapi_setkey_stexsms", "aapi_setkey_fastxotps", "aapi_setkey_voltxsms",
                              "aapi_setkey_zebrasms"):
                    kb = api_management_keyboard()
                elif step == "aauto_chat_id":
                    kb = auto_sms_keyboard()
                elif step.startswith("atm_"):
                    kb = temp_mail_admin_keyboard()
                elif step == "abackup_restore_wait":
                    kb = backup_keyboard()
                else:
                    kb = manage_services_keyboard()
                bot.send_message(chat_id, "⛔ Cancelled.", reply_markup=kb)
                return

            # ── API KEY INPUT ──────────────────────────────────────────────────
            if step == "aapi_setkey_smshadi":
                new_key = text.strip()
                if not new_key:
                    bot.send_message(chat_id, "⛔ Key cannot be empty.", reply_markup=cancel_keyboard())
                    return
                set_api_key("smshadi", new_key)
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ {stylish('SMShadi API key saved!')}", reply_markup=api_management_keyboard())
                return
            elif step == "aapi_setkey_lamix":
                new_key = text.strip()
                if not new_key:
                    bot.send_message(chat_id, "⛔ Key cannot be empty.", reply_markup=cancel_keyboard())
                    return
                set_api_key("lamix", new_key)
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ {stylish('Lamix API key saved!')}", reply_markup=api_management_keyboard())
                return
            elif step == "aapi_setkey_yesms":
                new_key = text.strip()
                if not new_key:
                    bot.send_message(chat_id, "⛔ Key cannot be empty.", reply_markup=cancel_keyboard())
                    return
                set_api_key("yesms", new_key)
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ {stylish('YesMS API key saved!')}", reply_markup=api_management_keyboard())
                return
            elif step == "aapi_setkey_stexsms":
                new_key = text.strip()
                if not new_key:
                    bot.send_message(chat_id, "⛔ Key cannot be empty.", reply_markup=cancel_keyboard())
                    return
                set_api_key("stexsms", new_key)
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ {stylish('StexSMS API key saved!')}", reply_markup=api_management_keyboard())
                return
            elif step == "aapi_setkey_fastxotps":
                new_key = text.strip()
                if not new_key:
                    bot.send_message(chat_id, "⛔ Key cannot be empty.", reply_markup=cancel_keyboard())
                    return
                set_api_key("fastxotps", new_key)
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ {stylish('FastXOTPs API key saved!')}", reply_markup=api_management_keyboard())
                return
            elif step == "aapi_setkey_voltxsms":
                new_key = text.strip()
                if not new_key:
                    bot.send_message(chat_id, "⛔ Key cannot be empty.", reply_markup=cancel_keyboard())
                    return
                set_api_key("voltxsms", new_key)
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ {stylish('VoltXSMS API key saved!')}", reply_markup=api_management_keyboard())
                return
            elif step == "aapi_setkey_zebrasms":
                new_key = text.strip()
                if not new_key:
                    bot.send_message(chat_id, "⛔ Key cannot be empty.", reply_markup=cancel_keyboard())
                    return
                set_api_key("zebrasms", new_key)
                admin_states.pop(user.id, None)
                live = _zebrasms_live_access()
                live_txt = "🟩 Live access OK" if live["ok"] else "🟥 Live access check failed"
                bot.send_message(chat_id, f"✔️ {stylish('ZebraSMS API key saved!')}\n{live_txt}", reply_markup=api_management_keyboard())
                return

            # ── ADMIN MANAGEMENT ──────────────────────────────────────────────
            if step == "amgmt_add_uid":
                if not _is_main_admin:
                    admin_states.pop(user.id, None)
                    return
                try:
                    new_admin_id = int(text.strip())
                except ValueError:
                    bot.send_message(chat_id, "⛔ Invalid ID. Enter a numeric User ID.", reply_markup=cancel_keyboard())
                    return
                if new_admin_id == ADMIN_ID:
                    bot.send_message(chat_id, "ℹ️ This is already the main admin.", reply_markup=admin_management_keyboard())
                    admin_states.pop(user.id, None)
                    return
                with get_conn() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO admins (user_id, added_by) VALUES (?,?)",
                        (new_admin_id, user.id),
                    )
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ User <code>{new_admin_id}</code> added as admin.", reply_markup=admin_management_keyboard())
                try:
                    bot.send_message(new_admin_id, "✔️ You have been granted admin access.")
                except Exception:
                    pass
                return

            elif step == "amgmt_remove_uid":
                if not _is_main_admin:
                    admin_states.pop(user.id, None)
                    return
                try:
                    rem_id = int(text.strip())
                except ValueError:
                    bot.send_message(chat_id, "⛔ Invalid ID.", reply_markup=cancel_keyboard())
                    return
                with get_conn() as conn:
                    conn.execute("DELETE FROM admins WHERE user_id=?", (rem_id,))
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ User <code>{rem_id}</code> removed from admins.", reply_markup=admin_management_keyboard())
                try:
                    bot.send_message(rem_id, f"ℹ️ {stylish('Your admin access has been revoked.')}")
                except Exception:
                    pass
                return

            # ── BAN / UNBAN ───────────────────────────────────────────────────
            if step == "aban_uid":
                try:
                    uid = int(text.strip())
                except ValueError:
                    bot.send_message(chat_id, "⛔ Invalid User ID.", reply_markup=cancel_keyboard())
                    return
                with get_conn() as conn:
                    conn.execute("UPDATE users SET is_banned=1 WHERE id=?", (uid,))
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"🛑 User <code>{uid}</code> has been <b>banned</b>.", reply_markup=ban_unban_keyboard())
                try:
                    bot.send_message(uid, "🛑 You have been banned by admin.")
                except Exception:
                    pass
                return

            elif step == "aunban_uid":
                try:
                    uid = int(text.strip())
                except ValueError:
                    bot.send_message(chat_id, "⛔ Invalid User ID.", reply_markup=cancel_keyboard())
                    return
                with get_conn() as conn:
                    conn.execute("UPDATE users SET is_banned=0 WHERE id=?", (uid,))
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ User <code>{uid}</code> has been <b>unbanned</b>.", reply_markup=ban_unban_keyboard())
                try:
                    bot.send_message(uid, f"✔️ {stylish('You have been unbanned.')}")
                except Exception:
                    pass
                return

            # ── BALANCE ───────────────────────────────────────────────────────
            elif step == "aset_otp_earn":
                try:
                    val = float(text.strip())
                    if val < 0:
                        raise ValueError()
                except ValueError:
                    bot.send_message(chat_id, "⛔ Invalid amount.", reply_markup=cancel_keyboard())
                    return
                set_setting("otp_earn_bdt", val)
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ OTP earn rate set to <b>{val:.2f} BDT</b>.", reply_markup=balance_management_keyboard())
                return

            elif step == "aadd_balance":
                parts = text.strip().split()
                if len(parts) < 2:
                    bot.send_message(chat_id, "⛔ Format: <code>user_id amount</code>", reply_markup=cancel_keyboard())
                    return
                try:
                    uid = int(parts[0])
                    amt = float(parts[1])
                    if amt <= 0:
                        raise ValueError()
                except ValueError:
                    bot.send_message(chat_id, "⛔ Invalid input.", reply_markup=cancel_keyboard())
                    return
                with get_conn() as conn:
                    conn.execute("INSERT OR IGNORE INTO wallet (user_id) VALUES (?)", (uid,))
                    conn.execute(
                        "UPDATE wallet SET balance=balance+?, total_income=total_income+? WHERE user_id=?",
                        (amt, amt, uid),
                    )
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ Added <b>{amt:.2f} BDT</b> to user <code>{uid}</code>.", reply_markup=balance_management_keyboard())
                try:
                    bot.send_message(uid, f"💲 Admin added <b>{amt:.2f} BDT</b> to your wallet.")
                except Exception:
                    pass
                return

            elif step == "arem_balance":
                parts = text.strip().split()
                if len(parts) < 2:
                    bot.send_message(chat_id, "⛔ Format: <code>user_id amount</code>", reply_markup=cancel_keyboard())
                    return
                try:
                    uid = int(parts[0])
                    amt = float(parts[1])
                    if amt <= 0:
                        raise ValueError()
                except ValueError:
                    bot.send_message(chat_id, "⛔ Invalid input.", reply_markup=cancel_keyboard())
                    return
                with get_conn() as conn:
                    conn.execute("INSERT OR IGNORE INTO wallet (user_id) VALUES (?)", (uid,))
                    conn.execute(
                        "UPDATE wallet SET balance=MAX(0, balance-?) WHERE user_id=?",
                        (amt, uid),
                    )
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ Removed <b>{amt:.2f} BDT</b> from user <code>{uid}</code>.", reply_markup=balance_management_keyboard())
                try:
                    bot.send_message(uid, f"🪙 Admin deducted <b>{amt:.2f} BDT</b> from your wallet.")
                except Exception:
                    pass
                return

            # ── WITHDRAW METHODS ──────────────────────────────────────────────
            # ── TEMP MAIL DOMAIN FLOWS ────────────────────────────────────
            elif step.startswith("atm_"):
                if text.strip() in ("❎️ Cancel", "⛔ Cancel"):
                    admin_states.pop(user.id, None)
                    bot.send_message(
                        chat_id, "⛔ Cancelled.", reply_markup=temp_mail_admin_keyboard()
                    )
                    return

                data = state.setdefault("data", {})

                if step == "atm_add_domain":
                    domain = text.strip().lstrip("@").lower()
                    if not domain or "." not in domain or " " in domain:
                        bot.send_message(
                            chat_id,
                            "⛔ সঠিক domain লিখুন (যেমন <code>mydomain.com</code>).",
                            reply_markup=cancel_keyboard(),
                        )
                        return
                    data["domain"] = domain
                    state["step"] = "atm_add_provider"
                    bot.send_message(
                        chat_id,
                        f"🛰 Domain: <code>{_html.escape(domain)}</code>\n\n"
                        "এই domain এর mail কোথা থেকে আসবে?\n\n"
                        "• <b>Own Domain (IMAP)</b> — আপনার নিজের domain, catch-all mailbox "
                        "(IMAP login লাগবে)\n"
                        "• বাকিগুলো — ওই free service এর domain হলে সেটা ব্যবহার হবে",
                        reply_markup=temp_mail_provider_keyboard(),
                    )
                    return

                if step == "atm_add_provider":
                    provider = TEMP_MAIL_PROVIDER_CHOICES.get(text.strip())
                    if not provider:
                        bot.send_message(
                            chat_id,
                            "⛔ নিচের যেকোনো একটি বেছে নিন।",
                            reply_markup=temp_mail_provider_keyboard(),
                        )
                        return
                    data["provider"] = provider
                    if provider != "imap":
                        add_temp_mail_domain(data["domain"], provider)
                        admin_states.pop(user.id, None)
                        bot.send_message(
                            chat_id,
                            f"✔️ Domain <code>{_html.escape(data['domain'])}</code> যোগ হয়েছে।\n\n"
                            f"{_temp_mail_domains_text()}",
                            reply_markup=temp_mail_admin_keyboard(),
                        )
                        return
                    state["step"] = "atm_add_imap_host"
                    bot.send_message(
                        chat_id,
                        "📥 IMAP server host লিখুন (যেমন <code>imap.hostinger.com</code>):",
                        reply_markup=cancel_keyboard(),
                    )
                    return

                if step == "atm_add_imap_host":
                    data["imap_host"] = text.strip()
                    state["step"] = "atm_add_imap_port"
                    bot.send_message(
                        chat_id,
                        "🔢 IMAP port লিখুন (SSL হলে <code>993</code>):",
                        reply_markup=cancel_keyboard(),
                    )
                    return

                if step == "atm_add_imap_port":
                    raw_port = text.strip()
                    if not raw_port.isdigit():
                        bot.send_message(
                            chat_id, "⛔ শুধু সংখ্যা লিখুন (993).", reply_markup=cancel_keyboard()
                        )
                        return
                    data["imap_port"] = int(raw_port)
                    state["step"] = "atm_add_imap_user"
                    bot.send_message(
                        chat_id,
                        "🧑 Catch-all mailbox এর email/username লিখুন\n"
                        f"(যেমন <code>catchall@{_html.escape(data['domain'])}</code>):",
                        reply_markup=cancel_keyboard(),
                    )
                    return

                if step == "atm_add_imap_user":
                    data["imap_user"] = text.strip()
                    state["step"] = "atm_add_imap_pass"
                    bot.send_message(
                        chat_id,
                        "🗝 ওই mailbox এর password লিখুন:",
                        reply_markup=cancel_keyboard(),
                    )
                    return

                if step == "atm_add_imap_pass":
                    data["imap_pass"] = text
                    try:
                        bot.delete_message(chat_id, message.message_id)
                    except Exception:
                        pass
                    config = {
                        "imap_host": data.get("imap_host"),
                        "imap_port": data.get("imap_port", 993),
                        "imap_user": data.get("imap_user"),
                        "imap_pass": data.get("imap_pass"),
                        "imap_folder": "INBOX",
                    }
                    try:
                        temp_mail_engine.imap_test_sync(config)
                    except Exception as exc:
                        admin_states.pop(user.id, None)
                        bot.send_message(
                            chat_id,
                            "⛔ IMAP connection failed:\n"
                            f"<code>{_html.escape(str(exc))}</code>\n\n"
                            "Host/port/username/password আবার দেখে try করুন।",
                            reply_markup=temp_mail_admin_keyboard(),
                        )
                        return
                    add_temp_mail_domain(
                        data["domain"],
                        "imap",
                        imap_host=config["imap_host"],
                        imap_port=config["imap_port"],
                        imap_user=config["imap_user"],
                        imap_pass=config["imap_pass"],
                        imap_folder="INBOX",
                    )
                    admin_states.pop(user.id, None)
                    bot.send_message(
                        chat_id,
                        "✔️ IMAP connection OK!\n"
                        f"Domain <code>{_html.escape(data['domain'])}</code> যোগ হয়েছে।\n"
                        "এখন Get Mail এই domain এ address বানাবে।\n\n"
                        f"{_temp_mail_domains_text()}",
                        reply_markup=temp_mail_admin_keyboard(),
                    )
                    return

                if step == "atm_del_domain":
                    removed = delete_temp_mail_domain(text.strip())
                    admin_states.pop(user.id, None)
                    bot.send_message(
                        chat_id,
                        ("✔️ Domain deleted." if removed else "⛔ Domain not found.")
                        + f"\n\n{_temp_mail_domains_text()}",
                        reply_markup=temp_mail_admin_keyboard(),
                    )
                    return

                if step == "atm_toggle_domain":
                    toggle_temp_mail_domain(text.strip())
                    admin_states.pop(user.id, None)
                    bot.send_message(
                        chat_id,
                        f"🔁 Updated.\n\n{_temp_mail_domains_text()}",
                        reply_markup=temp_mail_admin_keyboard(),
                    )
                    return

                admin_states.pop(user.id, None)
                bot.send_message(
                    chat_id, _temp_mail_admin_status_text(), reply_markup=temp_mail_admin_keyboard()
                )
                return

            elif step == "awm_add":
                method_name = text.strip().capitalize()
                if not method_name:
                    bot.send_message(chat_id, "⛔ Name cannot be empty.", reply_markup=cancel_keyboard())
                    return
                with get_conn() as conn:
                    try:
                        conn.execute("INSERT INTO withdraw_methods (name) VALUES (?)", (method_name,))
                        added = True
                    except Exception:
                        added = False
                admin_states.pop(user.id, None)
                if added:
                    bot.send_message(chat_id, f"✔️ Method <b>{method_name}</b> added.", reply_markup=withdraw_management_keyboard())
                else:
                    bot.send_message(chat_id, f"❗️ Method <b>{method_name}</b> already exists.", reply_markup=withdraw_management_keyboard())
                return

            elif step == "awm_del":
                method_name = text.strip()
                with get_conn() as conn:
                    conn.execute("DELETE FROM withdraw_methods WHERE name=?", (method_name,))
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ Method <b>{method_name}</b> removed.", reply_markup=withdraw_management_keyboard())
                return

            elif step == "awm_set_min":
                try:
                    val = float(text.strip())
                    if val < 0:
                        raise ValueError()
                except ValueError:
                    bot.send_message(chat_id, "⛔ Invalid amount.", reply_markup=cancel_keyboard())
                    return
                set_setting("min_withdraw_bdt", val)
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ Minimum withdraw set to <b>{val:.0f} BDT</b>.", reply_markup=withdraw_management_keyboard())
                return

            # ── AUTO SMS SETTINGS ─────────────────────────────────────────────
            if step == "aauto_chat_id":
                raw = text.strip()
                if not (re.fullmatch(r"-?\d{5,20}", raw) or raw.startswith("@")):
                    bot.send_message(chat_id, "⛔ Invalid chat ID (e.g. -1001234567890 or @groupname).", reply_markup=cancel_keyboard())
                    return
                set_unified_forward_chat_id(raw)
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ Forward Chat ID saved: <code>{raw}</code> (real OTP & Demo SMS both go here)\n\n" + _auto_sms_status_text(), reply_markup=auto_sms_keyboard())
                return

            if step == "aauto_demo_delay":
                raw = text.strip().lower().replace(" ", "")
                m = re.fullmatch(r"(\d+)(s|sec|secs|second|seconds|m|min|mins|minute|minutes)?", raw)
                if not m:
                    bot.send_message(chat_id, "⛔ Invalid delay. Example: 30s or 5m",
                                     reply_markup=cancel_keyboard())
                    return
                val = int(m.group(1))
                unit = m.group(2) or "s"
                if unit.startswith("m"):
                    val *= 60
                if val < DEMO_SMS_MIN_DELAY or val > DEMO_SMS_MAX_DELAY:
                    bot.send_message(
                        chat_id,
                        f"⛔ Delay must be between <b>{DEMO_SMS_MIN_DELAY} sec</b> and "
                        f"<b>{DEMO_SMS_MAX_DELAY // 60} min</b>.",
                        reply_markup=cancel_keyboard(),
                    )
                    return
                set_setting("auto_sms_demo_delay", val)
                admin_states.pop(user.id, None)
                bot.send_message(
                    chat_id,
                    f"✔️ Demo delay set to <b>{fmt_delay(val)}</b>.",
                )
                bot.send_message(chat_id, _auto_sms_status_text(), reply_markup=auto_sms_keyboard())
                return

            # ── BROADCAST ─────────────────────────────────────────────────────
            if step == "abroadcast_wait":
                with get_conn() as conn:
                    users_list = conn.execute("SELECT id FROM users WHERE is_banned=0").fetchall()
                sent = 0
                for u in users_list:
                    try:
                        bot.copy_message(u["id"], chat_id, message.message_id)
                        sent += 1
                    except Exception:
                        pass
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ Broadcast sent to <b>{sent}</b> users.", reply_markup=admin_keyboard(is_main_admin=_is_main_admin))
                return

            # ── FORCE JOIN: Add Channel (Task 3) ──────────────────────────────
            elif step == "aset_fj_add_channel":
                link = text.strip()
                ch_id, ch_name, ch_url = parse_channel_link(link)
                if not ch_id:
                    bot.send_message(
                        chat_id,
                        "⛔ Invalid link. Send a valid Telegram link like:\n<code>https://t.me/yourchannel</code>",
                        reply_markup=cancel_keyboard(),
                    )
                    return
                with get_conn() as conn:
                    conn.execute(
                        "INSERT INTO join_channels (channel_id, channel_name, channel_url, channel_type) VALUES (?,?,?,?)",
                        (ch_id, ch_name, ch_url, "channel"),
                    )
                admin_states.pop(user.id, None)
                bot.send_message(
                    chat_id,
                    f"✔️ Channel <b>{ch_name}</b> added successfully!\n\n"
                    f"❗️ Make sure the bot is an <b>admin</b> in that channel for membership checks to work.",
                    reply_markup=force_join_keyboard(),
                )
                return

            # ── FORCE JOIN: Add Group (Task 3) ────────────────────────────────
            elif step == "aset_fj_add_group":
                link = text.strip()
                ch_id, ch_name, ch_url = parse_channel_link(link)
                if not ch_id:
                    bot.send_message(
                        chat_id,
                        "⛔ Invalid link. Send a valid Telegram link like:\n<code>https://t.me/yourgroup</code>",
                        reply_markup=cancel_keyboard(),
                    )
                    return
                with get_conn() as conn:
                    conn.execute(
                        "INSERT INTO join_channels (channel_id, channel_name, channel_url, channel_type) VALUES (?,?,?,?)",
                        (ch_id, ch_name, ch_url, "group"),
                    )
                admin_states.pop(user.id, None)
                bot.send_message(
                    chat_id,
                    f"✔️ Group <b>{ch_name}</b> added successfully!\n\n"
                    f"❗️ Make sure the bot is an <b>admin</b> in that group for membership checks to work.",
                    reply_markup=force_join_keyboard(),
                )
                return

            # ── FORCE JOIN: Delete Channel ────────────────────────────────────
            elif step == "aset_fj_del_ch":
                ch_name = text.strip()
                with get_conn() as conn:
                    conn.execute("DELETE FROM join_channels WHERE channel_name=?", (ch_name,))
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ Channel <b>{ch_name}</b> removed.", reply_markup=force_join_keyboard())
                return

            # ── FORCE JOIN: Delete Group ──────────────────────────────────────
            elif step == "aset_fj_del_gr":
                gr_name = text.strip()
                with get_conn() as conn:
                    conn.execute("DELETE FROM join_channels WHERE channel_name=?", (gr_name,))
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ Group <b>{gr_name}</b> removed.", reply_markup=force_join_keyboard())
                return

            # ── OTHERS LINK: Support Btn (Task 4) ────────────────────────────
            elif step == "aset_ol_support":
                link = text.strip()
                if not link.startswith("http"):
                    bot.send_message(chat_id, "⛔ Please send a valid URL (starting with http/https).", reply_markup=cancel_keyboard())
                    return
                set_setting("support_link", normalize_link(link))
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ Support link saved:\n{link}", reply_markup=others_link_keyboard())
                return

            # ── OTHERS LINK: OTP Group Btn (Task 4) ──────────────────────────
            elif step == "aset_ol_otpgroup":
                link = text.strip()
                if not link.startswith("http"):
                    bot.send_message(chat_id, "⛔ Please send a valid URL (starting with http/https).", reply_markup=cancel_keyboard())
                    return
                set_setting("otp_group_link", link)
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ OTP Group link saved:\n{link}", reply_markup=others_link_keyboard())
                return

            # ── OTHERS LINK: Number Panel Link ───────────────────────────────
            elif step == "aset_ol_panel_link":
                link = text.strip()
                if not link.startswith("http"):
                    bot.send_message(chat_id, "⛔ Please send a valid URL (starting with http/https).", reply_markup=cancel_keyboard())
                    return
                set_setting("panel_link", link)
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ Number Panel link saved:\n{link}", reply_markup=others_link_keyboard())
                return

            # ── OTHERS LINK: Bot Developer Link ──────────────────────────────
            elif step == "aset_ol_auto_bot_link":
                link = normalize_link(text)
                if not link:
                    bot.send_message(chat_id, "❗️ Send a valid https:// link.", reply_markup=cancel_keyboard())
                    return
                set_setting("auto_sms_bot_link", link)
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ Auto SMS bot link saved:\n{link}", reply_markup=others_link_keyboard())
                return

            elif step == "aset_ol_auto_channel_link":
                link = normalize_link(text)
                if not link:
                    bot.send_message(chat_id, "❗️ Send a valid https:// link.", reply_markup=cancel_keyboard())
                    return
                set_setting("auto_sms_channel_link", link)
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ Auto SMS channel link saved:\n{link}", reply_markup=others_link_keyboard())
                return

            elif step == "aset_ol_dev_link":
                link = text.strip()
                if not link.startswith("http"):
                    bot.send_message(chat_id, "⛔ Please send a valid URL (starting with http/https).", reply_markup=cancel_keyboard())
                    return
                set_setting("dev_link", link)
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ Bot Developer link saved:\n{link}", reply_markup=others_link_keyboard())
                return

            # ── OTHERS LINK: Main Channel ───────────────────────────────────
            elif step == "aset_ol_main_channel":
                link = text.strip()
                if not link.startswith(("http://", "https://", "tg://")):
                    bot.send_message(
                        chat_id,
                        "⛔ Please send a valid Telegram URL (https://t.me/...).",
                        reply_markup=cancel_keyboard(),
                    )
                    return
                link = normalize_link(link)
                set_setting("main_channel_link", link)
                admin_states.pop(user.id, None)
                bot.send_message(
                    chat_id,
                    f"✔️ Main channel link saved:\n{_html.escape(link)}",
                    reply_markup=others_link_keyboard(),
                )
                return

            # ── OTHERS LINK: Payment Request ID (Task 4) ─────────────────────
            elif step == "aset_ol_payment_id":
                chat_id_val = text.strip()
                if not re.match(r'^-?\d+$', chat_id_val):
                    bot.send_message(chat_id, "⛔ Please send a valid numeric Chat ID (e.g. -1001234567890).", reply_markup=cancel_keyboard())
                    return
                set_setting("payment_forward_chat_id", chat_id_val)
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ Payment Request Chat ID saved: <code>{chat_id_val}</code>", reply_markup=others_link_keyboard())
                return

            # ── OTHERS LINK: OTP Forward ID (Task 4) ─────────────────────────
            elif step == "aset_ol_otp_fwd":
                chat_id_val = text.strip()
                if not (re.match(r'^-?\d+$', chat_id_val) or chat_id_val.startswith("@")):
                    bot.send_message(chat_id, "⛔ Please send a valid Chat ID (e.g. -1001234567890) or @username.", reply_markup=cancel_keyboard())
                    return
                set_unified_forward_chat_id(chat_id_val)
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ Forward Chat ID saved: <code>{chat_id_val}</code> (both real OTP and Demo SMS go here)", reply_markup=others_link_keyboard())
                return

            # ── OTHERS LINK: Bot Name ─────────────────────────────────────────
            elif step == "aset_ol_bot_name":
                val = text.strip()
                if not val:
                    bot.send_message(chat_id, "⛔ Bot name cannot be empty.", reply_markup=cancel_keyboard())
                    return
                set_setting("bot_name", val)
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ Bot name saved: <code>{val}</code>", reply_markup=others_link_keyboard())
                return

            # ── OTHERS LINK: Powered By ───────────────────────────────────────
            elif step == "aset_ol_powered_by":
                val = text.strip()
                if not val:
                    bot.send_message(chat_id, "⛔ Powered By text cannot be empty.", reply_markup=cancel_keyboard())
                    return
                set_setting("powered_by", val)
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, f"✔️ Powered By saved: <code>{val}</code>", reply_markup=others_link_keyboard())
                return

            # ── DEVELOPER INFO: set info text ─────────────────────────────────
            elif step == "adev_set_info":
                info_text = text.strip()
                if not info_text:
                    bot.send_message(chat_id, "⛔ Dev info cannot be empty.", reply_markup=cancel_keyboard())
                    return
                set_setting("developer_info", info_text)
                admin_states.pop(user.id, None)
                bot.send_message(chat_id, "✔️ Developer info saved!", reply_markup=developer_keyboard())
                return

            # ── ADD SERVICE: step 1 — service name (Task 1) ──────────────────
            if step == "asvc_add_name":
                service_name = text.strip()
                if not service_name:
                    bot.send_message(chat_id, "⛔ Service name cannot be empty.", reply_markup=cancel_keyboard())
                    return
                with get_conn() as conn:
                    try:
                        conn.execute("INSERT INTO services (name) VALUES (?)", (service_name,))
                        added = True
                    except Exception:
                        added = False
                admin_states.pop(user.id, None)
                if added:
                    bot.send_message(
                        chat_id,
                        f"✔️ <b>Service Added Successfully!</b>\n\n📟 Service: <b>{service_name}</b>",
                        reply_markup=manage_services_keyboard(),
                    )
                else:
                    bot.send_message(
                        chat_id,
                        f"❗️ Service <b>{service_name}</b> already exists.",
                        reply_markup=manage_services_keyboard(),
                    )
                return

            # ── IMPORT: step 1 — service selection ───────────────────────────
            elif step == "aimport_select_service":
                if text == "◀️ Back":
                    admin_states.pop(user.id, None)
                    bot.send_message(chat_id, f"🗂 <b>{stylish('Manage Services')}</b>", reply_markup=manage_services_keyboard())
                    return
                svc_name = text.replace("📟 ", "").strip()
                with get_conn() as conn:
                    svc = conn.execute("SELECT * FROM services WHERE name=?", (svc_name,)).fetchone()
                if not svc:
                    bot.send_message(chat_id, f"⛔ {stylish('Service not found.')}", reply_markup=import_service_keyboard())
                    return
                state["data"]["service_id"] = svc["id"]
                state["data"]["service_name"] = svc["name"]
                state["step"] = "aimport_country_info"
                bot.send_message(
                    chat_id,
                    f"✔️ Service: <b>{svc['name']}</b>\n\n"
                    f"Enter <b>Country Flag, Name and Code</b>:\n\nExample:\n🇧🇩 Bangladesh +880\n🇺🇸 USA +1",
                    reply_markup=cancel_keyboard(),
                )
                return

            # ── IMPORT: step 2 — country info ────────────────────────────────
            elif step == "aimport_country_info":
                parsed = parse_country_info(text)
                if not parsed:
                    bot.send_message(
                        chat_id,
                        "⛔ Invalid format. Use:\n🇧🇩 Bangladesh +880",
                        reply_markup=cancel_keyboard(),
                    )
                    return
                service_id = state["data"]["service_id"]
                with get_conn() as conn:
                    existing = conn.execute(
                        "SELECT * FROM countries WHERE service_id=? AND name=? AND code=?",
                        (service_id, parsed["name"], parsed["code"]),
                    ).fetchone()
                    if existing:
                        state["data"]["country_id"] = existing["id"]
                        state["data"]["country_flag"] = existing["flag"]
                        state["data"]["country_name"] = existing["name"]
                        state["data"]["country_code"] = existing["code"]
                    else:
                        conn.execute(
                            "INSERT INTO countries (service_id, flag, name, code) VALUES (?,?,?,?)",
                            (service_id, parsed["flag"], parsed["name"], parsed["code"]),
                        )
                        c = conn.execute(
                            "SELECT * FROM countries WHERE service_id=? AND name=? AND code=?",
                            (service_id, parsed["name"], parsed["code"]),
                        ).fetchone()
                        state["data"]["country_id"] = c["id"]
                        state["data"]["country_flag"] = parsed["flag"]
                        state["data"]["country_name"] = parsed["name"]
                        state["data"]["country_code"] = parsed["code"]
                state["step"] = "aimport_number_file"
                bot.send_message(
                    chat_id,
                    f"✔️ Country: <b>{state['data']['country_flag']} {state['data']['country_name']} "
                    f"+{state['data']['country_code']}</b>\n\n"
                    f"Upload a <b>.txt file</b> with numbers (one per line):\n\n"
                    f"Example:\n<code>8801711111111\n8801722222222</code>",
                    reply_markup=cancel_keyboard(),
                )
                return

            # ── INPUT RANGE: step 1 — service selection ──────────────────────
            elif step == "arange_select_service":
                if text == "◀️ Back":
                    admin_states.pop(user.id, None)
                    bot.send_message(chat_id, f"🗂 <b>{stylish('Manage Services')}</b>", reply_markup=manage_services_keyboard())
                    return
                svc_name = text.replace("📟 ", "").strip()
                with get_conn() as conn:
                    svc = conn.execute("SELECT * FROM services WHERE name=?", (svc_name,)).fetchone()
                if not svc:
                    bot.send_message(chat_id, f"⛔ {stylish('Service not found.')}", reply_markup=import_service_keyboard())
                    return
                state["data"]["service_id"] = svc["id"]
                state["data"]["service_name"] = svc["name"]
                state["step"] = "arange_country_info"
                bot.send_message(
                    chat_id,
                    f"✔️ Service: <b>{svc['name']}</b>\n\n"
                    f"Enter <b>Country Flag, Name and Code</b>:\n\nExample:\n🇧🇩 Bangladesh +880\n🇺🇸 USA +1",
                    reply_markup=cancel_keyboard(),
                )
                return

            # ── INPUT RANGE: step 2 — country info ────────────────────────────
            elif step == "arange_country_info":
                parsed = parse_country_info(text)
                if not parsed:
                    bot.send_message(
                        chat_id,
                        "⛔ Invalid format. Use:\n🇧🇩 Bangladesh +880",
                        reply_markup=cancel_keyboard(),
                    )
                    return
                service_id = state["data"]["service_id"]
                with get_conn() as conn:
                    existing = conn.execute(
                        "SELECT * FROM countries WHERE service_id=? AND name=? AND code=?",
                        (service_id, parsed["name"], parsed["code"]),
                    ).fetchone()
                    if existing:
                        state["data"]["country_id"] = existing["id"]
                        state["data"]["country_flag"] = existing["flag"]
                        state["data"]["country_name"] = existing["name"]
                        state["data"]["country_code"] = existing["code"]
                    else:
                        conn.execute(
                            "INSERT INTO countries (service_id, flag, name, code) VALUES (?,?,?,?)",
                            (service_id, parsed["flag"], parsed["name"], parsed["code"]),
                        )
                        c = conn.execute(
                            "SELECT * FROM countries WHERE service_id=? AND name=? AND code=?",
                            (service_id, parsed["name"], parsed["code"]),
                        ).fetchone()
                        state["data"]["country_id"] = c["id"]
                        state["data"]["country_flag"] = parsed["flag"]
                        state["data"]["country_name"] = parsed["name"]
                        state["data"]["country_code"] = parsed["code"]
                state["step"] = "arange_range_input"
                bot.send_message(
                    chat_id,
                    f"✔️ Country: <b>{state['data']['country_flag']} {state['data']['country_name']} "
                    f"+{state['data']['country_code']}</b>\n\n"
                    f"Enter the <b>Range ID</b> for this country (e.g. <code>880X01</code>):",
                    reply_markup=cancel_keyboard(),
                )
                return

            # ── INPUT RANGE: step 3 — range id ────────────────────────────────
            elif step == "arange_range_input":
                range_id = text.strip()
                if not range_id:
                    bot.send_message(chat_id, "⛔ Range ID cannot be empty.", reply_markup=cancel_keyboard())
                    return
                country_id = state["data"]["country_id"]
                with get_conn() as conn:
                    conn.execute("UPDATE countries SET range_id=? WHERE id=?", (range_id, country_id))
                admin_states.pop(user.id, None)
                bot.send_message(
                    chat_id,
                    f"✔️ <b>Range Saved!</b>\n\n"
                    f"📟 Service: <b>{state['data']['service_name']}</b>\n"
                    f"🗺 Country: <b>{state['data']['country_flag']} {state['data']['country_name']} "
                    f"+{state['data']['country_code']}</b>\n"
                    f"🧩 Range ID: <code>{range_id}</code>\n\n"
                    f"{stylish('Users selecting this country will now be allocated numbers dynamically from this range.')}",
                    reply_markup=manage_services_keyboard(),
                )
                return

            # ── DELETE SERVICE: select service ────────────────────────────────
            elif step == "adel_svc_select":
                if text == "◀️ Back":
                    admin_states.pop(user.id, None)
                    bot.send_message(chat_id, f"🗂 <b>{stylish('Manage Services')}</b>", reply_markup=manage_services_keyboard())
                    return
                svc_name = text.replace("📟 ", "").strip()
                with get_conn() as conn:
                    svc = conn.execute("SELECT * FROM services WHERE name=?", (svc_name,)).fetchone()
                if not svc:
                    bot.send_message(chat_id, f"⛔ {stylish('Service not found.')}", reply_markup=services_list_keyboard() or manage_services_keyboard())
                    return
                state["data"]["service_id"] = svc["id"]
                state["data"]["service_name"] = svc["name"]
                state["step"] = "adel_svc_options"
                bot.send_message(
                    chat_id,
                    f"📟 <b>{svc['name']}</b>\n\nWhat do you want to delete?",
                    reply_markup=delete_service_options_keyboard(),
                )
                return

            elif step == "adel_svc_options":
                if text == "◀️ Back":
                    state["step"] = "adel_svc_select"
                    kb = services_list_keyboard()
                    bot.send_message(chat_id, "🧹 <b>Select a Service to Delete:</b>",
                                     reply_markup=kb or manage_services_keyboard())
                    return
                elif text == f"🧹 {stylish('Delete Entire Service')}":
                    state["data"]["confirm_action"] = "delete_service"
                    state["step"] = "adel_confirm"
                    svc_name = state["data"]["service_name"]
                    bot.send_message(
                        chat_id,
                        f"❗️ Delete entire service <b>{svc_name}</b> and ALL its countries/numbers?",
                        reply_markup=confirm_keyboard_reply(),
                    )
                    return
                elif text == f"🗂 {stylish('Show Countries')}":
                    service_id = state["data"]["service_id"]
                    state["step"] = "adel_cntry_select"
                    bot.send_message(chat_id, f"🗺 <b>{stylish('Select a Country')}:</b>",
                                     reply_markup=delete_country_list_keyboard(service_id))
                    return
                return

            elif step == "adel_confirm":
                action = state["data"].get("confirm_action")
                if text == f"✔️ {stylish('Yes, Confirm')}":
                    if action == "delete_service":
                        service_id = state["data"]["service_id"]
                        svc_name = state["data"]["service_name"]
                        with get_conn() as conn:
                            conn.execute("DELETE FROM numbers WHERE country_id IN (SELECT id FROM countries WHERE service_id=?)", (service_id,))
                            conn.execute("DELETE FROM countries WHERE service_id=?", (service_id,))
                            conn.execute("DELETE FROM services WHERE id=?", (service_id,))
                        admin_states.pop(user.id, None)
                        bot.send_message(chat_id, f"✔️ Service <b>{svc_name}</b> deleted.", reply_markup=manage_services_keyboard())
                    elif action == "delete_country_full":
                        country_id = state["data"]["country_id"]
                        with get_conn() as conn:
                            c = conn.execute("SELECT * FROM countries WHERE id=?", (country_id,)).fetchone()
                            conn.execute("DELETE FROM numbers WHERE country_id=?", (country_id,))
                            conn.execute("DELETE FROM countries WHERE id=?", (country_id,))
                        admin_states.pop(user.id, None)
                        name = f"{c['flag']} {c['name']} +{c['code']}" if c else "Country"
                        bot.send_message(chat_id, f"✔️ Country <b>{name}</b> and numbers deleted.", reply_markup=manage_services_keyboard())
                    elif action == "delete_country_nums":
                        country_id = state["data"]["country_id"]
                        with get_conn() as conn:
                            c = conn.execute("SELECT * FROM countries WHERE id=?", (country_id,)).fetchone()
                            deleted = conn.execute("SELECT COUNT(*) FROM numbers WHERE country_id=?", (country_id,)).fetchone()[0]
                            conn.execute("DELETE FROM numbers WHERE country_id=?", (country_id,))
                        admin_states.pop(user.id, None)
                        name = f"{c['flag']} {c['name']} +{c['code']}" if c else "Country"
                        bot.send_message(chat_id, f"✔️ Deleted <b>{deleted}</b> numbers from <b>{name}</b>.", reply_markup=manage_services_keyboard())
                    elif action == "reset_country":
                        country_id = state["data"]["country_id"]
                        with get_conn() as conn:
                            c = conn.execute("SELECT * FROM countries WHERE id=?", (country_id,)).fetchone()
                            count = conn.execute("SELECT COUNT(*) FROM numbers WHERE country_id=? AND assigned=1", (country_id,)).fetchone()[0]
                            conn.execute("UPDATE numbers SET assigned=0, assigned_to=NULL, assigned_at=NULL WHERE country_id=?", (country_id,))
                        admin_states.pop(user.id, None)
                        name = f"{c['flag']} {c['name']} +{c['code']}" if c else "Country"
                        bot.send_message(chat_id, f"✔️ Reset <b>{count}</b> numbers in <b>{name}</b>.", reply_markup=manage_services_keyboard())
                elif text == f"⛔ {stylish('No, Cancel')}":
                    admin_states.pop(user.id, None)
                    bot.send_message(chat_id, "⛔ Cancelled.", reply_markup=manage_services_keyboard())
                return

            elif step == "adel_cntry_select":
                if text == "◀️ Back":
                    state["step"] = "adel_svc_options"
                    bot.send_message(chat_id, f"📟 <b>{state['data']['service_name']}</b>\n\nWhat do you want to delete?",
                                     reply_markup=delete_service_options_keyboard())
                    return
                service_id = state["data"]["service_id"]
                with get_conn() as conn:
                    countries = conn.execute("SELECT * FROM countries WHERE service_id=? ORDER BY name", (service_id,)).fetchall()
                selected = None
                for c in countries:
                    if text == f"{c['flag']} {c['name']} +{c['code']}":
                        selected = c
                        break
                if not selected:
                    return
                state["data"]["country_id"] = selected["id"]
                state["data"]["country_name"] = selected["name"]
                state["data"]["country_flag"] = selected["flag"]
                state["data"]["country_code"] = selected["code"]
                state["step"] = "adel_cntry_options"
                bot.send_message(
                    chat_id,
                    f"🗺 <b>{selected['flag']} {selected['name']} +{selected['code']}</b>\n\nWhat do you want to delete?",
                    reply_markup=delete_country_options_keyboard(),
                )
                return

            elif step == "adel_cntry_options":
                if text == "◀️ Back":
                    service_id = state["data"]["service_id"]
                    state["step"] = "adel_cntry_select"
                    bot.send_message(chat_id, f"🗺 <b>{stylish('Select a Country')}:</b>",
                                     reply_markup=delete_country_list_keyboard(service_id))
                    return
                elif text == f"🧹 {stylish('Delete Country + Numbers')}":
                    state["data"]["confirm_action"] = "delete_country_full"
                    state["step"] = "adel_confirm"
                    c = state["data"]
                    bot.send_message(
                        chat_id,
                        f"❗️ Delete <b>{c['country_flag']} {c['country_name']} +{c['country_code']}</b> and ALL its numbers?",
                        reply_markup=confirm_keyboard_reply(),
                    )
                    return
                elif text == f"🧹 {stylish('Delete Numbers Only')}":
                    state["data"]["confirm_action"] = "delete_country_nums"
                    state["step"] = "adel_confirm"
                    c = state["data"]
                    bot.send_message(
                        chat_id,
                        f"❗️ Delete all numbers from <b>{c['country_flag']} {c['country_name']} +{c['country_code']}</b>?",
                        reply_markup=confirm_keyboard_reply(),
                    )
                    return
                return

            elif step == "areset_svc_select":
                if text == "◀️ Back":
                    admin_states.pop(user.id, None)
                    bot.send_message(chat_id, f"🗂 <b>{stylish('Manage Services')}</b>", reply_markup=manage_services_keyboard())
                    return
                svc_name = text.replace("📟 ", "").strip()
                with get_conn() as conn:
                    svc = conn.execute("SELECT * FROM services WHERE name=?", (svc_name,)).fetchone()
                if not svc:
                    return
                state["data"]["service_id"] = svc["id"]
                state["data"]["service_name"] = svc["name"]
                state["step"] = "areset_cntry_select"
                bot.send_message(chat_id, "🗺 <b>Select Country to Reset:</b>",
                                 reply_markup=reset_countries_keyboard(svc["id"]))
                return

            elif step == "areset_cntry_select":
                if text == "◀️ Back":
                    state["step"] = "areset_svc_select"
                    kb = reset_services_keyboard()
                    bot.send_message(chat_id, "♻ <b>Select Service to Reset Numbers:</b>",
                                     reply_markup=kb or manage_services_keyboard())
                    return
                service_id = state["data"]["service_id"]
                with get_conn() as conn:
                    countries = conn.execute("SELECT * FROM countries WHERE service_id=? ORDER BY name", (service_id,)).fetchall()
                selected = None
                for c in countries:
                    if text == f"{c['flag']} {c['name']} +{c['code']}":
                        selected = c
                        break
                if not selected:
                    return
                state["data"]["country_id"] = selected["id"]
                state["data"]["country_flag"] = selected["flag"]
                state["data"]["country_name"] = selected["name"]
                state["data"]["country_code"] = selected["code"]
                state["data"]["confirm_action"] = "reset_country"
                state["step"] = "adel_confirm"
                with get_conn() as conn:
                    count = conn.execute("SELECT COUNT(*) FROM numbers WHERE country_id=? AND assigned=1", (selected["id"],)).fetchone()[0]
                bot.send_message(
                    chat_id,
                    f"❗️ Reset <b>{count}</b> assigned numbers in <b>{selected['flag']} {selected['name']} +{selected['code']}</b>?",
                    reply_markup=confirm_keyboard_reply(),
                )
                return

        # ══════════════════════════════════════════════════════════════════════
        # ADMIN MAIN MENU BUTTONS (no active state)
        # ══════════════════════════════════════════════════════════════════════
        if text == f"🛠️ {stylish('Manage Services')}":
            bot.send_message(chat_id, f"🗂 <b>{stylish('Manage Services')}</b>", reply_markup=manage_services_keyboard())
            return

        elif text == f"📊 {stylish('Dashboard')}":
            _show_dashboard(chat_id, is_main_admin=_is_main_admin)
            return

        elif text == f"🛡️ {stylish('Ban Unban')}":
            bot.send_message(chat_id, "🛑 <b>Ban / Unban Panel</b>", reply_markup=ban_unban_keyboard())
            return

        elif text == f"🛑 {stylish('Ban User')}":
            admin_states[user.id] = {"step": "aban_uid", "data": {}}
            bot.send_message(chat_id, "🛑 Enter the <b>User ID</b> to ban:", reply_markup=cancel_keyboard())
            return

        elif text == f"✔️ {stylish('Unban User')}":
            admin_states[user.id] = {"step": "aunban_uid", "data": {}}
            bot.send_message(chat_id, "✔️ Enter the <b>User ID</b> to unban:", reply_markup=cancel_keyboard())
            return

        elif text == f"📣 {stylish('Broadcast')}":
            admin_states[user.id] = {"step": "abroadcast_wait", "data": {}}
            bot.send_message(
                chat_id,
                "📣 <b>Broadcast</b>\n\nSend any message to broadcast to all users.\n\nPress ⛔ Cancel to abort.",
                reply_markup=cancel_keyboard(),
            )
            return

        elif text == f"💎 {stylish('Balance Mgmt')}":
            earn = get_otp_earn()
            bot.send_message(
                chat_id,
                f"💲 <b>Balance Management</b>\n\n🎛️ Current OTP Earn: <b>{earn:.2f} BDT</b>",
                reply_markup=balance_management_keyboard(),
            )
            return

        elif text == f"🎛️ {stylish('Set OTP Earn')}":
            admin_states[user.id] = {"step": "aset_otp_earn", "data": {}}
            bot.send_message(chat_id, "🎛️ Enter new <b>OTP earn amount</b> per OTP (in BDT):", reply_markup=cancel_keyboard())
            return

        elif text == f"💲 {stylish('Add Balance')}":
            admin_states[user.id] = {"step": "aadd_balance", "data": {}}
            bot.send_message(chat_id, "💲 Enter: <code>user_id amount</code>\nExample: <code>123456789 50</code>", reply_markup=cancel_keyboard())
            return

        elif text == f"➖ {stylish('Remove Balance')}":
            admin_states[user.id] = {"step": "arem_balance", "data": {}}
            bot.send_message(chat_id, "➖ Enter: <code>user_id amount</code>\nExample: <code>123456789 50</code>", reply_markup=cancel_keyboard())
            return

        elif text == f"🪙 {stylish('Withdraw Mgmt')}":
            min_wd = get_min_withdraw()
            bot.send_message(
                chat_id,
                f"🪙 <b>Withdraw Management</b>\n\n🎛️ Min Withdraw: <b>{min_wd:.0f} BDT</b>",
                reply_markup=withdraw_management_keyboard(),
            )
            return

        elif text == f"➕ {stylish('Add Method')}":
            admin_states[user.id] = {"step": "awm_add", "data": {}}
            bot.send_message(chat_id, "➕ Enter the payment method name (e.g. Bkash, Nagad, Rocket):", reply_markup=cancel_keyboard())
            return

        elif text == f"🧹 {stylish('Delete Method')}":
            methods = get_all_withdraw_methods()
            if not methods:
                bot.send_message(chat_id, "⛔ No methods found.", reply_markup=withdraw_management_keyboard())
                return
            admin_states[user.id] = {"step": "awm_del", "data": {}}
            kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
            for m in methods:
                kb.add(KeyboardButton(m["name"]))
            kb.add(KeyboardButton("❎️ Cancel"))
            bot.send_message(chat_id, "🧹 Select method to delete:", reply_markup=kb)
            return

        elif text == f"🧾 {stylish('List Methods')}":
            methods = get_all_withdraw_methods()
            if not methods:
                bot.send_message(chat_id, "⛔ No methods configured.", reply_markup=withdraw_management_keyboard())
            else:
                lines = ["<b>🪙 Withdraw Methods:</b>\n"]
                for m in methods:
                    status = "✔️" if m["is_enabled"] else "⛔"
                    lines.append(f"{status} {m['name']}")
                bot.send_message(chat_id, "\n".join(lines), reply_markup=withdraw_management_keyboard())
            return

        elif text == f"🎛️ {stylish('Set Min Withdraw')}":
            admin_states[user.id] = {"step": "awm_set_min", "data": {}}
            bot.send_message(chat_id, f"🎛️ {stylish('Enter minimum withdraw amount (in BDT):')}  ", reply_markup=cancel_keyboard())
            return

        # ── SETTINGS (Task 2) ──────────────────────────────────────────────
        elif text == f"🔧 {stylish('Settings')}":
            fj = "🟩 ON" if is_force_join_enabled() else "🟥 OFF"
            support = get_setting("support_link", "Not set")
            otp_grp = get_setting("otp_group_link", "Not set")
            pmt_id = get_setting("payment_forward_chat_id", "Not set")
            otp_fwd = get_unified_forward_chat_id() or "Not set"
            bot.send_message(
                chat_id,
                f"🎛️ <b>Settings</b>\n\n"
                f"🔏 Force Join: <b>{fj}</b>\n\n"
                f"☎ Support Link: <code>{support}</code>\n"
                f"🗨 OTP Group Link: <code>{otp_grp}</code>\n"
                f"🪪 Payment Fwd ID: <code>{pmt_id}</code>\n"
                f"📨 OTP Fwd ID: <code>{otp_fwd}</code>",
                reply_markup=settings_keyboard(is_main_admin=(user.id == ADMIN_ID)),
            )
            return

        # ── LEADERBOARD ON/OFF ───────────────────────────────────────────
        elif text in (f"🏆 {stylish('Leaderboard')}: ON", f"🏆 {stylish('Leaderboard')}: OFF"):
            new_val = "0" if is_leaderboard_enabled() else "1"
            set_setting("leaderboard_enabled", new_val)
            status = "🟩 ENABLED" if new_val == "1" else "🟥 DISABLED"
            bot.send_message(
                chat_id,
                f"✔️ Leaderboard is now <b>{status}</b>",
                reply_markup=settings_keyboard(is_main_admin=(user.id == ADMIN_ID)),
            )
            return

        # ── TEMP MAIL DOMAINS ────────────────────────────────────────────
        elif text == f"📬 {stylish('Temp Mail Domains')}":
            bot.send_message(
                chat_id, _temp_mail_admin_status_text(), reply_markup=temp_mail_admin_keyboard()
            )
            return

        elif text == f"➕ {stylish('Add Mail Domain')}":
            admin_states[user.id] = {"step": "atm_add_domain", "data": {}}
            bot.send_message(
                chat_id,
                "➕ <b>Add Mail Domain</b>\n\n"
                "আপনার domain টি লিখুন (যেমন <code>mydomain.com</code>):",
                reply_markup=cancel_keyboard(),
            )
            return

        elif text == f"🧾 {stylish('List Mail Domains')}":
            bot.send_message(
                chat_id, _temp_mail_domains_text(), reply_markup=temp_mail_admin_keyboard()
            )
            return

        elif text == f"🧹 {stylish('Del Mail Domain')}":
            rows = get_temp_mail_domains()
            if not rows:
                bot.send_message(
                    chat_id, "⛔ No mail domain added.", reply_markup=temp_mail_admin_keyboard()
                )
                return
            admin_states[user.id] = {"step": "atm_del_domain", "data": {}}
            kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
            for row in rows:
                kb.add(KeyboardButton(row["domain"]))
            kb.add(KeyboardButton("❎️ Cancel"))
            bot.send_message(chat_id, "🧹 Select a domain to delete:", reply_markup=kb)
            return

        elif text == f"🔁 {stylish('Toggle Mail Domain')}":
            rows = get_temp_mail_domains()
            if not rows:
                bot.send_message(
                    chat_id, "⛔ No mail domain added.", reply_markup=temp_mail_admin_keyboard()
                )
                return
            admin_states[user.id] = {"step": "atm_toggle_domain", "data": {}}
            kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
            for row in rows:
                kb.add(KeyboardButton(row["domain"]))
            kb.add(KeyboardButton("❎️ Cancel"))
            bot.send_message(chat_id, "🔁 Select a domain to enable/disable:", reply_markup=kb)
            return

        elif text == f"🧪 {stylish('Mail Provider Status')}":
            try:
                health = _run_temp_mail_coroutine(temp_mail_engine.provider_health())
                lines = ["🧪 <b>Free Mail Provider Status</b>\n"]
                for provider, is_up in health.items():
                    label = temp_mail_engine.PROVIDER_LABELS.get(provider, provider)
                    lines.append(f"{'🟩' if is_up else '🟥'} {label}")
                lines.append("\nGet Mail সবসময় প্রথমে যেটা কাজ করে সেটা ব্যবহার করবে।")
                bot.send_message(
                    chat_id, "\n".join(lines), reply_markup=temp_mail_admin_keyboard()
                )
            except Exception as exc:
                bot.send_message(
                    chat_id,
                    f"⛔ Status check failed: <code>{_html.escape(str(exc))}</code>",
                    reply_markup=temp_mail_admin_keyboard(),
                )
            return

        # ── AUTO SMS ─────────────────────────────────────────────────────
        elif text == f"🚀 {stylish('Auto SMS')}":
            bot.send_message(chat_id, _auto_sms_status_text(), reply_markup=auto_sms_keyboard())
            return

        elif text in (f"🚀 {stylish('Auto SMS')}: ON", f"🚀 {stylish('Auto SMS')}: OFF"):
            new_val = "0" if is_auto_sms_enabled() else "1"
            if new_val == "1" and not get_unified_forward_chat_id():
                bot.send_message(
                    chat_id,
                    f"⛔ {stylish('Set the Forward Group/Channel ID first.')}",
                    reply_markup=auto_sms_keyboard(),
                )
                return
            set_setting("auto_sms_enabled", new_val)
            bot.send_message(chat_id, _auto_sms_status_text(), reply_markup=auto_sms_keyboard())
            return

        elif text == f"🗨 {stylish('Set Forward Group ID')}":
            admin_states[user.id] = {"step": "aauto_chat_id", "data": {}}
            bot.send_message(
                chat_id,
                f"🗨 {stylish('Send the Auto SMS Group/Channel ID (e.g. -1001234567890):')}",
                reply_markup=cancel_keyboard(),
            )
            return

        elif text in (f"🧪 {stylish('Demo SMS')}: ON", f"🧪 {stylish('Demo SMS')}: OFF"):
            new_val = "0" if is_demo_sms_enabled() else "1"
            if new_val == "1" and not get_unified_forward_chat_id():
                bot.send_message(
                    chat_id,
                    f"⛔ {stylish('Set the Forward Group/Channel ID first.')}",
                    reply_markup=auto_sms_keyboard(),
                )
                return
            set_setting("auto_sms_demo_enabled", new_val)
            bot.send_message(chat_id, _auto_sms_status_text(), reply_markup=auto_sms_keyboard())
            return

        elif text == f"⏱ {stylish('Set Demo Delay')}":
            admin_states[user.id] = {"step": "aauto_demo_delay", "data": {}}
            bot.send_message(
                chat_id,
                f"⏱ {stylish('Send the demo delay.')}\n\n"
                f"Examples: <code>3s</code> · <code>30s</code> · <code>3m</code> · "
                f"<code>5m</code> · <code>10m</code>\n"
                f"Plain number = seconds. Minimum <b>3 sec</b>, maximum <b>60 min</b>.",
                reply_markup=cancel_keyboard(),
            )
            return

        elif text == f"📨 {stylish('Send Demo SMS Now')}":
            ok = _send_demo_sms()
            bot.send_message(
                chat_id,
                f"✔️ {stylish('Demo SMS sent.')}" if ok
                else f"⛔ {stylish('Could not send. Check the Auto SMS group ID.')}",
                reply_markup=auto_sms_keyboard(),
            )
            return

        elif text == f"🧹 {stylish('Del Auto SMS Group')}":
            delete_unified_forward_chat_id()
            set_setting("auto_sms_enabled", "0")
            set_setting("auto_sms_demo_enabled", "0")
            bot.send_message(chat_id, f"✔️ {stylish('Forward group removed.')}", reply_markup=auto_sms_keyboard())
            return

        # ── BACKUP MENU ──────────────────────────────────────────────────
        elif text == f"🗄 {stylish('Backup')}":
            bot.send_message(
                chat_id,
                f"🗄 <b>{stylish('Backup')}</b>\n\n"
                f"📤 {stylish('Backup File')} — {stylish('download the current database')}\n"
                f"📥 {stylish('Input File')} — {stylish('restore the database from a file you upload')}",
                reply_markup=backup_keyboard(),
            )
            return

        elif text == f"📤 {stylish('Backup File')}":
            admin_states.pop(user.id, None)
            try:
                with open(DB_PATH, "rb") as f:
                    bot.send_document(
                        chat_id,
                        f,
                        caption=f"🗄 {stylish('Database Backup')} — <code>voltx.db</code>",
                    )
            except Exception as e:
                logger.error(f"Manual backup error: {e}")
                bot.send_message(chat_id, f"⛔ {stylish('Backup failed.')}")
            bot.send_message(chat_id, f"✔️ {stylish('Backup sent.')}", reply_markup=backup_keyboard())
            return

        elif text == f"📥 {stylish('Input File')}":
            admin_states[user.id] = {"step": "abackup_restore_wait", "data": {}}
            bot.send_message(
                chat_id,
                f"📥 {stylish('Send the backup .db file to restore.')}\n\n"
                f"❗️ {stylish('This will replace the current database.')}",
                reply_markup=cancel_keyboard(),
            )
            return

        # ── FORCE JOIN menu (Task 3) ───────────────────────────────────────
        elif text == f"🔏 {stylish('Force Join')}":
            fj = "🟩 ON" if is_force_join_enabled() else "🟥 OFF"
            channels = get_join_channels()
            ch_list = ""
            for ch in channels:
                type_icon = "📣" if ch["channel_type"] == "channel" else "👥"
                ch_list += f"\n{type_icon} <b>{ch['channel_name']}</b> — <code>{ch['channel_id']}</code>"
            bot.send_message(
                chat_id,
                f"🔏 <b>Force Join</b>\n\nStatus: <b>{fj}</b>\n\n"
                f"<b>Configured Channels/Groups:</b>"
                + (ch_list if ch_list else "\n<i>None configured</i>"),
                reply_markup=force_join_keyboard(),
            )
            return

        elif text == f"➕ {stylish('Add Channel')}":
            admin_states[user.id] = {"step": "aset_fj_add_channel", "data": {}}
            bot.send_message(
                chat_id,
                "📣 Send your <b>Channel link</b>:\n\nExample:\n<code>https://t.me/yourchannel</code>\nor\n<code>@yourchannel</code>",
                reply_markup=cancel_keyboard(),
            )
            return

        elif text == f"➕ {stylish('Add Group')}":
            admin_states[user.id] = {"step": "aset_fj_add_group", "data": {}}
            bot.send_message(
                chat_id,
                "👥 Send your <b>Group link</b>:\n\nExample:\n<code>https://t.me/yourgroup</code>\nor\n<code>@yourgroup</code>",
                reply_markup=cancel_keyboard(),
            )
            return

        elif text == f"🧹 {stylish('Delete Channel')}":
            kb = join_channels_list_keyboard(channel_type="channel")
            if not kb:
                bot.send_message(chat_id, "⛔ No channels configured.", reply_markup=force_join_keyboard())
                return
            admin_states[user.id] = {"step": "aset_fj_del_ch", "data": {}}
            bot.send_message(chat_id, "🧹 Select a channel to remove:", reply_markup=kb)
            return

        elif text == f"🧹 {stylish('Delete Group')}":
            kb = join_channels_list_keyboard(channel_type="group")
            if not kb:
                bot.send_message(chat_id, "⛔ No groups configured.", reply_markup=force_join_keyboard())
                return
            admin_states[user.id] = {"step": "aset_fj_del_gr", "data": {}}
            bot.send_message(chat_id, "🧹 Select a group to remove:", reply_markup=kb)
            return

        elif (text in (
                f"🟩 {stylish('Force Join: ON')}",
                f"🟥 {stylish('Force Join: OFF')}",
                "🟩 Force Join: ON", "🟥 Force Join: OFF",
              ) or
              text.startswith("🟩") and "Force" in text and "Join" in text or
              text.startswith("🟥") and "Force" in text and "Join" in text):
            current = get_setting("force_join_enabled", "0")
            new_val = "0" if current == "1" else "1"
            set_setting("force_join_enabled", new_val)
            status = "🟩 ENABLED" if new_val == "1" else "🟥 DISABLED"
            bot.send_message(
                chat_id,
                f"✔️ Force Join is now <b>{status}</b>",
                reply_markup=force_join_keyboard(),
            )
            return

        elif text == f"↩ {stylish('Back to Settings')}":
            admin_states.pop(user.id, None)
            fj = "🟩 ON" if is_force_join_enabled() else "🟥 OFF"
            bot.send_message(
                chat_id,
                f"🎛️ <b>Settings</b>\n\nForce Join: <b>{fj}</b>",
                reply_markup=settings_keyboard(is_main_admin=(user.id == ADMIN_ID)),
            )
            return

        # ── OTHERS LINK menu (Task 4) ──────────────────────────────────────
        elif text == f"⛓ {stylish('Others Link')}":
            support = get_setting("support_link", "Not set")
            otp_grp = get_setting("otp_group_link", "Not set")
            panel_lnk = get_setting("panel_link", "Not set")
            dev_lnk = get_setting("dev_link", "Not set")
            main_channel = get_setting("main_channel_link", "Not set")
            pmt_id = get_setting("payment_forward_chat_id", "Not set")
            otp_fwd = get_unified_forward_chat_id() or "Not set"
            bn = get_setting("bot_name", "OTP BOT")
            pw = get_setting("powered_by", "সুমন")
            bot.send_message(
                chat_id,
                f"⛓ <b>Others Link Settings</b>\n\n"
                f"☎ <b>Support Link:</b> <code>{support}</code>\n"
                f"🗨 <b>OTP Group Link:</b> <code>{otp_grp}</code>\n"
                f"👑 <b>Number Panel Link:</b> <code>{panel_lnk}</code>\n"
                f"⚡ <b>Bot Developer Link:</b> <code>{dev_lnk}</code>\n"
                f"📣 <b>Main Channel Link:</b> <code>{main_channel}</code>\n"
                f"🪪 <b>Payment Fwd ID:</b> <code>{pmt_id}</code>\n"
                f"📨 <b>OTP Fwd ID:</b> <code>{otp_fwd}</code>\n"
                f"🦾 <b>Bot Name:</b> <code>{bn}</code>\n"
                f"🕷 <b>Powered By:</b> <code>{pw}</code>",
                reply_markup=others_link_keyboard(),
            )
            return

        elif text == f"☎ {stylish('Support Btn')}":
            admin_states[user.id] = {"step": "aset_ol_support", "data": {}}
            cur = get_setting("support_link", "Not set")
            bot.send_message(
                chat_id,
                f"☎ Current support link: <code>{cur}</code>\n\nSend new support URL:",
                reply_markup=cancel_keyboard(),
            )
            return

        elif text == f"🗨 {stylish('OTP Group Btn')}":
            admin_states[user.id] = {"step": "aset_ol_otpgroup", "data": {}}
            cur = get_setting("otp_group_link", "Not set")
            bot.send_message(
                chat_id,
                f"🗨 Current OTP group link: <code>{cur}</code>\n\nSend new OTP Group URL:",
                reply_markup=cancel_keyboard(),
            )
            return

        elif text == f"👑 {stylish('Panel Link')}":
            admin_states[user.id] = {"step": "aset_ol_panel_link", "data": {}}
            cur = get_setting("panel_link", "Not set")
            bot.send_message(
                chat_id,
                f"👑 Current Number Panel link: <code>{cur}</code>\n\nSend new Number Panel URL (https://...):",
                reply_markup=cancel_keyboard(),
            )
            return

        elif text == f"🧹 {stylish('Del Panel Link')}":
            delete_setting("panel_link")
            bot.send_message(chat_id, "✔️ Number Panel link removed.", reply_markup=others_link_keyboard())
            return

        elif text == f"🤖 {stylish('Auto SMS Bot Link')}":
            admin_states[user.id] = {"step": "aset_ol_auto_bot_link", "data": {}}
            cur = get_setting("auto_sms_bot_link", "Not set")
            bot.send_message(
                chat_id,
                f"🤖 Current Auto SMS <b>GO TO BOT</b> link: <code>{cur}</code>\n\n"
                f"Send the bot link (https://t.me/yourbot):",
                reply_markup=cancel_keyboard(),
            )
            return

        elif text == f"🧹 {stylish('Del Auto Bot Link')}":
            delete_setting("auto_sms_bot_link")
            bot.send_message(chat_id, "✔️ Auto SMS bot link removed.", reply_markup=others_link_keyboard())
            return

        elif text == f"📢 {stylish('Auto SMS Channel Link')}":
            admin_states[user.id] = {"step": "aset_ol_auto_channel_link", "data": {}}
            cur = get_setting("auto_sms_channel_link", "Not set")
            bot.send_message(
                chat_id,
                f"📢 Current Auto SMS <b>GO TO CHANNEL</b> link: <code>{cur}</code>\n\n"
                f"Send the channel link (https://t.me/yourchannel):",
                reply_markup=cancel_keyboard(),
            )
            return

        elif text == f"🧹 {stylish('Del Auto Channel Link')}":
            delete_setting("auto_sms_channel_link")
            bot.send_message(chat_id, "✔️ Auto SMS channel link removed.", reply_markup=others_link_keyboard())
            return

        elif text == f"⚡ {stylish('Dev Link')}":
            admin_states[user.id] = {"step": "aset_ol_dev_link", "data": {}}
            cur = get_setting("dev_link", "Not set")
            bot.send_message(
                chat_id,
                f"⚡ Current Bot Developer link: <code>{cur}</code>\n\nSend new Bot Developer URL (https://...):",
                reply_markup=cancel_keyboard(),
            )
            return

        elif text == f"📣 {stylish('Main Channel')}":
            admin_states[user.id] = {"step": "aset_ol_main_channel", "data": {}}
            cur = get_setting("main_channel_link", "Not set")
            bot.send_message(
                chat_id,
                f"📣 Current main channel link: <code>{cur}</code>\n\n"
                f"Send the channel URL (for the GO TO CHANNEL button):",
                reply_markup=cancel_keyboard(),
            )
            return

        elif text == f"🧹 {stylish('Del Main Channel')}":
            delete_setting("main_channel_link")
            bot.send_message(chat_id, "✔️ Main channel link removed.", reply_markup=others_link_keyboard())
            return

        elif text == f"🧹 {stylish('Del Dev Link')}":
            delete_setting("dev_link")
            bot.send_message(chat_id, "✔️ Bot Developer link removed.", reply_markup=others_link_keyboard())
            return

        elif text == f"🪪 {stylish('Payment Request ID')}":
            admin_states[user.id] = {"step": "aset_ol_payment_id", "data": {}}
            cur = get_setting("payment_forward_chat_id", "Not set")
            bot.send_message(
                chat_id,
                f"🪪 Current payment forward Chat ID: <code>{cur}</code>\n\nSend new Chat ID (numeric):",
                reply_markup=cancel_keyboard(),
            )
            return

        elif text == f"📨 {stylish('OTP Forward ID')}":
            admin_states[user.id] = {"step": "aset_ol_otp_fwd", "data": {}}
            cur = get_setting("otp_forward_chat_id", "Not set")
            bot.send_message(
                chat_id,
                f"📨 Current OTP forward Chat ID: <code>{cur}</code>\n\nSend new Chat ID (numeric):",
                reply_markup=cancel_keyboard(),
            )
            return

        elif text == f"🧹 {stylish('Del Support')}":
            delete_setting("support_link")
            bot.send_message(chat_id, "✔️ Support link removed.", reply_markup=others_link_keyboard())
            return

        elif text == f"🧹 {stylish('Del OTP Group')}":
            delete_setting("otp_group_link")
            bot.send_message(chat_id, "✔️ OTP Group link removed.", reply_markup=others_link_keyboard())
            return

        elif text == f"🧹 {stylish('Del Payment ID')}":
            delete_setting("payment_forward_chat_id")
            bot.send_message(chat_id, "✔️ Payment Request ID removed.", reply_markup=others_link_keyboard())
            return

        elif text == f"🧹 {stylish('Del OTP Fwd')}":
            delete_unified_forward_chat_id()
            bot.send_message(chat_id, "✔️ OTP & Demo Forward ID removed.", reply_markup=others_link_keyboard())
            return

        elif text == f"🦾 {stylish('Bot Name')}":
            admin_states[user.id] = {"step": "aset_ol_bot_name", "data": {}}
            cur = get_setting("bot_name", "OTP BOT")
            bot.send_message(
                chat_id,
                f"🦾 Current bot name: <code>{cur}</code>\n\nSend new bot name:",
                reply_markup=cancel_keyboard(),
            )
            return

        elif text == f"🧹 {stylish('Del Bot Name')}":
            delete_setting("bot_name")
            bot.send_message(chat_id, "✔️ Bot name reset to default.", reply_markup=others_link_keyboard())
            return

        elif text == f"🕷 {stylish('Powered By')}":
            admin_states[user.id] = {"step": "aset_ol_powered_by", "data": {}}
            cur = get_setting("powered_by", "সুমন")
            bot.send_message(
                chat_id,
                f"🕷 Current powered by text: <code>{cur}</code>\n\nSend new powered by text:",
                reply_markup=cancel_keyboard(),
            )
            return

        elif text == f"🧹 {stylish('Del Powered By')}":
            delete_setting("powered_by")
            bot.send_message(chat_id, f"✔️ {stylish('Powered By reset to default.')}", reply_markup=others_link_keyboard())
            return

        # ── API MANAGEMENT ─────────────────────────────────────────────────
        elif text == f"🗝 {stylish('API Management')}":
            lines = ["🗝 <b>API Management</b>\n"]
            for api_id, defn in API_DEFINITIONS.items():
                cfg = get_api_config(api_id)
                status = "🟩 ON" if cfg["enabled"] else "🟥 OFF"
                key_preview = cfg["key"][:8] + "..." if len(cfg["key"]) > 8 else cfg["key"]
                lines.append(f"<b>{defn['name']}</b> [{status}]\n⛓ Key: <code>{key_preview}</code>")
            bot.send_message(chat_id, "\n\n".join(lines), reply_markup=api_management_keyboard())
            return

        elif text == f"↩ {stylish('API Management')}":
            lines = ["🗝 <b>API Management</b>\n"]
            for api_id, defn in API_DEFINITIONS.items():
                cfg = get_api_config(api_id)
                status = "🟩 ON" if cfg["enabled"] else "🟥 OFF"
                key_preview = cfg["key"][:8] + "..." if len(cfg["key"]) > 8 else cfg["key"]
                lines.append(f"<b>{defn['name']}</b> [{status}]\n⛓ Key: <code>{key_preview}</code>")
            bot.send_message(chat_id, "\n\n".join(lines), reply_markup=api_management_keyboard())
            return

        # ── API MANAGEMENT DETAIL BUTTONS ─────────────────────────────────
        elif text in (f"🟩 {stylish('SMShadi')}", f"🟥 {stylish('SMShadi')}"):
            _show_api_detail(chat_id, "smshadi")
            return
        elif text in (f"🟩 {stylish('Lamix')}", f"🟥 {stylish('Lamix')}"):
            _show_api_detail(chat_id, "lamix")
            return
        elif text in (f"🟩 {stylish('YesMS API')}", f"🟥 {stylish('YesMS API')}"):
            _show_api_detail(chat_id, "yesms")
            return
        elif text in (f"🟩 {stylish('StexSMS')}", f"🟥 {stylish('StexSMS')}"):
            _show_api_detail(chat_id, "stexsms")
            return
        elif text in (f"🟩 {stylish('FastXOTPs')}", f"🟥 {stylish('FastXOTPs')}"):
            _show_api_detail(chat_id, "fastxotps")
            return
        elif text in (f"🟩 {stylish('VoltXSMS')}", f"🟥 {stylish('VoltXSMS')}"):
            _show_api_detail(chat_id, "voltxsms")
            return
        elif text in (f"🟩 {stylish('ZebraSMS')}", f"🟥 {stylish('ZebraSMS')}"):
            _show_api_detail(chat_id, "zebrasms")
            return
        elif text == f"🗝 {stylish('Set Key')} [smshadi]":
            admin_states[user.id] = {"step": "aapi_setkey_smshadi", "data": {}}
            bot.send_message(chat_id, f"🗝 {stylish('Send new API key for')} <b>SMShadi</b>:", reply_markup=cancel_keyboard())
            return
        elif text == f"🗝 {stylish('Set Key')} [lamix]":
            admin_states[user.id] = {"step": "aapi_setkey_lamix", "data": {}}
            bot.send_message(chat_id, f"🗝 {stylish('Send new API key for')} <b>Lamix</b>:", reply_markup=cancel_keyboard())
            return
        elif text == f"🗝 {stylish('Set Key')} [yesms]":
            admin_states[user.id] = {"step": "aapi_setkey_yesms", "data": {}}
            bot.send_message(chat_id, f"🗝 {stylish('Send new API key for')} <b>YesMS API</b>:", reply_markup=cancel_keyboard())
            return
        elif text == f"🗝 {stylish('Set Key')} [stexsms]":
            admin_states[user.id] = {"step": "aapi_setkey_stexsms", "data": {}}
            bot.send_message(chat_id, f"🗝 {stylish('Send new API key for')} <b>StexSMS</b>:", reply_markup=cancel_keyboard())
            return
        elif text == f"🗝 {stylish('Set Key')} [fastxotps]":
            admin_states[user.id] = {"step": "aapi_setkey_fastxotps", "data": {}}
            bot.send_message(chat_id, f"🗝 {stylish('Send new API key for')} <b>FastXOTPs</b>:", reply_markup=cancel_keyboard())
            return
        elif text == f"🗝 {stylish('Set Key')} [voltxsms]":
            admin_states[user.id] = {"step": "aapi_setkey_voltxsms", "data": {}}
            bot.send_message(chat_id, f"🗝 {stylish('Send new API key for')} <b>VoltXSMS</b>:", reply_markup=cancel_keyboard())
            return
        elif text == f"🗝 {stylish('Set Key')} [zebrasms]":
            admin_states[user.id] = {"step": "aapi_setkey_zebrasms", "data": {}}
            bot.send_message(chat_id, f"🗝 {stylish('Send new API key for')} <b>ZebraSMS</b>:", reply_markup=cancel_keyboard())
            return
        elif text == f"🧹 {stylish('Remove Key')} [smshadi]":
            remove_api_key("smshadi")
            bot.send_message(chat_id, f"✔️ {stylish('SMShadi key removed. Using default.')}", reply_markup=api_management_keyboard())
            return
        elif text == f"🧹 {stylish('Remove Key')} [lamix]":
            remove_api_key("lamix")
            bot.send_message(chat_id, f"✔️ {stylish('Lamix key removed. Using default.')}", reply_markup=api_management_keyboard())
            return
        elif text == f"🧹 {stylish('Remove Key')} [yesms]":
            remove_api_key("yesms")
            bot.send_message(chat_id, f"✔️ {stylish('YesMS API key removed. Using default.')}", reply_markup=api_management_keyboard())
            return
        elif text == f"🧹 {stylish('Remove Key')} [stexsms]":
            remove_api_key("stexsms")
            bot.send_message(chat_id, f"✔️ {stylish('StexSMS key removed. Using default.')}", reply_markup=api_management_keyboard())
            return
        elif text == f"🧹 {stylish('Remove Key')} [fastxotps]":
            remove_api_key("fastxotps")
            bot.send_message(chat_id, f"✔️ {stylish('FastXOTPs key removed. Using default.')}", reply_markup=api_management_keyboard())
            return
        elif text == f"🧹 {stylish('Remove Key')} [voltxsms]":
            remove_api_key("voltxsms")
            bot.send_message(chat_id, f"✔️ {stylish('VoltXSMS key removed. Using default.')}", reply_markup=api_management_keyboard())
            return
        elif text == f"🧹 {stylish('Remove Key')} [zebrasms]":
            remove_api_key("zebrasms")
            bot.send_message(chat_id, f"✔️ {stylish('ZebraSMS key removed.')}", reply_markup=api_management_keyboard())
            return
        elif text in (f"🟩 {stylish('Enable')} [smshadi]", f"🟥 {stylish('Disable')} [smshadi]"):
            new_state = toggle_api_enabled("smshadi")
            bot.send_message(chat_id, f"✔️ {stylish('SMShadi')} {stylish('is now')} {'🟩 ENABLED' if new_state else '🟥 DISABLED'}", reply_markup=api_management_keyboard())
            return
        elif text in (f"🟩 {stylish('Enable')} [lamix]", f"🟥 {stylish('Disable')} [lamix]"):
            new_state = toggle_api_enabled("lamix")
            bot.send_message(chat_id, f"✔️ {stylish('Lamix')} {stylish('is now')} {'🟩 ENABLED' if new_state else '🟥 DISABLED'}", reply_markup=api_management_keyboard())
            return
        elif text in (f"🟩 {stylish('Enable')} [yesms]", f"🟥 {stylish('Disable')} [yesms]"):
            new_state = toggle_api_enabled("yesms")
            bot.send_message(chat_id, f"✔️ {stylish('YesMS API')} {stylish('is now')} {'🟩 ENABLED' if new_state else '🟥 DISABLED'}", reply_markup=api_management_keyboard())
            return
        elif text in (f"🟩 {stylish('Enable')} [stexsms]", f"🟥 {stylish('Disable')} [stexsms]"):
            new_state = toggle_api_enabled("stexsms")
            bot.send_message(chat_id, f"✔️ {stylish('StexSMS')} {stylish('is now')} {'🟩 ENABLED' if new_state else '🟥 DISABLED'}", reply_markup=api_management_keyboard())
            return
        elif text in (f"🟩 {stylish('Enable')} [fastxotps]", f"🟥 {stylish('Disable')} [fastxotps]"):
            new_state = toggle_api_enabled("fastxotps")
            bot.send_message(chat_id, f"✔️ {stylish('FastXOTPs')} {stylish('is now')} {'🟩 ENABLED' if new_state else '🟥 DISABLED'}", reply_markup=api_management_keyboard())
            return
        elif text in (f"🟩 {stylish('Enable')} [voltxsms]", f"🟥 {stylish('Disable')} [voltxsms]"):
            new_state = toggle_api_enabled("voltxsms")
            bot.send_message(chat_id, f"✔️ {stylish('VoltXSMS')} {stylish('is now')} {'🟩 ENABLED' if new_state else '🟥 DISABLED'}", reply_markup=api_management_keyboard())
            return
        elif text in (f"🟩 {stylish('Enable')} [zebrasms]", f"🟥 {stylish('Disable')} [zebrasms]"):
            new_state = toggle_api_enabled("zebrasms")
            bot.send_message(chat_id, f"✔️ {stylish('ZebraSMS')} {stylish('is now')} {'🟩 ENABLED' if new_state else '🟥 DISABLED'}", reply_markup=api_management_keyboard())
            return
        elif text == f"📡 {stylish('Live Access')} [zebrasms]":
            live = _zebrasms_live_access()
            status = "🟩 OK" if live["ok"] else "🟥 FAILED"
            bot.send_message(
                chat_id,
                f"📡 <b>ZebraSMS Live Access:</b> {status}\n<code>{_html.escape(str(live['detail']))}</code>",
                reply_markup=api_detail_keyboard("zebrasms"),
            )
            return

        # ── DEVELOPER INFO (main admin only) ──────────────────────────────
        elif text == f"👨‍💻 {stylish('Developer')}":
            if user.id != ADMIN_ID:
                return
            cur = get_setting("developer_info", "Not set")
            bot.send_message(
                chat_id,
                f"👨‍💻 <b>Developer Info</b>\n\nCurrent info:\n<code>{cur}</code>",
                reply_markup=developer_keyboard(),
            )
            return

        elif text == f"✏️ {stylish('Set Dev Info')}":
            if user.id != ADMIN_ID:
                return
            admin_states[user.id] = {"step": "adev_set_info", "data": {}}
            cur = get_setting("developer_info", "Not set")
            bot.send_message(
                chat_id,
                f"Current dev info:\n<code>{cur}</code>\n\nSend new developer info text (HTML supported):",
                reply_markup=cancel_keyboard(),
            )
            return

        elif text == f"🧹 {stylish('Clear Dev Info')}":
            if user.id != ADMIN_ID:
                return
            delete_setting("developer_info")
            bot.send_message(chat_id, "✔️ Developer info cleared.", reply_markup=developer_keyboard())
            return

        # ── USERS LIST ────────────────────────────────────────────────────
        elif text == f"👥 {stylish('Users')}":
            with get_conn() as conn:
                top10 = conn.execute("""
                    SELECT u.id, u.first_name, u.username, COALESCE(w.total_otp, 0) as total_otp
                    FROM users u LEFT JOIN wallet w ON u.id=w.user_id
                    ORDER BY total_otp DESC LIMIT 10
                """).fetchall()
                all_users = conn.execute("""
                    SELECT u.id, u.first_name, u.username, COALESCE(w.total_otp, 0) as total_otp
                    FROM users u LEFT JOIN wallet w ON u.id=w.user_id
                    ORDER BY total_otp DESC
                """).fetchall()
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
            lines = ["<blockquote>👥 TOP 10 MOST ACTIVE USERS</blockquote>\n"]
            for i, u in enumerate(top10):
                name = u["first_name"] or "User"
                uname = f"@{u['username']}" if u["username"] else f"ID:{u['id']}"
                lines.append(f"{medals[i]} <b>{name}</b> ({uname}) — {u['total_otp']} OTPs")
            bot.send_message(chat_id, "\n".join(lines), reply_markup=admin_keyboard(is_main_admin=_is_main_admin))
            if len(all_users) > 0:
                file_lines = ["Name | Username | UID | Total OTPs", "-" * 50]
                for u in all_users:
                    name = u["first_name"] or "User"
                    uname = f"@{u['username']}" if u["username"] else "N/A"
                    file_lines.append(f"{name} | {uname} | {u['id']} | {u['total_otp']}")
                bot.send_document(
                    chat_id,
                    io.BytesIO("\n".join(file_lines).encode("utf-8")),
                    visible_file_name="all_users.txt",
                    caption=f"📄 Total users: {len(all_users)}",
                )
            return

        # ── ADMIN MANAGEMENT ──────────────────────────────────────────────
        elif text == f"👑 {stylish('Admin Management')}":
            if not _is_main_admin:
                return
            bot.send_message(chat_id, "👑 <b>Admin Management</b>", reply_markup=admin_management_keyboard())
            return

        elif text == f"➕ {stylish('Add Admin')}":
            if not _is_main_admin:
                return
            admin_states[user.id] = {"step": "amgmt_add_uid", "data": {}}
            bot.send_message(chat_id, "🧑 Enter the <b>User ID</b> to make admin:", reply_markup=cancel_keyboard())
            return

        elif text == f"👥 {stylish('View Admins')}":
            if not _is_main_admin:
                return
            sub_admins = get_sub_admins()
            if not sub_admins:
                bot.send_message(chat_id, "ℹ️ No sub-admins added yet.", reply_markup=admin_management_keyboard())
            else:
                lines = ["<blockquote>👑 CURRENT SUB-ADMINS</blockquote>\n"]
                for row in sub_admins:
                    uid = row["user_id"]
                    try:
                        member = bot.get_chat(uid)
                        name = member.first_name or "User"
                    except Exception:
                        name = "User"
                    lines.append(f"• <b>{name}</b> — <code>{uid}</code>")
                bot.send_message(chat_id, "\n".join(lines), reply_markup=admin_management_keyboard())
            return

        elif text == f"🧹 {stylish('Remove Admin')}":
            if not _is_main_admin:
                return
            sub_admins = get_sub_admins()
            if not sub_admins:
                bot.send_message(chat_id, "ℹ️ No sub-admins to remove.", reply_markup=admin_management_keyboard())
                return
            admin_states[user.id] = {"step": "amgmt_remove_uid", "data": {}}
            bot.send_message(chat_id, "🧹 Enter the <b>User ID</b> of the admin to remove:", reply_markup=cancel_keyboard())
            return

        # ── MANAGE SERVICES BUTTONS (Task 1) ──────────────────────────────
        elif text == f"➕ {stylish('Add Service')}":
            admin_states[user.id] = {"step": "asvc_add_name", "data": {}}
            bot.send_message(
                chat_id,
                "📝 <b>Add New Service</b>\n\nEnter the <b>Service Name</b>:\n\nExample: Facebook, WhatsApp, Telegram, Google",
                reply_markup=cancel_keyboard(),
            )
            return

        elif text == f"🧹 {stylish('Delete Service')}":
            kb = services_list_keyboard()
            if not kb:
                bot.send_message(chat_id, "⛔ No services found.", reply_markup=manage_services_keyboard())
                return
            admin_states[user.id] = {"step": "adel_svc_select", "data": {}}
            bot.send_message(chat_id, "🧹 <b>Select a Service to Delete:</b>", reply_markup=kb)
            return

        elif text == f"🧾 {stylish('View Services')}":
            with get_conn() as conn:
                services = conn.execute("SELECT * FROM services ORDER BY name").fetchall()
            if not services:
                bot.send_message(chat_id, "⛔ No services found.", reply_markup=manage_services_keyboard())
                return
            lines = ["<blockquote>🧾 ALL SERVICES</blockquote>\n"]
            for i, svc in enumerate(services, 1):
                with get_conn() as conn:
                    cnt = conn.execute(
                        "SELECT COUNT(*) FROM numbers WHERE country_id IN (SELECT id FROM countries WHERE service_id=?) AND assigned=0",
                        (svc["id"],),
                    ).fetchone()[0]
                lines.append(f"{i}. <b>{svc['name']}</b> — {cnt} numbers available")
            bot.send_message(chat_id, "\n".join(lines), reply_markup=manage_services_keyboard())
            return

        elif text == f"📊 {stylish('Service Statistics')}":
            _show_service_stats(chat_id)
            return

        elif text == f"📥 {stylish('Import Numbers')}":
            kb = import_service_keyboard()
            if not kb:
                bot.send_message(chat_id, "⛔ No services found. Add a service first.", reply_markup=manage_services_keyboard())
                return
            admin_states[user.id] = {"step": "aimport_select_service", "data": {}}
            bot.send_message(chat_id, "📥 <b>Import Numbers</b>\n\nSelect a service:", reply_markup=kb)
            return

        elif text == f"♻ {stylish('Reset Numbers')}":
            kb = reset_services_keyboard()
            if not kb:
                bot.send_message(chat_id, "⛔ No services found.", reply_markup=manage_services_keyboard())
                return
            admin_states[user.id] = {"step": "areset_svc_select", "data": {}}
            bot.send_message(chat_id, "♻ <b>Select Service to Reset Numbers:</b>", reply_markup=kb)
            return

        elif text == f"🧩 {stylish('Input Range')}":
            kb = import_service_keyboard()
            if not kb:
                bot.send_message(chat_id, "⛔ No services found. Add a service first.", reply_markup=manage_services_keyboard())
                return
            admin_states[user.id] = {"step": "arange_select_service", "data": {}}
            bot.send_message(chat_id, "🧩 <b>Input Range</b>\n\nSelect a service:", reply_markup=kb)
            return

        elif text == f"◀️ {stylish('Back to Admin')}":
            admin_states.pop(user.id, None)
            bot.send_message(
                chat_id, f"🔧 <b>{stylish('Admin Panel')}</b>",
                reply_markup=admin_keyboard(is_main_admin=_is_main_admin),
            )
            return

        elif text == f"◀️ {stylish('Back to User Panel')}":
            admin_states.pop(user.id, None)
            first_name = user.first_name or "User"
            _send_welcome(chat_id, first_name, user.id)
            return

    # ══════════════════════════════════════════════════════════════════════════
    # USER PANEL
    # ══════════════════════════════════════════════════════════════════════════

    if user.id in user_states:
        ustate = user_states[user.id]
        ustep = ustate.get("step")

        if ustep == "selecting_service":
            _MAIN_MENU_BUTTONS = {
                f"📱 {stylish('Get Number')}", f"📲 {stylish('Get Number')}", f"♻ {stylish('Get Another Number')}",
                f"🪪 {stylish('Balance')}", f"🟥 🪙 {stylish('Withdraw')}",
                f"🎧 {stylish('Support')}", f"🧑 {stylish('Profile')}",
                f"📊 {stylish('Traffic')}", f"🏆 {stylish('Leaderboard')}",
                f"🎁 {stylish('Refer')}", _shop_button_label(),
                f"✉️ {stylish('Temp Mail')}", f"📬 {stylish('Temp Mail')}",
                f"🧿 {stylish('CUSTOM RANGE')}", "🏠 Home", f"🎛️ {stylish('Admin Panel')}",
            }
            if text == "◀️ Back":
                user_states.pop(user.id, None)
                first_name = user.first_name or "User"
                _send_welcome(chat_id, first_name, user.id)
                return
            if text in _MAIN_MENU_BUTTONS:
                # Clear state and fall through to normal main menu handling
                user_states.pop(user.id, None)
                # (falls through below)
            else:
                svc_name = text.replace("📟 ", "").strip()
                with get_conn() as conn:
                    svc = conn.execute("""
                        SELECT DISTINCT s.id, s.name FROM services s
                        WHERE s.name=? AND EXISTS (
                            SELECT 1 FROM countries c
                            WHERE c.service_id = s.id
                            AND (
                                (c.range_id IS NOT NULL AND c.range_id != '')
                                OR c.id IN (SELECT n.country_id FROM numbers n WHERE n.assigned = 0)
                            )
                        )
                    """, (svc_name,)).fetchone()
                if not svc:
                    user_states.pop(user.id, None)
                    return
                ustate["service_id"] = svc["id"]
                ustate["step"] = "selecting_country"
                bot.send_message(
                    chat_id,
                    f"🗺 <b>{stylish('Select a Country')}:</b>",
                    reply_markup=user_countries_inline_keyboard(svc["id"]),
                )
                return

        # selecting_country is now handled via inline callback (cb_select_country)

    # ── OTP WORK CUSTOM RANGE INPUT ───────────────────────────────────────────
    if user.id in user_states and user_states[user.id].get("step") == "otpwork_custom_range_input":
        # If user pressed any menu/keyboard button, cancel range input and fall through
        _SKIP_BUTTONS = {
            f"📱 {stylish('Get Number')}", f"📲 {stylish('Get Number')}", f"♻ {stylish('Get Another Number')}",
            f"🪪 {stylish('Balance')}", f"🟥 🪙 {stylish('Withdraw')}",
            f"🎧 {stylish('Support')}", f"🧑 {stylish('Profile')}",
            f"📊 {stylish('Traffic')}", "🏠 Home",
            f"🏆 {stylish('Leaderboard')}", f"🎁 {stylish('Refer')}", _shop_button_label(),
            f"✉️ {stylish('Temp Mail')}", f"📬 {stylish('Temp Mail')}",
            f"🧿 {stylish('CUSTOM RANGE')}", f"🎛️ {stylish('Admin Panel')}",
            f"◀️ {stylish('Back')}", "◀️ Back",
        }
        if text in _SKIP_BUTTONS:
            user_states.pop(user.id, None)
            # fall through to normal button handling below
        else:
            cstate = user_states.pop(user.id, {})
            entry = cstate.get("ow_entry", {})
            raw_range = text.strip()
            # Keep the range as-is (including X's) — the API expects the full range format
            if not raw_range or not re.search(r'\d', raw_range):
                bot.send_message(chat_id, f"⛔ {stylish('Please enter a valid range with digits (e.g. 880X01, 995X5, 22467XXX)')}")
                return
            rid = raw_range
            loading_msg = bot.send_message(
                chat_id,
                f"⌛ {stylish('Getting number for custom range')} <code>{rid}</code>...",
            )
            full_numbers = fetch_api_numbers(rid)
            if len(full_numbers) < 1:
                try:
                    retry_kb = InlineKeyboardMarkup()
                    retry_kb.add(InlineKeyboardButton(f"♻ {stylish('Try Again')}", callback_data=f"custom_range_retry:{rid}"))
                    bot.edit_message_text(
                        f"⛔ {stylish('No number is available for range')} <code>{rid}</code>. {stylish('Please try another range.')}",
                        chat_id=chat_id,
                        message_id=loading_msg.message_id,
                        reply_markup=retry_kb,
                    )
                except Exception:
                    pass
                return
            full_number = full_numbers[0]
            service_name = entry.get("service_sid", "Facebook")
            # Strip X's only for country code prefix lookup
            digits_only = re.sub(r'[Xx]', '', str(rid))
            country_code = ""
            for length in (3, 2, 1):
                prefix = digits_only[:length]
                if prefix in PHONE_CODE_COUNTRY:
                    country_code = prefix
                    break
            if not country_code:
                country_code = digits_only[:3] if len(digits_only) >= 3 else digits_only
            country_with_flag = range_to_country_name(rid)
            flag, api_country = extract_flag_from_name(country_with_flag)
            text_card = build_number_card(
                flag, country_code, api_country, full_number, service_name,
                numbers=full_numbers,
            )
            try:
                sent = bot.edit_message_text(
                    text_card,
                    chat_id=chat_id,
                    message_id=loading_msg.message_id,
                    reply_markup=number_card_inline_keyboard(numbers=full_numbers),
                )
            except Exception:
                sent = bot.send_message(
                    chat_id, text_card,
                    reply_markup=number_card_inline_keyboard(numbers=full_numbers),
                )
            with get_conn() as conn:
                for full_number in full_numbers:
                    conn.execute(
                        """INSERT INTO allocations
                           (user_id, number_id, number, service_name, country_name,
                            country_flag, country_code, message_id, rid)
                           VALUES (?,NULL,?,?,?,?,?,?,?)""",
                        (
                            user.id, full_number, service_name, api_country,
                            flag, country_code, sent.message_id, rid,
                        ),
                    )
                conn.execute(
                    "UPDATE users SET numbers_generated = numbers_generated + 2 WHERE id=?",
                    (user.id,),
                )
            for full_number in full_numbers:
                _schedule_otp_polling(user.id, chat_id, sent.message_id, full_number)
            return

    # ── USER WITHDRAW STATE MACHINE ───────────────────────────────────────────
    if user.id in user_states and user_states[user.id].get("step", "").startswith("wd_"):
        wstate = user_states[user.id]
        wstep = wstate["step"]

        if wstep == "wd_enter_phone":
            phone = text.strip()
            if not phone:
                bot.send_message(chat_id, f"⛔ {stylish('Invalid phone number. Try again:')}")
                return
            wstate["phone"] = phone
            wstate["step"] = "wd_enter_amount"
            min_wd = get_min_withdraw()
            stats = get_wallet_stats(user.id)
            bot.send_message(
                chat_id,
                f"🪙 <b>{stylish('Enter Amount')}</b>\n\n{stylish('Available')}: <b>{stats['balance']:.2f} BDT</b>\n{stylish('Minimum')}: <b>{min_wd:.0f} BDT</b>",
            )
            return

        elif wstep == "wd_enter_amount":
            try:
                amount = float(text.strip())
            except ValueError:
                bot.send_message(chat_id, f"⛔ {stylish('Invalid amount. Enter a number:')}")
                return
            min_wd = get_min_withdraw()
            stats = get_wallet_stats(user.id)
            balance = stats["balance"]
            if amount < min_wd:
                bot.send_message(chat_id, f"⛔ {stylish('Minimum withdraw is')} <b>{min_wd:.0f} BDT</b>. {stylish('Try again:')}")
                return
            if amount > balance:
                bot.send_message(chat_id, f"⛔ {stylish('Insufficient balance. Your balance:')} <b>{balance:.2f} BDT</b>. {stylish('Try again:')}")
                return
            wstate["amount"] = amount
            wstate["step"] = "wd_confirm"
            method = wstate["method"]
            phone = wstate["phone"]
            bot.send_message(
                chat_id,
                f"<blockquote>🧾 {stylish('WITHDRAW CONFIRMATION')}</blockquote>\n\n"
                f"🪪 <b>{stylish('Method')}:</b> {method}\n"
                f"📟 <b>{stylish('Phone')}:</b> {phone}\n"
                f"💲 <b>{stylish('Amount')}:</b> {amount:.2f} BDT\n\n"
                f"{stylish('Confirm your withdrawal?')}",
                reply_markup=withdraw_confirm_inline_keyboard(),
            )
            return

    # ── TRAFFIC button ─────────────────────────────────────────────────────────
    if text in (f"📈 {stylish('Traffic')}", f"📊 {stylish('Traffic')}"):
        traffic_text = _build_traffic_text()
        bot.send_message(chat_id, traffic_text, parse_mode="HTML", reply_markup=_traffic_inline_keyboard())
        return

    # ── CUSTOM RANGE button (reply keyboard) ──────────────────────────────────
    if text in (f"🎯 {stylish('Custom Range')}", f"🎯 {stylish('CUSTOM RANGE')}", f"🧿 {stylish('CUSTOM RANGE')}"):
        if not is_member(user.id):
            bot.send_message(chat_id, _join_prompt_text(), reply_markup=join_keyboard())
            return
        user_states[user.id] = {"step": "otpwork_custom_range_input", "ow_entry": {}}
        bot.send_message(
            chat_id,
            f"🎛 {stylish('PLEASE ENTER YOUR CUSTOM RANGE(S)')}: ({stylish('e.g., 22890XXX')})",
        )
        return

    # ── USER MAIN MENU BUTTONS ────────────────────────────────────────────────
    if text in (f"📱 {stylish('Get Number')}", f"📲 {stylish('Get Number')}", f"♻ {stylish('Get Another Number')}"):
        if not is_member(user.id):
            bot.send_message(chat_id, _join_prompt_text(), reply_markup=join_keyboard())
            return
        kb = user_services_inline_keyboard()
        user_states[user.id] = {"step": "selecting_service"}
        bot.send_message(chat_id, f"📟 <b>{stylish('Select a Service')}:</b>", reply_markup=kb)
        return

    elif text in (f"✉️ {stylish('Temp Mail')}", f"📬 {stylish('Temp Mail')}"):
        _send_generated_temp_mail(chat_id, user.id)
        return

    elif text in (f"💬 {stylish('Support')}", f"🎧 {stylish('Support')}"):
        _send_support_message(chat_id)
        return

    elif text == f"🟥 🪙 {stylish('Withdraw')}":
        stats = get_wallet_stats(user.id)
        balance = stats["balance"]
        min_wd = get_min_withdraw()
        if balance < min_wd:
            bot.send_message(chat_id, f"⛔ {stylish('Insufficient balance')}!\n{stylish('Minimum')}: {min_wd:.0f} BDT\n{stylish('Yours')}: {balance:.2f} BDT")
            return
        if has_pending_withdraw(user.id):
            bot.send_message(chat_id, f"❗️ {stylish('You already have a pending withdrawal.')}")
            return
        methods_kb = withdraw_methods_inline_keyboard()
        if not methods_kb:
            bot.send_message(chat_id, f"⛔ {stylish('No payment methods configured. Contact admin.')}")
            return
        bot.send_message(
            chat_id,
            f"🪙 <b>{stylish('Select Payment Method')}</b>\n\n{stylish('Balance')}: <b>{balance:.2f} BDT</b>",
            reply_markup=methods_kb,
        )
        return

    elif text in (f"💰 {stylish('Balance')}", f"🪪 {stylish('Balance')}"):
        stats = get_wallet_stats(user.id)
        bot.send_message(chat_id, build_balance_text(stats), reply_markup=balance_inline_keyboard())
        return

    elif text in (f"👤 {stylish('Profile')}", f"🧑 {stylish('Profile')}"):
        try:
            upsert_user(user)
            db_user = get_user(user.id)
            stats = get_wallet_stats(user.id)
            _pkb = InlineKeyboardMarkup(row_width=1)
            _pkb.add(InlineKeyboardButton(f"🎁 {stylish('Referral')}", callback_data="profile_referral"))
            bot.send_message(
                chat_id,
                build_profile_text(user, db_user, stats),
                reply_markup=_pkb,
            )
        except Exception as e:
            logger.error(f"Profile error for user={user.id}: {e}")
            bot.send_message(chat_id, f"⛔ {stylish('Could not load your profile. Please try again.')}")
        return

    elif text == f"🏆 {stylish('Leaderboard')}":
        if not is_leaderboard_enabled():
            bot.send_message(
                chat_id,
                f"🛑 {stylish('Leaderboard is currently disabled by the admin.')}",
                reply_markup=welcome_keyboard(is_admin_user=is_admin(user.id)),
            )
            return
        sep = "━" * 30
        with get_conn() as conn:
            top = conn.execute("""
                SELECT id, username, first_name, otps_received as otp_count
                FROM users
                WHERE is_banned = 0
                ORDER BY otps_received DESC
                LIMIT 10
            """).fetchall()
        medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
        rows = ""
        for i, row in enumerate(top):
            name = row["first_name"] or f"User{row['id']}"
            rows += f"\n{medals[i]} <b>{_html.escape(name)}</b> — {row['otp_count']} OTP"
        if not rows:
            rows = "\n<i>এখনো কোনো OTP পাওয়া যায়নি।</i>"
        bot.send_message(
            chat_id,
            f"🏆 {sep}\n"
            f"   👑  {stylish('OTP LEADERBOARD')}\n"
            f"{sep}\n\n"
            f"<blockquote>{rows}</blockquote>\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔷 {stylish('POWERED BY')} <b>{stylish(_Developer_By())}</b>",
        )
        return

    elif text == f"🎁 {stylish('Refer')}":
        sep = "━" * 30
        bot_info = bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=ref_{user.id}"
        with get_conn() as conn:
            ref_count = conn.execute(
                "SELECT COUNT(*) as c FROM users WHERE referred_by=?", (user.id,)
            ).fetchone()
            count = ref_count["c"] if ref_count else 0
        earn = get_otp_earn()
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("⛓ Share Refer Link", url=f"https://t.me/share/url?url={ref_link}&text=Join+this+bot+%26+earn+BDT+per+OTP!"))
        bot.send_message(
            chat_id,
            f"🎁 {sep}\n"
            f"   🤝  {stylish('REFER & EARN')}\n"
            f"{sep}\n\n"
            f"<blockquote>"
            f"❖ 👥 {stylish('Your Referrals')}  ➤  <b>{count}</b>\n"
            f"❖ 💲 {stylish('Per OTP Earn')}   ➤  <b>{earn:.2f} BDT</b>\n"
            f"❖ ⛓ {stylish('Your Link')}      ➤\n"
            f"<code>{ref_link}</code>"
            f"</blockquote>\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔷 {stylish('POWERED BY')} <b>{stylish(_Developer_By())}</b>",
            reply_markup=kb,
        )
        return

    elif text == "🏠 Home":
        first_name = user.first_name or "User"
        _send_welcome(chat_id, first_name, user.id)
        return

    elif text in (f"👑 {stylish('Admin Panel')}", f"🎛️ {stylish('Admin Panel')}"):
        if not is_admin(user.id):
            return
        admin_states.pop(user.id, None)
        bot.send_message(chat_id, f"🔧 <b>{stylish('Admin Panel')}</b>", reply_markup=admin_keyboard(is_main_admin=(user.id == ADMIN_ID)))
        return


@bot.callback_query_handler(
    func=lambda c: (
        bool(c.data)
        and (
            c.data in ("temp_mail_new", "temp_mail_check")
            or c.data.startswith("temp_mail_copy_email:")
            or c.data.startswith("temp_mail_copy_otp:")
        )
    )
)
def cb_temp_mail_actions(call):
    user = call.from_user
    upsert_user(user)
    db_user = get_user(user.id)
    if db_user and db_user["is_banned"]:
        bot.answer_callback_query(call.id)
        return

    if call.data == "temp_mail_new":
        bot.answer_callback_query(call.id)
        _send_generated_temp_mail(call.message.chat.id, user.id)
        return

    if call.data == "temp_mail_check":
        bot.answer_callback_query(call.id)
        _send_temp_mail_check_result(call.message.chat.id, user.id)
        return

    value = call.data.split(":", 1)[1]
    if value == "unavailable":
        bot.answer_callback_query(
            call.id,
            "Copy is unavailable. Please use the email or OTP shown above.",
            show_alert=True,
        )
        return
    bot.answer_callback_query(call.id, text=value, show_alert=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DOCUMENT HANDLER (TXT file upload for number import)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _do_broadcast_media(message):
    user = message.from_user
    _is_main_admin = (user.id == ADMIN_ID)
    chat_id = message.chat.id
    with get_conn() as conn:
        users_list = conn.execute("SELECT id FROM users WHERE is_banned=0").fetchall()
    sent = 0
    for u in users_list:
        try:
            bot.copy_message(u["id"], chat_id, message.message_id)
            sent += 1
        except Exception:
            pass
    admin_states.pop(user.id, None)
    bot.send_message(chat_id, f"✔️ Broadcast sent to <b>{sent}</b> users.", reply_markup=admin_keyboard(is_main_admin=_is_main_admin))


@bot.message_handler(content_types=["photo", "video", "audio", "voice", "sticker", "video_note", "animation"])
def handle_media(message):
    user = message.from_user
    try:
        import shop as _shop
        if _shop.handle_media(message):
            return
    except Exception as _shop_exc:
        logger.error(f"shop media hook error: {_shop_exc}")
    if not is_admin(user.id):
        return
    state = admin_states.get(user.id)
    if state and state.get("step") == "abroadcast_wait":
        _do_broadcast_media(message)


@bot.message_handler(content_types=["document"])
def handle_document(message):
    user = message.from_user
    try:
        import shop as _shop
        if _shop.handle_media(message):
            return
    except Exception as _shop_exc:
        logger.error(f"shop document hook error: {_shop_exc}")
    if not is_admin(user.id):
        return
    state = admin_states.get(user.id)
    if state and state.get("step") == "abroadcast_wait":
        _do_broadcast_media(message)
        return
    if not state:
        return
    if state.get("step") == "abackup_restore_wait":
        doc = message.document
        if not doc.file_name.endswith(".db"):
            bot.send_message(message.chat.id, "⛔ Please upload a <b>.db</b> file.", reply_markup=cancel_keyboard())
            return
        try:
            file_info = bot.get_file(doc.file_id)
            downloaded = bot.download_file(file_info.file_path)
            tmp_path = os.path.join(DATA_DIR, f".voltx_restore_{user.id}.db")
            with open(tmp_path, "wb") as f:
                f.write(downloaded)
            # Sanity check — must be a valid sqlite db with a users table
            check_conn = sqlite3.connect(tmp_path)
            check_conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'").fetchall()
            check_conn.close()
            import shutil as _shutil
            _shutil.copy2(tmp_path, DB_PATH)
            import os as _os
            _os.remove(tmp_path)
        except Exception as e:
            logger.error(f"Restore error: {e}")
            bot.send_message(message.chat.id, f"⛔ {stylish('Restore failed — invalid or corrupted file.')}", reply_markup=backup_keyboard())
            return
        admin_states.pop(user.id, None)
        bot.send_message(message.chat.id, f"✔️ {stylish('Database restored successfully.')}", reply_markup=backup_keyboard())
        return

    if state.get("step") != "aimport_number_file":
        return

    doc = message.document
    if not doc.file_name.endswith(".txt"):
        bot.send_message(message.chat.id, "⛔ Please upload a <b>.txt</b> file.", reply_markup=cancel_keyboard())
        return

    try:
        file_info = bot.get_file(doc.file_id)
        downloaded = bot.download_file(file_info.file_path)
        content = downloaded.decode("utf-8", errors="ignore")
    except Exception as e:
        logger.error(f"File download error: {e}")
        bot.send_message(message.chat.id, "⛔ Failed to download file.", reply_markup=cancel_keyboard())
        return

    raw_numbers = [normalize_number(line) for line in content.splitlines() if line.strip()]
    valid_numbers = [n for n in raw_numbers if n and n.isdigit()]

    if not valid_numbers:
        bot.send_message(message.chat.id, "⛔ No valid numbers found in file.", reply_markup=cancel_keyboard())
        return

    country_id = state["data"]["country_id"]
    service_name = state["data"]["service_name"]
    country_name = state["data"]["country_name"]
    country_flag = state["data"]["country_flag"]
    country_code = state["data"]["country_code"]

    imported = 0
    skipped = 0
    with get_conn() as conn:
        existing_numbers = {
            r[0] for r in conn.execute(
                "SELECT number FROM numbers WHERE country_id=?", (country_id,)
            ).fetchall()
        }
        for num in valid_numbers:
            if num in existing_numbers:
                skipped += 1
                continue
            conn.execute("INSERT INTO numbers (country_id, number) VALUES (?,?)", (country_id, num))
            existing_numbers.add(num)
            imported += 1

    admin_states.pop(user.id, None)
    bot.send_message(
        message.chat.id,
        f"✔️ <b>Import Complete!</b>\n\n"
        f"📟 Service: <b>{service_name}</b>\n"
        f"🗺 Country: <b>{country_flag} {country_name} +{country_code}</b>\n\n"
        f"📥 Imported: <b>{imported}</b> numbers\n"
        f"⏭ Skipped (duplicates): <b>{skipped}</b>",
        reply_markup=manage_services_keyboard(),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TIMEOUT CHECKER — Background thread
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def timeout_checker():
    logger.info("Timeout checker started.")
    while True:
        try:
            now = int(time.time())
            timeout_secs = int(get_setting("timeout_minutes", 20)) * 60
            with get_conn() as conn:
                timed_out = conn.execute("""
                    SELECT * FROM allocations
                    WHERE otp_received=0 AND timed_out=0
                      AND message_id IS NOT NULL
                      AND allocated_at <= ? - ?
                """, (now, timeout_secs)).fetchall()
                for alloc in timed_out:
                    related_allocs = conn.execute(
                        """SELECT * FROM allocations
                           WHERE user_id=? AND message_id=?
                           ORDER BY id""",
                        (alloc["user_id"], alloc["message_id"]),
                    ).fetchall()
                    conn.execute(
                        """UPDATE allocations SET timed_out=1
                           WHERE user_id=? AND message_id=?
                             AND otp_received=0""",
                        (alloc["user_id"], alloc["message_id"]),
                    )
                    try:
                        related_numbers = [
                            row["number"] for row in related_allocs
                            if row["number"]
                        ]
                        timeout_text = build_timeout_card(
                            alloc["country_flag"], alloc["country_code"],
                            alloc["country_name"], alloc["number"], alloc["service_name"],
                            numbers=related_numbers,
                        )
                        bot.edit_message_text(
                            timeout_text,
                            chat_id=alloc["user_id"],
                            message_id=alloc["message_id"],
                            parse_mode="HTML",
                            reply_markup=number_card_inline_keyboard(numbers=related_numbers),
                        )
                    except Exception as e:
                        logger.warning(f"Timeout edit error: {e}")
        except Exception as e:
            logger.error(f"Timeout checker error: {e}")
        time.sleep(int(get_setting("poll_interval", 30)))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# API OTP POLLING — Smshadi & Lamix (Task 5)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _poll_api(api_url: str, api_token: str, number: str, allocated_at: int = None) -> list:
    """
    Query SMShadi or Lamix API for OTP messages for a given number.
    - Uses filternum for server-side filtering
    - Uses dt1 (allocation time) so only new OTPs are returned
    - Verifies returned 'num' field matches our number (safety check)
    - Returns list of dicts with 'message', 'dt', 'num' keys.
    """
    try:
        params = {
            "token": api_token,
            "filternum": number,
            "records": 50,
        }
        # Add dt1 = allocation time (minus 60s buffer) so we skip old OTPs
        if allocated_at:
            dt1 = datetime.utcfromtimestamp(allocated_at - 60).strftime("%Y-%m-%d %H:%M:%S")
            params["dt1"] = dt1

        resp = requests.get(api_url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") == "success":
            items = data.get("data", []) or []
            # Safety: keep only records whose 'num' field matches our number
            # (strip non-digits from both sides for a loose match)
            num_digits = re.sub(r'\D', '', number)
            matched = []
            for item in items:
                item_num = re.sub(r'\D', '', str(item.get("num", "") or ""))
                # Match if item_num ends with our number or our number ends with item_num
                if num_digits and item_num and (
                    item_num.endswith(num_digits) or num_digits.endswith(item_num)
                    or item_num == num_digits
                ):
                    matched.append(item)
            return matched

    except Exception as e:
        logger.debug(f"API poll error ({api_url}) for {number}: {e}")
    return []


def _schedule_otp_polling(user_id, chat_id, message_id, number):
    """
    Called after a number is allocated to a user.
    The global background thread (fetch_otps_from_api) already polls all active
    allocations every 7 seconds — no per-number thread is needed.
    This function is a no-op placeholder kept for API compatibility.
    """
    logger.debug(f"OTP polling scheduled for user={user_id} number={number} (handled by background poller)")


def _poll_yesms_success_otps() -> list:
    """Fetch recent OTP logs from YesMS GET /user_numbers endpoint."""
    cfg = get_api_config("yesms")
    if not cfg["enabled"] or not cfg["key"]:
        return []
    try:
        resp = requests.get(
            f"{YESMS_BASE}/user_numbers",
            headers={"authkey": cfg["key"]},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("success"):
            all_logs = data.get("logs", []) or []
            results = []
            for o in all_logs:
                raw_key = f"{o.get('number','')}|{o.get('time','')}|{o.get('otp_code','')}"
                otp_id = hashlib.sha256(raw_key.encode()).hexdigest()
                o = dict(o)
                o["otp_id"] = otp_id
                results.append(o)
            if results:
                logger.info(f"YesMS user_numbers: {len(results)} OTP(s) returned")
            return results
    except Exception as e:
        logger.warning(f"YesMS user_numbers error: {e}")
    return []


def _parse_panel_response(panel_name: str, data: dict, field_map: dict) -> list:
    """
    Generic parser for panel OTP responses — handles multiple response shapes:
      • data.otps[]  (StexSMS, FastXOTPs, VoltXSMS documented shape)
      • data[]       (plain list)
      • data directly (dict with number/message)
    field_map keys: number, message, otp_id  (values = field name in API response)
    """
    results = []
    if isinstance(data, list):
        raw_data = data
        data = {"data": data}
    elif not isinstance(data, dict):
        return results
    else:
        raw_data = data.get("data")
        # Different panel versions return the records under one of these
        # top-level keys instead of `data`.
        if raw_data is None:
            for key in ("otps", "otp", "results", "records", "items", "sms"):
                if isinstance(data.get(key), (list, dict)):
                    raw_data = data[key]
                    break
        if raw_data is None and any(
            data.get(k) for k in ("number", "full_number", "phone", "msisdn", "num")
        ):
            raw_data = data
    # Try data.otps first
    if isinstance(raw_data, dict):
        otps = raw_data.get("otps") or raw_data.get("otp") or raw_data.get("list") or []
        if not otps and raw_data:
            # data itself might be the single record
            otps = [raw_data]
    elif isinstance(raw_data, list):
        otps = raw_data
    else:
        otps = []

    if isinstance(otps, dict):
        otps = [otps]
    for o in otps:
        if not isinstance(o, dict):
            continue
        num_raw = ""
        for nf in (field_map["number"], "number", "full_number", "no_plus_number",
                   "national_number", "phone", "msisdn", "mobile", "num"):
            num_raw = str(o.get(nf, "") or "").strip()
            if num_raw:
                break
        num = normalize_number(num_raw)
        # Try multiple message field names
        msg = ""
        for mf in field_map["message"]:
            msg = str(o.get(mf, "") or "").strip()
            if msg:
                break
        if not num or not msg:
            continue
        oid_raw = o.get(field_map.get("otp_id", "otp_id"), "")
        oid = str(oid_raw) if oid_raw else hashlib.sha256(f"{num}|{msg}".encode()).hexdigest()
        results.append({"otp_id": oid, "number": num, "full_message": msg})

    logger.info(f"{panel_name} raw response meta={data.get('meta')} "
                f"data_type={type(raw_data).__name__} parsed={len(results)} OTP(s)")
    if results:
        logger.info(f"{panel_name} sample: num={results[0]['number']} msg={results[0]['full_message'][:60]}")
    return results


def _poll_stexsms_success_otps() -> list:
    """Fetch recent OTPs from StexSMS GET /success-otp (header: mauthapi)."""
    cfg = get_api_config("stexsms")
    if not cfg["enabled"] or not cfg["key"]:
        return []
    try:
        resp = requests.get(f"{STEXSMS_BASE}/success-otp",
                            headers={"mauthapi": cfg["key"]}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"StexSMS /success-otp HTTP={resp.status_code} raw={str(data)[:300]}")
        return _parse_panel_response("StexSMS", data,
                                     {"number": "number", "message": ["message", "msg", "sms", "text"], "otp_id": "otp_id"})
    except Exception as e:
        logger.warning(f"StexSMS OTP poll error: {e}")
    return []


def _poll_fastxotps_success_otps() -> list:
    """Fetch recent OTPs from FastXOTPs GET /api/success-otp-info (header: X-API-Key)."""
    cfg = get_api_config("fastxotps")
    if not cfg["enabled"] or not cfg["key"]:
        return []
    try:
        resp = requests.get(f"{FASTXOTPS_BASE}/api/success-otp-info",
                            headers={"X-API-Key": cfg["key"]}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"FastXOTPs /success-otp-info HTTP={resp.status_code} raw={str(data)[:300]}")
        return _parse_panel_response("FastXOTPs", data,
                                     {"number": "number", "message": ["sms", "message", "otp", "msg", "text"], "otp_id": "otp_id"})
    except Exception as e:
        logger.warning(f"FastXOTPs OTP poll error: {e}")
    return []


def _poll_voltxsms_success_otps() -> list:
    """Fetch recent OTPs from VoltXSMS GET /success-otp (header: mauthapi)."""
    cfg = get_api_config("voltxsms")
    if not cfg["enabled"] or not cfg["key"]:
        return []
    try:
        resp = requests.get(f"{VOLTXSMS_BASE}/success-otp",
                            headers={"mauthapi": cfg["key"]}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"VoltXSMS /success-otp HTTP={resp.status_code} raw={str(data)[:300]}")
        return _parse_panel_response("VoltXSMS", data,
                                     {"number": "number", "message": ["message", "msg", "sms", "text"], "otp_id": "otp_id"})
    except Exception as e:
        logger.warning(f"VoltXSMS OTP poll error: {e}")
    return []


# ─── ZEBRASMS ADAPTER (documented public API only) ────────────────────────────
# Base:  https://api.zebrasms.com/api/v1
#   POST /publicapi/getnum      MAuth: KEY   body {"range": "RANGE"}
#   GET  /publicapi/getupdate   MAuth: KEY
#   GET  /publicapi/liveaccess  MAuth: KEY   [?sender=...]
# No other / undocumented endpoint is ever called.

ZEBRA_GETNUM_PATH     = "/publicapi/getnum"
ZEBRA_GETUPDATE_PATH  = "/publicapi/getupdate"
ZEBRA_LIVEACCESS_PATH = "/publicapi/liveaccess"

_zebra_last_error = {"detail": ""}


def _zebra_headers(key: str, with_json: bool = False) -> dict:
    h = {"MAuth": key, "Accept": "application/json"}
    if with_json:
        h["Content-Type"] = "application/json"
    return h


def _zebra_call(method: str, path: str, key: str, json_body=None, params=None, timeout: int = 12):
    """Single documented ZebraSMS call. Returns parsed JSON or None."""
    url = f"{ZEBRASMS_BASE}{path}"
    try:
        if method == "POST":
            resp = requests.post(url, json=json_body or {},
                                 headers=_zebra_headers(key, True), timeout=timeout)
        else:
            resp = requests.get(url, params=params or None,
                                headers=_zebra_headers(key), timeout=timeout)
        try:
            data = resp.json()
        except Exception:
            _zebra_last_error["detail"] = f"{url} HTTP {resp.status_code} non-JSON: {resp.text[:150]}"
            logger.warning("ZebraSMS %s", _zebra_last_error["detail"])
            return None
        logger.info("ZebraSMS %s %s HTTP=%s raw=%s", method, url, resp.status_code, str(data)[:250])
        if resp.status_code >= 400:
            _zebra_last_error["detail"] = f"{url} HTTP {resp.status_code}: {str(data)[:200]}"
            return None
        return data
    except Exception as e:
        _zebra_last_error["detail"] = f"{url} {e}"
        logger.warning("ZebraSMS request error: %s", _zebra_last_error["detail"])
    return None


def _zebra_rows(data) -> list:
    """Extract data.rows[] from a ZebraSMS response."""
    if not isinstance(data, dict):
        return []
    payload = data.get("data")
    if isinstance(payload, dict):
        rows = payload.get("rows")
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = None
    if isinstance(rows, dict):
        rows = [rows]
    return [r for r in (rows or []) if isinstance(r, dict)]


def _zebra_row_number(row: dict) -> str:
    for key in ("number", "full_number", "msisdn", "phone", "no_plus_number"):
        val = str(row.get(key, "") or "").strip()
        if val:
            return normalize_number(val)
    return ""


def _poll_zebrasms_success_otps() -> list:
    """GET /publicapi/getupdate → existing internal OTP format."""
    cfg = get_api_config("zebrasms")
    if not cfg["enabled"] or not cfg["key"]:
        return []
    data = _zebra_call("GET", ZEBRA_GETUPDATE_PATH, cfg["key"])
    rows = _zebra_rows(data)
    results = []
    for row in rows:
        num = _zebra_row_number(row)
        msg = str(row.get("message", "") or row.get("sms", "") or "").strip()
        if not num or not msg:
            continue
        at_ms = row.get("at_ms") or row.get("at") or ""
        sender = str(row.get("sender", "") or "")
        oid = str(at_ms) if at_ms else ""
        if not oid:
            oid = hashlib.sha256(f"{num}|{msg}".encode()).hexdigest()
        else:
            oid = hashlib.sha256(f"{num}|{oid}|{msg}".encode()).hexdigest()
        results.append({
            "otp_id": oid,
            "number": num,
            "full_message": msg,
            "sender": sender,
            "country": str(row.get("country", "") or ""),
            "operator": str(row.get("operator", "") or ""),
        })
    logger.info("ZebraSMS getupdate parsed %s OTP(s)", len(results))
    return results


def _deliver_panel_otps(panel_name: str, otp_list: list, active: list):
    """Common OTP delivery logic for all panel pollers."""
    active_nums = [normalize_number(a["number"]) for a in active]
    logger.info(f"{panel_name} delivery check: {len(otp_list)} OTP(s), active_nums={active_nums}")
    for otp_item in otp_list:
        raw_num  = str(otp_item.get("number", "")).strip()
        msg_text = (otp_item.get("full_message") or "").strip()
        otp_id   = str(otp_item.get("otp_id") or otp_item.get("time") or time.time())
        if not raw_num or not msg_text:
            logger.warning(f"{panel_name} skipping: empty num={raw_num!r} or msg={msg_text!r}")
            continue
        norm_num = normalize_number(raw_num)
        # Auto SMS: forward every real panel SMS to the configured group/channel
        try:
            _auto_forward_panel_sms(panel_name, norm_num, msg_text, otp_id)
        except Exception as e:
            logger.warning(f"Auto SMS hook error: {e}")
        matched = False
        for alloc in active:
            alloc_num = normalize_number(alloc["number"])
            if alloc_num == norm_num or norm_num.endswith(alloc_num) or alloc_num.endswith(norm_num):
                mhash_val = msg_hash(norm_num, otp_id, msg_text)
                delivered = _deliver_otp_api(alloc, msg_text, otp_id, mhash_val)
                if delivered:
                    logger.info(f"{panel_name} OTP delivered: number={norm_num}")
                matched = True
                break
        if not matched:
            logger.info(f"{panel_name} no match: api_num={norm_num} vs active={active_nums}")


def fetch_otps_from_api():
    """
    Background thread: polls all enabled OTP panels every 7 seconds.
    Panels: YesMS, StexSMS, FastXOTPs, VoltXSMS, SMShadi, Lamix.
    """
    logger.info("API OTP poller started.")
    while True:
        try:
            now = int(time.time())
            timeout_secs = int(get_setting("timeout_minutes", 20)) * 60
            with get_conn() as conn:
                active = conn.execute("""
                    SELECT * FROM allocations
                    WHERE otp_received=0 AND timed_out=0
                      AND allocated_at > ? - ?
                """, (now, timeout_secs)).fetchall()

            # Keep polling when Auto SMS is ON, so every real panel SMS is
            # forwarded to the group even if no user has an active number.
            if not active and not is_auto_sms_enabled():
                time.sleep(7)
                continue
            active = list(active)


            # ── YesMS ────────────────────────────────────────────────────────
            try:
                otps = _poll_yesms_success_otps()
                if otps:
                    _deliver_panel_otps("YesMS", otps, active)
            except Exception as e:
                logger.error(f"YesMS OTP poll error: {e}")

            # ── StexSMS ───────────────────────────────────────────────────────
            try:
                otps = _poll_stexsms_success_otps()
                if otps:
                    _deliver_panel_otps("StexSMS", otps, active)
            except Exception as e:
                logger.error(f"StexSMS OTP poll error: {e}")

            # ── FastXOTPs ─────────────────────────────────────────────────────
            try:
                otps = _poll_fastxotps_success_otps()
                if otps:
                    _deliver_panel_otps("FastXOTPs", otps, active)
            except Exception as e:
                logger.error(f"FastXOTPs OTP poll error: {e}")

            # ── VoltXSMS ──────────────────────────────────────────────────────
            try:
                otps = _poll_voltxsms_success_otps()
                if otps:
                    _deliver_panel_otps("VoltXSMS", otps, active)
            except Exception as e:
                logger.error(f"VoltXSMS OTP poll error: {e}")

            # ── ZebraSMS ──────────────────────────────────────────────────────
            try:
                otps = _poll_zebrasms_success_otps()
                if otps:
                    _deliver_panel_otps("ZebraSMS", otps, active)
            except Exception as e:
                logger.error(f"ZebraSMS OTP poll error: {e}")

            # ── SMShadi + Lamix (legacy per-number polling) ───────────────────
            for alloc in active:
                number       = normalize_number(alloc["number"])
                allocated_at = alloc["allocated_at"]
                provider_list = []
                for api_id in ("smshadi", "lamix"):
                    cfg = get_api_config(api_id)
                    if cfg["enabled"] and cfg["url"]:
                        provider_list.append((cfg["url"], cfg["key"], api_id))
                for api_url, api_token, provider_id in provider_list:
                    messages = _poll_api(api_url, api_token, number, allocated_at)
                    for item in messages:
                        msg_text = (item.get("message") or "").strip()
                        dt = str(item.get("dt") or item.get("id") or time.time())
                        if not msg_text:
                            continue
                        mhash_val = msg_hash(number, dt, msg_text)
                        delivered = _deliver_otp_api(alloc, msg_text, dt, mhash_val)
                        if delivered:
                            logger.info(f"OTP delivered: number={number} provider={provider_id}")
                            break
                    else:
                        continue
                    break

        except Exception as e:
            logger.error(f"API fetch loop error: {e}")

        time.sleep(7)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OTP SOURCE GROUP HANDLER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.message_handler(
    func=lambda m: bool(get_setting("otp_source_group_id", "")) and str(m.chat.id) == get_setting("otp_source_group_id", "") and bool(m.text),
    content_types=["text"],
)
def handle_otp_source_group(message):
    """Listen to the OTP source group and match messages to active allocations."""
    msg_text = message.text or ""
    now = int(time.time())
    timeout_secs = int(get_setting("timeout_minutes", 20)) * 60

    with get_conn() as conn:
        active = conn.execute("""
            SELECT * FROM allocations
            WHERE otp_received=0 AND timed_out=0
              AND allocated_at > ? - ?
        """, (now, timeout_secs)).fetchall()

    for alloc in active:
        number = normalize_number(alloc["number"])
        digits = re.sub(r'[^0-9]', '', number)
        if len(digits) < 3:
            continue
        last3 = digits[-3:]
        if last3 in msg_text:
            _deliver_otp(alloc, msg_text, message.message_id)


# ─── PHONE CODE → COUNTRY MAPPING ─────────────────────────────────────────────

PHONE_CODE_COUNTRY = {
    "1": "🇺🇸 USA / Canada", "7": "🇷🇺 Russia",
    "20": "🇪🇬 Egypt", "27": "🇿🇦 South Africa",
    "30": "🇬🇷 Greece", "31": "🇳🇱 Netherlands", "32": "🇧🇪 Belgium",
    "33": "🇫🇷 France", "34": "🇪🇸 Spain", "36": "🇭🇺 Hungary",
    "39": "🇮🇹 Italy", "40": "🇷🇴 Romania", "41": "🇨🇭 Switzerland",
    "43": "🇦🇹 Austria", "44": "🇬🇧 United Kingdom", "45": "🇩🇰 Denmark",
    "46": "🇸🇪 Sweden", "47": "🇳🇴 Norway", "48": "🇵🇱 Poland",
    "49": "🇩🇪 Germany", "51": "🇵🇪 Peru", "52": "🇲🇽 Mexico",
    "53": "🇨🇺 Cuba", "54": "🇦🇷 Argentina", "55": "🇧🇷 Brazil",
    "56": "🇨🇱 Chile", "57": "🇨🇴 Colombia", "58": "🇻🇪 Venezuela",
    "60": "🇲🇾 Malaysia", "61": "🇦🇺 Australia", "62": "🇮🇩 Indonesia",
    "63": "🇵🇭 Philippines", "64": "🇳🇿 New Zealand", "65": "🇸🇬 Singapore",
    "66": "🇹🇭 Thailand", "81": "🇯🇵 Japan", "82": "🇰🇷 South Korea",
    "84": "🇻🇳 Vietnam", "86": "🇨🇳 China", "90": "🇹🇷 Turkey",
    "91": "🇮🇳 India", "92": "🇵🇰 Pakistan", "93": "🇦🇫 Afghanistan",
    "94": "🇱🇰 Sri Lanka", "95": "🇲🇲 Myanmar", "98": "🇮🇷 Iran",
    "211": "🇸🇸 South Sudan", "212": "🇲🇦 Morocco", "213": "🇩🇿 Algeria",
    "216": "🇹🇳 Tunisia", "218": "🇱🇾 Libya", "220": "🇬🇲 Gambia",
    "221": "🇸🇳 Senegal", "222": "🇲🇷 Mauritania", "223": "🇲🇱 Mali",
    "224": "🇬🇳 Guinea", "225": "🇨🇮 Ivory Coast", "226": "🇧🇫 Burkina Faso",
    "227": "🇳🇪 Niger", "228": "🇹🇬 Togo", "229": "🇧🇯 Benin",
    "230": "🇲🇺 Mauritius", "231": "🇱🇷 Liberia", "232": "🇸🇱 Sierra Leone",
    "233": "🇬🇭 Ghana", "234": "🇳🇬 Nigeria", "235": "🇹🇩 Chad",
    "236": "🇨🇫 Central African Rep.", "237": "🇨🇲 Cameroon",
    "238": "🇨🇻 Cape Verde", "239": "🇸🇹 Sao Tome", "240": "🇬🇶 Eq. Guinea",
    "241": "🇬🇦 Gabon", "242": "🇨🇬 Republic of Congo", "243": "🇨🇩 DR Congo",
    "244": "🇦🇴 Angola", "245": "🇬🇼 Guinea-Bissau", "248": "🇸🇨 Seychelles",
    "249": "🇸🇩 Sudan", "250": "🇷🇼 Rwanda", "251": "🇪🇹 Ethiopia",
    "252": "🇸🇴 Somalia", "253": "🇩🇯 Djibouti", "254": "🇰🇪 Kenya",
    "255": "🇹🇿 Tanzania", "256": "🇺🇬 Uganda", "257": "🇧🇮 Burundi",
    "258": "🇲🇿 Mozambique", "260": "🇿🇲 Zambia", "261": "🇲🇬 Madagascar",
    "263": "🇿🇼 Zimbabwe", "264": "🇳🇦 Namibia", "265": "🇲🇼 Malawi",
    "266": "🇱🇸 Lesotho", "267": "🇧🇼 Botswana", "268": "🇸🇿 Eswatini",
    "269": "🇰🇲 Comoros", "297": "🇦🇼 Aruba", "299": "🇬🇱 Greenland",
    "350": "🇬🇮 Gibraltar", "351": "🇵🇹 Portugal", "352": "🇱🇺 Luxembourg",
    "353": "🇮🇪 Ireland", "354": "🇮🇸 Iceland", "355": "🇦🇱 Albania",
    "356": "🇲🇹 Malta", "357": "🇨🇾 Cyprus", "358": "🇫🇮 Finland",
    "359": "🇧🇬 Bulgaria", "370": "🇱🇹 Lithuania", "371": "🇱🇻 Latvia",
    "372": "🇪🇪 Estonia", "373": "🇲🇩 Moldova", "374": "🇦🇲 Armenia",
    "375": "🇧🇾 Belarus", "380": "🇺🇦 Ukraine", "381": "🇷🇸 Serbia",
    "385": "🇭🇷 Croatia", "386": "🇸🇮 Slovenia", "387": "🇧🇦 Bosnia",
    "389": "🇲🇰 North Macedonia", "420": "🇨🇿 Czech Republic", "421": "🇸🇰 Slovakia",
    "501": "🇧🇿 Belize", "502": "🇬🇹 Guatemala", "503": "🇸🇻 El Salvador",
    "504": "🇭🇳 Honduras", "505": "🇳🇮 Nicaragua", "506": "🇨🇷 Costa Rica",
    "507": "🇵🇦 Panama", "509": "🇭🇹 Haiti", "591": "🇧🇴 Bolivia",
    "592": "🇬🇾 Guyana", "593": "🇪🇨 Ecuador", "595": "🇵🇾 Paraguay",
    "597": "🇸🇷 Suriname", "598": "🇺🇾 Uruguay", "673": "🇧🇳 Brunei",
    "675": "🇵🇬 Papua New Guinea", "679": "🇫🇯 Fiji", "850": "🇰🇵 North Korea",
    "852": "🇭🇰 Hong Kong", "853": "🇲🇴 Macau", "855": "🇰🇭 Cambodia",
    "856": "🇱🇦 Laos", "880": "🇧🇩 Bangladesh", "886": "🇹🇼 Taiwan",
    "960": "🇲🇻 Maldives", "961": "🇱🇧 Lebanon", "962": "🇯🇴 Jordan",
    "963": "🇸🇾 Syria", "964": "🇮🇶 Iraq", "965": "🇰🇼 Kuwait",
    "966": "🇸🇦 Saudi Arabia", "967": "🇾🇪 Yemen", "968": "🇴🇲 Oman",
    "971": "🇦🇪 UAE", "972": "🇮🇱 Israel", "973": "🇧🇭 Bahrain",
    "974": "🇶🇦 Qatar", "975": "🇧🇹 Bhutan", "976": "🇲🇳 Mongolia",
    "977": "🇳🇵 Nepal", "992": "🇹🇯 Tajikistan", "993": "🇹🇲 Turkmenistan",
    "994": "🇦🇿 Azerbaijan", "995": "🇬🇪 Georgia", "996": "🇰🇬 Kyrgyzstan",
    "998": "🇺🇿 Uzbekistan",
}


def range_to_country_name(range_str):
    """Extract country name from a range string like '22507XXX'.
    Tries 3-digit prefix, then 2-digit, then 1-digit."""
    digits = re.sub(r'[Xx]+$', '', range_str)
    for length in (3, 2, 1):
        prefix = digits[:length]
        if prefix in PHONE_CODE_COUNTRY:
            return PHONE_CODE_COUNTRY[prefix]
    return f"🛰 +{digits[:3]}..."


def extract_flag_from_name(country_with_flag: str) -> tuple:
    """Split '🇧🇩 Bangladesh' → ('🇧🇩', 'Bangladesh').
    Works for any flag emoji (regional indicator pairs) or plain globe emoji."""
    country_with_flag = country_with_flag.strip()
    # Flag emojis are regional indicator pairs (each char is 2 code points in some encodings)
    # Simple approach: if first char(s) form a flag emoji, split on first space
    parts = country_with_flag.split(" ", 1)
    if len(parts) == 2:
        potential_flag = parts[0]
        # Regional indicator symbols are in U+1F1E0–U+1F1FF
        if all(0x1F1E0 <= ord(c) <= 0x1F1FF for c in potential_flag) or potential_flag in ("🛰",):
            return potential_flag, parts[1]
    return "🛰", country_with_flag


def group_ranges_by_country(ranges):
    """Group ranges by country. Returns sorted list of (country_name, first_rid).
    Only one entry per country — uses the FIRST (top) range for that country.
    rid is the range digits with trailing X's stripped."""
    seen = {}
    for r in ranges:
        country = range_to_country_name(r)
        if country not in seen:
            rid = re.sub(r'[Xx]+$', '', r)
            seen[country] = rid
    return sorted(seen.items(), key=lambda x: x[0])


# ─── YesMS API HELPERS ────────────────────────────────────────────────────────

def _iso_to_flag(iso: str) -> str:
    """Convert a 2-letter ISO country code to a flag emoji (e.g. GB -> flag)."""
    if not iso or len(iso) < 2:
        return ""
    try:
        return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso.upper()[:2])
    except Exception:
        return ""


def _get_current_traffic_panel() -> str:
    """Return the currently-active traffic panel, rotating every 60 s among enabled ones."""
    global _traffic_panel_idx, _traffic_panel_last_sw
    panels = ["yesms", "stexsms", "fastxotps", "voltxsms", "zebrasms"]
    enabled = [p for p in panels if get_api_config(p)["enabled"] and get_api_config(p)["key"]]
    if not enabled:
        return "yesms"
    with _traffic_panel_lock:
        now = time.time()
        if now - _traffic_panel_last_sw >= 60:
            _traffic_panel_idx = (_traffic_panel_idx + 1) % len(enabled)
            _traffic_panel_last_sw = now
        return enabled[_traffic_panel_idx % len(enabled)]


def _build_traffic_rows_from_panel(panel_id: str):
    """Fetch raw traffic rows from the given panel's console endpoint.
    Returns list of (range_str, country_raw) tuples."""
    cfg = get_api_config(panel_id)
    rows = []
    if not cfg["enabled"] or not cfg["key"]:
        return rows
    try:
        if panel_id == "yesms":
            resp = requests.get(f"{YESMS_BASE}/console_data",
                                headers={"authkey": cfg["key"]}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            for row in (data.get("table") or []):
                if row and len(row) >= 2:
                    rows.append((str(row[0] or ""), str(row[1] or "Unknown")))

        elif panel_id == "stexsms":
            resp = requests.get(f"{STEXSMS_BASE}/console",
                                headers={"mauthapi": cfg["key"]}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            for hit in ((data.get("data") or {}).get("hits") or []):
                rng = str(hit.get("range", "") or "")
                sid = str(hit.get("sid", "") or "Unknown")
                country = _country_from_range_or_sid(rng, sid)
                rows.append((rng, country))

        elif panel_id == "fastxotps":
            resp = requests.get(f"{FASTXOTPS_BASE}/api/live-console",
                                params={"limit": 55},
                                headers={"X-API-Key": cfg["key"]}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            for otp in ((data.get("data") or {}).get("otps") or []):
                rng = str(otp.get("range", "") or "")
                country = str(otp.get("country", "") or "")
                if not country:
                    country = _country_from_range_or_sid(rng, "")
                rows.append((rng, country))

        elif panel_id == "zebrasms":
            for row in _zebrasms_ranges():
                rng = str(row.get("range", "") or row.get("prefix", "") or "")
                country = str(row.get("country", "") or "")
                if not country:
                    country = _country_from_range_or_sid(rng, "")
                iso2 = str(row.get("iso2", "") or "")
                if iso2 and not any(ch for ch in country if ord(ch) > 0x1F000):
                    country = f"{_iso_to_flag(iso2)} {country}".strip()
                hits = row.get("count") or row.get("hits") or row.get("live") or 1
                try:
                    hits = max(1, int(hits))
                except Exception:
                    hits = 1
                for _ in range(min(hits, 50)):
                    rows.append((rng, country))

        elif panel_id == "voltxsms":
            resp = requests.get(f"{VOLTXSMS_BASE}/console",
                                headers={"mauthapi": cfg["key"]}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            for hit in ((data.get("data") or {}).get("hits") or []):
                rng = str(hit.get("range", "") or "")
                sid = str(hit.get("sid", "") or "Unknown")
                country = _country_from_range_or_sid(rng, sid)
                rows.append((rng, country))

    except Exception as e:
        logger.warning(f"{panel_id} traffic fetch error: {e}")
    return rows


def _country_from_range_or_sid(rng: str, sid: str) -> str:
    """Best-effort country name from a range string like 22507XXX."""
    try:
        return range_to_country_name(rng)
    except Exception:
        return sid or "Unknown"


PANEL_LABELS = {
    "zebrasms":  "ZebraSMS",
    "yesms":     "YesMS",
    "stexsms":   "StexSMS",
    "fastxotps": "FastXOTPs",
    "voltxsms":  "VoltXSMS",
    "zebrasms":  "ZebraSMS",
}


def _build_traffic_text() -> str:
    """Build the Live Traffic message, rotating the source panel every 60 s."""
    panel_id = _get_current_traffic_panel()
    panel_label = PANEL_LABELS.get(panel_id, panel_id)
    rows = _build_traffic_rows_from_panel(panel_id)

    country_counts: dict = {}
    country_flags:  dict = {}
    country_codes:  dict = {}
    range_counts:   dict = {}

    for rng, country_raw in rows:
        flag, country = extract_flag_from_name(country_raw)
        country = country or "Unknown"
        country_counts[country] = country_counts.get(country, 0) + 1
        if flag and flag != "🛰" and country not in country_flags:
            country_flags[country] = flag
        if rng and country not in country_codes:
            digits = re.sub(r'[Xx\s]+$', '', rng)
            for length in (3, 2, 1):
                prefix = digits[:length]
                if prefix in PHONE_CODE_COUNTRY:
                    country_codes[country] = prefix
                    break
        if rng:
            range_counts[rng] = range_counts.get(rng, 0) + 1

    total = sum(country_counts.values())
    if total == 0:
        return (
            f"📊 <b>{stylish('Live Traffic')}</b>  <i>[{panel_label}]</i>\n\n"
            f"🕰 <b>{stylish('Window')}:</b> {stylish('Latest activity')}\n"
            f"👑 <b>{stylish('Results Sent')}:</b> 0\n\n"
            f"❗️ {stylish('No traffic data available right now.')}"
        )

    top_country_name = max(country_counts, key=country_counts.get)
    top_flag = country_flags.get(top_country_name, "")
    top_code = country_codes.get(top_country_name, "")
    top_display = f"{top_flag} {top_country_name}".strip()
    if top_code:
        top_display += f" (+{top_code})"

    sorted_countries = sorted(country_counts.items(), key=lambda x: -x[1])[:10]
    sorted_ranges    = sorted(range_counts.items(),   key=lambda x: -x[1])[:10]

    lines = [
        "⚡ " + "━" * 26,
        "   📡  " + stylish("LIVE TRAFFIC") + "  <i>[" + panel_label + "]</i>",
        "━" * 26 + "\n",
        "<blockquote>",
        f"🕰 <b>{stylish('Window')}:</b> {stylish('Latest activity')}",
        f"⚡ <b>{stylish('Results Sent')}:</b> <b>{total}</b>",
        f"🏆 <b>{stylish('Top Country')}:</b> {top_display}",
        "</blockquote>",
        "",
        f"🗺 <b>{stylish('Top Countries')}:</b>",
    ]
    country_items = [
        f"{r}. {country_flags.get(n, '')} {n} (+{country_codes.get(n, '?')}) — {c}".strip()
        for r, (n, c) in enumerate(sorted_countries, 1)
    ]
    for i in range(0, len(country_items), 2):
        lines.append("  |  ".join(country_items[i:i+2]))

    if sorted_ranges:
        lines.append("")
        lines.append(f"📡 <b>{stylish('Top Ranges')}:</b>")
        range_items = [f"{r}. <code>{rng}</code> — {c}" for r, (rng, c) in enumerate(sorted_ranges, 1)]
        for i in range(0, len(range_items), 2):
            lines.append("  |  ".join(range_items[i:i+2]))

    return "\n".join(lines)


def fetch_console_traffic() -> str:
    """Public wrapper — returns formatted traffic text."""
    return _build_traffic_text()


def _traffic_inline_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("♻ Refresh", callback_data="traffic_refresh"))
    kb.add(InlineKeyboardButton("◀️ Back", callback_data="traffic_back"))
    return kb


def is_standard_range(rid: str) -> bool:
    """Return True if rid is a standard digit+trailing-XXX range (22465, 22465XXX).
    All 4 panels can serve these.
    Return False for YesMS search-mode range_ids with X in the middle (63x99, 880X01).
    """
    stripped = re.sub(r'[Xx]+$', '', rid)
    return bool(stripped) and stripped.isdigit()


def _fetch_yesms_number(rid: str):
    """Allocate a number from YesMS API."""
    cfg = get_api_config("yesms")
    if not cfg["enabled"] or not cfg["key"]:
        return None
    try:
        resp = requests.post(
            f"{YESMS_BASE}/allocate_number",
            json={"range_id": rid},
            headers={"authkey": cfg["key"], "Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("success"):
            return data.get("data")
    except Exception as e:
        logger.warning(f"YesMS allocate_number error (rid={rid}): {e}")
    return None


def _fetch_stexsms_number(rid: str):
    """Allocate from StexSMS (POST /getnum, header: mauthapi, body: {"rid": digits})."""
    cfg = get_api_config("stexsms")
    if not cfg["enabled"] or not cfg["key"]:
        return None
    clean_rid = re.sub(r'[Xx]+$', '', rid)
    try:
        resp = requests.post(
            f"{STEXSMS_BASE}/getnum",
            json={"rid": clean_rid},
            headers={"mauthapi": cfg["key"], "Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("meta", {}).get("code") == 200 and data.get("data"):
            return data["data"]
    except Exception as e:
        logger.warning(f"StexSMS getnum error (rid={rid}): {e}")
    return None


def _fetch_fastxotps_number(rid: str):
    """Allocate from FastXOTPs (POST /api/getnum, header: X-API-Key, body: {"range": "26134XXX"})."""
    cfg = get_api_config("fastxotps")
    if not cfg["enabled"] or not cfg["key"]:
        return None
    clean_rid    = re.sub(r'[Xx]+$', '', rid)
    range_w_xxx  = clean_rid + "XXX"
    try:
        resp = requests.post(
            f"{FASTXOTPS_BASE}/api/getnum",
            json={"range": range_w_xxx},
            headers={"X-API-Key": cfg["key"], "Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("meta", {}).get("code") == 200 and data.get("data"):
            return data["data"]
    except Exception as e:
        logger.warning(f"FastXOTPs getnum error (rid={rid}): {e}")
    return None


def _fetch_voltxsms_number(rid: str):
    """Allocate from VoltXSMS (POST /getnum, header: mauthapi, body: {"rid": digits})."""
    cfg = get_api_config("voltxsms")
    if not cfg["enabled"] or not cfg["key"]:
        return None
    clean_rid = re.sub(r'[Xx]+$', '', rid)
    try:
        resp = requests.post(
            f"{VOLTXSMS_BASE}/getnum",
            json={"rid": clean_rid},
            headers={"mauthapi": cfg["key"], "Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("meta", {}).get("code") == 200 and data.get("data"):
            return data["data"]
    except Exception as e:
        logger.warning(f"VoltXSMS getnum error (rid={rid}): {e}")
    return None


def _zebra_clean_range(rid: str) -> str:
    return re.sub(r"[Xx]+$", "", str(rid or "")).strip()


def _zebrasms_ranges(sender: str = "") -> list:
    """GET /publicapi/liveaccess → list of available range rows."""
    cfg = get_api_config("zebrasms")
    if not cfg["enabled"] or not cfg["key"]:
        return []
    params = {"sender": sender} if sender else None
    data = _zebra_call("GET", ZEBRA_LIVEACCESS_PATH, cfg["key"], params=params)
    return _zebra_rows(data)


def _zebrasms_live_access() -> dict:
    """Admin panel health check for ZebraSMS (documented liveaccess endpoint)."""
    cfg = get_api_config("zebrasms")
    if not cfg["key"]:
        return {"ok": False, "detail": "No API key saved", "rows": []}
    data = _zebra_call("GET", ZEBRA_LIVEACCESS_PATH, cfg["key"])
    if data is None:
        return {
            "ok": False,
            "rows": [],
            "detail": "No valid response from ZebraSMS (check API key).\n"
                      f"Last attempt: {_zebra_last_error.get('detail', '')[:300]}",
        }
    rows = _zebra_rows(data)
    preview = ", ".join(
        str(r.get("range") or r.get("prefix") or r.get("sender") or "?") for r in rows[:10]
    )
    return {
        "ok": True,
        "rows": rows,
        "detail": f"{ZEBRA_LIVEACCESS_PATH} → {len(rows)} range(s)\n{preview or str(data)[:180]}",
    }


def _zebra_map_row_to_number(row: dict) -> dict:
    """Map a ZebraSMS data.rows[] entry onto the shared panel number structure."""
    number = _zebra_row_number(row)
    if not number:
        return None
    iso2 = str(row.get("iso2", "") or "")
    payload = dict(row)
    payload.update({
        "full_number":    number,
        "no_plus_number": number,
        "national_number": str(row.get("national_number", "") or number),
        "range":          str(row.get("range", "") or ""),
        "country":        str(row.get("country", "") or ""),
        "country_name":   str(row.get("country", "") or ""),
        "iso2":           iso2,
        "country_flag":   _iso_to_flag(iso2) if iso2 else "",
        "operator":       str(row.get("operator", "") or ""),
        "dial_code":      str(row.get("dial_code", "") or ""),
        "mccmnc":         str(row.get("mccmnc", "") or ""),
        "expires_ms":     row.get("expires_ms", ""),
        "panel":          "zebrasms",
    })
    return payload


def _fetch_zebrasms_number(rid: str):
    """Allocate a number from ZebraSMS — POST /publicapi/getnum {"range": RANGE}."""
    cfg = get_api_config("zebrasms")
    if not cfg["enabled"] or not cfg["key"]:
        return None
    clean = _zebra_clean_range(rid)
    if not clean:
        return None
    for rng in (clean + "XXX", clean):
        data = _zebra_call("POST", ZEBRA_GETNUM_PATH, cfg["key"], json_body={"range": rng})
        for row in _zebra_rows(data):
            mapped = _zebra_map_row_to_number(row)
            if mapped:
                logger.info("ZebraSMS allocated %s (range=%s)", mapped["full_number"], rng)
                return mapped
    logger.warning("ZebraSMS getnum: no number returned for rid=%s", rid)
    return None


def _fetch_zebra_number_verified(rid: str):
    """Return only a fully normalized, usable ZebraSMS number payload."""
    payload = _fetch_zebrasms_number(rid)
    if not payload:
        return None
    number = _number_from_api_data(payload)
    if not number:
        return None
    payload = dict(payload)
    payload["full_number"] = number
    payload["no_plus_number"] = normalize_number(number)
    return payload


def fetch_api_number(rid: str):
    """Allocate a number for the given range id.
    - Standard ranges (22465XXX / 22465) → round-robin across all enabled panels.
    - Non-standard ranges (63x99, 880X01) → YesMS only (search-mode range_id).
    """
    global _panel_alloc_idx
    # Zebra is the primary source for every Get Number / View Range request.
    # Other panels are fallbacks, never the first source.
    zebra_cfg = get_api_config("zebrasms")
    if zebra_cfg["enabled"] and zebra_cfg["key"]:
        result = _fetch_zebra_number_verified(rid)
        if result:
            logger.info("Number allocated via ZebraSMS (primary) for rid=%s", rid)
            return result

    if not is_standard_range(rid):
        logger.debug(f"Non-standard range '{rid}' — YesMS fallback only")
        return _fetch_yesms_number(rid)

    panel_funcs = {
        "yesms":     _fetch_yesms_number,
        "stexsms":   _fetch_stexsms_number,
        "fastxotps": _fetch_fastxotps_number,
        "voltxsms":  _fetch_voltxsms_number,
        "zebrasms":  _fetch_zebrasms_number,
    }
    enabled_panels = [
        p for p in ["yesms", "stexsms", "fastxotps", "voltxsms", "zebrasms"]
        if get_api_config(p)["enabled"] and get_api_config(p)["key"]
    ]
    if not enabled_panels:
        return None

    with _panel_alloc_lock:
        start_idx = _panel_alloc_idx % len(enabled_panels)
        _panel_alloc_idx += 1

    for i in range(len(enabled_panels)):
        panel_id = enabled_panels[(start_idx + i) % len(enabled_panels)]
        result = panel_funcs[panel_id](rid)
        if result:
            logger.info(f"Number allocated via {panel_id} for rid={rid}")
            return result
    return None


# ─── CALLBACK: Change Country on number card ───────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "num_change_country")
def cb_num_change_country(call):
    """Handle 🗺 Change Country button on number card."""
    user = call.from_user
    bot.answer_callback_query(call.id)

    with get_conn() as conn:
        alloc = conn.execute(
            "SELECT * FROM allocations WHERE user_id=? AND message_id=?",
            (user.id, call.message.message_id),
        ).fetchone()

    if not alloc:
        bot.answer_callback_query(call.id, f"❗️ {stylish('Session expired.')}", show_alert=True)
        return

    # Cancel old allocation — DB number stays assigned=1 permanently (never re-used)
    with get_conn() as conn:
        conn.execute("UPDATE allocations SET timed_out=1 WHERE id=?", (alloc["id"],))

    # API number (number_id is NULL, e.g. Custom Range) → Change Country not applicable
    if not alloc["number_id"]:
        try:
            bot.edit_message_text(
                f"❗️ {stylish('Change Country is not available for this number.')}",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
            )
        except Exception:
            bot.send_message(call.message.chat.id, f"❗️ {stylish('Change Country is not available for this number.')}")
        return

    # DB number → edit message to show country list from local DB
    with get_conn() as conn:
        svc = conn.execute(
            "SELECT id FROM services WHERE name=?", (alloc["service_name"],)
        ).fetchone()

    if not svc:
        try:
            bot.edit_message_text(f"❗️ {stylish('Service not found.')}", chat_id=call.message.chat.id, message_id=call.message.message_id)
        except Exception:
            bot.send_message(call.message.chat.id, f"❗️ {stylish('Service not found.')}")
        return

    user_states[user.id] = {
        "step": "selecting_country",
        "service_id": svc["id"],
        "service_name": alloc["service_name"],
    }
    kb = user_countries_inline_keyboard(svc["id"])
    if not kb:
        try:
            bot.edit_message_text(f"⛔ {stylish('No countries available for this service.')}", chat_id=call.message.chat.id, message_id=call.message.message_id)
        except Exception:
            bot.send_message(call.message.chat.id, f"⛔ {stylish('No countries available for this service.')}")
        return
    try:
        bot.edit_message_text(
            f"🗺 <b>{stylish('Select a Country')}:</b>",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
        )
    except Exception:
        bot.send_message(call.message.chat.id, f"🗺 <b>{stylish('Select a Country')}:</b>", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("custom_range_retry:"))
def cb_custom_range_retry(call):
    """Handle Try Again button on 'No numbers available for range' message."""
    user = call.from_user
    bot.answer_callback_query(call.id)
    rid = call.data.split(":", 1)[1]
    chat_id = call.message.chat.id

    try:
        bot.edit_message_text(
            f"⌛ {stylish('Getting number for custom range')} <code>{rid}</code>...",
            chat_id=chat_id,
            message_id=call.message.message_id,
            reply_markup=None,
        )
    except Exception:
        pass

    full_numbers = fetch_api_numbers(rid)
    if len(full_numbers) < 1:
        try:
            retry_kb = InlineKeyboardMarkup()
            retry_kb.add(InlineKeyboardButton(f"♻ {stylish('Try Again')}", callback_data=f"custom_range_retry:{rid}"))
            bot.edit_message_text(
                f"⛔ {stylish('No number is available for range')} <code>{rid}</code>. {stylish('Please try another range.')}",
                chat_id=chat_id,
                message_id=call.message.message_id,
                reply_markup=retry_kb,
            )
        except Exception:
            pass
        return

    full_number = full_numbers[0]
    # Strip X's only for country code prefix lookup
    digits_only = re.sub(r'[Xx]', '', str(rid))
    country_code = ""
    for length in (3, 2, 1):
        prefix = digits_only[:length]
        if prefix in PHONE_CODE_COUNTRY:
            country_code = prefix
            break
    if not country_code:
        country_code = digits_only[:3] if len(digits_only) >= 3 else digits_only

    country_with_flag = range_to_country_name(rid)
    flag, api_country = extract_flag_from_name(country_with_flag)

    # Try to determine service name from allocation history, default to "Facebook"
    with get_conn() as conn:
        last_alloc = conn.execute(
            "SELECT service_name FROM allocations WHERE user_id=? AND rid=? ORDER BY id DESC LIMIT 1",
            (user.id, rid),
        ).fetchone()
    service_name = last_alloc["service_name"] if last_alloc else "Facebook"
    text_card = build_number_card(
        flag, country_code, api_country, full_number, service_name,
        numbers=full_numbers,
    )
    try:
        bot.edit_message_text(
            text_card,
            chat_id=chat_id,
            message_id=call.message.message_id,
            reply_markup=number_card_inline_keyboard(numbers=full_numbers),
        )
        msg_id = call.message.message_id
    except Exception:
        sent = bot.send_message(
            chat_id, text_card,
            reply_markup=number_card_inline_keyboard(numbers=full_numbers),
        )
        msg_id = sent.message_id

    with get_conn() as conn:
        for full_number in full_numbers:
            conn.execute(
                """INSERT INTO allocations
                   (user_id, number_id, number, service_name, country_name,
                    country_flag, country_code, message_id, rid)
                   VALUES (?,NULL,?,?,?,?,?,?,?)""",
                (user.id, full_number, service_name, api_country, flag, country_code, msg_id, rid),
            )
        conn.execute(
            "UPDATE users SET numbers_generated = numbers_generated + 2 WHERE id=?",
            (user.id,),
        )
    for full_number in full_numbers:
        _schedule_otp_polling(user.id, chat_id, msg_id, full_number)


def _otpwork_fetch_number(call, entry: dict, rid: str, send_new: bool = False):
    """Shared: fetch number for a given OTP Work entry + range.

    send_new=False (default): EDIT the current message into the number card.
    send_new=True: keep the current message as-is, send a NEW number card below it.
    """
    user = call.from_user
    country_name = entry.get("country", "Unknown")
    service_name = entry.get("service_sid", "Facebook")

    # Show loading state — edit if we own the message, skip if send_new
    if not send_new:
        try:
            bot.edit_message_text(
                f"⌛ {stylish('Getting number for')} <b>{_html.escape(country_name)}</b>...",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=None,
            )
        except Exception:
            pass

    full_numbers = fetch_api_numbers(rid)
    if len(full_numbers) < 1:
        retry_kb = InlineKeyboardMarkup()
        retry_kb.add(InlineKeyboardButton(f"♻ {stylish('Try Again')}", callback_data=f"custom_range_retry:{rid}"))
        if not send_new:
            try:
                bot.edit_message_text(
                    f"⛔ {stylish('No number is available for')} {_html.escape(country_name)}. {stylish('Please try another.')}",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=retry_kb,
                )
                return
            except Exception:
                pass
        bot.send_message(call.message.chat.id, f"⛔ {stylish('No numbers for')} {_html.escape(country_name)}.", reply_markup=retry_kb)
        return

    full_number = full_numbers[0]
    flag, clean_name = extract_flag_from_name(country_name)
    api_country = clean_name
    # Extract country code from rid (strip X's only for prefix lookup)
    digits_only = re.sub(r'[Xx]', '', str(rid))
    country_code = ""
    for length in (3, 2, 1):
        prefix = digits_only[:length]
        if prefix in PHONE_CODE_COUNTRY:
            country_code = prefix
            break
    if not country_code:
        country_code = digits_only[:3] if len(digits_only) >= 3 else digits_only

    text_card = build_number_card(
        flag, country_code, api_country, full_number, service_name,
        numbers=full_numbers,
    )

    if send_new:
        # Change Number case: old card already has buttons removed; send fresh card below
        msg = bot.send_message(
            call.message.chat.id, text_card,
            reply_markup=number_card_inline_keyboard(numbers=full_numbers),
        )
    else:
        # Initial selection: edit the current message into the number card
        final_message_id = call.message.message_id
        try:
            bot.edit_message_text(
                text_card,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=number_card_inline_keyboard(numbers=full_numbers),
            )
        except Exception:
            sent = bot.send_message(
                call.message.chat.id, text_card,
                reply_markup=number_card_inline_keyboard(numbers=full_numbers),
            )
            final_message_id = sent.message_id

    saved_message_id = msg.message_id if send_new else final_message_id
    with get_conn() as conn:
        for full_number in full_numbers:
            conn.execute(
                """INSERT INTO allocations
                   (user_id, number_id, number, service_name, country_name,
                    country_flag, country_code, message_id, rid)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (user.id, None, full_number, service_name, api_country, flag, country_code, saved_message_id, rid),
            )
        conn.execute(
            "UPDATE users SET numbers_generated=numbers_generated+1 WHERE id=?",
            (user.id,),
        )


@bot.callback_query_handler(func=lambda c: c.data.startswith("api_svc:") or c.data == "api_svc_back")
def cb_api_service_sel(call):
    """Service selected → group ranges by country → show country buttons."""
    user = call.from_user

    if call.data == "api_svc_back":
        bot.answer_callback_query(call.id)
        user_states.pop(user.id, None)
        kb = user_services_inline_keyboard()
        try:
            bot.edit_message_text(
                f"📟 <b>{stylish('Select a Service')}:</b>",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=kb,
            )
        except Exception:
            pass
        return

    ustate = user_states.get(user.id)
    if not ustate or ustate.get("step") != "api_selecting_service":
        bot.answer_callback_query(call.id, f"❗️ {stylish('Session expired.')}", show_alert=True)
        return

    idx = int(call.data.split(":")[1])
    services = ustate.get("api_services", [])
    if idx >= len(services):
        bot.answer_callback_query(call.id, f"❗️ {stylish('Invalid selection.')}", show_alert=True)
        return

    svc = services[idx]
    ranges = svc.get("ranges", [])
    if not ranges:
        bot.answer_callback_query(call.id, "❗️ No ranges available for this service.", show_alert=True)
        return

    # Group ranges by country — ONE button per country (first range used)
    countries = group_ranges_by_country(ranges)
    if not countries:
        bot.answer_callback_query(call.id, "❗️ Could not determine countries.", show_alert=True)
        return

    ustate["step"] = "api_selecting_country"
    ustate["api_service_idx"] = idx
    ustate["api_service_name"] = svc["sid"]
    ustate["api_countries"] = countries
    user_states[user.id] = ustate

    bot.answer_callback_query(call.id)

    kb = InlineKeyboardMarkup(row_width=1)
    for i, (cname, _rid) in enumerate(countries):
        kb.add(InlineKeyboardButton(cname, callback_data=f"api_ctry:{i}"))
    kb.add(InlineKeyboardButton("◀️ Back", callback_data="api_ctry_back"))

    try:
        bot.edit_message_text(
            f"🗺 <b>Select a Country — {svc['sid']}:</b>",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("api_ctry:") or c.data == "api_ctry_back")
def cb_api_country_sel(call):
    """Country selected → use first range for that country → fetch number → show card."""
    user = call.from_user

    if call.data == "api_ctry_back":
        bot.answer_callback_query(call.id)
        user_states.pop(user.id, None)
        # Back from Facebook countries → go to main service selection
        kb = user_services_inline_keyboard()
        try:
            bot.edit_message_text(
                f"📟 <b>{stylish('Select a Service')}:</b>",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=kb,
            )
        except Exception:
            pass
        return

    ustate = user_states.get(user.id)
    if not ustate or ustate.get("step") != "api_selecting_country":
        bot.answer_callback_query(call.id, f"❗️ {stylish('Session expired.')}", show_alert=True)
        return

    idx = int(call.data.split(":")[1])
    countries = ustate.get("api_countries", [])
    if idx >= len(countries):
        bot.answer_callback_query(call.id, f"❗️ {stylish('Invalid selection.')}", show_alert=True)
        return

    country_name, rid = countries[idx]
    service_name = ustate.get("api_service_name", "Other")

    bot.answer_callback_query(call.id)
    user_states.pop(user.id, None)

    # Update message to loading state (removes buttons + old text)
    try:
        bot.edit_message_text(
            f"⌛ {stylish('Getting number for')} <b>{country_name}</b>...",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=None,
        )
    except Exception:
        pass

    full_numbers = fetch_api_numbers(rid)
    if len(full_numbers) < 1:
        try:
            bot.edit_message_text(
                f"⛔ {stylish('No number is available for')} {country_name}. {stylish('Please try another country.')}",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=None,
            )
        except Exception:
            bot.send_message(call.message.chat.id, f"⛔ {stylish('No numbers for')} {country_name}.")
        return

    full_number = full_numbers[0]
    # Extract flag + clean name from selected country_name (e.g. "🇧🇩 Bangladesh")
    flag, clean_name = extract_flag_from_name(country_name)
    api_country = clean_name
    country_code = rid[:3] if len(rid) >= 3 else rid

    text_card = build_number_card(
        flag, country_code, api_country, full_number, service_name,
        numbers=full_numbers,
    )
    # Edit the loading message into the number card
    try:
        msg = bot.edit_message_text(
            text_card,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=number_card_inline_keyboard(numbers=full_numbers),
        )
    except Exception:
        msg = bot.send_message(
            call.message.chat.id, text_card,
            reply_markup=number_card_inline_keyboard(numbers=full_numbers),
        )

    with get_conn() as conn:
        for full_number in full_numbers:
            conn.execute(
                """INSERT INTO allocations
                   (user_id, number_id, number, service_name, country_name,
                    country_flag, country_code, message_id, rid)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (user.id, None, full_number, service_name, api_country, flag, country_code, msg.message_id, rid),
            )
        conn.execute(
            "UPDATE users SET numbers_generated=numbers_generated+1 WHERE id=?",
            (user.id,),
        )


# ─── TRAFFIC CALLBACKS ────────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data in ("traffic_refresh", "traffic_back"))
def cb_traffic_actions(call):
    bot.answer_callback_query(call.id)
    if call.data == "traffic_back":
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return
    # Refresh
    new_text = _build_traffic_text()
    try:
        bot.edit_message_text(
            new_text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="HTML",
            reply_markup=_traffic_inline_keyboard(),
        )
    except Exception:
        pass


# ─── RAILWAY HEALTH SERVER ────────────────────────────────────────────────────
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            body = b'{"status":"ok","service":"telegram-number-bot"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        logger.debug("health server: " + format, *args)


def start_health_server():
    """Start the health endpoint. A busy port must never stop the bot itself."""
    port = int(os.getenv("PORT", "8080"))
    for candidate in (port, 0):
        try:
            server = ThreadingHTTPServer(("0.0.0.0", candidate), _HealthHandler)
        except OSError as exc:
            logger.warning("Health server could not bind port %s: %s", candidate, exc)
            continue
        thread = threading.Thread(target=server.serve_forever, daemon=True, name="health-server")
        thread.start()
        logger.info("Health server listening on 0.0.0.0:%s", server.server_address[1])
        return server
    logger.warning("Health server disabled (no free port); bot keeps running.")
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_STYLED_BUTTONS = {
    "InlineKeyboardButton": InlineKeyboardButton,
    "KeyboardButton": KeyboardButton,
}
_install_module("shop", _SRC_SHOP, _STYLED_BUTTONS)
_install_module("shop_admin", _SRC_SHOP_ADMIN, _STYLED_BUTTONS)


if __name__ == "__main__":
    start_health_server()
    _start_temp_mail_background_loop()

    timeout_thread = threading.Thread(target=timeout_checker, daemon=True)
    timeout_thread.start()

    api_poll_thread = threading.Thread(target=fetch_otps_from_api, daemon=True)
    api_poll_thread.start()

    auto_sms_thread = threading.Thread(target=_auto_sms_engine_loop, daemon=True,
                                       name="auto-sms-engine")
    auto_sms_thread.start()

    demo_sms_thread = threading.Thread(target=_demo_sms_loop, daemon=True,
                                       name="demo-sms-engine")
    demo_sms_thread.start()

    # Automatic hourly DB backup has been disabled.
    # Backups are now taken on demand via Admin Panel → Settings → Backup.

    try:
        import shop as _shop
        _shop.bind(_sys.modules[__name__])
        logger.info("Shop module loaded.")
    except Exception as _shop_exc:
        logger.error(f"Shop module failed to load: {_shop_exc}", exc_info=True)

    try:
        bot.remove_webhook()
    except Exception as _hook_exc:
        logger.warning("remove_webhook failed (safe to ignore): %s", _hook_exc)

    logger.info("Bot starting...")
    _backoff = 5
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=15,
                                 skip_pending=False)
            _backoff = 5
        except Exception as _poll_exc:
            logger.error("Polling crashed: %s", _poll_exc, exc_info=True)
            time.sleep(_backoff)
            _backoff = min(_backoff * 2, 60)
        else:
            time.sleep(3)
