import hashlib
import uuid
from datetime import datetime, timedelta
from sqlalchemy import select
from parser import parse_sms, cents, money, TZ, ACCOUNTS
from storage import Event, Tx, Debt, Outbox, Audit, Lock

IN = {'salary': 'Salary', 'borrow': 'Loan / borrowed', 'returned': 'Paid me back', 'transfer': 'My own transfer', 'other_income': 'Other income', 'refund': 'Refund'}
OUT = {'food': 'Food', 'transport': 'Transport', 'shopping': 'Shopping', 'bills': 'Bills', 'family': 'Family / friends', 'health': 'Health', 'other': 'Other expense', 'lend': 'Lending to someone', 'repay': 'Repaying a loan', 'transfer': 'My own transfer', 'cash': 'Cash withdrawal'}
EXPENSES = {'food', 'transport', 'shopping', 'bills', 'family', 'health', 'other'}


def now():
    return datetime.now(TZ).isoformat()


def audit(s, action, detail):
    s.add(Audit(created_at=now(), action=action, detail=detail))


def queue(s, text, keyboard=None, key=None):
    key = key or str(uuid.uuid4())
    if s.scalar(select(Outbox).where(Outbox.key == key)):
        return
    payload = {'text': text[:4000]}
    if keyboard:
        payload['reply_markup'] = {'inline_keyboard': keyboard}
    s.add(Outbox(key=key, payload=payload))


def buttons(items):
    b = [{'text': label, 'callback_data': data} for label, data in items]
    return [b[i:i+2] for i in range(0, len(b), 2)]


def tx_card(s, t, key=None):
    cats = IN if t.direction == 'in' else OUT
    text = (f'#{t.id} · {t.account} · {money(t.amount)} birr '
            f'{"received" if t.direction == "in" else "sent"}\n'
            f'Fees: {money(t.fee)} birr\nDate: {t.occurred_at[:16]}\n'
            f'Category: {t.category}\n{t.note}\nWhat was this for?')
    queue(s, text, buttons([(label, f'cat:{t.id}:{c}') for c, label in cats.items()]), key)


def ingest(s, text, account, captured_at):
    p = parse_sms(text, account, captured_at)
    identity = p['reference'] or ' '.join(text.split())
    key = hashlib.sha256((account + ':' + identity).encode()).hexdigest()
    existing = s.scalar(select(Event).where(Event.key == key))
    if existing:
        # If same reference arrives with different wording, do not silently overwrite.
        return existing.id, True
    e = Event(key=key, raw=text, data=p, status='review')
    s.add(e)
    s.flush()
    if p['kind'] == 'transaction':
        t = Tx(event_id=e.id, account=account, direction=p['direction'], amount=p['amount'], fee=p['fee'], occurred_at=p['occurred_at'], note=p['note'])
        s.add(t)
        s.flush()
        e.status = 'recorded'
        if p['balance'] is not None:
            prior = [old for old in s.scalars(select(Event).where(Event.id < e.id)) if old.data['account'] == account and old.data.get('balance') is not None]
            if prior:
                old = prior[-1]
                newer = datetime.fromisoformat(p['occurred_at']) >= datetime.fromisoformat(old.data['occurred_at'])
                expected = old.data['balance'] + (p['amount'] if p['direction'] == 'in' else -p['amount']) - p['fee']
                if newer and expected != p['balance']:
                    queue(s, f'{account}: balance differs by {money(abs(expected-p["balance"]))} birr from the previous SMS plus this movement. There may be missing activity or a charge to review. No balancing expense was created.')
        tx_card(s, t, f'event:{e.id}')
        if p['suggested']:
            queue(s, f'#{t.id}: This may be a transfer to your Telebirr. Choose “My own transfer” if so.')
        # Same amount alone is never sufficient to auto-classify a transfer.
        candidates = list(s.scalars(select(Tx).where(Tx.id != t.id, Tx.account != t.account, Tx.amount == t.amount, Tx.direction != t.direction, Tx.pair_id.is_(None), Tx.category.in_(['pending', 'transfer']))))
        for other in candidates[-10:]:
            if abs((datetime.fromisoformat(t.occurred_at) - datetime.fromisoformat(other.occurred_at)).total_seconds()) <= 86400:
                queue(s, f'Possible own-account transfer: #{other.id} ↔ #{t.id}, {money(t.amount)} birr. Confirm only if these are the same transfer.', buttons([('Confirm transfer', f'pair:{other.id}:{t.id}')]))
    else:
        queue(s, f'SMS review #{e.id} · {account}\n{p["note"]}\nUse /review {e.id} to inspect. This notice has NOT changed your totals.', key=f'event:{e.id}')
    audit(s, 'sms_received', {'event_id': e.id, 'kind': p['kind']})
    return e.id, False


def debt_balance(s, d):
    total = d.opening
    for t in s.scalars(select(Tx).where(Tx.debt_id == d.id)):
        if t.category in ('borrow', 'lend'):
            total += t.principal
        elif t.category in ('repay', 'returned'):
            total -= t.principal
    return total


def get_tx(s, ident):
    try:
        t = s.get(Tx, int(ident))
    except ValueError:
        t = None
    if not t or t.category == 'void':
        raise ValueError('Transaction not found.')
    return t


def reset(s, t):
    if t.debt_id:
        d = s.get(Debt, t.debt_id)
        if t.category in ('borrow', 'lend') and debt_balance(s, d) < t.principal:
            raise ValueError('Undo or correct linked repayments before changing this loan.')
    if t.pair_id:
        other = s.get(Tx, t.pair_id)
        other.pair_id = None
        other.category = 'pending'
    t.pair_id = None
    t.category = 'pending'
    t.debt_id = None
    t.principal = 0
    t.interest = 0
    t.person = ''


def classify(s, t, category):
    allowed = IN if t.direction == 'in' else OUT
    if category not in allowed:
        raise ValueError('That category is not valid for this transaction.')
    reset(s, t)
    if category in ('borrow', 'lend'):
        s.get(Lock, 1).state = {'flow': 'person', 'tx': t.id, 'category': category}
        queue(s, f'#{t.id}: Who {"lent you" if category == "borrow" else "did you lend"} this money? Reply with a name, Telebirr, or Awash. /cancel to stop.')
        return
    if category in ('repay', 'returned'):
        side = 'owe' if category == 'repay' else 'owed'
        ds = [d for d in s.scalars(select(Debt).where(Debt.side == side)) if debt_balance(s, d) > 0]
        if not ds:
            queue(s, 'No matching debt exists yet. Add an opening balance with /opening owe|owed AMOUNT NAME, then categorize this transaction again. Example: /opening owe 500 Alex')
            return
        queue(s, 'Which debt is this repayment for?', buttons([(f'#{d.id} {d.person}: {money(debt_balance(s, d))}', f'debt:{t.id}:{d.id}') for d in ds[:30]]))
        return
    t.category = category
    audit(s, 'category', {'tx': t.id, 'category': category})
    queue(s, f'#{t.id} saved as {allowed[category]}. /edit {t.id} to change it.')


def pair(s, a, b):
    if a.id == b.id or a.direction == b.direction or a.account == b.account or a.amount != b.amount:
        raise ValueError('Transfers require two different accounts, opposite directions, and equal amounts.')
    if a.pair_id == b.id and b.pair_id == a.id:
        queue(s, 'These transactions are already linked.')
        return
    if a.category not in ('pending', 'transfer') or b.category not in ('pending', 'transfer') or a.pair_id or b.pair_id:
        raise ValueError('Edit these transactions back to pending before linking them.')
    a.category = b.category = 'transfer'
    a.pair_id, b.pair_id = b.id, a.id
    audit(s, 'transfer_linked', {'a': a.id, 'b': b.id})
    queue(s, f'Linked #{a.id} ↔ #{b.id}. Only fees count as expenses.')


def summary(s, day):
    rows = [t for t in s.scalars(select(Tx).where(Tx.category != 'void')) if datetime.fromisoformat(t.occurred_at).astimezone(TZ).date().isoformat() == day]
    totals = {}
    for t in rows:
        if t.category in EXPENSES:
            totals[t.category] = totals.get(t.category, 0) + t.amount
    fees = sum(t.fee for t in rows)
    interest = sum(t.interest for t in rows if t.category == 'repay')
    income = sum(t.amount for t in rows if t.category in ('salary', 'other_income')) + sum(t.interest for t in rows if t.category == 'returned')
    repaid = sum(t.principal for t in rows if t.category == 'repay')
    pending = [t for t in rows if t.category == 'pending']
    lines = [f'Daily summary · {day} · Addis Ababa', f'Categorized spending: {money(sum(totals.values()) + fees + interest)} birr']
    lines += [f'• {OUT[k]}: {money(v)}' for k, v in sorted(totals.items())]
    lines += [f'Transaction fees: {money(fees)}', f'Loan interest/charges paid: {money(interest)}', f'Income: {money(income)}', f'Loan principal repaid: {money(repaid)}', f'Refunds: {money(sum(t.amount for t in rows if t.category == "refund"))}', f'Uncategorized: {len(pending)} — use /pending', 'Transfers, borrowing and returned principal are excluded from income/spending. Cash withdrawals are not purchases.']
    return '\n'.join(lines)


HELP = '''Finance tracker
/today — today's spending
/summary YYYY-MM-DD — another day
/accounts — last SMS balances (not live bank balances)
/pending — categorize transactions
/debts — what I owe / owed to me
/review — unprocessed loan/unknown SMS
/edit ID — change a category
/pair OUT_ID IN_ID — confirm an own-account transfer
/manual ACCOUNT in|out AMOUNT [NOTE] — cash or missing transaction
/opening owe|owed AMOUNT NAME — existing debt, no cash movement
/due DEBT_ID YYYY-MM-DD — record a due date
/date TX_ID YYYY-MM-DD — correct a transaction date
/amount TX_ID AMOUNT FEE — correct amounts; asks for category again
/openingfix DEBT_ID AMOUNT — correct an opening debt
/void TX_ID — remove an incorrect entry from totals
/export — download your data as JSON
/cancel — cancel the current question
Accounts: Telebirr, CBE, Awash, Cash.
Repayments are split between principal and costs. No automatic interest estimates.'''


def handle_callback(s, data):
    bits = data.split(':')
    if len(bits) != 3:
        raise ValueError('Unknown button. Use /pending.')
    action, ident, choice = bits
    t = get_tx(s, ident)
    if action == 'cat':
        classify(s, t, choice)
    elif action == 'pair':
        pair(s, t, get_tx(s, choice))
    elif action == 'debt':
        d = s.get(Debt, int(choice))
        if not d or d.side != ('owe' if t.direction == 'out' else 'owed'):
            raise ValueError('Debt does not match the transaction direction.')
        s.get(Lock, 1).state = {'flow': 'repayment', 'tx': t.id, 'debt': d.id}
        queue(s, f'#{t.id}: How much of the {money(t.amount)} birr was PRINCIPAL? Reply with a number. The rest will be recorded as {"borrowing costs" if t.direction == "out" else "interest income"}. Remaining principal: {money(debt_balance(s, d))}. /cancel to stop.')
    else:
        raise ValueError('Unknown button.')


def handle_text(s, text):
    text = text.strip()
    state = s.get(Lock, 1)
    if text == '/cancel':
        state.state = {}
        queue(s, 'Cancelled. Unfinished transactions remain pending. /pending to continue.')
        return
    if not text.startswith('/') and state.state:
        flow = state.state
        t = get_tx(s, str(flow['tx']))
        if flow['flow'] == 'person':
            if not 1 <= len(text) <= 80:
                raise ValueError('Use a name between 1 and 80 characters.')
            reset(s, t)
            d = Debt(side='owe' if flow['category'] == 'borrow' else 'owed', person=text, opening=0)
            s.add(d)
            s.flush()
            t.category = flow['category']
            t.debt_id, t.principal, t.person = d.id, t.amount, text
            queue(s, f'Saved #{t.id}. Debt #{d.id}: {text}, {money(t.amount)} birr. Optional: /due {d.id} YYYY-MM-DD')
        elif flow['flow'] == 'repayment':
            principal = cents(text)
            d = s.get(Debt, flow['debt'])
            # Reset only after validation; include existing repayment when editing the same row.
            available = debt_balance(s, d) + (t.principal if t.debt_id == d.id and t.category in ('repay', 'returned') else 0)
            if principal > t.amount or principal > available:
                raise ValueError('Principal exceeds this payment or the outstanding debt. Enter a smaller amount.')
            reset(s, t)
            t.category = 'repay' if t.direction == 'out' else 'returned'
            t.debt_id, t.principal, t.interest, t.person = d.id, principal, t.amount-principal, d.person
            queue(s, f'Saved #{t.id}. Principal: {money(principal)}; costs/interest: {money(t.interest)}. Debt remaining: {money(debt_balance(s, d))}.')
        state.state = {}
        audit(s, 'debt_transaction', {'tx': t.id, 'debt': t.debt_id, 'category': t.category})
        return
    parts = text.split()
    cmd = parts[0].split('@')[0] if parts else ''
    if cmd in ('/start', '/help'):
        queue(s, HELP)
    elif cmd in ('/today', '/summary'):
        day = datetime.now(TZ).date().isoformat() if cmd == '/today' or len(parts) == 1 else datetime.strptime(parts[1], '%Y-%m-%d').date().isoformat()
        queue(s, summary(s, day))
    elif cmd == '/pending':
        rows = list(s.scalars(select(Tx).where(Tx.category == 'pending').order_by(Tx.id).limit(10)))
        if not rows:
            queue(s, 'No pending transactions.')
        for t in rows:
            tx_card(s, t)
    elif cmd == '/edit' and len(parts) == 2:
        t = get_tx(s, parts[1])
        tx_card(s, t)
    elif cmd == '/pair' and len(parts) == 3:
        pair(s, get_tx(s, parts[1]), get_tx(s, parts[2]))
    elif cmd == '/void' and len(parts) == 2:
        t = get_tx(s, parts[1])
        reset(s, t)
        t.category = 'void'
        audit(s, 'void', {'tx': t.id})
        queue(s, f'#{t.id} excluded from totals. Original SMS remains in the export.')
    elif cmd == '/amount' and len(parts) == 4:
        t = get_tx(s, parts[1])
        value, fee = cents(parts[2]), cents(parts[3])
        if value <= 0:
            raise ValueError('Amount must be positive.')
        reset(s, t)
        t.amount, t.fee = value, fee
        audit(s, 'amount_corrected', {'tx': t.id, 'amount': value, 'fee': fee})
        tx_card(s, t)
    elif cmd == '/openingfix' and len(parts) == 3:
        d = s.get(Debt, int(parts[1]))
        if not d:
            raise ValueError('Debt not found.')
        value = cents(parts[2])
        if debt_balance(s, d) - d.opening + value < 0:
            raise ValueError('Opening balance would make this debt negative. Correct repayments first.')
        d.opening = value
        audit(s, 'opening_corrected', {'debt': d.id, 'amount': value})
        queue(s, 'Opening balance corrected.')
    elif cmd == '/date' and len(parts) == 3:
        t = get_tx(s, parts[1])
        day = datetime.strptime(parts[2], '%Y-%m-%d').date()
        dt = datetime.fromisoformat(t.occurred_at).astimezone(TZ)
        t.occurred_at = dt.replace(year=day.year, month=day.month, day=day.day).isoformat()
        audit(s, 'date_changed', {'tx': t.id, 'date': parts[2]})
        queue(s, f'#{t.id} date updated.')
    elif cmd == '/manual' and len(parts) >= 4:
        account = next((a for a in ACCOUNTS if a.lower() == parts[1].lower()), None)
        if not account or parts[2] not in ('in', 'out') or cents(parts[3]) <= 0:
            raise ValueError('Use /manual Telebirr|CBE|Awash|Cash in|out AMOUNT [NOTE].')
        t = Tx(account=account, direction=parts[2], amount=cents(parts[3]), occurred_at=now(), note=' '.join(parts[4:])[:500])
        s.add(t)
        s.flush()
        audit(s, 'manual', {'tx': t.id})
        tx_card(s, t)
    elif cmd == '/opening' and len(parts) >= 4:
        if parts[1] not in ('owe', 'owed') or cents(parts[2]) <= 0:
            raise ValueError('Use /opening owe|owed AMOUNT NAME.')
        d = Debt(side=parts[1], person=' '.join(parts[3:])[:80], opening=cents(parts[2]))
        s.add(d)
        s.flush()
        audit(s, 'opening_debt', {'debt': d.id, 'amount': d.opening})
        queue(s, f'Opening debt #{d.id} saved. No income or spending added. /due {d.id} YYYY-MM-DD to set a due date.')
    elif cmd == '/due' and len(parts) == 3:
        d = s.get(Debt, int(parts[1]))
        if not d:
            raise ValueError('Debt not found.')
        d.due = datetime.strptime(parts[2], '%Y-%m-%d').date().isoformat()
        audit(s, 'due_date', {'debt': d.id, 'due': d.due})
        queue(s, 'Due date saved. It will appear in your daily summary reminders.')
    elif cmd == '/debts':
        lines = ['Debts — recorded principal only; unconfirmed fees/interest are not estimated.']
        totals = {'owe': 0, 'owed': 0}
        for d in s.scalars(select(Debt).order_by(Debt.id)):
            bal = debt_balance(s, d)
            totals[d.side] += bal
            if bal:
                lines.append(f'#{d.id} · {"I owe" if d.side == "owe" else "Owed to me"} · {d.person}: {money(bal)} · due {d.due or "not set"}')
        lines.append(f'Total I owe: {money(totals["owe"])}; owed to me: {money(totals["owed"])}')
        for i in range(0, len(lines), 25):
            queue(s, '\n'.join(lines[i:i+25]))
    elif cmd == '/accounts':
        lines = ['Last reported SMS balances — these are not live bank balances.']
        for account in ACCOUNTS[:3]:
            events = [e for e in s.scalars(select(Event)) if e.data['account'] == account and e.data.get('balance') is not None and e.status != 'dismissed']
            if events:
                e = max(events, key=lambda e: (datetime.fromisoformat(e.data['occurred_at']), e.id))
                lines.append(f'{account}: {money(e.data["balance"])} birr · {e.data["occurred_at"][:16]} ({e.data["time_source"]} time)')
            else:
                lines.append(f'{account}: no balance received yet')
        queue(s, '\n'.join(lines))
    elif cmd == '/review':
        if len(parts) == 2:
            e = s.get(Event, int(parts[1]))
            if not e:
                raise ValueError('SMS not found.')
            queue(s, f'SMS #{e.id} · {e.status}\n{e.raw[:2700]}\n\nIf already represented by a transaction: /resolve {e.id} TX_ID\nIf informational only: /dismiss {e.id}\nIf money is missing: /manual ACCOUNT in|out AMOUNT, then resolve to that transaction. Do not record a credit twice.')
        else:
            es = list(s.scalars(select(Event).where(Event.status == 'review').limit(20)))
            queue(s, '\n'.join([f'#{e.id} · {e.data["account"]} · {e.data["kind"]} — /review {e.id}' for e in es]) or 'No SMS awaiting review.')
    elif cmd == '/resolve' and len(parts) == 3:
        e = s.get(Event, int(parts[1]))
        t = get_tx(s, parts[2])
        if not e or e.status != 'review':
            raise ValueError('Only review notices can be linked.')
        e.status = 'linked'
        e.data = {**e.data, 'linked_tx': t.id}
        audit(s, 'notice_link', {'event': e.id, 'tx': t.id})
        queue(s, 'Notice linked. No extra money recorded. Confirm loan terms with your lender; recorded debt principal stays unchanged.')
    elif cmd == '/dismiss' and len(parts) == 2:
        e = s.get(Event, int(parts[1]))
        if not e or e.status != 'review':
            raise ValueError('Only review notices can be dismissed.')
        e.status = 'dismissed'
        audit(s, 'notice_dismissed', {'event': e.id})
        queue(s, 'Notice dismissed; kept in export.')
    elif cmd == '/export':
        queue(s, 'Preparing your private JSON export. It contains SMS text and financial records; keep it private.')
        s.add(Outbox(key=str(uuid.uuid4()), method='export', payload={}))
    else:
        queue(s, 'Use /help for commands or /pending to categorize transactions. To add cash: /manual Cash out 100 lunch')
