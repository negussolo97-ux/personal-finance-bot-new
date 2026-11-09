"""Single-owner Telegram finance bot. Run one Gunicorn worker (see render.yaml)."""
import hmac
import hashlib
import json
import os
import threading
import time
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timedelta
from flask import Flask, request
from werkzeug.exceptions import HTTPException
from sqlalchemy import select
from storage import Store, Event, Tx, Debt, Update, Outbox, Audit
from parser import TZ, money
from ledger import ingest, handle_text, handle_callback, queue, summary, debt_balance, now


def webhook_secret(value):
    # Render-generated secrets may contain characters Telegram does not allow in this header.
    return hashlib.sha256(value.encode()).hexdigest()


def telegram(token, method, payload):
    url = f'https://api.telegram.org/bot{token}/{method}'
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            response = json.load(r)
        return response.get('ok') is True
    except (urllib.error.URLError, TimeoutError, ValueError):
        # Never log the request URL: it contains the bot token.
        return False


def send_export(token, chat_id, data):
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="finance-export.json"\r\nContent-Type: application/json\r\n\r\n').encode()
    body += json.dumps(data, ensure_ascii=False, indent=2).encode() + f'\r\n--{boundary}--\r\n'.encode()
    req = urllib.request.Request(f'https://api.telegram.org/bot{token}/sendDocument', data=body, headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get('ok') is True
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


def snapshot(s):
    result = {'schema_version': 1, 'exported_at': now()}
    for cls in (Event, Tx, Debt, Audit):
        result[cls.__tablename__] = [{c.name: getattr(row, c.name) for c in cls.__table__.columns} for row in s.scalars(select(cls))]
    return result


def schedule_digest(s, local_now, hour):
    # Send yesterday's completed day to avoid leaving late-night spending out.
    if local_now.hour < hour:
        return
    day = (local_now.date() - timedelta(days=1)).isoformat()
    key = 'digest:' + day
    if s.scalar(select(Outbox).where(Outbox.key == key)):
        return
    text = summary(s, day)
    upcoming = []
    for d in s.scalars(select(Debt)):
        if d.due and debt_balance(s, d) > 0 and d.due <= (local_now.date() + timedelta(days=2)).isoformat():
            upcoming.append(f'#{d.id} {d.person}: {money(debt_balance(s, d))} principal · due {d.due}')
    if upcoming:
        text += '\n\nDue soon / overdue:\n' + '\n'.join(upcoming[:15])
    queue(s, text, key=key)


def drain(store, token, owner, limit=10):
    for _ in range(limit):
        # Short at-most-one active sender lease. Delivery is at-least-once: a crash after
        # Telegram accepts a message can duplicate a notification, never a ledger entry.
        with store.transaction() as s:
            item = s.scalar(select(Outbox).where(Outbox.sent.is_(False), Outbox.next_try <= time.time()).order_by(Outbox.id).limit(1))
            if item is None:
                return
            ident, method, payload = item.id, item.method, dict(item.payload)
            item.attempts += 1
            attempt = item.attempts
            item.next_try = time.time() + 90
            export = snapshot(s) if method == 'export' else None
        if method == 'export':
            ok = send_export(token, owner, export)
        else:
            if method == 'sendMessage':
                payload['chat_id'] = owner
            ok = telegram(token, method, payload)
        with store.transaction() as s:
            item = s.get(Outbox, ident)
            item.sent = ok
            item.next_try = time.time() + min(3600, 2 ** min(attempt, 11))


def create_app(config=None, start_worker=False):
    cfg = dict(os.environ)
    cfg.update(config or {})
    for key in ('DATABASE_URL', 'TELEGRAM_BOT_TOKEN', 'OWNER_CHAT_ID', 'INGEST_SECRET', 'WEBHOOK_SECRET'):
        if not cfg.get(key):
            raise RuntimeError(f'Missing required setting: {key}')
    for key in ('INGEST_SECRET', 'WEBHOOK_SECRET'):
        if len(cfg[key]) < 32:
            raise RuntimeError(f'{key} must contain at least 32 characters.')
    owner = int(cfg['OWNER_CHAT_ID'])
    if owner <= 0:
        raise RuntimeError('OWNER_CHAT_ID must be a positive private-chat ID.')
    hour = int(cfg.get('SUMMARY_HOUR', '8'))
    if not 0 <= hour <= 23:
        raise RuntimeError('SUMMARY_HOUR must be 0–23.')
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 32768
    store = Store(cfg['DATABASE_URL'])
    app.extensions['store'] = store
    app.extensions['finance_config'] = cfg

    def authorized(value, secret):
        return isinstance(value, str) and hmac.compare_digest(value, secret)

    @app.errorhandler(Exception)
    def safe_error(exc):
        if isinstance(exc, HTTPException):
            return {'error': exc.name}, exc.code
        app.logger.error('Request failed; financial data and credentials omitted.')
        return {'error': 'Temporary service error; retry the same submission.'}, 503

    @app.get('/')
    def index():
        return 'Finance bot is running. Use your private Telegram chat. No financial data is served here.'

    @app.get('/health')
    def health():
        with store.transaction() as s:
            s.execute(select(1))
        return {'ok': True}

    @app.post('/sms')
    def sms():
        if not authorized(request.headers.get('Authorization', ''), 'Bearer ' + cfg['INGEST_SECRET']):
            return {'error': 'Unauthorized'}, 401
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return {'error': 'JSON object required'}, 400
        text, account = data.get('text'), data.get('account')
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 10000:
            return {'error': 'text must contain 1–10000 characters'}, 400
        # Never persist obvious OTP messages, even if an automation filter was too broad.
        import re
        if re.search(r'\bOTP\b|one[- ]time (?:password|code)|verification code|security code', text, re.I):
            return {'ok': True, 'ignored': 'verification_message'}
        try:
            timestamp = data.get('captured_at') or now()
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                raise ValueError('captured_at must include a timezone')
            if dt > datetime.now(TZ) + timedelta(minutes=10):
                raise ValueError('captured_at is in the future')
            with store.transaction() as s:
                ident, duplicate = ingest(s, text, account, dt.astimezone(TZ).isoformat())
            return {'ok': True, 'event_id': ident, 'duplicate': duplicate}
        except (ValueError, TypeError, AttributeError) as exc:
            return {'error': str(exc) if isinstance(exc, ValueError) else 'Invalid request'}, 400

    @app.post('/telegram')
    def webhook():
        if not authorized(request.headers.get('X-Telegram-Bot-Api-Secret-Token', ''), webhook_secret(cfg['WEBHOOK_SECRET'])):
            return {'error': 'Unauthorized'}, 401
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or type(data.get('update_id')) is not int:
            return {'error': 'Invalid update'}, 400
        cb = data.get('callback_query')
        msg = cb.get('message', {}) if isinstance(cb, dict) else data.get('message', {})
        sender = cb.get('from', {}) if isinstance(cb, dict) else msg.get('from', {})
        if sender.get('id') != owner or msg.get('chat', {}).get('id') != owner or msg.get('chat', {}).get('type') != 'private':
            return {'ok': True}  # Ignore everyone except the owner, including groups.
        with store.transaction() as s:
            if s.get(Update, data['update_id']):
                return {'ok': True}
            s.add(Update(id=data['update_id']))
            s.flush()
            try:
                # A bad command rolls back all partial accounting changes, not the inbox marker.
                with s.begin_nested():
                    if cb:
                        handle_callback(s, str(cb.get('data', '')))
                    else:
                        handle_text(s, str(msg.get('text', '')))
            except (ValueError, IndexError) as exc:
                queue(s, f'Could not save: {exc}\nUse /help or /cancel.')
            if cb:
                s.add(Outbox(key=f'callback:{data["update_id"]}', method='answerCallbackQuery', payload={'callback_query_id': cb['id']}))
        return {'ok': True}

    if start_worker:
        def loop():
            while True:
                try:
                    with store.transaction() as s:
                        schedule_digest(s, datetime.now(TZ), hour)
                    drain(store, cfg['TELEGRAM_BOT_TOKEN'], owner)
                except Exception:
                    # No exception values/tracebacks: SQL and HTTP errors can contain private data.
                    app.logger.error('Background processing failed; will retry.')
                time.sleep(5)
        threading.Thread(target=loop, daemon=True).start()
    return app
