# India gold and silver Telegram alerts

This monitor reads Indian gold and silver quotes directly in INR per gram. It no longer converts an international USD spot price using a separate exchange rate. By default, it uses OroPocket's public price endpoint, which returns separate buy and sell rates, a GST amount, and a quote timestamp.

## Price meaning and freshness

- The alert trigger uses the provider's **buy rate per gram before GST**. The notification also shows the buy rate including the supplied GST amount and the sell rate.
- This is the provider's Indian buy/sell quote, not a universal benchmark, a guaranteed dealer quote, or an MCX/LBMA price. Local rates can differ.
- The provider documents a limit of 10 requests per minute and says the quote typically regenerates every two to two-and-a-half minutes based on a short observation. Polling every 30 seconds stays within the request limit; it does not force a fresh quote every 30 seconds. The endpoint has no SLA, so the script reports errors and tries again on its next poll.
- The quote timestamp from the response is included in each Telegram alert and every price log line.

## Set your triggers

Edit `config.json`, or use Telegram commands. Amounts are INR per gram; thresholds apply to the buy rate before GST. Telegram command changes are saved to this file automatically.

```json
{
  "poll_interval_seconds": 30,
  "triggers_inr_per_gram": {
    "XAU": { "default": 20000 },
    "XAG": { "default": 300 }
  }
}
```

`XAU` is gold and `XAG` is silver. The example values are placeholders; set your own. The monitor sends one alert when the buy rate reaches or crosses a threshold. It becomes eligible to alert again after the rate falls below that trigger. `alert_state.json` remembers this across restarts; delete it to reset the alert state.

The minimum poll interval is 10 seconds, but use 30 seconds or longer to respect the provider's published limit. Telegram delivery, internet access, and provider availability can affect alert timing.

## Telegram setup

1. Create a bot with **@BotFather** and keep its token private.
2. Open the bot and send `/start`. Get your chat ID from the `chat.id` field in Telegram's `getUpdates` response.
3. Create `.env` beside `price_alert.py`:

   ```text
   TELEGRAM_BOT_TOKEN=your-bot-token
   TELEGRAM_CHAT_ID=your-chat-id
   ```

   The script loads `.env` automatically, and `.env` is ignored by Git. If a token was pasted into a chat or browser address, revoke it with @BotFather and use its replacement.

Only the private chat whose ID matches `TELEGRAM_CHAT_ID` is allowed to use commands. Messages from other chats or groups are ignored without a reply.

## Telegram commands

Send these commands to your bot (metal may be `gold`, `silver`, `XAU`, or `XAG`):

| Command | What it does |
| --- | --- |
| `/rates` | Show current gold and silver rates |
| `/gold` or `/silver` | Show one metal's rate |
| `/triggers` | List all configured triggers |
| `/add gold alert1 20000` | Add a named trigger |
| `/change gold alert1 20500` | Change a trigger |
| `/delete gold alert1` | Delete a trigger |
| `/help` | Show command help |

Trigger names must be unique and use up to 24 letters, numbers, underscores, or hyphens. Each trigger alerts once per upward crossing and can alert again after the price falls below it.

## Run

From PowerShell in the project folder:

```powershell
python .\price_alert.py
```

Keep the process running. To start it automatically with Windows, configure Task Scheduler to launch Python with `price_alert.py` as the argument and the project folder as the working directory. Do not put secrets in task arguments.

## Changing the price provider

Set `INDIA_METALS_API_URL` to another endpoint that returns the same JSON structure: `data.gold` and `data.silver`, each with numeric `buy`, `sell`, optional `gst`, `currency` of INR, and `unit` of gram; `data.timestamp` should identify when the quote was generated. The script rejects responses with unexpected units instead of silently presenting them as INR per gram.
