# Deploy on Render

The code is ready for setup, not yet live. Do not publish credentials in GitHub, screenshots, chat, or command-line arguments.

## 1. Create resources

In Render, choose **New → Blueprint**, authorize this repository, select branch `main`, and use the root `render.yaml`.

Review the resource list and monthly estimate before confirming. It creates:

- one paid Python web service, `personal-finance-bot`, in Frankfurt;
- one small paid PostgreSQL database, `finance-db`, in the same region with 1 GB storage;
- no custom domain and no separate paid worker.

The current database plan identifier is `0.1c-256mb`. If Render offers different plans, use its current smallest suitable persistent paid PostgreSQL plan and confirm the displayed price. The service needs to stay running for daily summaries and outbox retries.

Enter the prompted values:

| Setting | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your current private BotFather token; revoke any token that was exposed earlier |
| `OWNER_CHAT_ID` | Your positive private chat ID from `getUpdates` |

Render generates separate `INGEST_SECRET` and `WEBHOOK_SECRET` values. Keep them private. `DATABASE_URL` is linked automatically. The service starts only when the required settings exist. External database access is disabled; use the internal database connection.

## 2. Connect Telegram

Wait for the deployment to be healthy. In the web service's **Shell**, run:

```sh
python manage.py webhook
```

The command reads the token and webhook secret from the environment and uses Render's HTTPS service URL. It does not print credentials. For hosting outside Render, set `PUBLIC_BASE_URL` first.

Then open your bot and send `/start`. It should show the command list. Registration does not drop pending Telegram updates. `getUpdates` stops being usable while the webhook is active; this is expected.

## 3. Connect your iPhone

Copy the web service URL, such as `https://your-service.onrender.com`, and get `INGEST_SECRET` from **Environment**. Follow `IPHONE.md`. No Telegram token is needed in the final Shortcut.

## 4. Verify before real use

Complete `ACCEPTANCE.md`. Check that only you can use the bot, SMS imports survive a restart, and category/debt totals match the test cases. Confirm database recovery settings and download an export. None of these live checks have been performed merely by publishing the repository.

## Updates

Auto-deploy is off. After code changes are tested, choose **Manual Deploy → Deploy latest commit**. Keep one worker. Never point production at SQLite or a database file on the temporary web-service filesystem. Back up PostgreSQL before schema changes.

References: [Render Blueprint reference](https://render.com/docs/blueprint-spec), [Telegram Bot API](https://core.telegram.org/bots/api), [Flask production deployment](https://flask.palletsprojects.com/en/stable/deploying/).
