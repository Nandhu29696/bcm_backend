"""
Performance budgets for the cost code list (Phase 2.5).

Two kinds of test live here.

The query-count budgets run on every CI build. They are the durable guard: an
N+1 or a lost `select_related` fails them immediately, regardless of machine.

The latency budget is marked `load` and skipped by default. It needs tens of
thousands of rows, which takes minutes to build, and wall-clock assertions are
machine-dependent. Run it deliberately:

    python manage.py generate_load_data            # once, against the dev database
    pytest -m load --reuse-db

A green run on a small fixture proves nothing about a 50,000-row estate, so the
query-count tests are written to fail for structural reasons — a query whose cost
is per-row rather than per-page — instead of relying on the clock.
"""

import time

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.organization.models import CostCode
from apps.organization.querysets import CURRENT_STATUS, cost_code_list_queryset
from apps.organization.tests.conftest import rows
from apps.plans.models import Plan, PlanStatus, PlanVersion

pytestmark = pytest.mark.django_db


def cost_code_url(estate):
    return reverse("organization:estate-cost-codes", args=[estate.estate_id])


def bulk_populate(estate, org, *, count: int, versions: int = 3):
    """Cost codes with full plan histories, created without per-row overhead."""
    CostCode.objects.bulk_create(
        [
            CostCode(
                cost_code=f"CC-P-{index:05d}",
                estate=estate,
                process=org["support"],
                subprocess=org["tier1"],
                region=org["north"],
                bu_lead=org["priya"],
            )
            for index in range(count)
        ]
    )
    created = list(CostCode.objects.filter(estate=estate, cost_code__startswith="CC-P-"))
    Plan.objects.bulk_create(
        [Plan(cost_code=cost_code, process=org["support"]) for cost_code in created]
    )
    plan_ids = list(Plan.objects.filter(cost_code__in=created).values_list("plan_id", flat=True))
    PlanVersion.objects.bulk_create(
        [
            PlanVersion(
                plan_id=plan_id,
                version_number=number,
                status=(PlanStatus.APPROVED if number < versions else PlanStatus.WORK_IN_PROGRESS),
            )
            for plan_id in plan_ids
            for number in range(1, versions + 1)
        ]
    )
    return created


# --------------------------------------------------------------------------- #
# Structural budgets — always run
# --------------------------------------------------------------------------- #


def test_page_cost_is_independent_of_estate_size(alpha_client, org):
    """The defining property: page 1 of 500 rows costs what page 1 of 5 rows costs.

    If the status resolution were a Python loop, or a `prefetch_related` over plan
    versions, this would fail — the second figure would climb with the data.
    """
    url = cost_code_url(org["alpha"])

    # `force_authenticate` reuses one user object across requests, and role codes
    # are cached on it, so a first request costs one query more than every
    # subsequent one. Warm that up before measuring, or the comparison below
    # measures the cache rather than the data volume.
    alpha_client.get(url)

    with CaptureQueriesContext(connection) as small:
        alpha_client.get(url, {"page_size": 25})

    bulk_populate(org["alpha"], org, count=500)

    with CaptureQueriesContext(connection) as large:
        response = alpha_client.get(url, {"page_size": 25})

    assert response.data["count"] == 504
    assert len(large) == len(small)


def test_filtering_by_status_does_not_add_queries(alpha_client, org):
    """Status is a database annotation, so filtering on it is free query-wise."""
    url = cost_code_url(org["alpha"])
    bulk_populate(org["alpha"], org, count=200)
    alpha_client.get(url)  # warm the cached role codes; see the test above

    with CaptureQueriesContext(connection) as unfiltered:
        alpha_client.get(url)
    with CaptureQueriesContext(connection) as filtered:
        alpha_client.get(url, {"bcp_status": PlanStatus.APPROVED})

    assert len(filtered) == len(unfiltered)


def test_serialising_a_page_touches_no_extra_tables(alpha_client, org):
    """Every FK the row serializer reads must be in `select_related`.

    A missing one is invisible in the response and shows up only as a query per
    row per column — the classic way a list endpoint degrades after a new column
    is added to the table.
    """
    bulk_populate(org["alpha"], org, count=50)

    with CaptureQueriesContext(connection) as ctx:
        response = alpha_client.get(cost_code_url(org["alpha"]), {"page_size": 50})

    assert len(rows(response)) == 50
    select_queries = [q["sql"] for q in ctx.captured_queries if q["sql"].startswith("SELECT")]
    for table in ("`processes`", "`subprocesses`", "`bu_leads`", "`regions`"):
        # Each may appear in the single page query's joins, never as its own query.
        standalone = [q for q in select_queries if q.startswith(f"SELECT {table}")]
        assert standalone == [], f"{table} is being fetched per row"


def test_ordering_by_status_is_pushed_to_the_database(alpha_client, org):
    """A Python sort would silently order only the current page."""
    bulk_populate(org["alpha"], org, count=60)
    queryset = cost_code_list_queryset(CostCode.objects.filter(estate=org["alpha"]))
    sql = str(queryset.order_by(CURRENT_STATUS).query)
    assert "ORDER BY" in sql


def test_the_status_index_is_present():
    """`idx_pv_plan_version` is what makes the correlated subquery an index seek.

    Without it the subquery scans plan_versions once per row of every page, and
    the cost code list degrades from milliseconds to seconds on a real estate.
    """
    with connection.cursor() as cursor:
        cursor.execute("SHOW INDEX FROM plan_versions")
        index_names = {row[2] for row in cursor.fetchall()}
    assert "idx_pv_plan_version" in index_names


def test_the_cost_code_list_indexes_are_present():
    with connection.cursor() as cursor:
        cursor.execute("SHOW INDEX FROM cost_codes")
        index_names = {row[2] for row in cursor.fetchall()}
    for expected in (
        "idx_cc_estate_active_code",
        "idx_cc_estate_process",
        "idx_cc_estate_bulead",
        "idx_cc_estate_subproc",
        "idx_cc_estate_region",
    ):
        assert expected in index_names, f"missing {expected}"


# --------------------------------------------------------------------------- #
# Latency budget — opt in with `-m load`
# --------------------------------------------------------------------------- #


@pytest.mark.load
def test_latency_budget_on_a_large_estate(alpha_client, org):
    """A filtered page must return in well under a second at scale.

    5,000 cost codes and 15,000 plan versions here rather than the full 50,000 the
    generator produces: enough for the index behaviour to dominate, few enough to
    build inside a test. Use `generate_load_data` for the full-size profile.
    """
    bulk_populate(org["alpha"], org, count=5_000)
    url = cost_code_url(org["alpha"])

    # Warm the connection and the query plan cache.
    alpha_client.get(url)

    started = time.perf_counter()
    response = alpha_client.get(url, {"bcp_status": PlanStatus.WORK_IN_PROGRESS, "page_size": 25})
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 1.5, f"cost code page took {elapsed:.2f}s"
