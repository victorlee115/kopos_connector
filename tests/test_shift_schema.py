"""Schema-byte and canonical fixture parity; domain/legacy rules stay separate."""
import copy
import json
import os
from pathlib import Path

from jsonschema import Draft7Validator

from .fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()
from kopos_connector.api import shifts
from kopos_connector.api import devices


def test_shared_shift_schema_and_fixtures():
    pos = Path(os.environ.get("KOPOS_TYPESCRIPT_ROOT", Path(__file__).resolve().parents[2] / "JiJiPOS/kopos/src")).resolve().parents[1]
    packaged = Path(shifts.__file__).resolve().parents[1] / "contracts/shift.v1.schema.json"
    assert packaged.read_bytes() == (pos / "contracts/api/shift.v1.schema.json").read_bytes()
    schema = json.loads(packaged.read_text())
    Draft7Validator.check_schema(schema)
    for fixture in json.loads((pos / "contracts/fixtures/shift.v1.json").read_text()):
        before = copy.deepcopy(fixture["payload"])
        validator = Draft7Validator({**schema, "$ref": f"#/definitions/{fixture['schema']}"})
        assert validator.is_valid(fixture["payload"]) == fixture["valid"], fixture
        assert fixture["payload"] == before


def test_legacy_money_parser_still_normalizes_before_schema_validation():
    assert shifts._parse_non_negative_sen("100", "opening_float_sen") == 100
    assert shifts._parse_non_negative_sen(None, "opening_float_sen") == 0


def test_receipt_schema_is_the_exact_pos_contract():
    pos = Path(os.environ.get("KOPOS_TYPESCRIPT_ROOT", Path(__file__).resolve().parents[2] / "JiJiPOS/kopos/src")).resolve().parents[1]
    packaged = Path(devices.__file__).resolve().parents[1] / "contracts/receipt-header.v1.schema.json"
    assert packaged.read_bytes() == (pos / "contracts/api/receipt-header.v1.schema.json").read_bytes()
    Draft7Validator.check_schema(devices._RECEIPT_HEADER_VALIDATOR.schema)
