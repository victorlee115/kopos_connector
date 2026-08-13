# pyright: reportMissingImports=false

"""Live, read-only Maybank status transport evidence for production UAT."""

from __future__ import annotations

import hashlib
import re
from typing import Any

import frappe
from frappe.utils import cint, cstr

from kopos_connector.acceptance.maybank_uat_common import (
    SHA256_PATTERN,
    build_export_bindings,
    canonical_json_sha256,
    load_acceptance_context,
    producer_source_sha256,
    write_report_atomically,
)
from kopos_connector.api._maybank_qr_contract import (
    STATUS_MAP,
    _extract_status_entry,
    _validate_status_entry_identity,
    _validate_status_response,
)
from kopos_connector.services.maybank.client import MaybankClient


PRODUCER = "kopos_connector.acceptance.maybank_uat_transport.export_v1"


def _fail(message: str) -> None:
    frappe.throw(message, frappe.ValidationError)
    raise AssertionError("frappe.throw must raise")


def _capture_transaction(
    client: MaybankClient,
    transaction: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    reference = cstr(transaction.get("transaction_refno"))
    result, transport = client.check_status_with_transport_evidence(reference)
    _validate_status_response(result)
    entry = _extract_status_entry(result)
    if entry is None:
        _fail(f"Maybank status response {index} contains no transaction")
    raw_status = _validate_status_entry_identity(transaction, entry)
    provider_status = STATUS_MAP.get(str(raw_status))
    persisted_status = cstr(transaction.get("status")).strip()
    persisted_raw_status = cint(transaction.get("maybank_status"))
    if (
        provider_status not in {"paid", "pending", "scanned"}
        or provider_status != persisted_status
        or raw_status != persisted_raw_status
    ):
        _fail(
            f"Maybank live status {index} does not match the exact durable ERP state"
        )

    request_sha256 = cstr(transport.get("request_body_sha256")).strip()
    response_sha256 = cstr(transport.get("response_body_sha256")).strip()
    certificate_sha256 = cstr(
        transport.get("tls_peer_certificate_sha256")
    ).strip()
    observed_at = cstr(transport.get("observed_at")).strip()
    if (
        not SHA256_PATTERN.fullmatch(request_sha256)
        or not SHA256_PATTERN.fullmatch(response_sha256)
        or not SHA256_PATTERN.fullmatch(certificate_sha256)
        or transport.get("tls_verified") is not True
        or not re.search(r"(?:Z|[+-][0-9]{2}:[0-9]{2})$", observed_at)
    ):
        _fail(f"Maybank transport evidence {index} is incomplete")

    return {
        "transactionReferenceDigest": hashlib.sha256(
            reference.encode("utf-8")
        ).hexdigest(),
        "amountSen": int(transaction["sale_amount_sen"]),
        "result": "paid" if raw_status == 1 else "issued",
        "maybankStatus": raw_status,
        "observedAt": observed_at,
        "requestBodySha256": request_sha256,
        "responseBodySha256": response_sha256,
        "tlsPeerCertificateSha256": certificate_sha256,
        "tlsVerified": True,
    }


@frappe.whitelist(methods=["POST"])
def export_v1(
    transaction_references: Any,
    capacity_fence_transaction: str,
    candidate_apk_sha256: str,
    erp_artifact_sha256: str,
    mobile_manifest_sha256: str,
    device_udid: str,
    run_nonce: str,
    output_filename: str = "maybank-provider-transport.json",
) -> dict[str, Any]:
    """Capture ten exact live Maybank status exchanges without changing ERP.

    The completed report is atomically written to
    ``private/files/kopos-acceptance/<output_filename>``. No raw provider
    reference, request/response body, certificate, credential, or token is
    included in the report.
    """

    bindings = build_export_bindings(
        candidate_apk_sha256=candidate_apk_sha256,
        erp_artifact_sha256=erp_artifact_sha256,
        mobile_manifest_sha256=mobile_manifest_sha256,
        device_udid=device_udid,
        run_nonce=run_nonce,
    )
    context = load_acceptance_context(
        transaction_references=transaction_references,
        capacity_fence_transaction=capacity_fence_transaction,
        bindings=bindings,
    )
    if not context.transactions:
        _fail("Maybank UAT requires at least one transaction snapshot")
    client = MaybankClient.from_transaction(context.transactions[0])
    if (
        client.base_url != context.provider_origin
        or cstr(client.outlet_id).strip() != context.outlet_id
    ):
        _fail("Maybank client configuration changed during evidence capture")

    transactions = [
        _capture_transaction(client, transaction, index)
        for index, transaction in enumerate(context.transactions)
    ]
    paid_count = sum(
        1
        for transaction in transactions
        if transaction["result"] == "paid"
        and transaction["maybankStatus"] == 1
    )
    if paid_count != 1:
        _fail("Maybank transport evidence must contain exactly one paid result")

    capture_session_id = canonical_json_sha256(
        {
            "erpArtifactSha256": bindings.erp_artifact_sha256,
            "observations": [
                {
                    "observedAt": transaction["observedAt"],
                    "transactionReferenceDigest": transaction[
                        "transactionReferenceDigest"
                    ],
                }
                for transaction in transactions
            ],
            "runNonce": bindings.run_nonce,
        }
    )
    report = {
        "schemaVersion": "2",
        "status": "passed",
        "source": "maybank_uat_provider_transport",
        "candidateApkSha256": bindings.candidate_apk_sha256,
        "erpArtifactSha256": bindings.erp_artifact_sha256,
        "mobileManifestSha256": bindings.mobile_manifest_sha256,
        "deviceUdid": bindings.device_udid,
        "runNonce": bindings.run_nonce,
        "providerOrigin": context.provider_origin,
        "outletIdSha256": context.outlet_id_sha256,
        "company": context.company,
        "currency": context.currency,
        "provenance": {
            "producer": PRODUCER,
            "producerSourceSha256": producer_source_sha256(__file__),
            "captureSessionId": capture_session_id,
            "erpArtifactSha256": bindings.erp_artifact_sha256,
            "runNonce": bindings.run_nonce,
        },
        "transactions": transactions,
        "capacityRejection": context.capacity_rejection,
        "mockModeEnabled": False,
        "developerModeEnabled": False,
        "simulationEnabled": False,
        "simulationAuditRecordCount": (
            context.simulation_audit_record_count
        ),
    }
    write_report_atomically(report, output_filename)
    return report
