# Set up your store QR

Development workflow, not yet deployed or accepted for production. You need a
saved POS Profile, permission to manage it, your bank-issued reusable store QR
image, and the payment account already prepared by whoever manages your accounts.
Do not use a QR copied from a customer's payment or a QR for one fixed amount.

1. Open your POS Profile. Save any unrelated edits first. Choose **KoPOS → Set
   Up QR Payments** and check the store/profile and company shown at the top.
2. Choose your bank QR image: PNG, JPG or WebP, at most 5 MB. Use a clear image
   containing one QR. The image is read locally, without upload or camera access.
3. Keep the existing **QR payments awaiting confirmation** account unless your
   accounts adviser has given you another account. If it is empty or you are
   unsure, stop and ask them; do not guess. This screen does not create accounts.
4. Leave **Automatic bank confirmation (optional)** unchanged unless your
   Maybank merchant login and accounts have been prepared for that service.
5. Select **Review settings**. Check the profile, company, payment account and
   merchant name/ID against your bank's own store QR. On a small screen, scroll
   down within the dialog to see all merchant details.
6. Tick the merchant-confirmation box only when those details match. Select
   **Save QR settings**. You should see **QR settings saved**. This changes this
   POS Profile and its linked tablets, not company-wide payment accounts.

Reading or reviewing an image does not save it. Cancel discards this dialog's
unsaved choices. To keep an existing saved QR, leave the image chooser alone.
To stop offering your store QR, untick **Accept payments with my store QR**,
review and save; this does not delete its retained payload.

If the image cannot be read, crop around the QR or obtain a clearer image from
your bank, then try again. The saved QR has not been replaced. If another person
changes the profile, close setup, reload and review again. If a save cannot be
confirmed, preserve any unsaved form edits and inspect the profile's saved QR
settings before retrying; a lost response does not prove the save failed.

The merchant checkbox is a human verification, not bank authentication. A ready
configuration is not proof that any customer paid. Follow the store's approved
payment-confirmation procedure.

## Screenshot and interaction review — 2026-09-14

The screenshots use synthetic data in Chromium at 1280×1050 and 390×844. They
load the installed Frappe Dialog/Link controls, styling and html5-qrcode decoder.
The backend and parent POS form are simulated. No real merchant was commissioned
and no production or bank service was contacted. Exact source/asset bindings,
screenshots and receipts are retained under
`../../.maintenance-colima.gRhh9W/qr-import.bNR7AF/`.

| Screen | Review |
| --- | --- |
| [Start](../../.maintenance-colima.gRhh9W/qr-import.bNR7AF/browser-scroll-verified-01-start.png) | Numbered steps, existing account default, optional bank settings collapsed, explicit Cancel. No raw payload or JSON entry. |
| [Desktop review](../../.maintenance-colima.gRhh9W/qr-import.bNR7AF/browser-scroll-verified-02-review.png) | Store, company, account and merchant visible. Save without confirmation sends no save request. |
| [Saved](../../.maintenance-colima.gRhh9W/qr-import.bNR7AF/browser-scroll-verified-03-saved.png) | Shows the applied profile scope and merchant, then reloads a still-clean parent form. The save response here is simulated. |
| [Narrow start](../../.maintenance-colima.gRhh9W/qr-import.bNR7AF/browser-scroll-verified-04-narrow-start.png) | No horizontal clipping; native account search/select works. |
| [Narrow confirmation](../../.maintenance-colima.gRhh9W/qr-import.bNR7AF/browser-scroll-verified-05b-narrow-confirmation.png) | Merchant details and checkbox are reachable by scrolling; Save and Cancel remain accessible. Review is longer than one narrow viewport. |
| [Invalid image](../../.maintenance-colima.gRhh9W/qr-import.bNR7AF/browser-scroll-verified-06-narrow-error.png) | Format/size guidance is visible, saved QR is retained, review is invalidated and cancellation removes the old file input. |

Browser review exposed two defects missed by the initial mock: a hidden Frappe
parent section concealed merchant confirmation, and closed dialogs retained old
file inputs. The implementation now refreshes native section visibility and
removes each one-use dialog after Frappe's hide event. The final browser run
passes with no uncaught page errors or missing assets. Fixture-only failures
(missing native assets/schema/Link validation response) are retained separately.

This is simpler for a manager whose accounts are already prepared, not proof of
zero-technical-skill setup from scratch. Before acceptance, an uncoached novice
must independently choose the intended profile, import their own bank QR, spot
a deliberately wrong merchant, recover from an unreadable image and identify
what was actually saved. Run that workflow in real ERP after installing the
exact wheel and migrating twice, then verify the linked physical POS and bank
payment procedure. Screen-reader, keyboard-only, Safari and physical-device
acceptance remain unrun.

## Maintenance decision

Use [Frappe's native Dialog](https://docs.frappe.io/framework/user/en/api/dialog)
and the existing [html5-qrcode file-decoding API](https://scanapp.org/html5-qrcode-docs/docs/apis/classes/Html5Qrcode#scanfile).
The custom code only joins image selection, existing account selectors, server
review and guarded save. It does not implement image recognition, account search,
a second permission engine or another QR parser. No new dependency, schema,
service or subscription is introduced. Image compressed size is bounded; decoded
pixel-memory limits and broader browser coverage remain regression risks.
