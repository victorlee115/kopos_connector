# Offers: real browser walkthrough and corrections

Follow-up after the Mac was unlocked on 2026-09-09. Scope: the isolated
synthetic Colima site at `http://127.0.0.1:18308/app/jiji_promotions`, not the
home/production ERP. Browser checks are agent-operated interaction evidence,
not acceptance by uncoached café owners or physical tablets.

Current status: **the final F browser recheck is complete for the exercised
editing workflow**. Nine fresh native screenshots now supplement the 22 C/D
captures. Rename/save/reopen/copy, quantity-error recovery, overnight schedule
retention, desktop and narrow layouts, manual status refresh and stop-dialog
cancellation were checked. Live notifications have a separate fixture-origin
error; full keyboard, novice, physical-device and release acceptance remain open.

## Findings corrected

1. **Receipt stuck on Checking.** Initializing Frappe's sample-outlet control
   asynchronously fired its change handler, starting another successful preview
   request repeatedly. Candidate C visibly failed despite passing the old mocks.
   The editor now ignores hydration that did not change the selected outlet and
   rejects callbacks belonging to another preview/editor.
2. **Saved drafts appeared changed; keyboard focus was lost.** Frappe's delayed
   initial values marked clean forms dirty. Equal initial values now do nothing,
   and heading/error focus is applied after the busy form stops being inert.
   Leaving Review also invalidates any late preview response.
3. **Renaming broke subsequent editing.** Real Frappe keeps its autoname field
   tied to the document ID. Candidate D saved the new label but then rejected
   its own draft as advanced. Guided v2 names now use the existing display label;
   permanent IDs, legacy rules, published snapshots and historical names stay
   unchanged. Preview credits use the current draft name.
4. **Recovery and narrow-screen polish.** Missing name, invalid percentage and
   invalid amount have specific messages. Correcting sample quantity clears only
   its own warning, not an unrelated save error. Receipt amounts cannot split
   the minus sign from RM; sample quantity rows are vertically centered. Inputs,
   selects and panel buttons have at least 44px targets.

These changes extend the existing Page, APIs and tests. No new runtime dependency,
service, paid account, database field, queue or telemetry system was introduced
in this follow-up. Arithmetic and matching still use the shared open-source-backed
pricing artifact. Admission remains `development_not_checkout_admitted`.

## Walkthrough before the final rename correction

Candidate D was inspected at 1280×800 and 390×844. The fixture has one outlet
and one menu item: Strawberry Matcha Latte, RM12. This does not prove mixed-price
or multi-outlet usability. The actual sample receipts showed:

| Offer | Basket | Menu subtotal → sample total |
| --- | --- | --- |
| 10% off | 3 drinks | RM36 → RM32.40 |
| RM1 off each | 3 drinks | RM36 → RM33 |
| Special price RM5 | 3 drinks | RM36 → RM15 |
| Buy 1, get 1 free | 3 drinks | RM36 → RM24 |
| Second item half price | 3 drinks | RM36 → RM30 |
| Second item half price | Two rows, quantities 3 + 1 | RM48 → RM36 |

The percentage draft saved without activation. Entering zero quantity restored
the previous valid quantity and focused an explanation. Friday 22:00–02:00
clearly continued into Saturday; upcoming runs were September 11/18/25 into
12/19/26. At exactly Saturday September 12, 02:00, the sample correctly showed
no qualifying discount (RM48). Normal menu selection, changing templates,
navigation and review were exercised through visible controls.

The D rename failure is retained as evidence, not omitted because a later fix
passes. Screenshots are native browser captures, not generated mockups. Earlier
September 8 modal screenshots do not describe this full-page editor.
The raw capture files contain unused canvas margins; they are individual
step/viewport captures, not proof of complete stitched pages. Visual inspection
was supplemented by the real accessibility tree and navigation at each step.

## Evidence directories

- C, original failure: `../../.maintenance-colima.gRhh9W/promotion-ui-c-review.HaU2fP/`.
- D, working calculations and remaining rename failure:
  `../../.maintenance-colima.gRhh9W/promotion-ui-d-review.jP23cs/` (14 captures).
- Test logs: `/private/tmp/jiji-promotion-verification.y6zmtV/`.
- Lifecycle receipts: `../../.maintenance-colima.gRhh9W/`.

Selected intermediate D captures:
[percentage receipt](../../.maintenance-colima.gRhh9W/promotion-ui-d-review.jP23cs/02-percent-receipt.png),
[draft safety checks](../../.maintenance-colima.gRhh9W/promotion-ui-d-review.jP23cs/04-draft-safety-checks.png),
[buy-one-get-one odd basket](../../.maintenance-colima.gRhh9W/promotion-ui-d-review.jP23cs/07-bogo-odd-basket.png),
[Friday overnight schedule](../../.maintenance-colima.gRhh9W/promotion-ui-d-review.jP23cs/11-friday-overnight-narrow.png),
[exact-end exclusion](../../.maintenance-colima.gRhh9W/promotion-ui-d-review.jP23cs/13-exact-end-no-discount.png),
[rename failure before F](../../.maintenance-colima.gRhh9W/promotion-ui-d-review.jP23cs/14-renamed-draft-rejected.png).

## Final candidate verification

Final candidate F wheel SHA-256:
`10c034a26d72dc37d8198ed72ae155fcbbf9ea7d3793d35fa3beb4a88a67d2f1`.
Kernel SHA-256 (unchanged):
`7887a3ec03d1ab4fe0f348e421e039d4b55cbb0332ac9cc15e76649fe85c2983`.
F is installed on the isolated test site. Running image:
`sha256:647d2351aa193b2293349d96c61b2f51d08c5a484dbd735967bfa7f0075c163f`.
All six application containers restarted at 03:01:46–47 UTC; each independently
returned the exact F wheel, shared kernel, editor and both modified API hashes.
Both migrations and restart completed successfully. No individual source files
were copied into Bench. The [follow-up receipt](audit/promotion-ui-followup-2026-09-09.json)
records the exact hashes, source baseline, exits, screenshots and limitations.

The earlier F receipt records a genuine Mac-lock blocker during the build. After
the user unlocked the Mac again, the final F editor was loaded into a fresh tab
and rechecked. That earlier receipt remains historical, not rewritten as a pass.
The [final browser receipt](audit/promotion-ui-final-browser-2026-09-09.json)
binds the new screenshots and results to the unchanged F wheel/source. The
temporary viewport override was reset and the test page left available.

The complete F connector suite passed: 2,045 tests plus 117 subtests, exit 0;
18 editor tests passed, exit 0. Editor tests now model Frappe's asynchronous
initialization. Four new UI regression tests and the rename test failed against
the old behavior before the fixes. Source HEADs remain the recorded dirty-tree
baselines; no branches, commits, stashes or unrelated edits were changed.

| Final F check | Command / actual result |
| --- | --- |
| Complete connector suite | `.venv-test/bin/python -m pytest tests kopos_connector/tests -q`; exact F wheel and authoritative TypeScript root supplied; exit 0, 2,045 tests + 117 subtests, no skips. |
| Editor regression suite | `node --test tests/promotion-setup-ui.test.cjs`; exit 0, 18 tests. |
| Real Frappe/MariaDB/native runtime | `bench --site test.localhost run-tests --app kopos_connector --module kopos_connector.kopos.tests.test_promotion_snapshot_persistence_contract` inside the isolated backend; exit 0, 8 tests in 15.780s. |
| Authoritative image build | First F attempt exited 2 for disk space. Same wheel retry exited 0 at 03:00:31 UTC. |
| Migration twice and restart | Authoritative harness `up`; exit 0, 03:01:21–03:01:57 UTC. |
| Commercial smoke and state dump | Authoritative harness `smoke` and `dump`; both exit 0, completed 03:03:43 and 03:03:52 UTC. |
| Relevant static checks | Development methodology, agent guardrails, contract vocabulary and CI-workflow validators; each exit 0. Both worktree whitespace checks exit 0. |

The eighth real test renames and changes an already snapshotted guided draft
through all five templates, retries each save, reopens it, checks its home-card
name/editability, calculates a native preview, runs genuine readiness checks,
and makes an inactive copy. MariaDB IDs and every retained snapshot byte/count
stay unchanged. This is real API/persistence evidence, not browser interaction.

The final dump retains invoices `ACC-SINV-2026-00078`–`00084`: normal, zero-total,
modified, refund-candidate, cancelled and return cases. All have zero outstanding
balance and exact-sen balanced GL totals; expected payment identities are retained.
The return references `00082`. Duplicate order/invoice keys and failed projections
are empty. Active legacy POS Invoice/opening/closing counts are zero. Smoke's
deliberate +RM12/−RM12 shift variances remain visible. These invoices have no v2
promotion applied, so they do not establish v2 discount accounting acceptance.

No POS product source changed in this follow-up: all 14 source hashes bound to
the preceding complete POS package gates were rechecked unchanged. Those earlier
gates remain historical evidence, not newly rerun tests. No touched product file
was over 1,500 lines: final `promotions.py` 1,191, `promotion_setup.py` 543, Page JS
502. The earlier execution report retains its large-file before/after counts.

## Final browser recheck after the second unlock

No product source, dependencies, schema, server configuration or deployed bytes
changed in this resumed check. Both recorded HEADs and all bound ERP source/test
hashes were unchanged. All six running services again returned the exact F wheel,
kernel and editor hashes. The 18 editor regression tests were rerun and passed
(exit 0). The full package and real-service suites above belong to the same F
bytes; they were not unnecessarily relabelled as freshly rerun suites.

The existing draft reopened with its repaired D-era label. Through visible
controls it was renamed to **Friday night drinks — UI review F**, saved and
reopened from the list. Its half-price template, selected product/outlet and
Friday 22:00–Saturday 02:00 schedule were retained. The receipt used the new
label: two RM12 drinks → RM18; three → RM30. Quantity zero restored the previous
quantity and focused the explanation; correcting it to three removed that
warning and recalculated the receipt.

Make a copy supplied an automatic `(copy)` name and retained selections/schedule.
Changing the copy to 10% off produced RM12 → RM10.80. Saving created a separate
draft; both cards said **Draft only · not sent to tills**. Activation remained
disabled with the genuine rollout/legacy-offer checklist. No activation or stop
publication was performed. The standard stop confirmation named the offer and
outlet and explained reconnect delays; **No** was selected. Manual tablet-status
refresh completed and retained the honest waiting-to-sync state.

Native viewport captures (no generated images, cropping or image modification):

| Capture | Evidence |
| --- | --- |
| [01 recovered draft](../../.maintenance-colima.gRhh9W/promotion-ui-f-review.uz1mAh/01-recovered-renamed-draft.jpg) | Previously rejected draft has Edit and Copy controls. |
| [02 products](../../.maintenance-colima.gRhh9W/promotion-ui-f-review.uz1mAh/02-products-retained.jpg) | Named product, RM12 menu price, outlet and selected count retained. |
| [03 overnight schedule](../../.maintenance-colima.gRhh9W/promotion-ui-f-review.uz1mAh/03-overnight-schedule.jpg) | Friday, 22:00–02:00 and next-morning explanation. |
| [04 quantity recovery](../../.maintenance-colima.gRhh9W/promotion-ui-f-review.uz1mAh/04-quantity-error-focus.jpg) | Specific error, visible focus and retained draft. |
| [05 narrow receipt](../../.maintenance-colima.gRhh9W/promotion-ui-f-review.uz1mAh/05-corrected-receipt-390.jpg) | 390×844; warning cleared, quantity three, −RM6 kept together, RM30 total. |
| [06 desktop editor](../../.maintenance-colima.gRhh9W/promotion-ui-f-review.uz1mAh/06-desktop-reopened-offer.jpg) | 1280×800; all five choices and saved new name. |
| [07 copy receipt](../../.maintenance-colima.gRhh9W/promotion-ui-f-review.uz1mAh/07-desktop-copy-receipt.jpg) | Separate percentage copy gives RM10.80. |
| [08 separate drafts](../../.maintenance-colima.gRhh9W/promotion-ui-f-review.uz1mAh/08-separate-draft-copy.jpg) | Original and copy saved separately without publication. |
| [09 stop confirmation](../../.maintenance-colima.gRhh9W/promotion-ui-f-review.uz1mAh/09-stop-confirmation.jpg) | Named scope and offline-delay warning; cancelled afterward. |

The native capture path avoids the unused canvas margins in the earlier C/D
full-page capture attempt. At 390px the total label wraps across lines, but the
amount and minus sign remain readable and controls do not visibly overlap.

Post-walkthrough authoritative `dump` exited 0 at 03:22:13 UTC. Its 84 invoice,
72 order and 24 shift records exactly matched the pre-walkthrough dump. Its
promotion-snapshot section is empty because it only includes versions referenced
by those baseline sales; that empty comparison is **not** proof of all published
snapshot bytes. Historical snapshot preservation remains supported by the real
Frappe tests above, not overstated from this empty dump section.

### Remaining observations

- Browser console recorded `Error connecting to socket.io: Invalid origin` at
  03:14:10 UTC in the local Frappe fixture. Cause/configuration correction was not
  established in this UI recheck. Editing, saving, previews and manual status
  refresh worked; realtime notification delivery is not verified. Do not weaken
  origin validation to hide the warning.
- Step navigation and invalid-quantity focus worked. A successful asynchronous
  save/preview refresh returned focus to the page in this run. Full keyboard-only
  completion and focus continuity still need acceptance; this is not a complete
  accessibility audit.
- The standard Frappe stop dialog still uses generic Yes/No buttons. The message
  is explicit, but action-specific labels are a sensible polish item to evaluate
  with novices. Actual stopping and offline delivery were deliberately not run.
- One browser automation wait timed out even though its own diagnostics reported
  the receipt visible. A fresh native accessibility observation confirmed the
  completed receipt; no product failure was inferred from that tool timeout.

## Recoverable build-space cleanup

Only this task's exact unused images/cache, resolved from its own build receipts
and checked against all containers, were removed. Published/tested images were
archived first; OCI root identities and every archived blob hash were verified.
The archives are recoverable with `docker load`:

| Archive under the private Colima directory | SHA-256 |
| --- | --- |
| `promotion-candidate-d.qgJshN/previous-b-823e137b4ca0.tar` | `6f8eb561f1f722fd5c53259463f7635a9514efd1927895f0812ec79af84fa056` |
| `promotion-candidate-f.BNP2bo/previous-e-34fc62bc08e5.tar` | `e6eb0da2c4e34fc2f690662a11b393ced832f35ce173e20190277f7a08253357` |
| `promotion-candidate-f.BNP2bo/previous-a-c-images.tar` | `1213ee25a57b37b8fd6b8b6d8fdac288fd87140bc1e8fae2ca099c8c7b9d4c8c` |

Reproducible source-stage cache and the exact failed F build container
`af580135410dd4f2c301c02b117eb3e10dccf4fe06da36bb0de11de4ddcf6f68`
were removed without deleting any volume. An initial B archive validation used
the wrong Docker identity field; the corrected OCI-index check passed before
removal. All wheels, logs and screenshot evidence remain. E was built but never
deployed; F superseded it after the D rename finding.

Temporary build headroom, not just final-image size, matters: F failed near
2.0GB free; cleanup restored 2.9GB before its successful unchanged retry. Final
free space was 2.5GB. No broad prune, Colima resize/restart, database deletion,
queued-sale deletion or production/home-server operation was used. Do not repeat
large builds without checking temporary headroom on this shared engine.

## Acceptance still required

Screenshots and agent-driven interaction do not establish zero-technical-skill
acceptance. Three uncoached first-time users must still create all five types,
edit/copy, schedule overnight offers, recover from mistakes, compare overlapping
offers and stop one. Multi-outlet/mixed-price user journeys, actual emergency
stop delivery, physical-tablet offline/restart, submitted v2 invoices/GL/refunds,
recent restored-data rehearsal, commissioned costs and pilot/live verification
remain separate gates. No new offer was activated through the UI; no admission
or cost guard was disabled for the walkthrough.

The real-service publication fixture explicitly stubs admission/economics and
rolls back fixture transactions. It establishes MariaDB persistence, not rollout
permission or real accounting acceptance. The native preview and rename/readiness
checks do not stub the calculator or readiness checks. Standard smoke sales are
synthetic baseline commercial evidence, not v2 discount invoice acceptance.
