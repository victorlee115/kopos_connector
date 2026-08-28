# Inventory Autopilot operations runbook

Use this runbook for an alert from the external Inventory Autopilot monitor or
for a red **Needs you** item in JiJi Stock Autopilot. Work on one affected
warehouse at a time. Checkout, payment, printing, shifts, and queued tablet
sales stay available while inventory automation is contained.

## Who does what

Start with the place shown below. Nobody needs to know the name of an ERP
record before beginning. Only Company Directors use the ERP stock page for
business decisions; System Managers may open it for support. Outlet staff and
managers use the tablet.

| Person | Start here | What to do | Finished when |
| --- | --- | --- | --- |
| Outlet staff | **Tablet → Stock tasks** | Count what is physically present. Record preparation, deliveries, transfers, and waste. Enter what was actually seen; never guess the expected amount. | The tablet says the task is saved or sent. A saved offline task stays on the tablet until it can be sent safely. |
| Outlet manager | **Tablet → Stock tasks** | Check unusual amounts and confirm the stock change. If the evidence is unclear, leave it for review instead of changing the staff answer. | The tablet confirms the update, or clearly says that a Company Director must decide. |
| Company Director | **ERP → JiJi Stock Overview → Your next step** | Choose the outlet and do the one action shown. Review large count differences, urgent problems, restock plans, and supplier order drafts. | **Your next step** says that nothing needs attention. |
| Buying | **ERP → JiJi Stock Overview → Who does each part? → Buying** | Check the restock plan, supplier quotation, pack size, price, and supplier order draft. The Company Director personally decides whether to send it. | The draft is deliberately left unplaced or the checked order is sent by the director. |
| Supplier | The order sent by the Company Director | Supply only the order that was personally checked and sent by the director. Suppliers do not use JiJi. | The outlet receives the delivery against the approved order. |
| Finance | Standard ERP stock and accounting reports | Check ingredient cost, stock movement, Stock Ledger, GL entries, and the submitted source document. Never edit submitted ledger rows. | The source document, Stock Ledger, GL, and ingredient cost agree. |
| Support | **ERP → JiJi Stock Overview** and the approved system checks | Restore the connection, scheduler, worker, or tablet update. Preserve queued work and evidence. Never delete a record to hide a warning. | A new successful check is visible and the original work remains traceable. |
| JiJi | Automatic; no person starts it | Retry safely, create standard stock records or reviewable drafts, and pause risky work for a person. It never sends or approves a supplier order. | The standard document is created once, or a plain-language next action is shown. |

One person may carry more than one business responsibility, such as Company
Director, buying, and finance. The handoff still happens in the order above.

## Normal stock workflow

1. Staff record the physical event on the tablet.
2. The outlet manager checks it on the tablet when confirmation is required.
3. JiJi saves the approved standard stock document once, or pauses and shows a
   clear reason.
4. The Company Director opens **JiJi Stock Overview**, chooses the outlet, and
   follows **Your next step** until nothing needs attention.
5. A supplier order remains a Draft until the Company Director checks and sends
   it. JiJi never contacts the supplier.
6. Finance checks accounting only after the stock document is submitted.
7. Support becomes involved only for a connection or system problem and
   restores the service without deleting business evidence.

## No-training acceptance

This check supplements the campaign's production acceptance gates; it does not
replace or relax them. Use the exact tablet and ERP release candidates. Give
each participant only the name of the result they must achieve. Do not show
them the workflow first, explain an ERP record name, or coach them through a
stuck step.

- First-time outlet staff must complete a count, delivery, and waste task,
  including saving once without internet and confirming it sends after the
  connection returns.
- First-time outlet managers must find work that needs checking, confirm a
  clear amount, and leave an unclear amount for the Company Director.
- First-time Company Directors must choose an outlet, follow **Your next
  step**, review a large count difference, and reach **Nothing needs your
  attention** without opening an unrelated ERP list.
- The person responsible for buying must find the restock plan and supplier
  order draft, check the important fields, and understand that JiJi has not
  sent the order.
- The person responsible for finance must open the stock movement report and
  trace an ingredient movement to its submitted source document without
  editing a ledger row.
- A support operator must identify a stopped worker or stale update, state the
  safe recovery action, and preserve queued work and evidence.
- A supplier who receives a sample order must be able to identify the outlet,
  item, pack amount, delivery date, and who approved it without access to JiJi.

Pass only when every participant finishes without coaching, can explain what
happened in plain language, and can recover from one ordinary mistake without
losing work. Record confusion, wrong turns, and misunderstood words as product
defects. Fix and repeat the affected check before advancing the release.

## First response

1. Open **JiJi Stock Autopilot → Needs you** and select the affected warehouse.
2. Read the plain-language reason, source document, first/last seen time, and
   recommended action. Do not close the exception before the underlying health
   check passes.
3. If the policy is not already paused and the alert is critical, set the
   affected warehouse to **Paused**. Do not pause unrelated outlets.
4. Preserve the exception, Projection Log, related ERP documents, device
   report, catalog/overlay identity, worker logs, and deployed artifact hash.
5. Notify the domain owner below and the Release Owner. Expansion to another
   outlet stops while any critical condition remains.

## Alert ownership and safe diagnosis

| Alert | Owner | Read-only checks | Safe corrective action |
| --- | --- | --- | --- |
| Projection delayed, dead letter, duplicate target, Redis/worker identity | Engineering | Health page; Projection Log state, lease owner/expiry, source order, target document and payload hash; scheduler/worker logs; running artifact identity | Repair the worker or mapping, then use the supported idempotent retry. Never create a replacement Stock Entry by hand to hide a failed projection. |
| Stale device or unacknowledged overlay | Support | Device config revision, effective report time, commercial and inventory queue counts, applied overlay version/hash, connectivity | Restore connectivity, keep the tablet's data, sync queued sales first, refresh the catalog/overlay, and verify a new monotonic device report. |
| Count, hold, receiving, transfer, preparation, or supplier exception | Outlet Operations | Assigned task and revision, immutable observation, Stock Ledger watermark, standard source document, batch/expiry and warehouse | Correct the physical task through its guided POS flow or the linked standard ERP document. Recount when stock moved; do not edit the immutable observation. |
| COGS/GL mismatch, valuation/account authority, unsafe Draft PO configuration | Finance | Submitted Stock Entry, Stock Ledger and GL rows; company/account/currency/dimension mapping; Material Request, Supplier Quotation and Draft PO provenance; outbound safety check | Correct the approved accounting or purchasing authority, reverse through a standard ERP document when required, and rerun the exact check. JiJi-created POs remain Draft. |
| Rollout freeze or mixed artifact identity | Release Owner | Accepted wheel/APK hashes, process identity for web/scheduler/short/long workers, device package identity, current campaign receipt | Keep the outlet paused, deploy only the accepted immutable artifacts, restart all required processes, and repeat the failed acceptance gate. |

## Critical conditions

Treat these as immediate rollout stops for the affected warehouse:

- missing or duplicate ingredient Stock Entry;
- projection older than 60 minutes or any inventory dead letter;
- COGS, Stock Ledger, or GL mismatch;
- a JiJi-created Purchase Order submitted, printed, emailed, sent, or placed;
- mixed ERP runtime identities;
- scheduler overdue by more than 90 minutes;
- unacknowledged current overlay for more than 30 minutes;
- lost count evidence or duplicate Stock Reconciliation;
- unsafe purchasing action, unexplained migration failure, or commercial
  checkout/payment/printing/shift regression.

An inventory projection older than 15 minutes is a warning. Device freshness
uses the warehouse policy limit, normally 30 minutes. Warnings require review
but do not authorize bypassing a hard automation gate.

## Forbidden actions

- Do not clear tablet application data, SQLite, sales queues, count drafts, or
  guided-task drafts.
- Do not delete or rewrite Projection Logs, resolved sales, observations,
  holds, exceptions, Stock Ledger, GL, batches, or migration evidence.
- Do not use direct SQL to change business state or reset an idempotency key.
- Do not re-enable selling by deleting a stale automation hold. Restore source
  truth and let the matching hold release through the supported path.
- Do not submit or send a Purchase Order to make a planning alert disappear.
- Do not weaken a freshness, count, forecast, shelf-life, permission, or value
  gate during an incident.
- Do not uninstall/reinstall the connector or clear a tablet to repair an
  upgrade. Use the supported in-place upgrade and migration path.

## Resume criteria

The domain owner records the corrective action and attaches the preserved
evidence. The Release Owner may resume the warehouse only when:

1. the root cause is identified;
2. the policy, source, device, and artifact hashes are known;
3. the failed health check and its focused behavioral test pass;
4. there is no duplicate or unexplained stock/accounting document;
5. commercial queues are clean and the current overlay is acknowledged;
6. every critical exception is resolved by source truth, not manual deletion;
7. a golden sale or the relevant guided task passes when the incident affected
   posting, counts, preparation, receiving, or transfers.

Keep automation in **Review First** after recovery until the responsible owner
and Release Owner both approve returning it to **Active**.
