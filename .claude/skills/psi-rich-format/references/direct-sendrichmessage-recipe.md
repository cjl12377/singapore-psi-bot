# Calling sendRichMessage directly

python-telegram-bot 21.6 has no wrapper for the Bot API 9.5+ `sendRichMessage` endpoint, so the bot posts to it with httpx (`_send_rich()` in `bot.py`).

```python
resp = await client.post(
    f"https://api.telegram.org/bot{token}/sendRichMessage",
    json={
        "chat_id": chat_id,
        # JSON-ENCODED STRING wrapping {"markdown": ...} or {"html": ...}
        "rich_message": json.dumps({"markdown": markdown}, ensure_ascii=False),
    },
)
```

Key facts (verified against a live send from this bot):

- `rich_message` is a **JSON-encoded string**, not a bare markdown string and not a `text` field. Sending `text` to `sendRichMessage`, or `rich_message` to `sendMessage`, is rejected.
- Success: `{"ok": true, "result": {"message_id": N, "rich_message": {"blocks": [...]}}}`. The `blocks` list shows how Telegram parsed the message (e.g. a `heading` block), which is a quick check that formatting was recognised.
- Real headings (`#`/`##`/`###`), tables, `---` and blockquotes are all accepted.
- The user must have started the bot (`/start`) or the send fails.

## Testing without deploying
Run the formatter against live data and send to yourself (needs `BOT_TOKEN` and `ADMIN_USER_ID` from `.env`):

```bash
set -a && . ./.env && set +a && python3 -c "
import asyncio, os, json, httpx
from psi import get_psi_data, format_psi_rich
async def main():
    data, stale = await get_psi_data()
    r = httpx.post(f'https://api.telegram.org/bot{os.environ[\"BOT_TOKEN\"]}/sendRichMessage', json={'chat_id': int(os.environ['ADMIN_USER_ID']), 'rich_message': json.dumps({'markdown': format_psi_rich(data, stale)}, ensure_ascii=False)})
    print(r.status_code, r.text[:300])
asyncio.run(main())
"
```

Never log the request URL: it contains the bot token.
