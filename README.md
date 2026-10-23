# Voice booking webhook

Author: Daniel Perea

A voice agent can end a call with "you're all booked" while nothing landed on the
calendar. I've seen it happen: the booking tool's Server URL was empty, the tool
never answered, and the AI's call summary still said the appointment was booked.

This sample is the server side of that booking flow, built so the mistake can't
hide. A call only counts as booked when the `book_appointment` tool itself returned
`booked: true`. If the summary claims a booking and the tool result doesn't back it
up, the call is marked `not_booked` and a warning is logged.

The business is fictional (Maple Street Dental). So are the callers.

## How it works

1. The agent asks the server which times are open (`check_availability`).
2. The caller picks one and the agent calls `book_appointment`.
3. The server writes the slot to SQLite. A second caller can't take the same slot.
4. The agent gets the real result back, and an alert goes out (a log line, or Telegram).
5. After the call, Vapi sends its end-of-call report. The server reads the tool
   results in it, not the summary, and stores the outcome.

```text
Caller -> Vapi -> /vapi/tools  -> SQLite calendar -> booking result -> alert
          Vapi -> /vapi/events -> tool results  -> stored outcome
                                  summary       -> warning only
```

The rule lives in `classify.py`. The test that proves it is
`test_summary_says_booked_but_tool_never_ran_is_not_booked` in `tests/test_webhooks.py`.

## Run it locally

You need Python 3.11. No Vapi or Telegram account is needed to try the examples.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # then set WEBHOOK_SECRET to a value of your own
uvicorn app:create_app --factory --env-file .env --host 127.0.0.1 --port 8000
```

The server won't start with an empty secret. In a second terminal:

```bash
set -a; . ./.env; set +a
H="X-Vapi-Secret: $WEBHOOK_SECRET"
curl -H "$H" -H 'Content-Type: application/json' --data-binary @examples/tools-availability.json http://127.0.0.1:8000/vapi/tools
curl -H "$H" -H 'Content-Type: application/json' --data-binary @examples/tools-book.json http://127.0.0.1:8000/vapi/tools
curl -H "$H" 'http://127.0.0.1:8000/bookings?date=2030-01-07'
curl -H "$H" -H 'Content-Type: application/json' --data-binary @examples/events-summary-only.json http://127.0.0.1:8000/vapi/events
```

The last call is the interesting one. Its summary says "booked", but there's no
tool result, so the answer is `not_booked` with the warning
`summary_claims_booking_but_tool_failed`.

Everything in `examples/` follows the same pattern: `tools-*.json` goes to
`/vapi/tools` and `events-*.json` goes to `/vapi/events`.

## Tests

```bash
ruff check .
pytest -q
```

86 tests, no network. The headline one is the misleading summary. Others try
to double-book the same slot with simultaneous requests, send a wrong secret,
replay a webhook, and book across both daylight-saving changes. GitHub Actions
runs them on every push.

## Connecting a real Vapi assistant

Host the server at an HTTPS address, then:

1. Create two Function tools named `check_availability` and `book_appointment`.
   Point both Server URLs at `https://<your-host>/vapi/tools` and leave async off.
   An async tool answers right away with a generic acknowledgement, which is the
   exact gap this project closes.
2. Add a custom credential that sends the header `X-Vapi-Secret` with your
   `WEBHOOK_SECRET` (turn off the Bearer prefix). Attach it to both tools and to
   the assistant's server settings.
3. Set the assistant's Server URL to `https://<your-host>/vapi/events` and
   subscribe it to `end-of-call-report`.
4. Tell the assistant to confirm a booking only after it gets `booked: true` back.
5. Make a test call and check the calendar (`/bookings`), not the summary.

Tool parameter schemas:

```json
{"type":"object","properties":{"date":{"type":"string","description":"Business-local date, YYYY-MM-DD"}},"required":["date"],"additionalProperties":false}
```

```json
{"type":"object","properties":{"start":{"type":"string","description":"An offered ISO 8601 start with the business timezone's UTC offset"},"name":{"type":"string"},"phone":{"type":"string"},"reason":{"type":"string"}},"required":["start","name","phone"],"additionalProperties":false}
```

Payload formats were checked against Vapi's docs on 2026-10-24:
[function tools](https://docs.vapi.ai/tools/custom-tools),
[server events](https://docs.vapi.ai/server-url/events),
[authentication](https://docs.vapi.ai/server-url/server-authentication), and the
[tool result](https://raw.githubusercontent.com/VapiAI/server-sdk-typescript/main/src/api/types/ToolCallResultMessage.ts)
and [end-of-call report](https://raw.githubusercontent.com/VapiAI/server-sdk-typescript/main/src/api/types/ServerMessageEndOfCallReport.ts)
types in Vapi's SDK. The tests run on local copies of those shapes.

## Settings

Defaults: weekdays 9:00 to 17:00, 30-minute slots, `America/New_York`. Change them
in `.env`. Keep them fixed once bookings exist.

## What it leaves out

This is a sample, not a finished product. It has one local calendar with no
Google or Outlook sync. There's no cancellation flow and no holiday rules. Alerts
are best effort with no retry queue. The summary warning is a simple English
keyword check. It only raises a flag and never decides the outcome.

## Docker

```bash
docker build -t voice-booking-webhook .
docker run --rm --env-file .env -p 127.0.0.1:8000:8000 -v voice-booking-data:/app/data voice-booking-webhook
```

Code map: `app.py` routes, `vapi.py` payload parsing, `booking.py` schedule rules,
`db.py` storage, `classify.py` the booked rule, `notify.py` alerts, `config.py`
settings. MIT licensed.
