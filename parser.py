"""Conservative parsers. Amounts are integer cents; unrecognized SMS stay review-only."""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Africa/Addis_Ababa')
MONEY = r'([0-9][0-9,]*(?:\.[0-9]{1,2})?)'
ACCOUNTS = ('Telebirr', 'CBE', 'Awash', 'Cash')


def cents(value):
    try:
        d = Decimal(str(value).replace(',', '').strip())
        if not d.is_finite() or d < 0 or d > Decimal('1000000000') or d.as_tuple().exponent < -2:
            raise ValueError('Use a positive amount with at most two decimal places.')
        return int(d * 100)
    except (InvalidOperation, TypeError):
        raise ValueError('Invalid amount.') from None


def money(value):
    return f'{Decimal(value) / 100:,.2f}'


def amount(text, pattern):
    m = re.search(pattern, text, re.I)
    return cents(m.group(1)) if m else None


def parse_sms(text, account, captured_at):
    s = ' '.join(text.split())
    result = dict(account=account, kind='review', direction=None, amount=None,
                  fee=0, balance=None, reference=None, occurred_at=captured_at,
                  time_source='capture', note='', suggested=None)
    # Account is supplied by the sender-specific iPhone automation, not guessed from brand mentions.
    if account not in ACCOUNTS[:3]:
        raise ValueError('Account must be Telebirr, CBE or Awash.')
    bal = amount(s, rf'(?:balance\s+is|balance)\s*(?:ETB|Birr)\s*{MONEY}')
    result['balance'] = bal
    dt = re.search(r'\b(\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', s)
    if dt:
        try:
            fmt = '%d/%m/%Y %H:%M:%S' if '/' in dt.group(1) else '%Y-%m-%d %H:%M:%S'
            result.update(occurred_at=datetime.strptime(dt.group(1), fmt).replace(tzinfo=TZ).isoformat(), time_source='bank')
        except ValueError:
            result['note'] = 'Bank timestamp could not be read. '
    ref = re.search(r'(?:transaction (?:number is|ID\s*:))\s*([A-Za-z0-9-]+)', s, re.I)
    if ref:
        result['reference'] = ref.group(1)
    else:
        # Receipt links are identifiers only. Never fetch SMS links.
        link = re.search(r'https://(?:mbreceipt\.cbe\.com\.et|awashpay\.awashbank\.com(?::\d+)?|transactioninfo\.ethiotelecom\.et)/([^\s]+)', s, re.I)
        if link:
            result['reference'] = link.group(1).rstrip('.,')
    if re.search(r'\breversed\b|\breversal\b|\bfailed\b|\bcancelled\b', s, re.I):
        result['note'] = 'Reversal or failed transaction needs review. No automatic ledger change.'
        return result
    # Notices may overlap with credits/debits. Never post these automatically.
    if re.search(r'loan.*disbursed|credit request.*successful|loan amount|facilitation fee|repayment|loan.*due|interest.*per day', s, re.I):
        result['kind'] = 'loan_notice'
        result['note'] += 'Loan notice: link to a recorded credit/debit or record a missing movement once. Fees and interest need confirmation.'
        return result
    patterns_in = [rf'(?:received|credited with)\s*(?:ETB|Birr)\s*{MONEY}']
    patterns_out = [rf'(?:transferred|paid)\s*(?:ETB|Birr)\s*{MONEY}',
                    rf'debit transaction of\s*(?:ETB|Birr)\s*{MONEY}',
                    rf'Telebirr Transfer of\s*{MONEY}\s*ETB']
    for direction, patterns in [('in', patterns_in), ('out', patterns_out)]:
        for pattern in patterns:
            a = amount(s, pattern)
            if a is not None and a > 0:
                result.update(kind='transaction', direction=direction, amount=a)
                break
        if result['kind'] == 'transaction':
            break
    if result['kind'] != 'transaction':
        result['note'] += 'Unrecognized message. No money recorded.'
        return result
    if result['direction'] == 'out':
        fee = amount(s, rf'(?:service fee is|Service charge of|Charge)\s*(?:ETB\s*)?{MONEY}') or 0
        vat = amount(s, rf'(?:VAT\s*\([^)]*\)\s*of|VAT on the service fee is|VAT\s*:)\s*(?:ETB\s*)?{MONEY}') or 0
        levy = amount(s, rf'(?:Disaster Recovery\s*\([^)]*\)\s*of|EDRRF)\s*(?:ETB\s*)?{MONEY}') or 0
        result['fee'] = fee + vat + levy
        total = amount(s, rf'total of\s*ETB\s*{MONEY}')
        if total is not None and total != result['amount'] + result['fee']:
            result.update(kind='review', note='Debit total does not match parsed amount and charges. No money recorded.')
        if account == 'Awash' and re.search('Telebirr Transfer', s, re.I):
            result['suggested'] = 'transfer'
    if re.search('includes a bonus', s, re.I):
        result['note'] += 'The credited amount already includes the bonus. '
    if result['time_source'] == 'capture':
        result['note'] += 'Date uses capture time; correct it if this is an older SMS.'
    return result
