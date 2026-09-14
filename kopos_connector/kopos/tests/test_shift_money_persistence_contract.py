"""Cash dashboard SELECTs against real MariaDB, with rollback-only fixtures.

Direct fixture inserts preserve the installed schema's actual money types.
This tests the read boundary, not shift creation or accounting lifecycle.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import json

import frappe
from frappe.tests.utils import FrappeTestCase
from jsonschema import Draft7Validator

from kopos_connector.api.shift_money import shift_money_overview
from kopos_connector.api.shift_money import get_shift_money
from kopos_connector.api.director_home import get_director_home
from kopos_connector.api.shifts import get_device_open_shift_payload


class TestShiftMoneyPersistenceContract(FrappeTestCase):
    def setUp(self):
        self.company = frappe.get_all("Company", pluck="name", limit=1)[0]
        self.prefix = "CASH-READ-" + frappe.generate_hash(length=12)
        self.baseline = shift_money_overview(self.company)["counts"]

    def tearDown(self):
        frappe.db.rollback()

    def insert_shift(self, index, variance=Decimal("0"), *, counted=Decimal("100"), status="Closed"):
        name = f"{self.prefix}-{index}"
        closed_at = datetime(2099, 1, 1) + timedelta(minutes=index)
        frappe.db.sql(
            """INSERT INTO `tabFB Shift`
            (name, shift_code, device_id, staff_id, company, status,
             opened_at, closed_at, expected_cash, counted_cash, cash_variance)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (name, name, self.prefix, "Administrator", self.company, status,
             closed_at, closed_at, Decimal("100"), counted, variance),
        )

    def test_older_shortage_is_not_hidden_by_sixty_matches(self):
        self.insert_shift(0, Decimal("-20"), counted=Decimal("80"))
        for index in range(1, 61):
            self.insert_shift(index)
        result = shift_money_overview(self.company)
        self.assertEqual(result["counts"]["matched"], self.baseline["matched"] + 60)
        self.assertEqual(result["counts"]["needs_review"], self.baseline["needs_review"] + 1)
        self.assertEqual(result["needs_review"][0]["variance_sen"], -2000)

    def test_pages_keep_complete_counts_and_decimal_sen(self):
        for index in range(120):
            self.insert_shift(index, Decimal("-0.01"), counted=Decimal("99.99"))
        pages = [shift_money_overview(self.company, offset=offset) for offset in (0, 50, 100)]
        for page in pages:
            self.assertEqual(page["counts"]["needs_review"], self.baseline["needs_review"] + 120)
            self.assertLessEqual(len(page["needs_review"]), 50)
        self.assertEqual([page["next_offset"] for page in pages[:2]], [50, 100])
        fixture_rows = [row for page in pages for row in page["needs_review"] if row["closed_at"].startswith("2099-")]
        self.assertEqual(len({row["closed_at"] for row in fixture_rows}), 120)
        self.assertTrue(all(row["variance_sen"] == -1 for row in fixture_rows))
        money = get_shift_money(company=self.company, offset=50)
        self.assertEqual(money["summary_scope"], "all_closed_shifts")
        self.assertEqual(money["offset"], 50)
        self.assertEqual(money["next_offset"], 100)
        home = get_director_home(company=self.company)
        self.assertNotIn("money", home["unavailable_areas"])
        tile = next(tile for tile in home["tiles"] if tile["key"] == "money")
        self.assertEqual(tile["value"], str(self.baseline["needs_review"] + self.baseline["missing_count"] + 120))
        self.assertTrue(tile["attention"])

    def test_zero_cash_is_a_real_count_and_open_shifts_are_excluded(self):
        # Frappe Currency columns here are NOT NULL. Do not alter the schema
        # to manufacture the legacy-null condition covered by unit fixtures.
        self.insert_shift(0, Decimal("-100"), counted=Decimal("0"))
        self.insert_shift(2, status="Open")
        result = shift_money_overview(self.company)
        self.assertEqual(result["counts"]["matched"], self.baseline["matched"])
        self.assertEqual(result["counts"]["missing_count"], self.baseline["missing_count"])
        self.assertEqual(result["counts"]["needs_review"], self.baseline["needs_review"] + 1)
        self.assertEqual(result["needs_review"][0]["counted_sen"], 0)
        self.assertEqual(result["needs_review"][0]["variance_sen"], -10000)

    def test_native_open_shift_currency_reads_match_the_shared_response_contract(self):
        # Reuse the test site's real device, with a transaction-local shift.
        # Existing rows and device configuration are never deleted or rewritten.
        device = frappe.get_all("KoPOS Device", fields=["device_id"], limit=1)[0]["device_id"]
        self.assertTrue(device)
        self.insert_shift(10, status="Open")
        name = f"{self.prefix}-10"
        frappe.db.sql(
            "UPDATE `tabFB Shift` SET device_id=%s, opening_float=%s, creation=%s WHERE name=%s",
            (device, Decimal("99999999999.91"), datetime(2099, 1, 1), name),
        )
        response = {"status": "ok", "shift": get_device_open_shift_payload(device)}
        self.assertEqual(response["shift"]["fb_shift"], name)
        self.assertEqual(response["shift"]["opening_float_sen"], 9999999999991)
        schema_path = Path(__file__).resolve().parents[2] / "contracts/shift.v1.schema.json"
        schema = json.loads(schema_path.read_text())
        Draft7Validator({**schema, "$ref": "#/definitions/GetDeviceOpenShiftApiResponse"}).validate(response)
