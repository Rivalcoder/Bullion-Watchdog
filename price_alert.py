"""Poll live precious-metal prices and send threshold alerts to Telegram."""

from __future__ import annotations

import json
import html
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("PRICE_ALERT_CONFIG", ROOT / "config.json"))
STATE_PATH = Path(os.getenv("PRICE_ALERT_STATE", ROOT / "alert_state.json"))
INDIA_PRICES_URL = os.getenv("INDIA_METALS_API_URL", "https://api.oropocket.com/public/prices")
LOG = logging.getLogger("price_alert")


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
    try:
        with urlopen(req, timeout=timeout) as response:
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
    prices, observed = fetch_prices()
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
                send_telegram(format_alert(symbol, name, threshold, quote, observed))
                LOG.warning("Sent Telegram alert for %s/%s buy rate at INR %.2f/g", symbol, name, buy_inr_g)
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
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
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
        # Ignore every other account and all group/channel chats without replying.
        if str(chat.get("id", "")) != allowed_chat or chat.get("type") != "private":
            continue
        text = message.get("text", "")
        try:
            reply = handle_command(text, config, state)
            if reply:
                send_telegram(reply)
        except (HTTPError, URLError, OSError, ValueError, RuntimeError) as exc:
            LOG.error("Command failed: %s", exc)
            send_telegram("⚠️ I couldn't complete that request. Please try again shortly.")
    return offset


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        load_dotenv()
        config = read_config()
        state = load_state()
        allowed_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not allowed_chat:
            raise RuntimeError("Set TELEGRAM_CHAT_ID in .env to restrict bot access to your private chat")
        register_bot_commands()
        LOG.info("Monitoring Indian gold/silver rates in INR/gram every %s seconds; private commands enabled for configured chat", config["poll_interval_seconds"])
        update_offset = 0
        next_price_check = 0.0
        while True:
            try:
                update_offset = poll_telegram(config, state, update_offset)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
                LOG.error("Telegram polling failed: %s", exc)
                time.sleep(5)
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
