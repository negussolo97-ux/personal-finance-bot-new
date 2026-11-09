from datetime import datetime
import pytest
from sqlalchemy import select
from app import create_app, schedule_digest, drain, webhook_secret
from parser import parse_sms, cents
from ledger import ingest, classify, handle_text, handle_callback, debt_balance, summary, pair
from storage import Event, Tx, Debt, Outbox, Update, Lock

STAMP = '2026-11-09T13:00:00+03:00'

@pytest.fixture
def app():
    return create_app({'DATABASE_URL': 'sqlite://', 'TELEGRAM_BOT_TOKEN': 'test-only', 'OWNER_CHAT_ID': '123', 'INGEST_SECRET': 's'*32, 'WEBHOOK_SECRET': 'w'*32})

@pytest.fixture
def store(app):
    return app.extensions['store']


def manual(s, direction, amount, account='Telebirr', fee=0):
    t = Tx(account=account, direction=direction, amount=amount, fee=fee, occurred_at=STAMP)
    s.add(t)
    s.flush()
    return t

@pytest.mark.parametrize('account,text,amount,fee,direction,balance', [
    ('Telebirr', 'You have received ETB 123.45 from Example on 09/01/2026 13:23:14. Your transaction number is TEST1. Your current E-Money Account balance is ETB 1,234.56.', 12345, 0, 'in', 123456),
    ('Telebirr', 'You have transferred ETB 200.00 to Example on 09/01/2026 13:24:49. Your transaction number is TEST2. The service fee is ETB 4.20 and 15% VAT on the service fee is ETB 0.63. Your current E-Money Account balance is ETB 1,029.73.', 20000, 483, 'out', 102973),
    ('CBE', 'You have received ETB 500.00 from account 1***1234 to your account 1***5678. Your current balance is ETB 1,500.00.', 50000, 0, 'in', 150000),
    ('CBE', 'A debit transaction of ETB 250.00 has occurred. Service charge of ETB 2.00 and VAT(15%) of ETB 0.30 and Disaster Recovery(5%) of 0.10 with total of ETB 252.40. Your current balance is ETB 1,247.60.', 25000, 240, 'out', 124760),
    ('Awash', 'You have received ETB 2,200.00 via AwashRemit( includes a bonus ETB 200.00). Transaction ID: TEST3. 2026-01-06 08:35:29.', 220000, 0, 'in', None),
    ('Awash', 'Telebirr Transfer of 1,500.00 ETB to Example from BANK, Reason-Bidder, Charge 5.00 VAT: 0.75 EDRRF 0.25 ETB. Your Balance is ETB 100.00.', 150000, 600, 'out', 10000),
    ('Telebirr', 'Your telebirr account has been credited with ETB 1,000.00. Your current telebirr E-Money Account balance is ETB 1,500.00.', 100000, 0, 'in', 150000),
])
def test_parsers(account, text, amount, fee, direction, balance):
    p = parse_sms(text, account, STAMP)
    assert p['kind'] == 'transaction'
    assert (p['amount'], p['fee'], p['direction'], p['balance']) == (amount, fee, direction, balance)

@pytest.mark.parametrize('account,text', [
    ('Awash', 'Your Awash Lehul um loan of 200.0birr has been disbursed. Processing fee: 2.50 Birr. Interest: .60% per day. First repayment due on 09/11/2026.'),
    ('Telebirr', 'You have paid ETB 20.00 for Ethiopian Disaster Risk Management fund (1% of your loan amount).'),
    ('Telebirr', 'Your credit request with TEST contract number is successful. The credit amount is ETB 2,000.00 and facilitation fee ETB 50.00 with due date 03/12/2026.'),
])
def test_loan_notices_are_not_posted(store, account, text):
    with store.transaction() as s:
        ingest(s, text, account, STAMP)
        assert list(s.scalars(select(Tx))) == []
        assert s.scalar(select(Event)).data['kind'] == 'loan_notice'


def test_inconsistent_debit_needs_review():
    p = parse_sms('A debit transaction of ETB 100.00. Service charge of ETB 2.00 with total of ETB 150.00.', 'CBE', STAMP)
    assert p['kind'] == 'review'


def test_deduplication(store):
    with store.transaction() as s:
        a = ingest(s, 'You have received ETB 20.00. Your transaction number is TEST4.', 'Telebirr', STAMP)
        b = ingest(s, 'You have received ETB 20.00. Your transaction number is TEST4.', 'Telebirr', '2026-11-10T00:00:00+03:00')
        assert a == (1, False) and b == (1, True)
        assert len(list(s.scalars(select(Tx)))) == 1


def test_transfer_does_not_inflate_income_or_expenses(store):
    with store.transaction() as s:
        a, b = manual(s, 'out', 1000000, 'Awash', 1200), manual(s, 'in', 1000000)
        pair(s, a, b)
        result = summary(s, STAMP[:10])
        assert 'spending: 12.00' in result and 'Income: 0.00' in result
        pair(s, a, b)
        assert a.pair_id == b.id


def test_borrow_spend_repay(store):
    with store.transaction() as s:
        t = manual(s, 'in', 100000)
        classify(s, t, 'borrow')
        handle_text(s, 'Alex')
        d = s.get(Debt, t.debt_id)
        assert debt_balance(s, d) == 100000
        purchase = manual(s, 'out', 30000)
        classify(s, purchase, 'food')
        repay = manual(s, 'out', 22000, fee=200)
        handle_callback(s, f'debt:{repay.id}:{d.id}')
        handle_text(s, '200')
        assert debt_balance(s, d) == 80000
        assert repay.interest == 2000
        result = summary(s, STAMP[:10])
        assert 'spending: 322.00' in result
        assert 'Income: 0.00' in result
        assert 'principal repaid: 200.00' in result
        with pytest.raises(ValueError):
            classify(s, t, 'salary')  # Would orphan a repayment.


def test_lending_returned_and_partial_payment(store):
    with store.transaction() as s:
        t = manual(s, 'out', 50000)
        classify(s, t, 'lend')
        handle_text(s, 'Jamie')
        d = s.get(Debt, t.debt_id)
        incoming = manual(s, 'in', 20000)
        handle_callback(s, f'debt:{incoming.id}:{d.id}')
        handle_text(s, '200')
        assert debt_balance(s, d) == 30000
        assert 'Income: 0.00' in summary(s, STAMP[:10])
        assert 'spending: 0.00' in summary(s, STAMP[:10])


def test_repayment_overflow_rejected(store):
    with store.transaction() as s:
        handle_text(s, '/opening owe 100 Alex')
        d = s.scalar(select(Debt))
        t = manual(s, 'out', 20000)
        handle_callback(s, f'debt:{t.id}:{d.id}')
        with pytest.raises(ValueError):
            handle_text(s, '200')
        assert debt_balance(s, d) == 10000
        assert t.category == 'pending'


def test_unauthorized_requests(app, store):
    c = app.test_client()
    assert c.post('/sms', json={'text': 'hello'}).status_code == 401
    assert c.post('/telegram', json={'update_id': 1}).status_code == 401
    response = c.post('/telegram', json={'update_id': 2, 'message': {'from': {'id': 999}, 'chat': {'id': 999, 'type': 'private'}, 'text': '/manual Cash out 100'}}, headers={'X-Telegram-Bot-Api-Secret-Token': webhook_secret('w'*32)})
    assert response.status_code == 200
    with store.transaction() as s:
        assert list(s.scalars(select(Tx))) == []


def test_sms_endpoint_and_otp_filter(app, store):
    c = app.test_client()
    headers = {'Authorization': 'Bearer ' + 's'*32}
    assert c.post('/sms', json={'account': 'Telebirr', 'text': 'Your OTP is 111111'}, headers=headers).json['ignored']
    assert c.post('/sms', json={'account': 'Telebirr', 'text': 'You have received ETB 100.00'}, headers=headers).status_code == 200
    with store.transaction() as s:
        assert len(list(s.scalars(select(Event)))) == 1
        assert len(list(s.scalars(select(Tx)))) == 1


def test_telegram_retries_do_not_duplicate_manual_entries(app, store):
    c = app.test_client()
    u = {'update_id': 22, 'message': {'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}, 'text': '/manual Cash out 100 lunch'}}
    for _ in range(2):
        assert c.post('/telegram', json=u, headers={'X-Telegram-Bot-Api-Secret-Token': webhook_secret('w'*32)}).status_code == 200
    with store.transaction() as s:
        assert len(list(s.scalars(select(Tx)))) == 1


def test_malformed_command_does_not_break_webhook(app):
    u = {'update_id': 33, 'message': {'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}, 'text': '/due nope yesterday'}}
    assert app.test_client().post('/telegram', json=u, headers={'X-Telegram-Bot-Api-Secret-Token': webhook_secret('w'*32)}).status_code == 200


def test_digest_is_idempotent(store):
    with store.transaction() as s:
        stamp = datetime.fromisoformat('2026-11-10T08:01:00+03:00')
        schedule_digest(s, stamp, 8)
        schedule_digest(s, stamp, 8)
        assert len(list(s.scalars(select(Outbox)))) == 1


def test_delivery_failure_keeps_ledger_and_outbox(store, monkeypatch):
    with store.transaction() as s:
        ingest(s, 'You have received ETB 100.00', 'Telebirr', STAMP)
    monkeypatch.setattr('app.telegram', lambda *a: False)
    drain(store, 'unused', 123)
    with store.transaction() as s:
        assert len(list(s.scalars(select(Tx)))) == 1
        item = s.scalar(select(Outbox))
        assert not item.sent and item.attempts == 1

@pytest.mark.parametrize('bad', ['NaN', '-1', '0.001', 'Infinity', 'abc'])
def test_bad_amounts_rejected(bad):
    with pytest.raises(ValueError):
        cents(bad)


def test_balance_gap_warns_without_inventing_expense(store):
    with store.transaction() as s:
        ingest(s, 'You have received ETB 100.00. Your balance is ETB 500.00.', 'CBE', STAMP)
        ingest(s, 'A debit transaction of ETB 100.00. Your balance is ETB 350.00.', 'CBE', STAMP)
        texts = [o.payload.get('text', '') for o in s.scalars(select(Outbox))]
        assert any('differs by 50.00' in text for text in texts)
        assert len(list(s.scalars(select(Tx)))) == 2


def test_returned_interest_is_income(store):
    with store.transaction() as s:
        handle_text(s, '/opening owed 100 Jamie')
        t = manual(s, 'in', 11000)
        d = s.scalar(select(Debt))
        handle_callback(s, f'debt:{t.id}:{d.id}')
        handle_text(s, '100')
        assert 'Income: 10.00' in summary(s, STAMP[:10])
        assert debt_balance(s, d) == 0


def test_reversal_is_review_only():
    assert parse_sms('You have received ETB 100.00. Transaction reversed.', 'Telebirr', STAMP)['kind'] == 'review'


def test_export_has_no_secrets(store):
    from app import snapshot
    with store.transaction() as s:
        ingest(s, 'You have received ETB 100.00', 'Telebirr', STAMP)
        data = snapshot(s)
        assert len(data['transactions']) == 1
        assert 'finance_config' not in data and 'outbox' not in data


def test_correct_amount_and_fee(store):
    with store.transaction() as s:
        t = manual(s, 'in', 9000)
        handle_text(s, f'/amount {t.id} 100 10')
        classify(s, t, 'borrow')
        handle_text(s, 'Example Lender')
        assert debt_balance(s, s.get(Debt, t.debt_id)) == 10000
        assert 'spending: 10.00' in summary(s, STAMP[:10])


def test_no_public_finance_routes(app):
    c = app.test_client()
    assert c.get('/health').json == {'ok': True}
    assert c.get('/export').status_code == 404
    assert c.get('/sms').status_code == 405


def test_owner_id_in_group_is_not_enough(app, store):
    msg = {'update_id': 40, 'message': {'from': {'id': 123}, 'chat': {'id': -123, 'type': 'group'}, 'text': '/manual Cash out 100'}}
    app.test_client().post('/telegram', json=msg, headers={'X-Telegram-Bot-Api-Secret-Token': webhook_secret('w'*32)})
    with store.transaction() as s:
        assert list(s.scalars(select(Tx))) == []
