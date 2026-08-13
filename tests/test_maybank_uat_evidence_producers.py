from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from requests.adapters import HTTPAdapter

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()
if "Crypto.Cipher" not in sys.modules:
    crypto = ModuleType("Crypto")
    cipher = ModuleType("Crypto.Cipher")
    cipher.AES = SimpleNamespace(MODE_CBC=2)
    crypto.Cipher = cipher
    sys.modules["Crypto"] = crypto
    sys.modules["Crypto.Cipher"] = cipher

import frappe  # noqa: E402

from kopos_connector.acceptance import maybank_uat_business_state as business  # noqa: E402
from kopos_connector.acceptance import maybank_uat_common as common  # noqa: E402
from kopos_connector.acceptance import maybank_uat_transport as transport  # noqa: E402
from kopos_connector.services.maybank import client as maybank_client  # noqa: E402


AMOUNT_SEN = 1_280
DEVICE = "SM-X135G-ACCEPTANCE"
OUTLET = "MBBQRTEST0001"
COMPANY = "JiJi Coffee Sdn Bhd"
RUN_NONCE = "4a" * 32
REFERENCES = tuple(f"MBB-UAT-REF-{index:02d}" for index in range(10))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bindings() -> common.ExportBindings:
    return common.ExportBindings(
        candidate_apk_sha256="11" * 32,
        erp_artifact_sha256="22" * 32,
        mobile_manifest_sha256="33" * 32,
        device_udid=DEVICE,
        run_nonce=RUN_NONCE,
    )


def _transactions() -> tuple[dict, ...]:
    rows = []
    for index, reference in enumerate(REFERENCES):
        is_winner = index == 0
        rows.append(
            {
                "name": f"MBQR-UAT-{index:02d}",
                "transaction_refno": reference,
                "status": "paid" if is_winner else "pending",
                "maybank_status": 1 if is_winner else 2,
                "sale_amount_sen": AMOUNT_SEN,
                "currency": "MYR",
                "provider": "maybank_qr",
                "company": COMPANY,
                "device_id": DEVICE,
                "pos_profile": "UAT POS Profile",
                "maybank_qrpaybiz_account": "Maybank QRPayBiz Account - UAT",
                "outlet_id": OUTLET,
                "idempotency_key": f"sale-order-{index}:qr:payment-{index}:1",
                "request_fingerprint": _digest(f"generation-{index}"),
                "fb_order": "FB-ORDER-UAT-1" if is_winner else f"FB-DRAFT-{index}",
                "fb_order_payment": (
                    "FB-PAYMENT-UAT-1" if is_winner else f"FB-DRAFT-PAY-{index}"
                ),
                "sales_invoice": "SINV-UAT-1" if is_winner else None,
                "consumption_key": "FB-ORDER-UAT-1" if is_winner else None,
                "invoice_consumption_key": "SINV-UAT-1" if is_winner else None,
                "consumed_at": "2026-07-28T08:00:30+08:00" if is_winner else None,
                "creation": f"2026-07-28 08:00:{index:02d}",
                "created_at": f"2026-07-28T08:00:{index:02d}+08:00",
                "is_test_simulation": 0,
            }
        )
    return tuple(rows)


def _capacity_rejection() -> dict:
    return {
        "status": "blocked",
        "reason": "rate_limit_exceeded",
        "capacityLimit": 10,
        "activeProviderReferenceDigests": sorted(
            _digest(reference) for reference in REFERENCES
        ),
        "providerRequestSent": False,
        "attemptedAt": "2026-07-28T08:00:20+08:00",
        "requestFingerprintSha256": "ab" * 32,
    }


def _context() -> common.AcceptanceContext:
    return common.AcceptanceContext(
        bindings=_bindings(),
        transaction_references=REFERENCES,
        transactions=_transactions(),
        capacity_fence={"name": "MBQR-CAPACITY-FENCE"},
        capacity_rejection=_capacity_rejection(),
        provider_origin=maybank_client.DEFAULT_BASE_URL,
        outlet_id=OUTLET,
        outlet_id_sha256=_digest(OUTLET),
        company=COMPANY,
        currency="MYR",
        amount_sen=AMOUNT_SEN,
        simulation_audit_record_count=0,
    )


def test_tls_adapter_hashes_live_peer_certificate_without_retaining_bytes() -> None:
    certificate = b"exact-peer-certificate-der"
    response = SimpleNamespace(
        raw=SimpleNamespace(
            connection=SimpleNamespace(
                sock=SimpleNamespace(
                    getpeercert=lambda *, binary_form: (
                        certificate if binary_form else {}
                    )
                )
            )
        )
    )
    adapter = maybank_client._TlsEvidenceAdapter()
    with patch.object(HTTPAdapter, "send", return_value=response):
        captured = adapter.send(Mock(), verify=True)

    assert getattr(
        captured,
        maybank_client.TLS_PEER_CERTIFICATE_SHA256_ATTR,
    ) == hashlib.sha256(certificate).hexdigest()
    assert getattr(captured, maybank_client.TLS_VERIFIED_ATTR) is True
    assert certificate not in vars(captured).values()


def test_status_transport_evidence_hashes_exact_prepared_and_response_bytes() -> None:
    request_body = b'{ "transaction_refno" : "MBB-UAT-REF-00" }'
    response_body = b'{"status":"QR000","data":[]}\n'
    response = SimpleNamespace(
        request=SimpleNamespace(body=request_body),
        content=response_body,
    )
    setattr(
        response,
        maybank_client.TLS_PEER_CERTIFICATE_SHA256_ATTR,
        "55" * 32,
    )
    setattr(response, maybank_client.TLS_VERIFIED_ATTR, True)

    evidence = maybank_client._status_transport_evidence(
        response,
        "MBB-UAT-REF-00",
    )

    assert evidence["request_body_sha256"] == hashlib.sha256(
        request_body
    ).hexdigest()
    assert evidence["response_body_sha256"] == hashlib.sha256(
        response_body
    ).hexdigest()
    assert evidence["tls_peer_certificate_sha256"] == "55" * 32
    assert request_body.decode() not in json.dumps(evidence)
    assert response_body.decode() not in json.dumps(evidence)


def test_status_transport_evidence_fails_closed_without_verified_tls() -> None:
    response = SimpleNamespace(
        request=SimpleNamespace(
            body=b'{"transaction_refno":"MBB-UAT-REF-00"}'
        ),
        content=b'{"status":"QR000"}',
    )
    setattr(
        response,
        maybank_client.TLS_PEER_CERTIFICATE_SHA256_ATTR,
        "",
    )
    setattr(response, maybank_client.TLS_VERIFIED_ATTR, False)

    with pytest.raises(frappe.ValidationError, match="peer-certificate"):
        maybank_client._status_transport_evidence(
            response,
            "MBB-UAT-REF-00",
        )


def test_capacity_fence_proves_exact_ten_and_no_provider_call() -> None:
    transactions = _transactions()
    rejection = {
        "status": "rejected",
        "error_code": "MAYBANK_QR_REQUEST_REJECTED_NO_PROVIDER_ATTEMPT",
        "message": "Automatic QR request limit reached",
        "preflight_reason_code": "rate_limit_exceeded",
        "provider_request_attempted": False,
        "rejection_fence_registered": True,
        "local_release_authorized": True,
        "recovery_action": "release_local_provider_intent",
        "device_id": DEVICE,
        "idempotency_key": "eleventh-attempt",
        "amount_sen": AMOUNT_SEN,
        "currency": "MYR",
        "checked_at": "2026-07-28T08:00:20+08:00",
    }
    fence = {
        **transactions[0],
        "transaction_refno": f"REQUEST-{'AB' * 32}",
        "status": "failed",
        "maybank_status": 0,
        "idempotency_key": "eleventh-attempt",
        "request_fingerprint": "ab" * 32,
        "fb_order": "FB-DRAFT-11",
        "fb_order_payment": "FB-DRAFT-PAY-11",
        "sales_invoice": None,
        "consumption_key": None,
        "invoice_consumption_key": None,
        "consumed_at": None,
        "raw_response": json.dumps(
            rejection,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "replacement_reason": None,
        "replaces_transaction_refno": None,
        "round_number": 1,
        "created_at": "2026-07-28T08:00:20+08:00",
    }
    window_rows = [
        {"transaction_refno": reference}
        for reference in REFERENCES
    ] + [{"transaction_refno": fence["transaction_refno"]}]

    with patch.object(common, "_get_all", return_value=window_rows):
        proof = common._validate_capacity_fence(
            fence,
            transactions=transactions,
            bindings=_bindings(),
            outlet_id=OUTLET,
            company=COMPANY,
            currency="MYR",
            amount_sen=AMOUNT_SEN,
        )

    assert proof == _capacity_rejection()
    rejection["provider_request_attempted"] = True
    fence["raw_response"] = json.dumps(rejection)
    with (
        patch.object(common, "_get_all", return_value=window_rows),
        pytest.raises(frappe.ValidationError, match="no-provider-call"),
    ):
        common._validate_capacity_fence(
            fence,
            transactions=transactions,
            bindings=_bindings(),
            outlet_id=OUTLET,
            company=COMPANY,
            currency="MYR",
            amount_sen=AMOUNT_SEN,
        )


class _LiveStatusClient:
    base_url = maybank_client.DEFAULT_BASE_URL
    outlet_id = OUTLET

    def __init__(self, *, mismatch: bool = False) -> None:
        self.mismatch = mismatch

    def check_status_with_transport_evidence(self, reference: str):
        index = REFERENCES.index(reference)
        raw_status = 1 if index == 0 else 2
        if self.mismatch and index == 1:
            raw_status = 3
        return (
            {
                "status": "QR000",
                "data": [
                    {
                        "transaction_refno": reference,
                        "sale_amount": "12.80",
                        "outlet_id": OUTLET,
                        "currency": "MYR",
                        "status": raw_status,
                    }
                ],
            },
            {
                "request_body_sha256": _digest(f"status-request-{index}"),
                "response_body_sha256": _digest(f"status-response-{index}"),
                "tls_peer_certificate_sha256": "66" * 32,
                "tls_verified": True,
                "observed_at": f"2026-07-28T00:01:{index:02d}Z",
            },
        )


def _export_kwargs() -> dict:
    bindings = _bindings()
    return {
        "transaction_references": list(REFERENCES),
        "capacity_fence_transaction": "MBQR-CAPACITY-FENCE",
        "candidate_apk_sha256": bindings.candidate_apk_sha256,
        "erp_artifact_sha256": bindings.erp_artifact_sha256,
        "mobile_manifest_sha256": bindings.mobile_manifest_sha256,
        "device_udid": bindings.device_udid,
        "run_nonce": bindings.run_nonce,
    }


def test_transport_exporter_reads_live_status_without_business_mutation() -> None:
    written = []
    with (
        patch.object(transport, "load_acceptance_context", return_value=_context()),
        patch.object(
            transport.MaybankClient,
            "from_transaction",
            return_value=_LiveStatusClient(),
        ),
        patch.object(
            transport,
            "write_report_atomically",
            side_effect=lambda report, filename: written.append((report, filename)),
        ),
        patch.object(
            frappe.db,
            "set_value",
            side_effect=AssertionError("business mutation attempted"),
        ),
        patch.object(
            frappe.db,
            "commit",
            side_effect=AssertionError("business commit attempted"),
        ),
    ):
        report = transport.export_v1(**_export_kwargs())

    assert report["status"] == "passed"
    assert len(report["transactions"]) == 10
    assert [row["maybankStatus"] for row in report["transactions"]] == [
        1,
        *([2] * 9),
    ]
    assert report["transactions"][0]["requestBodySha256"] != (
        _transactions()[0]["request_fingerprint"]
    )
    assert written == [(report, "maybank-provider-transport.json")]
    serialized = json.dumps(report)
    assert all(reference not in serialized for reference in REFERENCES)
    assert "test-merchant-username-secret" not in serialized
    assert "test-merchant-pin-secret" not in serialized


def test_transport_exporter_does_not_write_partial_report_on_status_mismatch() -> None:
    with (
        patch.object(transport, "load_acceptance_context", return_value=_context()),
        patch.object(
            transport.MaybankClient,
            "from_transaction",
            return_value=_LiveStatusClient(mismatch=True),
        ),
        patch.object(transport, "write_report_atomically") as writer,
        pytest.raises(frappe.ValidationError, match="does not match"),
    ):
        transport.export_v1(**_export_kwargs())
    writer.assert_not_called()


def _gl_rows() -> list[dict]:
    base = {
        "posting_date": "2026-07-28",
        "account_currency": "MYR",
        "voucher_type": "Sales Invoice",
        "voucher_no": "SINV-UAT-1",
        "cost_center": None,
        "project": None,
        "remarks": "Maybank UAT acceptance",
        "is_cancelled": 0,
    }
    return [
        {
            **base,
            "name": "GLE-1",
            "account": "Debtors - JIJI",
            "debit": "12.80",
            "credit": "0.00",
            "debit_in_account_currency": "12.80",
            "credit_in_account_currency": "0.00",
            "party_type": "Customer",
            "party": "Walk-in Customer",
            "against": "Sales - JIJI",
            "against_voucher_type": "Sales Invoice",
            "against_voucher": "SINV-UAT-1",
        },
        {
            **base,
            "name": "GLE-2",
            "account": "Sales - JIJI",
            "debit": "0.00",
            "credit": "12.80",
            "debit_in_account_currency": "0.00",
            "credit_in_account_currency": "12.80",
            "party_type": None,
            "party": None,
            "against": "Walk-in Customer",
            "against_voucher_type": None,
            "against_voucher": None,
        },
        {
            **base,
            "name": "GLE-3",
            "account": "Maybank QR - JIJI",
            "debit": "12.80",
            "credit": "0.00",
            "debit_in_account_currency": "12.80",
            "credit_in_account_currency": "0.00",
            "party_type": None,
            "party": None,
            "against": "Walk-in Customer",
            "against_voucher_type": None,
            "against_voucher": None,
        },
        {
            **base,
            "name": "GLE-4",
            "account": "Debtors - JIJI",
            "debit": "0.00",
            "credit": "12.80",
            "debit_in_account_currency": "0.00",
            "credit_in_account_currency": "12.80",
            "party_type": "Customer",
            "party": "Walk-in Customer",
            "against": "Maybank QR - JIJI",
            "against_voucher_type": "Sales Invoice",
            "against_voucher": "SINV-UAT-1",
        },
    ]


def _install_business_database(monkeypatch, *, bank_type: str = "Bank") -> None:
    order = {
        "name": "FB-ORDER-UAT-1",
        "status": "Submitted",
        "docstatus": 1,
        "sales_invoice": "SINV-UAT-1",
        "device_id": DEVICE,
        "company": COMPANY,
        "currency": "MYR",
        "external_idempotency_key": "sale-order-0",
        "automatic_qr_state": "finalized",
        "automatic_qr_payment": "FB-PAYMENT-UAT-1",
        "automatic_qr_winner_channel": "maybank_qr",
        "invoice_status": "Posted",
    }
    invoice = {
        "name": "SINV-UAT-1",
        "docstatus": 1,
        "is_return": 0,
        "currency": "MYR",
        "company": COMPANY,
        "custom_fb_order": "FB-ORDER-UAT-1",
        "custom_fb_device_id": DEVICE,
        "custom_fb_idempotency_key": "sale-order-0",
        "grand_total": "12.80",
        "net_total": "12.80",
        "total_taxes_and_charges": "0.00",
        "write_off_amount": "0.00",
        "outstanding_amount": "0.00",
        "customer": "Walk-in Customer",
        "debit_to": "Debtors - JIJI",
    }
    order_payment = SimpleNamespace(
        name="FB-PAYMENT-UAT-1",
        source_payment_id="PAYMENT-LOCAL-1",
        payment_method="DuitNow QR",
        payment_channel_code="maybank",
        amount="12.80",
        reference_no=REFERENCES[0],
        external_transaction_id=REFERENCES[0],
        is_manual_confirmation=0,
        maybank_qr_transaction="MBQR-UAT-00",
        settlement_status="verified",
    )
    invoice_payment = SimpleNamespace(
        custom_fb_source_payment_id="PAYMENT-LOCAL-1",
        mode_of_payment="DuitNow QR",
        amount="12.80",
        account="Maybank QR - JIJI",
    )
    docs = {
        ("FB Order", "FB-ORDER-UAT-1"): SimpleNamespace(
            payments=[order_payment]
        ),
        ("Sales Invoice", "SINV-UAT-1"): SimpleNamespace(
            items=[SimpleNamespace(income_account="Sales - JIJI")],
            payments=[invoice_payment],
        ),
    }

    def get_value(doctype, name, fields, as_dict=False):
        if doctype == "Stock Entry":
            raise AssertionError("commercial evidence queried optional stock")
        row = {
            ("FB Order", "FB-ORDER-UAT-1"): order,
            ("Sales Invoice", "SINV-UAT-1"): invoice,
        }.get((doctype, name))
        return row

    def count(doctype, filters=None):
        if doctype == "GL Entry":
            return 4
        if doctype == "Manual QR Reconciliation":
            return 0
        if doctype in {"FB Order", "Sales Invoice"}:
            return 1
        raise AssertionError((doctype, filters))

    accounts = [
        {
            "name": "Debtors - JIJI",
            "account_type": "Receivable",
            "account_currency": "MYR",
            "company": COMPANY,
        },
        {
            "name": "Sales - JIJI",
            "account_type": "Income Account",
            "account_currency": "MYR",
            "company": COMPANY,
        },
        {
            "name": "Maybank QR - JIJI",
            "account_type": bank_type,
            "account_currency": "MYR",
            "company": COMPANY,
        },
    ]

    def get_all(doctype, **kwargs):
        if doctype == "GL Entry":
            return _gl_rows()
        if doctype == "Account":
            return accounts
        if doctype == "Stock Ledger Entry":
            raise AssertionError("commercial evidence queried optional stock")
        raise AssertionError((doctype, kwargs))

    monkeypatch.setattr(frappe.db, "get_value", get_value)
    monkeypatch.setattr(frappe.db, "count", count, raising=False)
    monkeypatch.setattr(frappe, "get_doc", lambda doctype, name: docs[(doctype, name)])
    monkeypatch.setattr(frappe, "get_all", get_all)
    monkeypatch.setattr(
        frappe.db,
        "set_value",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("business mutation attempted")
        ),
    )
    monkeypatch.setattr(
        frappe.db,
        "commit",
        lambda: (_ for _ in ()).throw(
            AssertionError("business commit attempted")
        ),
    )


def test_business_state_exporter_queries_exact_sale_without_equating_idempotencies(
    monkeypatch,
) -> None:
    _install_business_database(monkeypatch)
    written = []
    monkeypatch.setattr(
        business,
        "load_acceptance_context",
        lambda **kwargs: _context(),
    )
    monkeypatch.setattr(
        business,
        "write_report_atomically",
        lambda report, filename: written.append((report, filename)),
    )

    report = business.export_v1(**_export_kwargs())

    assert report["status"] == "passed"
    assert report["schemaVersion"] == "3"
    assert report["inventoryAcceptance"] is False
    assert report["inventoryEvaluation"] == "excluded_not_evaluated"
    assert "inventoryMutationCount" not in report
    assert "stockEntries" not in report
    assert "stockLedgerEntries" not in report
    assert "ingredientStockEntry" not in report["fbOrders"][0]
    assert len(report["providerTransactions"]) == 10
    assert report["providerTransactions"][0]["idempotencyKey"] != (
        report["fbOrders"][0]["idempotencyKey"]
    )
    assert report["fbOrders"][0]["idempotencyKey"] == (
        report["salesInvoices"][0]["idempotencyKey"]
    )
    assert report["payments"][0]["accountType"] == "Bank"
    assert report["settlementGlQuery"]["complete"] is True
    assert len(report["settlementGlRows"]) == 4
    assert written == [(report, "maybank-erp-business-state.json")]
    serialized = json.dumps(report)
    assert all(reference not in serialized for reference in REFERENCES)


def test_business_state_exporter_fails_closed_on_non_bank_settlement(
    monkeypatch,
) -> None:
    _install_business_database(monkeypatch, bank_type="Cash")
    monkeypatch.setattr(
        business,
        "load_acceptance_context",
        lambda **kwargs: _context(),
    )
    writer = Mock()
    monkeypatch.setattr(business, "write_report_atomically", writer)

    with pytest.raises(frappe.ValidationError, match="balanced Bank settlement"):
        business.export_v1(**_export_kwargs())
    writer.assert_not_called()


def test_atomic_report_writer_keeps_only_complete_private_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    private_files = tmp_path / "private" / "files"
    private_files.mkdir(parents=True)
    monkeypatch.setattr(
        frappe,
        "get_site_path",
        lambda *parts: str(tmp_path.joinpath(*parts)),
        raising=False,
    )
    report = {"schemaVersion": "2", "status": "passed"}

    common.write_report_atomically(report, "maybank-provider-transport.json")

    output_directory = private_files / "kopos-acceptance"
    output = output_directory / "maybank-provider-transport.json"
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert [path.name for path in output_directory.iterdir()] == [output.name]
    assert output.stat().st_mode & 0o777 == 0o600


def test_atomic_report_writer_can_preserve_an_existing_campaign_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    private_files = tmp_path / "private" / "files"
    private_files.mkdir(parents=True)
    monkeypatch.setattr(
        frappe,
        "get_site_path",
        lambda *parts: str(tmp_path.joinpath(*parts)),
        raising=False,
    )
    filename = "target-machine-preflight-1234567890abcdef.json"
    first = {"schemaVersion": "1", "runNonce": "first"}
    common.write_report_atomically(first, filename, must_not_exist=True)

    with pytest.raises(frappe.ValidationError, match="already exists"):
        common.write_report_atomically(
            {"schemaVersion": "1", "runNonce": "second"},
            filename,
            must_not_exist=True,
        )

    output = private_files / "kopos-acceptance" / filename
    assert json.loads(output.read_text(encoding="utf-8")) == first
