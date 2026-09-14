# Offers: a guide for café owners

Development feature. Activation remains locked until this exact pricing release
has passed its checkout rollout checks. Existing published prices stay unchanged.

## Create an offer

1. Open JiJi Home → Offers → Create an offer.
2. Choose Percent off, Amount off each, Special price, Buy 1 get 1 free, or
   Second item half price. Give it a recognisable name. Paid extras stay full price.
3. Choose the outlets, then tick products by name. The list shows current menu
   prices; the server checks availability at every selected outlet.
4. Choose a one-off date range or Repeat on selected days every week. All inputs
   are Malaysia time, even if your account or computer uses another timezone.
   Overnight offers continue into the next morning. The chosen weekday is the
   starting day. A repeating window stops at its Until time.
5. Review the sample receipt. Try another outlet, date/time, product or quantity.
   It uses current menu prices, this draft and the outlet's published offers.
   Tax, paid extras and manual discounts are not part of this sample.
6. Save draft. This does not change till prices. The checklist explains missing
   menu/recipe costs, unsupported older offers or tablets needing an update.
7. When all checks pass and you approve the receipt, Activate offer publishes
   the change for the selected outlets together. A failed review publishes none.

Customers receive the best permitted total price. A manual offer is eligible
only when staff select it; selection does not force a worse price.

## Change, copy and stop

Edit offer opens the same four-step editor. Changing or saving a draft never
changes an already-published version. Review and activate the update separately.
You can change its name here too. The saved offer remains the same offer;
previously published versions and past receipts keep their original names.

Make a copy starts an unsaved, inactive offer. Give it a different name.

Stop offer asks you to confirm the affected outlets. The stop takes effect on
each till only after that till receives the new list. An offline till may still
use its previous valid saved list. Check Which tills?; Published or Stopped in
ERP is not a claim that every till has received it.

Advanced older offers are not silently converted. Ask the maintainer to review
their special settings. An outlet with other legacy offers cannot be silently
migrated to the new best-price calculation.

## If a check fails

Your draft stays available. Correct the stated product, price, recipe or schedule,
then save and review again. Current cost checks include every same-product and
mixed-product pair for buy-and-get offers, rather than an average that hides a
loss. A loss or missing cost still needs the existing distinct second-director
exception process; the guided screen cannot bypass that safeguard.

“Pricing rollout is not finished” is a maintainer/deployment task, not something
to solve by changing your offer or disabling a safety check.

The calculator has strict workload limits. It never substitutes a partly
calculated discount when it cannot prove the best price. Unusually large mixed
baskets and advanced once-only overlaps still need additional admission work.

## Acceptance still required

Three first-time non-technical people must each create, schedule, compare, edit,
copy and stop an offer without coaching or accidental publication. Targets are
five minutes to create and one minute to issue a stop, excluding the openly shown
offline delivery delay. Real online/offline tablets, restart recovery, historical
sale replay, restored-data rehearsal and a controlled pilot remain separate
checks. Automated tests and screenshots do not replace those checks.
