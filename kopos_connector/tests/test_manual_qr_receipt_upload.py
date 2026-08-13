# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


JPEG_BYTES = b"\xff\xd8\xff\xe0receipt-image"


@dataclass
class UploadEnv:
    transactions: dict[str, SimpleNamespace] = field(default_factory=dict)
    manual_reconciliations: dict[str, SimpleNamespace] = field(default_factory=dict)
    files: dict[str, Any] = field(default_factory=dict)
    comments: list[SimpleNamespace] = field(default_factory=list)
    payment_evidence: dict[str, str] = field(default_factory=dict)
    file_inserts: int = 0


class UploadedFile:
    def __init__(self, content: bytes, mimetype: str = "image/jpeg") -> None:
        self.stream = BytesIO(content)
        self.mimetype = mimetype
        self.content_type = mimetype

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)


@pytest.fixture
def receipt_module(monkeypatch):
    install_fake_frappe_modules()

    import frappe
    import kopos_connector.api.manual_qr_receipt as manual_qr_receipt

    env = UploadEnv()
    device = SimpleNamespace(
        name="KoPOS Device A",
        device_id="DEVICE-A",
        api_user="device-a@kopos.local",
        enabled=1,
        config_version=8,
        pos_profile="POS-MAIN",
    )
    env.transactions["MBQR-TXN-1"] = build_transaction(
        name="MBQR-TXN-1",
        transaction_refno="TXN-1",
        device_id="DEVICE-A",
        amount_sen=1200,
    )
    env.transactions["MBQR-TXN-2"] = build_transaction(
        name="MBQR-TXN-2",
        transaction_refno="TXN-2",
        device_id="DEVICE-A",
        amount_sen=1200,
    )
    env.transactions["MBQR-TXN-B"] = build_transaction(
        name="MBQR-TXN-B",
        transaction_refno="TXN-B",
        device_id="DEVICE-B",
        amount_sen=1200,
    )
    linked_transaction = build_transaction(
        name="MBQR-TXN-LINKED",
        transaction_refno="TXN-LINKED",
        device_id="DEVICE-A",
        amount_sen=1200,
    )
    linked_transaction.status = "timeout"
    linked_transaction.fb_order_payment = "FB-PAY-LINKED"
    linked_transaction.manual_reconciliation_status = "pending_reconciliation"
    env.transactions[linked_transaction.name] = linked_transaction
    env.payment_evidence["FB-PAY-LINKED"] = json.dumps(
        {
            "evidence_kind": "receipt_photo",
            "captured_at": "2026-03-13T18:05:45",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    env.manual_reconciliations["MQR-1"] = build_manual_reconciliation()

    def fake_get_value(
        doctype: str,
        filters: Any = None,
        fieldname: Any = None,
        as_dict: bool = False,
    ) -> Any:
        if doctype == "Maybank QR Transaction":
            if isinstance(filters, dict):
                for txn in env.transactions.values():
                    if all(getattr(txn, key, None) == value for key, value in filters.items()):
                        return getattr(txn, fieldname) if isinstance(fieldname, str) else txn
                return None
        if doctype == "Manual QR Reconciliation":
            if isinstance(filters, dict):
                for reconciliation in env.manual_reconciliations.values():
                    if all(
                        getattr(reconciliation, key, None) == value
                        for key, value in filters.items()
                    ):
                        return (
                            getattr(reconciliation, fieldname)
                            if isinstance(fieldname, str)
                            else reconciliation
                        )
                return None
        if doctype == "File":
            file_doc = env.files.get(str(filters))
            if not file_doc:
                return None
            if as_dict and isinstance(fieldname, list):
                return {field: getattr(file_doc, field, None) for field in fieldname}
            return getattr(file_doc, fieldname, None)
        if (
            doctype == "FB Order Payment"
            and fieldname == "manual_confirmation_evidence_json"
        ):
            return env.payment_evidence.get(str(filters))
        if doctype == "Company" and fieldname == "default_currency":
            return "MYR"
        return None

    def fake_set_value(
        doctype: str,
        name: str,
        values: dict[str, Any],
        update_modified: bool = True,
    ) -> None:
        records = (
            env.manual_reconciliations
            if doctype == "Manual QR Reconciliation"
            else env.transactions
        )
        txn = records[name]
        for key, value in values.items():
            setattr(txn, key, value)

    def fake_get_doc(*args: Any, **kwargs: Any) -> Any:
        if len(args) == 1 and isinstance(args[0], dict):
            payload = args[0]
            if payload.get("doctype") == "File":
                return FakeFileDoc(env, payload)
            if payload.get("doctype") == "Comment":
                return FakeCommentDoc(env, payload)
        if len(args) >= 2 and args[0] == "Maybank QR Transaction":
            return env.transactions[str(args[1])]
        if len(args) >= 2 and args[0] == "Manual QR Reconciliation":
            return env.manual_reconciliations[str(args[1])]
        raise AssertionError(f"unexpected get_doc call: {args!r} {kwargs!r}")

    monkeypatch.setattr(frappe.db, "get_value", fake_get_value, raising=False)
    locked_transactions: list[str] = []

    def fake_sql(query: str, values: tuple[Any, ...] = (), **_kwargs: Any) -> list[Any]:
        if "FOR UPDATE" in query:
            locked_transactions.append(str(values[0]))
        return []

    monkeypatch.setattr(frappe.db, "sql", fake_sql, raising=False)
    monkeypatch.setattr(frappe.db, "set_value", fake_set_value, raising=False)
    monkeypatch.setattr(
        frappe.db,
        "get_single_value",
        lambda doctype, fieldname: "OUTLET-1"
        if (doctype, fieldname) == ("Maybank Settings", "outlet_id")
        else None,
        raising=False,
    )
    monkeypatch.setattr(frappe, "get_doc", fake_get_doc, raising=False)
    monkeypatch.setattr(
        frappe,
        "get_cached_doc",
        lambda doctype, name: SimpleNamespace(company="KoPOS Cafe", currency="MYR"),
        raising=False,
    )
    monkeypatch.setattr(
        frappe,
        "logger",
        lambda *_args, **_kwargs: SimpleNamespace(info=lambda *_a, **_k: None),
        raising=False,
    )
    monkeypatch.setattr(frappe, "as_json", lambda value: str(value), raising=False)
    monkeypatch.setattr(frappe, "get_traceback", lambda: "traceback", raising=False)
    monkeypatch.setattr(frappe, "conf", {}, raising=False)
    monkeypatch.setattr(
        frappe,
        "request",
        SimpleNamespace(files={"file": UploadedFile(JPEG_BYTES)}),
        raising=False,
    )
    monkeypatch.setattr(
        manual_qr_receipt,
        "get_authenticated_device_doc",
        lambda: device,
    )
    monkeypatch.setattr(
        manual_qr_receipt,
        "require_device_context",
        lambda device_id: device,
    )
    monkeypatch.setattr(
        manual_qr_receipt,
        "lock_device_for_operational_mutation",
        lambda device_id: device,
    )
    return SimpleNamespace(
        module=manual_qr_receipt,
        env=env,
        frappe=frappe,
        device=device,
        locked_transactions=locked_transactions,
    )


def test_upload_attaches_private_file_to_matching_transaction(receipt_module):
    result = receipt_module.module.upload_manual_qr_receipt(**valid_payload())

    txn = receipt_module.env.transactions["MBQR-TXN-1"]
    file_doc = receipt_module.env.files[txn.receipt_file]
    expected_hash = hashlib.sha256(JPEG_BYTES).hexdigest()

    assert result == {
        "status": "ok",
        "file_name": "receipt.jpg",
        "file_url": "/private/files/receipt.jpg",
        "file_hash": expected_hash,
        "attached_to_doctype": "Maybank QR Transaction",
        "attached_to_name": "MBQR-TXN-1",
    }
    assert file_doc.is_private == 1
    assert file_doc.attached_to_doctype == "Maybank QR Transaction"
    assert file_doc.attached_to_name == "MBQR-TXN-1"
    assert file_doc.insert_ignore_permissions is True
    assert txn.receipt_file_hash == expected_hash
    assert txn.receipt_idempotency_key == "upload-key-1"
    assert txn.manual_reconciliation_status == "pending_reconciliation"
    assert len(receipt_module.env.comments) == 1
    assert receipt_module.locked_transactions == ["MBQR-TXN-1"]


def test_static_qr_upload_attaches_to_manual_reconciliation(receipt_module):
    result = receipt_module.module.upload_manual_qr_receipt(
        **valid_payload(
            transaction_refno="static-session-1",
            idempotency_key="static-upload-key-1",
        )
    )

    reconciliation = receipt_module.env.manual_reconciliations["MQR-1"]
    file_doc = receipt_module.env.files[reconciliation.receipt_file]

    assert result["status"] == "ok"
    assert result["attached_to_doctype"] == "Manual QR Reconciliation"
    assert result["attached_to_name"] == "MQR-1"
    assert file_doc.attached_to_doctype == "Manual QR Reconciliation"
    assert file_doc.attached_to_name == "MQR-1"
    assert receipt_module.env.comments[-1].reference_doctype == (
        "Manual QR Reconciliation"
    )
    assert reconciliation.status == "pending_reconciliation"
    assert receipt_module.locked_transactions == ["MQR-1"]


def test_offline_receipt_upload_attaches_after_transaction_timeout(
    receipt_module,
    monkeypatch,
):
    monkeypatch.setattr(
        receipt_module.module,
        "now_datetime",
        lambda: datetime(2026, 3, 15, 18, 5, 45),
    )

    result = receipt_module.module.upload_manual_qr_receipt(
        **valid_payload(
            transaction_refno="TXN-LINKED",
            captured_at="2026-03-13T18:05:45",
            idempotency_key="upload-key-linked",
        )
    )

    transaction = receipt_module.env.transactions["MBQR-TXN-LINKED"]
    assert result["status"] == "ok"
    assert result["attached_to_name"] == "MBQR-TXN-LINKED"
    assert transaction.manual_reconciliation_status == "pending_reconciliation"
    assert transaction.receipt_file


def test_late_receipt_upload_preserves_provider_paid_and_reconciled_truth(
    receipt_module,
):
    transaction = receipt_module.env.transactions["MBQR-TXN-LINKED"]
    transaction.status = "paid"
    transaction.manual_reconciliation_status = "reconciled"

    result = receipt_module.module.upload_manual_qr_receipt(
        **valid_payload(
            transaction_refno="TXN-LINKED",
            captured_at="2026-03-13T18:05:45",
            idempotency_key="upload-key-linked-after-reconciled",
        )
    )

    assert result["status"] == "ok"
    assert transaction.status == "paid"
    assert transaction.manual_reconciliation_status == "reconciled"
    assert transaction.receipt_file


def test_linked_offline_receipt_must_match_persisted_capture_time(receipt_module):
    with pytest.raises(
        receipt_module.frappe.ValidationError,
        match="captured_at does not match",
    ):
        receipt_module.module.upload_manual_qr_receipt(
            **valid_payload(
                transaction_refno="TXN-LINKED",
                captured_at="2026-03-13T18:05:46",
                idempotency_key="upload-key-linked-mismatch",
            )
        )


def test_authentication_happens_before_receipt_bytes_are_read(receipt_module, monkeypatch):
    class ReadMustNotRun:
        mimetype = "image/jpeg"
        content_type = "image/jpeg"

        def read(self, _size: int = -1) -> bytes:
            raise AssertionError("unauthorized upload body was read")

    monkeypatch.setattr(
        receipt_module.frappe,
        "request",
        SimpleNamespace(files={"file": ReadMustNotRun()}),
        raising=False,
    )
    monkeypatch.setattr(
        receipt_module.module,
        "require_device_context",
        lambda device_id: (_ for _ in ()).throw(
            receipt_module.frappe.ValidationError("denied")
        ),
    )

    with pytest.raises(receipt_module.frappe.ValidationError, match="denied"):
        receipt_module.module.upload_manual_qr_receipt(**valid_payload())


def test_receipt_body_is_processed_before_device_mutation_lock(
    receipt_module,
    monkeypatch,
):
    events: list[str] = []

    class TrackingUpload:
        mimetype = "image/jpeg"
        content_type = "image/jpeg"

        def __init__(self) -> None:
            self.stream = self

        def read(self, _size: int = -1) -> bytes:
            events.append("read_receipt")
            assert "device_lock" not in events
            return JPEG_BYTES

        def seek(self, _position: int) -> None:
            return None

    original_load = receipt_module.module._load_and_validate_transaction

    def track_transaction_load(payload, device):
        events.append("transaction_mutation")
        return original_load(payload, device)

    monkeypatch.setattr(
        receipt_module.frappe,
        "request",
        SimpleNamespace(files={"file": TrackingUpload()}),
        raising=False,
    )
    monkeypatch.setattr(
        receipt_module.module,
        "require_device_context",
        lambda device_id: events.append("authorization_preflight")
        or receipt_module.device,
    )
    monkeypatch.setattr(
        receipt_module.module,
        "lock_device_for_operational_mutation",
        lambda device_id: events.append("device_lock") or receipt_module.device,
    )
    monkeypatch.setattr(
        receipt_module.module,
        "_load_and_validate_transaction",
        track_transaction_load,
    )

    receipt_module.module.upload_manual_qr_receipt(**valid_payload())

    assert events == [
        "authorization_preflight",
        "read_receipt",
        "device_lock",
        "transaction_mutation",
    ]


def test_receipt_mutation_rejects_device_authority_change_after_body_read(
    receipt_module,
    monkeypatch,
):
    changed_device = SimpleNamespace(
        **{
            **vars(receipt_module.device),
            "config_version": receipt_module.device.config_version + 1,
        }
    )
    monkeypatch.setattr(
        receipt_module.module,
        "lock_device_for_operational_mutation",
        lambda device_id: changed_device,
    )

    with pytest.raises(
        receipt_module.frappe.ValidationError,
        match="authority changed",
    ):
        receipt_module.module.upload_manual_qr_receipt(**valid_payload())

    assert receipt_module.locked_transactions == []
    assert receipt_module.env.file_inserts == 0


def test_receipt_reader_is_bounded_to_configured_limit_plus_one(receipt_module, monkeypatch):
    read_sizes: list[int] = []

    class OversizedStream:
        mimetype = "image/jpeg"
        content_type = "image/jpeg"

        def __init__(self) -> None:
            self.stream = self

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return JPEG_BYTES + (b"x" * 10)

        def seek(self, _position: int) -> None:
            return None

    monkeypatch.setattr(receipt_module.frappe, "conf", {"kopos_manual_qr_receipt_max_bytes": 8}, raising=False)
    monkeypatch.setattr(
        receipt_module.frappe,
        "request",
        SimpleNamespace(files={"file": OversizedStream()}),
        raising=False,
    )

    with pytest.raises(receipt_module.frappe.ValidationError, match="maximum allowed size"):
        receipt_module.module.upload_manual_qr_receipt(**valid_payload())

    assert read_sizes == [9]


def test_wrong_device_is_rejected(receipt_module):
    payload = valid_payload(device_id="DEVICE-B", transaction_refno="TXN-B")

    with pytest.raises(receipt_module.frappe.ValidationError) as excinfo:
        receipt_module.module.upload_manual_qr_receipt(**payload)

    assert "device_id does not match authenticated" in str(excinfo.value)
    assert receipt_module.env.file_inserts == 0


def test_fractional_amount_sen_is_rejected(receipt_module):
    with pytest.raises(
        receipt_module.frappe.ValidationError,
        match="amount_sen must be an integer",
    ):
        receipt_module.module.upload_manual_qr_receipt(
            **valid_payload(amount_sen="1200.9")
        )

    assert receipt_module.env.file_inserts == 0


def test_duplicate_idempotent_upload_returns_existing_file(receipt_module):
    first = receipt_module.module.upload_manual_qr_receipt(**valid_payload())
    second = receipt_module.module.upload_manual_qr_receipt(**valid_payload())

    assert second == first
    assert receipt_module.env.file_inserts == 1


def test_non_jpeg_file_is_rejected(receipt_module, monkeypatch):
    rollbacks: list[bool] = []
    monkeypatch.setattr(
        receipt_module.frappe.db,
        "rollback",
        lambda: rollbacks.append(True),
        raising=False,
    )
    monkeypatch.setattr(
        receipt_module.frappe,
        "request",
        SimpleNamespace(files={"file": UploadedFile(b"not-jpeg", "image/png")}),
        raising=False,
    )

    with pytest.raises(receipt_module.frappe.ValidationError) as excinfo:
        receipt_module.module.upload_manual_qr_receipt(**valid_payload())

    assert "image/jpeg" in str(excinfo.value)
    assert receipt_module.env.file_inserts == 0
    assert rollbacks == [True]


def test_oversize_file_is_rejected(receipt_module, monkeypatch):
    monkeypatch.setattr(receipt_module.frappe, "conf", {"kopos_manual_qr_receipt_max_bytes": 8}, raising=False)

    with pytest.raises(receipt_module.frappe.ValidationError) as excinfo:
        receipt_module.module.upload_manual_qr_receipt(**valid_payload())

    assert "maximum allowed size" in str(excinfo.value)
    assert receipt_module.env.file_inserts == 0


def test_idempotency_key_reuse_against_different_transaction_is_rejected(receipt_module):
    receipt_module.module.upload_manual_qr_receipt(**valid_payload())

    reused_payload = valid_payload(transaction_refno="TXN-2")
    with pytest.raises(receipt_module.frappe.ValidationError) as excinfo:
        receipt_module.module.upload_manual_qr_receipt(**reused_payload)

    assert "idempotency_key was already used" in str(excinfo.value)
    assert receipt_module.env.file_inserts == 1


def test_device_role_cannot_browse_arbitrary_private_files(receipt_module, monkeypatch):
    private_file = SimpleNamespace(
        name="PRIVATE-FILE",
        doctype="File",
        file_name="other.jpg",
        is_private=1,
        attached_to_doctype="Sales Invoice",
        attached_to_name="SINV-OTHER",
    )

    monkeypatch.setattr(
        receipt_module.frappe,
        "has_permission",
        lambda doctype, ptype=None, doc=None: not (
            doctype == "File" and getattr(doc, "is_private", 0)
        ),
        raising=False,
    )

    assert not receipt_module.frappe.has_permission("File", "read", doc=private_file)

    result = receipt_module.module.upload_manual_qr_receipt(**valid_payload())
    attached_file = receipt_module.env.files[receipt_module.env.transactions["MBQR-TXN-1"].receipt_file]

    assert result["status"] == "ok"
    assert attached_file.is_private == 1
    assert attached_file.insert_ignore_permissions is True
    assert not receipt_module.frappe.has_permission("File", "read", doc=private_file)


def build_transaction(
    *, name: str, transaction_refno: str, device_id: str, amount_sen: int
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        transaction_refno=transaction_refno,
        device_id=device_id,
        sale_amount_sen=amount_sen,
        status="pending",
        pos_profile="POS-MAIN",
        maybank_qrpaybiz_account="Maybank QRPayBiz Account - Test",
        outlet_id="OUTLET-1",
        created_at=datetime(2026, 3, 13, 18, 4, 30),
        expires_at=datetime(2026, 3, 13, 18, 5, 30),
        receipt_file=None,
        receipt_idempotency_key=None,
        receipt_idempotency_fingerprint=None,
        receipt_payment_id=None,
        receipt_order_id=None,
        receipt_amount_sen=None,
        receipt_file_name=None,
        receipt_file_hash=None,
        receipt_captured_at=None,
        receipt_uploaded_at=None,
        manual_reconciliation_status="",
        fb_order_payment=None,
    )


def build_manual_reconciliation() -> SimpleNamespace:
    return SimpleNamespace(
        doctype="Manual QR Reconciliation",
        name="MQR-1",
        provider_session_id="static-session-1",
        device_id="DEVICE-A",
        amount_sen=1200,
        company="KoPOS Cafe",
        currency="MYR",
        business_date="2026-03-13",
        status="pending_reconciliation",
        evidence_captured_at=datetime(2026, 3, 13, 18, 5, 0),
        evidence_json=json.dumps(
            {"captured_at": "2026-03-13T18:05:00"},
            sort_keys=True,
            separators=(",", ":"),
        ),
        created_at=datetime(2026, 3, 13, 18, 5, 1),
        receipt_file=None,
        receipt_idempotency_key=None,
        receipt_idempotency_fingerprint=None,
        receipt_payment_id=None,
        receipt_order_id=None,
        receipt_amount_sen=None,
        receipt_file_name=None,
        receipt_file_hash=None,
        receipt_captured_at=None,
        receipt_uploaded_at=None,
    )


def valid_payload(**overrides: str) -> dict[str, str]:
    payload = {
        "device_id": "DEVICE-A",
        "payment_id": "PAY-1",
        "order_id": "ORDER-1",
        "transaction_refno": "TXN-1",
        "file_name": "receipt.jpg",
        "captured_at": "2026-03-13T18:05:00",
        "amount_sen": "1200",
        "currency": "MYR",
        "company": "KoPOS Cafe",
        "idempotency_key": "upload-key-1",
    }
    payload.update(overrides)
    return payload


class FakeFileDoc:
    def __init__(self, env: UploadEnv, payload: dict[str, Any]) -> None:
        self.env = env
        self.doctype = "File"
        self.file_name = payload["file_name"]
        self.attached_to_doctype = payload["attached_to_doctype"]
        self.attached_to_name = payload["attached_to_name"]
        self.is_private = payload["is_private"]
        self.content = payload["content"]
        self.name = f"FILE-{len(env.files) + 1}"
        self.file_url = f"/private/files/{self.file_name}"
        self.insert_ignore_permissions = False

    def insert(self, ignore_permissions: bool = False) -> None:
        if not ignore_permissions:
            raise AssertionError("receipt File must be inserted with ignore_permissions=True")
        self.insert_ignore_permissions = True
        self.env.file_inserts += 1
        self.env.files[self.name] = self


class FakeCommentDoc:
    def __init__(self, env: UploadEnv, payload: dict[str, Any]) -> None:
        self.env = env
        self.payload = payload

    def insert(self, ignore_permissions: bool = False) -> None:
        assert ignore_permissions is True
        self.env.comments.append(SimpleNamespace(**self.payload))
