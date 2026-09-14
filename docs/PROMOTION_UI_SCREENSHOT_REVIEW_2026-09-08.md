# Promotion workflow — screenshot review

Reviewed on 8 September 2026 in an isolated Colima ERP, using synthetic café
data and an Administrator account. Screenshots are actual 1280 × 720 browser
captures, not mock-ups. No production ERP or tablet was changed.

## Verdict

**Creating and correcting a simple offer is substantially clearer. The whole
promotion lifecycle is not yet ready for a person with zero technical skills.**

The walkthrough exposed two functional UI faults as well as layout problems:
the reward dropdown could be empty after changing offer type, and a cost check
updated the saved record without refreshing the form, causing a later save to
fail its stale-record check. Both have focused regression tests and source fixes.

There is also a genuine product limitation: pair offers have pricing previews
but do not yet have reviewed cost-calculation support for publication. The
server correctly blocks them. Completing that support is separate money-domain
work, not something to conceal with a nicer screen or bypass with an override.

## What changed

| Before | Revised behavior |
| --- | --- |
| One crowded step mixed item tables, outlets, dates and example prices. | Four focused steps: offer; items/outlets; dates/example; review/save. |
| Blank tables looked invalid immediately and both said “Add row”. | Neutral empty tables with “Add menu item” and “Add outlet”; incomplete choices are checked before advancing. |
| Internal item codes and precise timestamps dominated the review. | Readable drink names and Malaysia date/time labels; price before, savings and price after appear first. |
| The list repeated the offer name and hid its cost status. | One offer-name column, explicit next-publication status, readable offer type and visible cost-check result. |
| An inactive offer returned only “the promotion is inactive”. | The message explains the checkbox and Save step, and says this does not publish anything. |
| Changing to a pair offer could reveal a Select with no options. | Native Frappe conditional display initialises the reward choice before hiding it. |
| A cost check left the open form’s saved version stale. | Reload the same clean form after the result; preserve unsaved edits and report reload failures. |
| Pair templates looked fully publishable. | Final source labels them “preview only” and explains that the maintainer must complete cost-check support. |

The implementation uses existing Frappe dialogs, tables, Link searches,
conditional controls, list indicators and ordinary form saves. No UI framework,
paid service, dependency, new data field or second pricing engine was added.
The server remains the pricing, permission and publication authority.

## Final corrections checked in the browser

Pair-offer limitations now appear before the user advances, in normal
high-contrast text rather than relying on a faint warning colour. The reward
dropdown remains usable. Cancel was tested without creating another offer.

![Final early warning for preview-only templates](/Users/victor/dev/jiji/JiJiPOS-Everything/.maintenance-colima.gRhh9W/promotion-ui-review.Lellpg/final-02-preview-only-warning.jpg)

After a real blocked cost check, switched the offer off and saved successfully
without manually reloading or losing the schedule. The database confirmed both
rehearsal offers inactive and the scheduled offer still set to 9 September,
14:00:00 through 16:59:59 Malaysia time. The existing published snapshot remained
unchanged; no offer was published through this browser walkthrough.

![Successful inactive save after checking costs](/Users/victor/dev/jiji/JiJiPOS-Everything/.maintenance-colima.gRhh9W/promotion-ui-review.Lellpg/final-03-saved-after-cost-check.jpg)

## Screenshot walkthrough

The gallery records actual iterations. `after-*` captures below use the
`c4c942f2d75f` development image. The post-cost save was checked on `ad8b541b23dc`;
the final high-contrast warning uses delivery image `ae38089eaa92`. The delivery
image differs from the post-cost-check build only in that warning’s markup.
All are isolated development fixtures, not a production acceptance certificate.

### 1. Offer list

Before: repeated names and truncated settings obscured the information staff
need to decide what to do next.

![Offer list before review](/Users/victor/dev/jiji/JiJiPOS-Everything/.maintenance-colima.gRhh9W/promotion-ui-review.Lellpg/before-01-offer-list.jpg)

After: next-publication inclusion and cost readiness are separate. Neither
claims that an offline tablet has received a change.

![Offer list after review](/Users/victor/dev/jiji/JiJiPOS-Everything/.maintenance-colima.gRhh9W/promotion-ui-review.Lellpg/after-01-offer-list.jpg)

### 2. Choose the offer

The reward frequency is expressed as what happens when the customer buys four
items. The repaired dropdown has both options and a visible default. The final
warning correction adds the preview-only limitation to this screen.

![Choose an offer and reward frequency](/Users/victor/dev/jiji/JiJiPOS-Everything/.maintenance-colima.gRhh9W/promotion-ui-review.Lellpg/after-02-offer-type.jpg)

### 3. Choose items and outlets

The old middle step required scrolling through unrelated decisions.

![Crowded choices before review](/Users/victor/dev/jiji/JiJiPOS-Everything/.maintenance-colima.gRhh9W/promotion-ui-review.Lellpg/before-03-crowded-choices.jpg)

The revised step has one clear add action for each table and no premature red
border. Standard Item search still exposes item codes alongside names; this is
a remaining novice-usability concern, not a custom search engine to maintain.

![Separated item and outlet choices](/Users/victor/dev/jiji/JiJiPOS-Everything/.maintenance-colima.gRhh9W/promotion-ui-review.Lellpg/after-03-items-outlets.jpg)

### 4. Dates and example

Used the calendars and AM/PM selectors to choose 9 September, 2:00 PM through
4:59 PM Malaysia time. Dates and example controls, including the next action,
are visible at the tested desktop size.

![Scheduled offer with an example quantity](/Users/victor/dev/jiji/JiJiPOS-Everything/.maintenance-colima.gRhh9W/promotion-ui-review.Lellpg/after-04-dates-example.jpg)

### 5. Price-first review and correction

Four RM10 drinks cost RM30 before tax/extras when the half-price reward repeats.
The example price is entered by the operator, not looked up from the menu.

![Readable price-first offer review](/Users/victor/dev/jiji/JiJiPOS-Everything/.maintenance-colima.gRhh9W/promotion-ui-review.Lellpg/after-05-price-review.jpg)

Went Back through all three prior steps. The name, item, outlet, dates and
example were retained. Changed the reward to once per order; the result became
RM35. Saved that corrected offer inactive through the ordinary form save.

![Corrected once-only reward example](/Users/victor/dev/jiji/JiJiPOS-Everything/.maintenance-colima.gRhh9W/promotion-ui-review.Lellpg/after-05b-corrected-price.jpg)

### 6. Saved offer

The first section is balanced across two columns. The introduction explains
that saving is not publication and gives the next steps. Advanced settings
remain below this section; this is not yet guided editing of a saved offer.

![Saved inactive offer with next steps](/Users/victor/dev/jiji/JiJiPOS-Everything/.maintenance-colima.gRhh9W/promotion-ui-review.Lellpg/after-06-saved-offer.jpg)

### 7. Cost check

An inactive offer now explains what to switch on and save. This is a prerequisite
for checking, not permission to publish or a claim that the offer is live.

![Actionable inactive-offer guidance](/Users/victor/dev/jiji/JiJiPOS-Everything/.maintenance-colima.gRhh9W/promotion-ui-review.Lellpg/after-07-cost-prerequisite.jpg)

The scenario form uses ordinary language and preselects the single chosen outlet.

![Cost-check scenario form](/Users/victor/dev/jiji/JiJiPOS-Everything/.maintenance-colima.gRhh9W/promotion-ui-review.Lellpg/after-08-cost-check.jpg)

The real pair-offer check was blocked. This is evidence of missing cost-rule
support, not a successful publish test. Its raw technical error wording remains
an open usability finding.

![Pair offer cannot pass publication cost check](/Users/victor/dev/jiji/JiJiPOS-Everything/.maintenance-colima.gRhh9W/promotion-ui-review.Lellpg/after-09-pair-publication-block.jpg)

### 8. Outlet publication review

The screen explains that publication replaces the outlet-wide offer list,
not only the offer currently open. The rehearsal offers were switched off again.

![Publication scope explained before checking](/Users/victor/dev/jiji/JiJiPOS-Everything/.maintenance-colima.gRhh9W/promotion-ui-review.Lellpg/after-10-publication-scope.jpg)

The remaining synthetic item-discount offer also failed the current cost-evidence
gate. No confirmation or successful tablet delivery is claimed. The stacked
messages and “COGS” terminology need a clearer recovery path before novice acceptance.

![Outlet publication blocked on cost evidence](/Users/victor/dev/jiji/JiJiPOS-Everything/.maintenance-colima.gRhh9W/promotion-ui-review.Lellpg/after-11-outlet-cost-block.jpg)

## Remaining work before calling this novice-ready

1. Complete and verify pair-offer cost calculations against the existing price
   authority, including mixed prices, repeat choices, ingredients and refunds.
   Until then, keep those templates preview-only and preserve the publication gate.
2. Replace raw cost-rule/COGS error wording with specific, plain-language recovery
   actions. Complete the fixture’s cost evidence and test successful confirmation,
   publication and stopping an offer on an online tablet.
3. Make correction of saved offers as guided as creation. Staff account timezones
   must be Malaysia before rollout: the deliberately non-Malaysia test account’s
   ordinary saved-form date fields display Asia/Kolkata, unlike the guided setup.
4. Run the five uncoached operator tasks in the setup guide with ordinary staff
   and director accounts. Also test smaller viewports, keyboard-only use, long
   item names, multiple items/outlets and denied permissions. None is a pass here.

No real novice participated. No physical tablet, restored production backup,
pilot, canary or production verification was run for this UI review. The wider
campaign source-review gate also remains blocked on an existing `post-pay.ts`
reviewed-digest mismatch; that file and gate were not changed.

## Verification and maintenance notes

| Check | Result and evidence level |
| --- | --- |
| JavaScript syntax check and `git diff --check` | Exit 0, static. |
| `node --test tests/promotion-setup-ui.test.cjs` | Exit 0: 17 tests, mocked UI/controller checks. |
| Complete connector contract suite using the delivery wheel | Exit 0: 2,039 tests and 119 subtests; this suite is not real-service acceptance. |
| Promotion snapshot, economics and catalog tests in the delivery Frappe/MariaDB Bench | Exit 0: 7 real integration tests. |
| Delivery build and fixture startup | Exit 0; migrations ran twice; all six application services use the same image. |
| Authoritative ERP smoke and business-state dump | Both exit 0. Dump: 54 orders, 63 invoices, no duplicate order/invoice keys, no outstanding invoices, and no failed projections (144 succeeded, 18 reversed). These are synthetic fixture results. |
| Methodology, guardrails, vocabulary, topology and CI validators | Exit 0, static. |
| Campaign source-scope validator with `--require-all` | Exit 1, existing unrelated `post-pay.ts` reviewed-digest mismatch. No waiver or allowlist change. |

The full commands, source hashes, wheel identity, screenshot hashes and smoke
receipts are in the [evidence receipt](/Users/victor/dev/jiji/JiJiPOS-Everything/worktree-fnb-erpnext/docs/audit/promotion-ui-review-2026-09-08.json). POS/core/mobile
product code was not changed or retested in this screenshot turn.

No product file already over 1,500 lines was touched. The existing promotion UI
owner changed from 415 to 483 lines, the preview adapter from 173 to 177, and
the list script from 15 to 23. No dependency, paid service, persistent production
state, or new pricing authority was introduced. New review artifacts are this
report, its evidence receipt and local browser captures. Two synthetic offers
were retained inactive; nothing was deleted from the ERP database.

Several rebuild attempts exhausted Colima’s disk space (exit 2). Removed only
task-owned failed-build containers/layers and an unused intermediate build.
Two previously used review images were preserved as checksum-verified,
restorable local image archives before removing their unused VM cache copies;
their screenshots, wheels and logs remain. No database volume or unrelated
project was removed. One startup attempt also failed during fixture seeding
while a rehearsal offer was included; after restoring it to inactive, startup
passed. These failed attempts remain recorded rather than being counted as passes.

The test browser also reported a pre-existing socket connection “Invalid origin”
error. This walkthrough does not establish clean-console, realtime-update,
accessibility, cross-browser or small-screen acceptance.
