# iPhone SMS forwarding

The successful Telegram `sendMessage` Shortcut was a delivery test. For tracking, SMS must go to this app's `/sms` endpoint so it can save the transaction and then send category buttons. A bot does not receive its own outgoing `sendMessage` text as a new user update.

## Create a service Shortcut

Create a separate Shortcut called **Finance Telebirr**. Keep the test Shortcut until this is verified.

1. Add a **URL** action: `https://YOUR-SERVICE.onrender.com/sms`.
2. Add **Get Contents of URL**, method **POST**.
3. Expand **Headers** and add `Authorization` with value `Bearer YOUR_INGEST_SECRET`. Include one space after `Bearer`; use the generated secret from Render.
4. Set **Request Body → JSON** and add these Text fields:

| Key | Value |
|---|---|
| `account` | `Telebirr` (typed exactly) |
| `text` | **Shortcut Input** variable bubble (not typed text) |

5. Optionally add `captured_at` using Current Date formatted as ISO 8601 **with timezone**, such as `2026-11-09T13:00:00+03:00`. If omitted, the service uses arrival time. Older forwarded CBE alerts need `/date` correction because their SMS body has no timestamp.
6. During setup add **Show Result** to inspect the server response. A success has `ok: true` and an `event_id`; `duplicate: true` means it was already captured. Remove Show Result after the manual test so it does not interrupt locked-phone execution.
7. Run a harmless test manually to grant network access to the service while the phone is unlocked.

## Attach to automation

Use the tested Message automation pattern: **Run Immediately → Run Shortcut → Finance Telebirr → Input = incoming Message → Content**. The earlier screenshot showed the property called **Content**, not Body.

Start with a test keyword. After acceptance testing, create sender-filtered automations for Telebirr, CBE, Awash, and separate loan sender IDs if applicable. Choose the actual sender shown in your Messages app. The Telebirr loan sample came from a short code, which may differ from the normal transaction sender. Do not assume all providers use one sender.

Duplicate the service Shortcut for CBE and Awash, changing only `account`. Select the corresponding Shortcut for each automation. Do not infer the source bank from a message merely mentioning another provider, and do not create overlapping automations for the same message.

Do not forward all SMS. Filter transaction/loan sender(s), exclude OTPs and unrelated notifications, and test with your real sender IDs. Unknown formats are saved to `/review` without financial effects.

## Offline or failure handling

This version has durable retries **after the server accepts an SMS**, but no automatic iPhone offline queue. If the phone cannot reach the service, the SMS might not be imported. Check the automation's failure notification and compare bank activity with `/accounts` and `/pending`.

For manual recovery, create a helper Shortcut with **Text** containing the exact missed SMS, followed by **Run Shortcut → Finance Telebirr/CBE/Awash**, passing that Text as input. Check the resulting category prompt. A replay of an already-recorded message will not create another entry. Correct the date for old messages lacking a bank timestamp.

No old SMS history is imported automatically. Do not erase source SMS while validating this setup.
