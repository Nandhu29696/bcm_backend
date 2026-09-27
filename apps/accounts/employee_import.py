"""Bulk employee import from the legacy Excel export."""

from __future__ import annotations

import datetime as dt
import io
import re
from dataclasses import dataclass
from typing import Generic, TypedDict, TypeVar

from django.db import models, transaction
from openpyxl import load_workbook

from apps.accounts.models import Employee
from apps.organization.models import (
    BuClassification,
    BuLead,
    Center,
    CostCode,
    EmployeeGrade,
    EmployeeGroup,
    Estate,
    Lob,
    Location,
    Process,
    Region,
    Subprocess,
)


MODEL_BY_COLUMN = {
    "BU_Lead_ID": (BuLead, "bu_lead_id"),
    "BUClassification_ID": (BuClassification, "bu_classification_id"),
    "Center_ID": (Center, "center_id"),
    "Process_ID": (Process, "process_id"),
    "Costcode_ID": (CostCode, "cost_code_id"),
    "CurrentLocation_ID": (Location, "location_id"),
    "EmployeeGroup_ID": (EmployeeGroup, "employee_group_id"),
    "Grade": (EmployeeGrade, "employee_grade_id"),
    "LOB_ID": (Lob, "lob_id"),
    "Location_ID": (Location, "location_id"),
    "Region_ID": (Region, "region_id"),
    "Subprocess_ID": (Subprocess, "subprocess_id"),
}

#: The name field each referenced model is matched on when a row gives a name
#: instead of (or in addition to failing) a legacy ID. Keyed by model so the
#: cache builder below only has to query each table once.
LOOKUP_FIELD_BY_MODEL = {
    BuLead: "lead_name",
    BuClassification: "classification_name",
    Center: "center_name",
    CostCode: "cost_code",
    EmployeeGrade: "grade_name",
    EmployeeGroup: "group_name",
    Lob: "lob_name",
    Location: "location_name",
    Process: "process_name",
    Region: "region_name",
    Subprocess: "subprocess_name",
}

RELATION_BY_COLUMN = {
    "BU_Lead_ID": "bu_lead",
    "BUClassification_ID": "bu_classification",
    "Center_ID": "center",
    "Process_ID": "process",
    "Costcode_ID": "cost_code",
    "CurrentLocation_ID": "current_location",
    "EmployeeGroup_ID": "employee_group",
    "Grade": "employee_grade",
    "LOB_ID": "lob",
    "Location_ID": "location",
    "Region_ID": "region",
    "Subprocess_ID": "subprocess",
}

#: A synchronous, single-request import: this bounds how long one HTTP call
#: can run for, independent of the file-size cap (a narrow sheet can still
#: have an enormous number of rows).
MAX_IMPORT_ROWS = 20_000


class TooManyRowsError(ValueError):
    pass


FIELD_BY_COLUMN = {
    "EmpID": "employee_number",
    "Title": "full_name",
    "Designation": "designation",
    "Domain_ID": "domain_name",
    "EmailID": "email",
    "Gender": "gender",
    "Contact_NO": "contact_number",
    "Emp_Status": "employment_status",
    "LWD": "last_working_date",
    "Date_of_Joining": "date_of_joining",
    "ID": "legacy_row_id",
}

TEMPLATE_HEADERS = [*FIELD_BY_COLUMN.keys(), *MODEL_BY_COLUMN.keys(), "Estate", "Estate_Name", "EstateID", "Manager_EmpID", "Supervisor_EmpID"]


@dataclass
class ImportResult:
    created: int
    updated: int
    estates_created: int
    errors: list[dict[str, object]]


class _PendingItem(TypedDict):
    employee_number: str
    fields: dict[str, object]
    manager_number: str
    supervisor_number: str
    estate_created: bool


def _clean(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _number(value: object) -> int:
    return int(float(_clean(value).replace(",", "")))


def _date(value: object) -> dt.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = _clean(value)
    for pattern in ("%m/%d/%Y %I:%M %p", "%d-%m-%Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date: {text}")


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


_T = TypeVar("_T", bound=models.Model)


@dataclass
class _ReferenceCache(Generic[_T]):
    """One model's rows, keyed both ways a row can name them.

    Built once per import instead of per row: with ~11 referenced columns,
    re-querying (or worse, re-scanning) the whole table on every row turns an
    import into an O(rows x columns) full-table-scan loop.
    """

    by_legacy_id: dict[int, _T]
    by_name: dict[str, _T]


def _build_reference_caches() -> dict[type, _ReferenceCache[models.Model]]:
    caches: dict[type, _ReferenceCache[models.Model]] = {}
    for model, lookup_field in LOOKUP_FIELD_BY_MODEL.items():
        by_legacy_id: dict[int, models.Model] = {}
        by_name: dict[str, models.Model] = {}
        for item in model.objects.all():
            if item.legacy_id is not None:
                by_legacy_id[item.legacy_id] = item
            by_name[_normalize(str(getattr(item, lookup_field)))] = item
        caches[model] = _ReferenceCache(by_legacy_id, by_name)
    return caches


def _build_estate_cache() -> _ReferenceCache[Estate]:
    by_legacy_id: dict[int, Estate] = {}
    by_name: dict[str, Estate] = {}
    # Ordered so that, if duplicate legacy IDs exist, the first one wins — the
    # same choice `.filter(legacy_id=...).first()` made before this was cached.
    for item in Estate.all_objects.order_by("estate_id"):
        if item.legacy_id is not None:
            by_legacy_id.setdefault(item.legacy_id, item)
        by_name[_normalize(item.estate_name)] = item
    return _ReferenceCache(by_legacy_id, by_name)


def _resolve_reference(
    column: str, value: object, caches: dict[type, _ReferenceCache[models.Model]]
) -> models.Model | None:
    text = _clean(value)
    if not text:
        return None
    model, _pk_name = MODEL_BY_COLUMN[column]
    cache = caches[model]
    try:
        legacy_id = _number(text)
    except (TypeError, ValueError):
        legacy_id = None
    if legacy_id is not None and legacy_id in cache.by_legacy_id:
        return cache.by_legacy_id[legacy_id]
    return cache.by_name.get(_normalize(text))


def _resolve_estate(
    value: object, cache: _ReferenceCache[Estate], *, allow_create: bool = True
) -> tuple[Estate | None, bool]:
    text = _clean(value)
    if not text:
        return None, False
    estate = cache.by_name.get(_normalize(text))
    if estate is None:
        try:
            legacy_id = _number(text)
        except (TypeError, ValueError):
            pass
        else:
            estate = cache.by_legacy_id.get(legacy_id)
    if estate:
        if not estate.active_flag:
            estate.restore()
        return estate, False
    try:
        _number(text)
    except (TypeError, ValueError):
        pass
    else:
        return None, False
    if not allow_create:
        return None, False
    estate = Estate.objects.create(estate_name=text)
    # So a later row naming this same brand-new estate reuses it instead of
    # creating a duplicate.
    cache.by_name[_normalize(text)] = estate
    return estate, True


def _header_map(headers: list[object]) -> dict[str, int]:
    return {_clean(header): index for index, header in enumerate(headers) if _clean(header)}


@transaction.atomic
def import_employees(file_bytes: bytes) -> ImportResult:
    workbook = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    sheet = workbook.active
    rows = sheet.iter_rows(values_only=True)
    try:
        headers = _header_map(list(next(rows)))
    except StopIteration:
        return ImportResult(0, 0, 0, [{"row": 1, "detail": "The workbook is empty."}])

    missing = [column for column in ("EmpID", "Title") if column not in headers]
    if missing:
        return ImportResult(0, 0, 0, [{"row": 1, "detail": f"Missing required column(s): {', '.join(missing)}."}])

    row_count = (sheet.max_row or 1) - 1
    if row_count > MAX_IMPORT_ROWS:
        raise TooManyRowsError(
            f"The workbook has {row_count} rows; split it into batches of {MAX_IMPORT_ROWS} or fewer."
        )

    created = updated = estates_created = 0
    errors: list[dict[str, object]] = []
    pending: list[tuple[int, _PendingItem]] = []
    reference_caches = _build_reference_caches()
    estate_cache = _build_estate_cache()
    for row_number, values in enumerate(rows, start=2):
        raw = {column: values[index] if index < len(values) else None for column, index in headers.items()}
        if not any(value not in (None, "") for value in raw.values()):
            continue
        try:
            employee_number = _clean(raw.get("EmpID"))
            if not employee_number:
                raise ValueError("EmpID is required")
            fields: dict[str, object] = {
                field: _clean(raw[column]) for column, field in FIELD_BY_COLUMN.items() if column in raw
            }
            fields["legacy_row_id"] = _number(raw["ID"]) if _clean(raw.get("ID")) else None
            fields["date_of_joining"] = _date(raw.get("Date_of_Joining"))
            fields["last_working_date"] = _date(raw.get("LWD"))
            for column in MODEL_BY_COLUMN:
                if column in raw:
                    relation_field = RELATION_BY_COLUMN[column]
                    fields[relation_field] = _resolve_reference(column, raw[column], reference_caches)
                    if _clean(raw[column]) and fields[relation_field] is None:
                        raise ValueError(f"Unknown reference in {column}: {_clean(raw[column])}")
            estate_name = raw.get("Estate") or raw.get("Estate_Name")
            estate_id = raw.get("EstateID")
            estate, estate_created = (
                _resolve_estate(estate_name, estate_cache, allow_create=True)
                if _clean(estate_name)
                else _resolve_estate(estate_id, estate_cache, allow_create=False)
            )
            if _clean(estate_name) or _clean(estate_id):
                if estate is None:
                    raise ValueError(f"Unknown estate: {_clean(estate_name or estate_id)}")
            fields["estate"] = estate
            manager_number = _clean(raw.get("Manager_EmpID"))
            supervisor_number = _clean(raw.get("Supervisor_EmpID"))
            pending.append((row_number, {"employee_number": employee_number, "fields": fields, "manager_number": manager_number, "supervisor_number": supervisor_number, "estate_created": estate_created}))
        except (TypeError, ValueError) as exc:
            errors.append({"row": row_number, "detail": str(exc)})

    employees_by_number: dict[str, Employee] = {}
    for row_number, item in pending:
        employee, was_created = Employee.objects.update_or_create(
            employee_number=item["employee_number"], defaults=item["fields"]
        )
        employees_by_number[employee.employee_number] = employee
        created += was_created
        updated += not was_created
        estates_created += item["estate_created"]

    for _, item in pending:
        employee = employees_by_number[item["employee_number"]]
        if item["manager_number"]:
            employee.manager_employee = employees_by_number.get(item["manager_number"]) or Employee.objects.filter(employee_number=item["manager_number"]).first()
        if item["supervisor_number"]:
            employee.supervisor_employee = employees_by_number.get(item["supervisor_number"]) or Employee.objects.filter(employee_number=item["supervisor_number"]).first()
        employee.save(update_fields=["manager_employee", "supervisor_employee", "updated_at"])

    return ImportResult(created, updated, estates_created, errors)