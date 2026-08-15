# Inventory Autopilot operations runbook

Use this runbook for an alert from the external Inventory Autopilot monitor or
for a red **Needs you** item in JiJi Stock Autopilot. Work on one affected
warehouse at a time. Checkout, payment, printing, shifts, and queued tablet
sales stay available while inventory automation is contained.

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
