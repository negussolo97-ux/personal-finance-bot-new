# Live acceptance checklist

Local automated tests cannot verify your iPhone's lock-screen behavior, Render networking, Telegram delivery, or bank SMS wording changes. Complete these once deployed, then mark fake transactions void.

- [ ] `/start` responds only in the owner's private chat. A second Telegram account gets no finance data.
- [ ] A synthetic `You have received ETB 10.00. Your transaction number is DEMO-001.` sent through the service Shortcut creates one pending Telebirr transaction.
- [ ] Replaying the same SMS creates no extra transaction.
- [ ] Choose Salary; `/today` includes 10 birr income. Change it to Loan, enter a fictional lender; income becomes zero and `/debts` shows 10 birr owed.
- [ ] Create a 10 birr outgoing transaction in Awash and a 10 birr incoming transaction in Telebirr, confirm them as a transfer, and verify income/spending exclude them. Use separate synthetic references.
- [ ] Confirm a fee-bearing synthetic payment counts principal in the selected expense category and fees exactly once.
- [ ] Create a loan with 100 birr principal, a 25 birr repayment containing 20 birr principal, and verify 80 birr debt remains and 5 birr borrowing cost is shown.
- [ ] A loan announcement does not create an extra deposit. It appears in `/review`; link or dismiss it.
- [ ] While the iPhone is locked, a new matching test SMS reaches the service and triggers a category prompt.
- [ ] A disconnected-phone test exposes the expected failure; replaying the SMS after reconnecting succeeds exactly once.
- [ ] Restart the Render service; transactions and debts are still present.
- [ ] `/export` downloads valid JSON containing the synthetic transactions and audit records. Save it securely, outside the server.
- [ ] Confirm Render PostgreSQL backup/recovery settings and review a restore procedure before relying on the tracker.
- [ ] The 08:00 summary arrives for the previous completed day in Addis Ababa time, including due reminders where applicable.
- [ ] Remove test data from totals: void repayments before original borrowed/lent transactions, then set any test opening debt to zero with `/openingfix`.
- [ ] Enter actual opening debts once, set due dates, enable real bank sender filters, and compare the first day's records with the banks.
