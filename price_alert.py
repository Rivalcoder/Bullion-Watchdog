"""Poll live precious-metal prices, serve health/status on PORT from env, and send Telegram alerts."""

from __future__ import annotations

import json
import html
import logging
import math
import os
import re
import sys
import time
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("PRICE_ALERT_CONFIG", ROOT / "config.json"))
STATE_PATH = Path(os.getenv("PRICE_ALERT_STATE", ROOT / "alert_state.json"))
INDIA_PRICES_URL = os.getenv("INDIA_METALS_API_URL", "https://api.oropocket.com/public/prices")
LOG = logging.getLogger("price_alert")

RUNTIME_LOCK = threading.Lock()
RUNTIME_STATE: dict = {
    "start_time": time.time(),
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_check_at": None,
    "last_prices": None,
    "last_observed": None,
    "last_error": None,
    "checks_count": 0,
    "alerts_sent": 0,
}
ACTIVE_CONFIG: dict = {}
ACTIVE_STATE: dict = {}
ACTIVE_PORT: int | None = None


def load_dotenv() -> None:
    """Load simple KEY=VALUE entries from the project .env without overriding the shell."""
    env_path = ROOT / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def get_server_port() -> int | None:
    """Read the HTTP server port strictly from PORT in environment or .env without any hardcoded fallback."""
    port_val = os.getenv("PORT")
    if port_val is None:
        return None
    port_val = port_val.strip()
    if not port_val or port_val.lower() in {"0", "disabled", "false", "none", "off"}:
        return None
    try:
        port = int(port_val)
        if 1 <= port <= 65535:
            return port
        LOG.error("PORT '%s' in environment/.env is out of range (1-65535)", port_val)
        return None
    except ValueError:
        LOG.error("Invalid PORT value '%s' in environment/.env; must be a valid integer port", port_val)
        return None


def get_server_host() -> str:
    """Read the HTTP server bind address from HOST in environment or .env."""
    return os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0"


def get_uptime_seconds() -> int:
    return int(time.time() - RUNTIME_STATE.get("start_time", time.time()))


def get_ssl_context():
    """Create an unverified SSL context if SSL_VERIFY is explicitly disabled."""
    if os.getenv("SSL_VERIFY", "1").lower() in {"0", "false", "no"}:
        import ssl
        return ssl._create_unverified_context()
    return None


def read_config() -> dict:
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Config file not found: {CONFIG_PATH}") from exc
    if not isinstance(config, dict):
        raise RuntimeError("config.json must contain a JSON object")
    interval = int(config.get("poll_interval_seconds", 30))
    if interval < 10:
        raise RuntimeError("poll_interval_seconds must be at least 10 seconds")
    trigger_config = config.get("triggers_inr_per_gram")
    if trigger_config is None:
        # Migrate the earlier one-trigger-per-metal config in memory.
        old = config.get("thresholds_inr_per_gram", {"XAU": 20000, "XAG": 300})
        trigger_config = {metal: {"default": value} for metal, value in old.items()}
    if not isinstance(trigger_config, dict):
        raise RuntimeError("triggers_inr_per_gram must map metals to named thresholds")
    cleaned = {"XAU": {}, "XAG": {}}
    for metal, triggers in trigger_config.items():
        symbol = str(metal).upper()
        if symbol not in cleaned or not isinstance(triggers, dict):
            raise RuntimeError(f"Invalid trigger group {metal!r}; use XAU and XAG maps")
        for name, threshold in triggers.items():
            label = str(name).lower()
            if not re.fullmatch(r"[a-z0-9_-]{1,24}", label):
                raise RuntimeError(f"Invalid trigger name {name!r}; use 1-24 letters, numbers, _ or -")
            value = float(threshold)
            if value <= 0:
                raise RuntimeError(f"Threshold for {symbol}/{label} must be greater than zero")
            cleaned[symbol][label] = value
    return {"poll_interval_seconds": interval, "triggers": cleaned}


def save_config(config: dict) -> None:
    data = {
        "poll_interval_seconds": config["poll_interval_seconds"],
        "triggers_inr_per_gram": config["triggers"],
    }
    temp = CONFIG_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temp.replace(CONFIG_PATH)


def request_json(url: str, payload: dict | None = None, timeout: int = 15) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"User-Agent": "gold-silver-price-alert/1.0", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method="GET" if data is None else "POST")
    ssl_context = get_ssl_context()
    try:
        with urlopen(req, timeout=timeout, context=ssl_context) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"HTTP request failed with status {exc.code}") from None
    except URLError as exc:
        raise RuntimeError(f"Network request failed: {exc.reason}") from None
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected response from {url}")
    return result


def fetch_prices() -> tuple[dict, str]:
    result = request_json(INDIA_PRICES_URL)
    data = result.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"India price provider returned an unexpected response: {result}")
    timestamp = str(data.get("timestamp") or "unknown")
    prices = {}
    for symbol, key in (("XAU", "gold"), ("XAG", "silver")):
        quote = data.get(key)
        if not isinstance(quote, dict):
            raise RuntimeError(f"India price provider omitted {key} data")
        if str(quote.get("currency", "INR")).upper() != "INR" or str(quote.get("unit", "gram")).lower() != "gram":
            raise RuntimeError(f"India price provider returned unexpected units for {key}; expected INR per gram")
        try:
            buy = float(quote["buy"])
            sell = float(quote["sell"])
            gst = float(quote.get("gst", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"India price provider returned invalid {key} prices") from exc
        if buy <= 0 or sell <= 0 or gst < 0:
            raise RuntimeError(f"India price provider returned non-positive {key} prices")
        prices[symbol] = {"buy": buy, "sell": sell, "gst": gst}
    return prices, timestamp


def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    result = request_json(url, {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": False,
        "link_preview_options": {"is_disabled": True},
    })
    if not result.get("ok"):
        raise RuntimeError(f"Telegram rejected the message: {result.get('description', 'unknown error')}")


def load_state() -> dict:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        LOG.warning("Could not read alert state (%s); starting with fresh state", exc)
        return {}


def save_state(state: dict) -> None:
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temp.replace(STATE_PATH)


def check_once(config: dict, state: dict) -> None:
    try:
        prices, observed = fetch_prices()
    except Exception as exc:
        with RUNTIME_LOCK:
            RUNTIME_STATE["last_error"] = str(exc)
        raise

    with RUNTIME_LOCK:
        RUNTIME_STATE["last_prices"] = prices
        RUNTIME_STATE["last_observed"] = observed
        RUNTIME_STATE["last_check_at"] = datetime.now(timezone.utc).isoformat()
        RUNTIME_STATE["checks_count"] = RUNTIME_STATE.get("checks_count", 0) + 1
        RUNTIME_STATE["last_error"] = None

    for symbol, triggers in config["triggers"].items():
        quote = prices[symbol]
        buy_inr_g = quote["buy"]
        sell_inr_g = quote["sell"]
        buy_with_gst = buy_inr_g + quote["gst"]
        LOG.info("%s buy INR/g %.2f (incl GST %.2f) | sell INR/g %.2f | quote time %s", symbol, buy_inr_g, buy_with_gst, sell_inr_g, observed)
        metal_state = state.setdefault(symbol, {})
        for name, threshold in triggers.items():
            previous = metal_state.get(name, {})
            same_trigger = previous.get("source") == "india_buy_quote" and previous.get("threshold_inr_per_gram") == threshold
            was_above = bool(previous.get("above", False)) if same_trigger else False
            is_above = buy_inr_g >= threshold
            if is_above and not was_above:
                token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
                chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
                if token and chat_id:
                    try:
                        send_telegram(format_alert(symbol, name, threshold, quote, observed))
                        with RUNTIME_LOCK:
                            RUNTIME_STATE["alerts_sent"] = RUNTIME_STATE.get("alerts_sent", 0) + 1
                        LOG.warning("Sent Telegram alert for %s/%s buy rate at INR %.2f/g", symbol, name, buy_inr_g)
                    except Exception as exc:
                        LOG.error("Failed to send Telegram alert: %s", exc)
                else:
                    LOG.info("Alert triggered for %s/%s at INR %.2f/g, but Telegram is not configured in .env", symbol, name, buy_inr_g)
            metal_state[name] = {"source": "india_buy_quote", "threshold_inr_per_gram": threshold, "above": is_above, "last_buy_inr_per_gram": buy_inr_g, "checked_at": datetime.now(timezone.utc).isoformat()}
        save_state(state)


def format_alert(symbol: str, name: str, threshold: float, quote: dict, observed: str) -> str:
    metal_name = "Gold" if symbol == "XAU" else "Silver"
    checked = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M:%S %p IST")
    buy = quote["buy"]
    return (
        f"🪙 <b>{metal_name.upper()} PRICE ALERT</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Buy</b> <code>₹{buy:,.2f}/g</code>\n"
        f"🧾 <b>With GST</b> <code>₹{buy + quote['gst']:,.2f}/g</code>\n"
        f"↩️ <b>Sell</b> <code>₹{quote['sell']:,.2f}/g</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Trigger <b>{html.escape(name)}</b>: <b>₹{threshold:,.2f}/g</b>\n"
        f"🕒 Quote <code>{html.escape(observed)}</code>\n"
        f"✅ Checked <i>{html.escape(checked)}</i>"
    )


def format_rates(prices: dict, observed: str, only: str | None = None) -> str:
    rows = ["<b>🇮🇳 CURRENT METAL RATES</b>", "━━━━━━━━━━━━━━━━━━"]
    for symbol in ((only,) if only else ("XAU", "XAG")):
        quote = prices[symbol]
        label = "GOLD" if symbol == "XAU" else "SILVER"
        rows.extend([
            f"<b>{label}</b>",
            f"💰 Buy: <code>₹{quote['buy']:,.2f}/g</code>",
            f"🧾 Buy incl. GST: <code>₹{quote['buy'] + quote['gst']:,.2f}/g</code>",
            f"↩️ Sell: <code>₹{quote['sell']:,.2f}/g</code>",
            "━━━━━━━━━━━━━━━━━━",
        ])
    rows.append(f"🕒 Quote: <code>{html.escape(observed)}</code>")
    return "\n".join(rows)


HELP_TEXT = (
    "<b>METAL ALERT COMMANDS</b>\n"
    "<code>/rates</code> — gold and silver rates\n"
    "<code>/gold</code> or <code>/silver</code> — one metal's rate\n"
    "<code>/triggers</code> — list your alerts\n"
    "<code>/add gold name 20000</code> — add a trigger\n"
    "<code>/change gold name 20500</code> — change one\n"
    "<code>/delete gold name</code> — remove one\n"
    "<code>/help</code> — show this guide\n\n"
    "Triggers use the Indian buy rate before GST, in INR per gram. Names may use letters, numbers, _ or - (up to 24 characters)."
)


def handle_command(text: str, config: dict, state: dict) -> str | None:
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return None
    command = parts[0].split("@", 1)[0].lower()
    args = parts[1:]
    if command in {"/start", "/help"}:
        return HELP_TEXT
    if command in {"/rates", "/gold", "/silver"}:
        prices, observed = fetch_prices()
        only = "XAU" if command == "/gold" else "XAG" if command == "/silver" else None
        return format_rates(prices, observed, only)
    if command == "/triggers":
        lines = ["<b>YOUR PRICE TRIGGERS</b>"]
        for symbol, metal in (("XAU", "Gold"), ("XAG", "Silver")):
            named = config["triggers"][symbol]
            lines.append(f"\n<b>{metal}</b>")
            if not named:
                lines.append("  None set")
            for name, value in named.items():
                lines.append(f"  • <code>{html.escape(name)}</code> — ₹{value:,.2f}/g")
        return "\n".join(lines)
    if command == "/add":
        if len(args) != 3:
            return "Usage: <code>/add gold name 20000</code>"
        symbol = metal_symbol(args[0])
        if not symbol:
            return "Choose <code>gold</code> or <code>silver</code>."
        name = valid_trigger_name(args[1])
        if not name:
            return "Use a name of 1–24 letters, numbers, underscores, or hyphens."
        if any(name in group for group in config["triggers"].values()):
            return "That trigger name is already in use. Choose a unique name."
        value = parse_threshold(args[2])
        if value is None:
            return "Enter a positive trigger price, for example <code>/add gold alert1 20000</code>."
        config["triggers"][symbol][name] = value
        save_config(config)
        return f"✅ Added <b>{'Gold' if symbol == 'XAU' else 'Silver'} {html.escape(name)}</b> at <b>₹{value:,.2f}/g</b>."
    if command == "/change":
        if len(args) != 3:
            return "Usage: <code>/change gold name 20500</code>"
        symbol = metal_symbol(args[0])
        name = valid_trigger_name(args[1])
        value = parse_threshold(args[2])
        if not symbol:
            return "Choose <code>gold</code> or <code>silver</code>."
        if not name or name not in config["triggers"][symbol]:
            return "I couldn't find that trigger. Use <code>/triggers</code> to list them."
        if value is None:
            return "Enter a positive trigger price."
        config["triggers"][symbol][name] = value
        save_config(config)
        return f"✅ Changed <b>{'Gold' if symbol == 'XAU' else 'Silver'} {html.escape(name)}</b> to <b>₹{value:,.2f}/g</b>."
    if command == "/delete":
        if len(args) != 2:
            return "Usage: <code>/delete gold name</code>"
        symbol = metal_symbol(args[0])
        name = valid_trigger_name(args[1])
        if not symbol:
            return "Choose <code>gold</code> or <code>silver</code>."
        if not name or name not in config["triggers"][symbol]:
            return "I couldn't find that trigger. Use <code>/triggers</code> to list them."
        del config["triggers"][symbol][name]
        save_config(config)
        return f"🗑 Deleted <b>{'Gold' if symbol == 'XAU' else 'Silver'} {html.escape(name)}</b>."
    return "Unknown command. Send <code>/help</code> to see available commands."


def metal_symbol(value: str) -> str | None:
    return {"gold": "XAU", "xau": "XAU", "silver": "XAG", "xag": "XAG"}.get(value.lower())


def valid_trigger_name(value: str) -> str | None:
    value = value.lower()
    return value if re.fullmatch(r"[a-z0-9_-]{1,24}", value) else None


def parse_threshold(value: str) -> float | None:
    try:
        number = float(value.replace(",", ""))
    except ValueError:
        return None
    return number if math.isfinite(number) and number > 0 else None


def register_bot_commands() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    commands = [
        {"command": "rates", "description": "Show current gold and silver rates"},
        {"command": "gold", "description": "Show current gold rate"},
        {"command": "silver", "description": "Show current silver rate"},
        {"command": "triggers", "description": "List your price triggers"},
        {"command": "add", "description": "Add trigger: /add gold name price"},
        {"command": "change", "description": "Change trigger: /change gold name price"},
        {"command": "delete", "description": "Delete trigger: /delete gold name"},
        {"command": "help", "description": "Show bot help"},
    ]
    result = request_json(f"https://api.telegram.org/bot{token}/setMyCommands", {"commands": commands})
    if not result.get("ok"):
        raise RuntimeError(f"Could not register Telegram commands: {result.get('description', 'unknown error')}")


def poll_telegram(config: dict, state: dict, offset: int) -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    allowed_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not allowed_chat:
        return offset
    result = request_json(
        f"https://api.telegram.org/bot{token}/getUpdates",
        {"offset": offset, "timeout": 10, "allowed_updates": ["message"]},
        timeout=15,
    )
    if not result.get("ok"):
        raise RuntimeError(f"Telegram update request failed: {result.get('description', 'unknown error')}")
    for update in result.get("result", []):
        offset = max(offset, int(update["update_id"]) + 1)
        message = update.get("message", {})
        chat = message.get("chat", {})
        if str(chat.get("id", "")) != allowed_chat or chat.get("type") != "private":
            continue
        text = message.get("text", "")
        try:
            reply = handle_command(text, config, state)
            if reply:
                send_telegram(reply)
        except (HTTPError, URLError, OSError, ValueError, RuntimeError) as exc:
            LOG.error("Command failed: %s", exc)
            try:
                send_telegram("⚠️ I couldn't complete that request. Please try again shortly.")
            except Exception:
                pass
    return offset


def render_dashboard_html() -> str:
    """Generate modern, responsive web dashboard HTML for the root / endpoint."""
    with RUNTIME_LOCK:
        prices = RUNTIME_STATE.get("last_prices") or {}
        observed = RUNTIME_STATE.get("last_observed") or "Waiting for quote..."
        checks_count = RUNTIME_STATE.get("checks_count", 0)

    gold = prices.get("XAU", {"buy": 0.0, "sell": 0.0, "gst": 0.0})
    silver = prices.get("XAG", {"buy": 0.0, "sell": 0.0, "gst": 0.0})

    gold_buy = gold.get("buy", 0.0)
    gold_gst = gold.get("gst", 0.0)
    gold_with_gst = round(gold_buy + gold_gst, 2)
    gold_sell = gold.get("sell", 0.0)

    silver_buy = silver.get("buy", 0.0)
    silver_gst = silver.get("gst", 0.0)
    silver_with_gst = round(silver_buy + silver_gst, 2)
    silver_sell = silver.get("sell", 0.0)

    triggers = ACTIVE_CONFIG.get("triggers", {"XAU": {}, "XAG": {}})

    triggers_html = []
    for symbol, label, current_buy, color_cls in [
        ("XAU", "Gold", gold_buy, "gold-badge"),
        ("XAG", "Silver", silver_buy, "silver-badge"),
    ]:
        metal_triggers = triggers.get(symbol, {})
        if not metal_triggers:
            triggers_html.append(
                f'<div class="trigger-row empty"><span>{label}</span><span class="muted">No triggers configured</span></div>'
            )
        else:
            for name, threshold in metal_triggers.items():
                is_above = current_buy >= threshold
                status_text = "TRIGGERED" if is_above else "MONITORING"
                badge_class = "status-tag alert" if is_above else "status-tag watching"
                triggers_html.append(
                    f'<div class="trigger-row">'
                    f'  <div class="trigger-meta">'
                    f'    <span class="trigger-metal {color_cls}">{label}</span>'
                    f'    <span class="trigger-name"><code>{html.escape(name)}</code></span>'
                    f'  </div>'
                    f'  <div class="trigger-values">'
                    f'    <span class="trigger-target">Target: ₹{threshold:,.2f}/g</span>'
                    f'    <span class="{badge_class}">{status_text}</span>'
                    f'  </div>'
                    f'</div>'
                )

    triggers_rendered = "\n".join(triggers_html)

    telegram_ready = bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip() and os.getenv("TELEGRAM_CHAT_ID", "").strip())
    tg_status_badge = '<span class="status-tag watching">CONNECTED</span>' if telegram_ready else '<span class="status-tag" style="background:rgba(234,179,8,0.15);color:#facc15;border:1px solid rgba(234,179,8,0.3);">UNCONFIGURED</span>'
    port_display = str(ACTIVE_PORT) if ACTIVE_PORT is not None else "ENV"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="description" content="Bullion Watchdog - Live precious metals tracking & Telegram alert monitor">
  <title>Bullion Watchdog | Live Gold &amp; Silver Rates</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #090d16;
      --card-bg: rgba(18, 26, 43, 0.75);
      --card-border: rgba(255, 255, 255, 0.08);
      --card-hover: rgba(255, 255, 255, 0.12);
      --gold-primary: #f59e0b;
      --gold-gradient: linear-gradient(135deg, #fbbf24 0%, #d97706 100%);
      --silver-primary: #94a3b8;
      --silver-gradient: linear-gradient(135deg, #f1f5f9 0%, #64748b 100%);
      --emerald: #10b981;
      --emerald-glow: rgba(16, 185, 129, 0.25);
      --rose: #f43f5e;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --text-dim: #64748b;
      --font: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    }}
    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }}
    body {{
      background: radial-gradient(circle at top center, #131c31 0%, #090d16 100%);
      color: var(--text-main);
      font-family: var(--font);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      padding: 2rem 1rem;
    }}
    .container {{
      max-width: 960px;
      margin: 0 auto;
      width: 100%;
    }}
    header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 1rem;
      margin-bottom: 2rem;
      padding-bottom: 1.5rem;
      border-bottom: 1px solid var(--card-border);
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 0.85rem;
    }}
    .brand-icon {{
      font-size: 2.2rem;
      line-height: 1;
      background: rgba(245, 158, 11, 0.1);
      border: 1px solid rgba(245, 158, 11, 0.3);
      border-radius: 12px;
      padding: 0.4rem;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .brand-text h1 {{
      font-size: 1.5rem;
      font-weight: 800;
      letter-spacing: -0.02em;
      background: linear-gradient(135deg, #ffffff 0%, #cbd5e1 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }}
    .brand-text p {{
      font-size: 0.82rem;
      color: var(--text-muted);
    }}
    .live-badge {{
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.45rem 0.9rem;
      border-radius: 9999px;
      background: rgba(16, 185, 129, 0.1);
      border: 1px solid var(--emerald-glow);
      font-size: 0.78rem;
      font-weight: 600;
      color: #34d399;
      letter-spacing: 0.04em;
    }}
    .pulse-dot {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--emerald);
      box-shadow: 0 0 10px var(--emerald);
      animation: pulse 2s infinite ease-in-out;
    }}
    @keyframes pulse {{
      0% {{ transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }}
      70% {{ transform: scale(1.1); box-shadow: 0 0 0 8px rgba(16, 185, 129, 0); }}
      100% {{ transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }}
    }}
    .grid-2 {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 1.5rem;
      margin-bottom: 1.5rem;
    }}
    .price-card {{
      background: var(--card-bg);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 1.5rem;
      position: relative;
      overflow: hidden;
      transition: border-color 0.25s, transform 0.25s;
    }}
    .price-card:hover {{
      border-color: var(--card-hover);
      transform: translateY(-2px);
    }}
    .price-card.gold {{
      border-top: 3px solid var(--gold-primary);
    }}
    .price-card.silver {{
      border-top: 3px solid var(--silver-primary);
    }}
    .card-top {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1rem;
    }}
    .metal-title {{
      font-size: 0.85rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--text-muted);
    }}
    .metal-pill {{
      font-size: 0.7rem;
      font-weight: 700;
      padding: 0.2rem 0.5rem;
      border-radius: 6px;
      letter-spacing: 0.05em;
    }}
    .gold-pill {{
      background: rgba(245, 158, 11, 0.15);
      color: #fbbf24;
      border: 1px solid rgba(245, 158, 11, 0.3);
    }}
    .silver-pill {{
      background: rgba(148, 163, 184, 0.15);
      color: #cbd5e1;
      border: 1px solid rgba(148, 163, 184, 0.3);
    }}
    .primary-rate {{
      margin-bottom: 1.25rem;
    }}
    .rate-label {{
      font-size: 0.75rem;
      color: var(--text-dim);
      text-transform: uppercase;
      letter-spacing: 0.06em;
      margin-bottom: 0.25rem;
    }}
    .rate-val {{
      font-size: 2.2rem;
      font-weight: 800;
      letter-spacing: -0.02em;
      line-height: 1.1;
    }}
    .gold-rate {{
      background: var(--gold-gradient);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }}
    .silver-rate {{
      background: var(--silver-gradient);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }}
    .rate-unit {{
      font-size: 0.95rem;
      font-weight: 500;
      color: var(--text-muted);
      -webkit-text-fill-color: var(--text-muted);
    }}
    .breakdown-table {{
      width: 100%;
      border-top: 1px solid var(--card-border);
      padding-top: 0.9rem;
      font-size: 0.85rem;
    }}
    .breakdown-row {{
      display: flex;
      justify-content: space-between;
      padding: 0.35rem 0;
      color: var(--text-muted);
    }}
    .breakdown-row span:last-child {{
      font-weight: 600;
      color: var(--text-main);
    }}
    .section-card {{
      background: var(--card-bg);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 1.5rem;
      margin-bottom: 1.5rem;
    }}
    .section-title {{
      font-size: 0.95rem;
      font-weight: 700;
      letter-spacing: 0.02em;
      margin-bottom: 1rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }}
    .trigger-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0.75rem 0.9rem;
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      margin-bottom: 0.5rem;
    }}
    .trigger-row.empty {{
      justify-content: center;
      color: var(--text-dim);
      font-size: 0.85rem;
    }}
    .trigger-meta {{
      display: flex;
      align-items: center;
      gap: 0.65rem;
    }}
    .trigger-metal {{
      font-size: 0.7rem;
      font-weight: 700;
      padding: 0.2rem 0.5rem;
      border-radius: 6px;
    }}
    .gold-badge {{ background: rgba(245, 158, 11, 0.2); color: #fbbf24; }}
    .silver-badge {{ background: rgba(148, 163, 184, 0.2); color: #cbd5e1; }}
    .trigger-name code {{
      font-family: monospace;
      color: var(--text-main);
      background: rgba(255, 255, 255, 0.05);
      padding: 0.15rem 0.4rem;
      border-radius: 4px;
      font-size: 0.85rem;
    }}
    .trigger-values {{
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }}
    .trigger-target {{
      font-size: 0.85rem;
      font-weight: 600;
      color: var(--text-muted);
    }}
    .status-tag {{
      font-size: 0.68rem;
      font-weight: 700;
      padding: 0.2rem 0.5rem;
      border-radius: 6px;
      letter-spacing: 0.05em;
    }}
    .status-tag.watching {{
      background: rgba(59, 130, 246, 0.15);
      color: #60a5fa;
      border: 1px solid rgba(59, 130, 246, 0.3);
    }}
    .status-tag.alert {{
      background: rgba(244, 63, 94, 0.15);
      color: #fb7185;
      border: 1px solid rgba(244, 63, 94, 0.3);
    }}
    .stats-row {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 1rem;
    }}
    .stat-box {{
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 0.9rem;
    }}
    .stat-label {{
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-dim);
      margin-bottom: 0.25rem;
    }}
    .stat-value {{
      font-size: 1rem;
      font-weight: 700;
      color: var(--text-main);
    }}
    .api-pills {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
      margin-top: 1rem;
    }}
    .api-pill {{
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid var(--card-border);
      padding: 0.35rem 0.75rem;
      border-radius: 8px;
      color: #93c5fd;
      text-decoration: none;
      font-size: 0.8rem;
      font-family: monospace;
      transition: background 0.2s, border-color 0.2s;
    }}
    .api-pill:hover {{
      background: rgba(255, 255, 255, 0.08);
      border-color: rgba(255, 255, 255, 0.2);
    }}
    footer {{
      margin-top: auto;
      text-align: center;
      padding-top: 2rem;
      font-size: 0.75rem;
      color: var(--text-dim);
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="brand">
        <div class="brand-icon">🪙</div>
        <div class="brand-text">
          <h1>Bullion Watchdog</h1>
          <p>Indian Gold &amp; Silver Price Monitor</p>
        </div>
      </div>
      <div class="live-badge">
        <span class="pulse-dot"></span>
        <span>PORT {port_display} &bull; LIVE</span>
      </div>
    </header>

    <div class="grid-2">
      <div class="price-card gold">
        <div class="card-top">
          <span class="metal-title">Gold 24K (999)</span>
          <span class="metal-pill gold-pill">XAU / INR</span>
        </div>
        <div class="primary-rate">
          <div class="rate-label">Buy Rate (Pre-GST)</div>
          <div class="rate-val gold-rate">₹<span id="gold-buy">{gold_buy:,.2f}</span> <span class="rate-unit">/ g</span></div>
        </div>
        <div class="breakdown-table">
          <div class="breakdown-row">
            <span>Buy incl. 3% GST</span>
            <span>₹<span id="gold-buy-gst">{gold_with_gst:,.2f}</span> / g</span>
          </div>
          <div class="breakdown-row">
            <span>Sell Quote</span>
            <span>₹<span id="gold-sell">{gold_sell:,.2f}</span> / g</span>
          </div>
          <div class="breakdown-row">
            <span>GST Amount</span>
            <span>₹<span id="gold-gst">{gold_gst:,.2f}</span> / g</span>
          </div>
        </div>
      </div>

      <div class="price-card silver">
        <div class="card-top">
          <span class="metal-title">Fine Silver (999)</span>
          <span class="metal-pill silver-pill">XAG / INR</span>
        </div>
        <div class="primary-rate">
          <div class="rate-label">Buy Rate (Pre-GST)</div>
          <div class="rate-val silver-rate">₹<span id="silver-buy">{silver_buy:,.2f}</span> <span class="rate-unit">/ g</span></div>
        </div>
        <div class="breakdown-table">
          <div class="breakdown-row">
            <span>Buy incl. 3% GST</span>
            <span>₹<span id="silver-buy-gst">{silver_with_gst:,.2f}</span> / g</span>
          </div>
          <div class="breakdown-row">
            <span>Sell Quote</span>
            <span>₹<span id="silver-sell">{silver_sell:,.2f}</span> / g</span>
          </div>
          <div class="breakdown-row">
            <span>GST Amount</span>
            <span>₹<span id="silver-gst">{silver_gst:,.2f}</span> / g</span>
          </div>
        </div>
      </div>
    </div>

    <div class="section-card">
      <div class="section-title">🎯 Active Triggers</div>
      <div id="triggers-list">
        {triggers_rendered}
      </div>
    </div>

    <div class="section-card">
      <div class="section-title">⚡ Watchdog System &amp; API</div>
      <div class="stats-row">
        <div class="stat-box">
          <div class="stat-label">Server Port</div>
          <div class="stat-value">{port_display}</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Poll Interval</div>
          <div class="stat-value">{ACTIVE_CONFIG.get("poll_interval_seconds", 30)}s</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Total Checks</div>
          <div class="stat-value" id="checks-count">{checks_count}</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Telegram Bot</div>
          <div class="stat-value">{tg_status_badge}</div>
        </div>
      </div>
      <div class="api-pills">
        <a class="api-pill" href="/rates" target="_blank">GET /rates</a>
        <a class="api-pill" href="/health" target="_blank">GET /health</a>
        <a class="api-pill" href="/status" target="_blank">GET /status</a>
      </div>
    </div>

    <footer>
      Bullion Watchdog &bull; Running on port {port_display} &bull; Polls OroPocket India Precious Metals Endpoint
    </footer>
  </div>

  <script>
    async function updateRates() {{
      try {{
        const res = await fetch('/rates');
        if (!res.ok) return;
        const data = await res.json();
        if (data && data.rates) {{
          if (data.rates.gold) {{
            document.getElementById('gold-buy').textContent = Number(data.rates.gold.buy).toLocaleString('en-IN', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
            document.getElementById('gold-buy-gst').textContent = Number(data.rates.gold.buy_with_gst).toLocaleString('en-IN', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
            document.getElementById('gold-sell').textContent = Number(data.rates.gold.sell).toLocaleString('en-IN', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
            document.getElementById('gold-gst').textContent = Number(data.rates.gold.gst).toLocaleString('en-IN', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
          }}
          if (data.rates.silver) {{
            document.getElementById('silver-buy').textContent = Number(data.rates.silver.buy).toLocaleString('en-IN', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
            document.getElementById('silver-buy-gst').textContent = Number(data.rates.silver.buy_with_gst).toLocaleString('en-IN', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
            document.getElementById('silver-sell').textContent = Number(data.rates.silver.sell).toLocaleString('en-IN', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
            document.getElementById('silver-gst').textContent = Number(data.rates.silver.gst).toLocaleString('en-IN', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
          }}
        }}
      }} catch (err) {{
        console.error('Rates fetch error:', err);
      }}
    }}
    setInterval(updateRates, 15000);
  </script>
</body>
</html>
"""


class WatchdogHTTPHandler(BaseHTTPRequestHandler):
    server_version = "BullionWatchdog/1.0"

    def do_HEAD(self) -> None:
        self.handle_request(is_head=True)

    def do_GET(self) -> None:
        self.handle_request(is_head=False)

    def handle_request(self, is_head: bool = False) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if not path:
            path = "/"

        if path in {"/health", "/healthz"}:
            self.send_json({"status": "ok", "uptime_seconds": get_uptime_seconds()}, is_head=is_head)
        elif path in {"/rates", "/api/rates"}:
            self.send_rates(is_head=is_head)
        elif path in {"/status", "/api/status", "/json"}:
            self.send_status(is_head=is_head)
        elif path == "/":
            accept = self.headers.get("Accept", "")
            if "application/json" in accept and "text/html" not in accept:
                self.send_status(is_head=is_head)
            else:
                self.send_dashboard(is_head=is_head)
        else:
            self.send_json({"error": "Not Found", "status": 404}, status_code=404, is_head=is_head)

    def send_json(self, data: dict, status_code: int = 200, is_head: bool = False) -> None:
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        if not is_head:
            self.wfile.write(payload)

    def send_rates(self, is_head: bool = False) -> None:
        with RUNTIME_LOCK:
            prices = RUNTIME_STATE.get("last_prices")
            observed = RUNTIME_STATE.get("last_observed")
            last_checked = RUNTIME_STATE.get("last_check_at")

        if not prices:
            self.send_json({
                "status": "pending",
                "message": "First price check is still in progress"
            }, status_code=503, is_head=is_head)
            return

        response_data = {
            "status": "ok",
            "provider_quote_time": observed,
            "last_checked_at": last_checked,
            "rates": {
                "gold": {
                    "symbol": "XAU",
                    "currency": "INR",
                    "unit": "gram",
                    "buy": prices["XAU"]["buy"],
                    "buy_with_gst": round(prices["XAU"]["buy"] + prices["XAU"]["gst"], 2),
                    "sell": prices["XAU"]["sell"],
                    "gst": prices["XAU"]["gst"],
                },
                "silver": {
                    "symbol": "XAG",
                    "currency": "INR",
                    "unit": "gram",
                    "buy": prices["XAG"]["buy"],
                    "buy_with_gst": round(prices["XAG"]["buy"] + prices["XAG"]["gst"], 2),
                    "sell": prices["XAG"]["sell"],
                    "gst": prices["XAG"]["gst"],
                },
            },
        }
        self.send_json(response_data, is_head=is_head)

    def send_status(self, is_head: bool = False) -> None:
        with RUNTIME_LOCK:
            prices = RUNTIME_STATE.get("last_prices")
            observed = RUNTIME_STATE.get("last_observed")
            last_checked = RUNTIME_STATE.get("last_check_at")
            checks_count = RUNTIME_STATE.get("checks_count", 0)
            alerts_sent = RUNTIME_STATE.get("alerts_sent", 0)
            last_error = RUNTIME_STATE.get("last_error")

        telegram_configured = bool(
            os.getenv("TELEGRAM_BOT_TOKEN", "").strip() and os.getenv("TELEGRAM_CHAT_ID", "").strip()
        )

        response_data = {
            "service": "Bullion-Watchdog",
            "status": "running",
            "port": ACTIVE_PORT,
            "uptime_seconds": get_uptime_seconds(),
            "started_at": RUNTIME_STATE.get("started_at"),
            "checks_count": checks_count,
            "alerts_sent": alerts_sent,
            "telegram_configured": telegram_configured,
            "poll_interval_seconds": ACTIVE_CONFIG.get("poll_interval_seconds", 30),
            "last_check_at": last_checked,
            "last_error": last_error,
            "provider_quote_time": observed,
            "triggers": ACTIVE_CONFIG.get("triggers", {}),
            "rates": prices,
        }
        self.send_json(response_data, is_head=is_head)

    def send_dashboard(self, is_head: bool = False) -> None:
        html_content = render_dashboard_html()
        payload = html_content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        if not is_head:
            self.wfile.write(payload)

    def log_message(self, format: str, *args: any) -> None:
        # Route HTTP access logs to DEBUG level to avoid flooding stderr
        LOG.debug("HTTP %s - %s", self.address_string(), format % args)


def start_http_server(host: str, port: int) -> tuple[HTTPServer, threading.Thread] | None:
    """Start the HTTP server on a daemon thread."""
    global ACTIVE_PORT
    ACTIVE_PORT = port
    try:
        server = HTTPServer((host, port), WatchdogHTTPHandler)
    except OSError as exc:
        LOG.error("Failed to bind HTTP server to %s:%s: %s", host, port, exc)
        return None

    thread = threading.Thread(target=server.serve_forever, daemon=True, name="watchdog-http")
    thread.start()
    LOG.info("HTTP server listening on http://%s:%s (health: /health, rates: /rates, status: /status)",
             host if host != "0.0.0.0" else "localhost", port)
    return server, thread


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        load_dotenv()
        config = read_config()
        state = load_state()

        global ACTIVE_CONFIG, ACTIVE_STATE
        ACTIVE_CONFIG = config
        ACTIVE_STATE = state

        # 1. Start HTTP Server strictly based on PORT in environment / .env
        port = get_server_port()
        host = get_server_host()
        if port is not None:
            start_http_server(host, port)
        else:
            LOG.info("No PORT defined in environment or .env; running monitor without web server")

        # 2. Run initial price check to immediately populate live rates
        try:
            check_once(config, state)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
            LOG.warning("Initial price check failed (%s); will retry on schedule", exc)

        # 3. Check Telegram configuration
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        allowed_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        telegram_enabled = bool(bot_token and allowed_chat)

        if telegram_enabled:
            try:
                register_bot_commands()
                LOG.info("Telegram commands registered successfully for configured chat")
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
                LOG.warning("Could not register Telegram commands on startup: %s", exc)
        else:
            LOG.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured in .env; running in web & price monitoring mode (threshold alerts disabled)")

        LOG.info("Monitoring Indian gold/silver rates in INR/gram every %s seconds", config["poll_interval_seconds"])

        update_offset = 0
        next_price_check = time.monotonic() + config["poll_interval_seconds"]
        while True:
            if telegram_enabled:
                try:
                    update_offset = poll_telegram(config, state, update_offset)
                except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
                    LOG.error("Telegram polling failed: %s", exc)
                    time.sleep(5)
            else:
                time.sleep(1)

            if time.monotonic() >= next_price_check:
                try:
                    check_once(config, state)
                except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
                    LOG.error("Price check failed: %s", exc)
                next_price_check = time.monotonic() + config["poll_interval_seconds"]

    except (OSError, ValueError, RuntimeError) as exc:
        LOG.error("Startup failed: %s", exc)
        return 1
    except KeyboardInterrupt:
        LOG.info("Stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
