"""Run from Render Shell, never put tokens in command-line arguments."""
import json
import os
import sys
from app import telegram, create_app, snapshot, webhook_secret


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else 'help'
    if command == 'webhook':
        base = os.environ.get('RENDER_EXTERNAL_URL') or os.environ.get('PUBLIC_BASE_URL', '')
        if not base.startswith('https://'):
            raise SystemExit('Set PUBLIC_BASE_URL to the HTTPS service URL.')
        ok = telegram(os.environ['TELEGRAM_BOT_TOKEN'], 'setWebhook', {
            'url': base.rstrip('/') + '/telegram',
            'secret_token': webhook_secret(os.environ['WEBHOOK_SECRET']),
            'allowed_updates': ['message', 'callback_query'],
            'drop_pending_updates': False,
        })
        if not ok:
            raise SystemExit('Webhook setup failed. Check the token and HTTPS service URL in environment settings.')
        print('Webhook registered. Open your bot and send /start.')
    elif command == 'backup':
        if len(sys.argv) != 3:
            raise SystemExit('Usage: python manage.py backup /secure/path/export.json')
        app = create_app()
        with app.extensions['store'].transaction() as s:
            data = snapshot(s)
        fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print('Backup written. Store it outside the server; never commit it.')
    else:
        print('Commands: webhook | backup PATH')

if __name__ == '__main__':
    main()
