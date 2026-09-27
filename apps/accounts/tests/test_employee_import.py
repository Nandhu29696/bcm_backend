"""Employee bulk import: reference resolution and the estate cache.

No test coverage existed for `employee_import.py` before its lookup functions
were rewritten to cache each referenced table once per import instead of
re-querying (or, on a name match, re-scanning) it per row per column.
"""

import io

import pytest
from openpyxl import Workbook

from apps.accounts.employee_import import import_employees
from apps.accounts.models import Employee
from apps.organization.models import Estate, Region

pytestmark = pytest.mark.django_db


def _workbook(headers, rows):
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_resolves_reference_by_legacy_id_and_by_name():
    region = Region.objects.create(region_name="APAC", legacy_id=42)
    file_bytes = _workbook(
        ["EmpID", "Title", "Region_ID"],
        [
            ["E1", "By legacy id", "42"],
            ["E2", "By name", "apac"],  # case/whitespace-insensitive fallback
        ],
    )
    result = import_employees(file_bytes)
    assert result.errors == []
    assert Employee.objects.get(employee_number="E1").region_id == region.region_id
    assert Employee.objects.get(employee_number="E2").region_id == region.region_id


def test_unknown_reference_is_reported_not_silently_dropped():
    file_bytes = _workbook(["EmpID", "Title", "Region_ID"], [["E1", "Nobody", "Nowhere"]])
    result = import_employees(file_bytes)
    assert result.errors == [{"row": 2, "detail": "Unknown reference in Region_ID: Nowhere"}]
    assert not Employee.objects.filter(employee_number="E1").exists()


def test_a_new_estate_named_twice_in_one_file_is_created_once():
    """The estate cache is written back to on create — otherwise the second row
    would not see the first row's brand-new estate and would create another."""
    file_bytes = _workbook(
        ["EmpID", "Title", "Estate_Name"],
        [["E1", "First", "New Site"], ["E2", "Second", "new site"]],
    )
    result = import_employees(file_bytes)
    assert result.errors == []
    assert result.estates_created == 1
    estate = Estate.objects.get(estate_name="New Site")
    assert Employee.objects.get(employee_number="E1").estate_id == estate.estate_id
    assert Employee.objects.get(employee_number="E2").estate_id == estate.estate_id


def test_an_existing_estate_is_reused_not_recreated():
    estate = Estate.objects.create(estate_name="Existing Site")
    file_bytes = _workbook(["EmpID", "Title", "Estate_Name"], [["E1", "Someone", "existing site"]])
    result = import_employees(file_bytes)
    assert result.errors == []
    assert result.estates_created == 0
    assert Employee.objects.get(employee_number="E1").estate_id == estate.estate_id


def test_update_or_create_counts_by_employee_number():
    Employee.objects.create(employee_number="E1", full_name="Old Name")
    file_bytes = _workbook(
        ["EmpID", "Title"], [["E1", "New Name"], ["E2", "Brand New"]]
    )
    result = import_employees(file_bytes)
    assert result.errors == []
    assert result.created == 1
    assert result.updated == 1
    assert Employee.objects.get(employee_number="E1").full_name == "New Name"
