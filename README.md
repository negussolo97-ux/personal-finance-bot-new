# Personal Finance Bot

A single-owner Telegram bot for Telebirr, CBE and Awash SMS alerts. Your iPhone forwards selected messages to a private HTTPS endpoint. The bot records transactions, asks for categories, tracks debts, and sends a daily summary. No bank login or banking API access is used.

**Status:** first version, tested locally. Deploy and complete the acceptance checklist before using it as your primary record. No credentials, real account numbers, chat IDs, or original SMS samples belong in this repository.

## What works

- Incoming choices: Salary, Loan / borrowed, Paid me back, My own transfer, Other income, Refund.
- Outgoing categories, lending, loan repayment and cash withdrawal.
- Service fees, VAT and the additional fund charge parsed separately where explicitly stated.
- Borrowing from providers or individuals; lending to individuals; opening debts; partial repayments and due dates.
- Confirmed transfers between accounts do not inflate income or spending. Equal-amount candidates are suggestions, never automatically linked.
- Loan notices are review items, not extra deposits. Link them to the actual transaction to avoid counting the credit twice.
- Transaction references or normalized-message fingerprints prevent duplicate imports; Telegram update IDs prevent duplicate command execution.
- Last reported balances with timestamps, balance discrepancy warnings, manual cash entries, corrections, voids, exports, and an audit history.
- Persistent outbox retries notification failures without undoing recorded transactions.
- Daily summary at 08:00 Addis Ababa time for the **previous completed day**, plus due-soon/overdue debt reminders. `/today` shows today's running total. Change `SUMMARY_HOUR` to another 24-hour clock hour if preferred.

## Get started

1. Make your GitHub repository private.
2. Follow [Render setup](docs/DEPLOY.md). The supplied Blueprint creates paid resources; review the price before confirming.
3. Follow [iPhone setup](docs/IPHONE.md). The final Shortcut sends to **your service**, not Telegram's `sendMessage` endpoint.
4. Complete [acceptance checks](docs/ACCEPTANCE.md).

## Accounting rules

| Movement | Treatment |
|---|---|
| Salary / other income | Income |
| Own-account transfer | Neither income nor spending; fees remain spending |
| Borrowed funds | New debt, not income |
| Lending money | New receivable, not consumption spending |
| Principal repaid | Reduces debt; not counted as spending twice |
| Principal returned to you | Reduces receivable; not income |
| Repayment beyond principal | Borrowing costs (outgoing) or interest income (incoming) |
| Cash withdrawal | Cash movement, not a purchase; log actual cash purchases separately |
| Refund | Shown separately; not silently subtracted from an unrelated expense category |
| Loan announcement / fee notice | Review only until its cash effect is confirmed |

Amounts use integer cents, never floating-point arithmetic. Report totals depend on categorization and SMS coverage. Pending transactions are counted separately. Missing SMS, cash spending, and messages captured while offline require reconciliation.

## Loan workflow

1. When the credit arrives, choose **Loan / borrowed** and enter a lender name.
2. For a pre-existing debt, `/opening owe 1000 Example Lender` creates a debt without adding cash or income. Use `owed` for money others owe you.
3. On a repayment, choose **Repaying a loan**, select the debt, and enter the principal portion. The remainder is recorded as borrowing costs; separately parsed transfer fees remain separate.
4. `/due 1 2026-12-01` sets a due date. `/debts` shows principal remaining.
5. `/review` lists notices. `/resolve NOTICE_ID TX_ID` links a notice to its recorded movement without posting money. If a disbursement exists only as a loan notice, confirm the actual amount credited, create `/manual Awash in 300`, categorize it as borrowed, then resolve the notice to it.
6. If fees were withheld from a disbursement, confirm gross principal and cash credited first. Use `/amount TX_ID GROSS_PRINCIPAL PAID_FEE` to record the gross borrowing and the fee once. Do not treat an announced unpaid fee as already paid. A confirmed fee paid separately can be entered as `/manual ACCOUNT out AMOUNT loan fee` and categorized as an expense.
7. The app does **not** calculate loan interest from an ambiguous rate or automatically interpret a lender's changing terms. Debt totals show recorded principal, not a lender-confirmed payoff quote.

All commands are available through `/help`. Correct a transaction using `/edit`, `/date`, or `/amount`. `/void` excludes it from totals but preserves the source. To correct an opening debt use `/openingfix ID AMOUNT`; linked repayments must remain valid.

## Local development

Use Python 3.12 and a virtual environment. Install `requirements.txt` and `pytest==8.4.2`, then run:

```sh
python -m pytest -q
```

Tests use fake SMS and an in-memory SQLite database; no Telegram traffic or credentials are required. Production uses PostgreSQL. A local server needs the environment variables described in `.env.example`; that file is documentation and is not loaded automatically.

Run **one Gunicorn worker** with threads, as configured in `render.yaml`. A background thread drains the durable outbox and schedules the digest. The database lock serializes ledger changes across threads and overlapping deployments. Application schema creation is idempotent; future schema changes require explicit migrations.

## Reliability and privacy

- `/sms` requires a bearer secret; `/telegram` requires Telegram's webhook secret header and the configured private-chat owner ID.
- The app never fetches payment receipt links. They are used only as deduplication identifiers.
- Known OTP phrases are ignored, but the iPhone sender/content filters are the primary way to exclude unrelated SMS.
- SMS bodies and transaction details are stored in your database. Telegram messages and exports also contain financial details. Bot chats are not end-to-end encrypted.
- Secrets stay in Render environment settings. Logging avoids request bodies, SQL error details, and token-bearing URLs.
- Telegram notification delivery is at-least-once. A process crash after delivery but before confirmation can duplicate a notification, not a ledger entry.
- A repeated identical SMS without a unique reference is conservatively treated as a duplicate. If it was a separate real transaction, add it manually after checking the bank.
- The phone must reach the service to import an SMS. This version does not install an offline queue on iOS. Replay missed messages after connectivity returns; server deduplication protects against repeats.
- Repeat-payee automatic categorization is not enabled in this version. All new transactions ask for a category.
- Sender-provided account selection is trusted only after authentication. Authenticate submissions and configure each bank's Shortcut correctly.

## Backups and maintenance

Use Render's database recovery/backups, and verify the retention and restore options in your selected plan before production. `/export` sends a JSON snapshot to your private chat; save it somewhere secure outside Render. `python manage.py backup /secure/path/export.json` writes the same export with restricted file permissions; it must be downloaded because the web service filesystem is temporary. These are exports, not a one-command restore system. A database-native backup is preferable for full disaster recovery.

Check `/accounts`, `/review`, and `/pending` regularly against bank records. After SMS wording or iOS changes, rerun acceptance checks. Review dependencies periodically and test before redeploying. Auto-deploy is disabled so a repository update does not immediately change the live finance tracker.
