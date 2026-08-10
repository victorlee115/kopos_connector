# Target configuration preflight

Run this only on the intended ERP site, after the exact candidate wheel is
installed and every web, worker, and scheduler process has been restarted on
that wheel. It is a non-device System Manager operation. It makes no provider
network call, commits no database change, and makes no inventory mutation or
inventory acceptance claim.

## Protected inputs

Obtain these values from the release authority; do not invent or copy them from
the target report:

- campaign nonce;
- exact ERP commit and wheel SHA-256;
- expected installed-package inventory SHA-256;
- exact production APK and both release-manifest SHA-256 values;
- approved HTTPS origin and hashed site identity;
- approved company, currency, and Maybank account type.

The release authority must separately pin the expected producer-closure hash,
approved static-QR device-set hash, and approved QR settlement-account hash.
Those values are checked after the report is collected and are not accepted
from the target itself.

## Run on the target Bench

From the Bench directory, replace every angle-bracket placeholder and run:

```bash
bench --site <site-name> execute \
  kopos_connector.acceptance.target_preflight_machine.run_v1 \
  --kwargs '{"run_nonce":"<nonce>","erp_commit":"<40-char-commit>","erp_artifact_sha256":"<wheel-sha256>","expected_runtime_inventory_sha256":"<installed-package-sha256>","candidate_apk_sha256":"<apk-sha256>","mobile_manifest_sha256":"<mobile-manifest-sha256>","erp_manifest_sha256":"<erp-manifest-sha256>","expected_origin":"https://<approved-origin>","expected_site_id_sha256":"<site-id-sha256>","company":"<approved-company>","currency":"MYR","expected_maybank_account_type":"corporate"}'
```

Do not pass `output_filename`. The producer derives the only permitted private
filename from the campaign nonce and refuses to overwrite an earlier report.
A successful command writes:

```text
sites/<site-name>/private/files/kopos-acceptance/target-machine-preflight-<nonce-digest-prefix>.json
```

## Collect and verify

1. Copy that exact private JSON file through the protected release-authority
   channel. Do not expose it as a public File or HTTP method.
2. Record its SHA-256 and preserve the original bytes; never edit or reformat
   the report.
3. The protected authority derives the producer closure and installed runtime
   inventory from the exact ERP wheel, then compares every binding and result.
4. The authority also compares the report's static-QR device-set and settlement
   account hashes with the independently approved values.
5. A missing report, an existing nonce file, any failed check, or any identity
   mismatch blocks acceptance. Fix the target and run a new campaign nonce;
   never alter the old evidence.

The output must report zero provider calls, zero committed database mutations,
and zero inventory mutations. The static QR payload and Maybank credentials
must never appear in retained or shared evidence.
