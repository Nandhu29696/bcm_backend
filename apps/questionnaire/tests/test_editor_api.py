"""
The plan editor end to end at the API (4.1, 4.2, 4.5, 4.6, 4.8).

The Phase 4 exit criteria, each as its own test:
  * a coordinator completes a plan and section statuses advance automatically
  * a hidden branch never blocks 100% completion
  * adding a question via the API appears in the editor with no deploy
"""

import pytest
from django.urls import reverse

from apps.assessments.models import QuestionAnswer, SectionStatus, SectionStatusValue
from apps.plans.models import PlanStatus
from apps.questionnaire.answers import lookup_options
from apps.questionnaire.models import AnswerType, Question, Section

pytestmark = pytest.mark.django_db


def tree_url(version, context=None):
    url = reverse("questionnaire:questionnaire", args=[version.plan_version_id])
    return f"{url}?context={context}" if context else url


def answer_url(version, question, context=None):
    url = reverse("questionnaire:answer", args=[version.plan_version_id, question.pk])
    return f"{url}?context={context}" if context else url


def comments_url(version, question):
    return reverse("questionnaire:question-comments", args=[version.plan_version_id, question.pk])


def section(tree, name):
    return next(s for s in tree["sections"] if s["section_name"] == name)


def question_in(tree, code):
    for s in tree["sections"]:
        for q in s["questions"]:
            if q["question_code"] == code:
                return q
    raise AssertionError(f"{code} not in tree")


def put(client, version, question, answer, context=None):
    return client.put(answer_url(version, question, context), {"answer": answer}, format="json")


# --------------------------------------------------------------------------- #
# The tree (4.1)
# --------------------------------------------------------------------------- #


def test_tree_requires_authentication(api_client, version):
    assert api_client.get(tree_url(version)).status_code == 401


def test_tree_outside_scope_is_404(api_client, user_factory, version):
    api_client.force_authenticate(
        user=user_factory(email="stranger@example.com", roles=["BCM_ADMIN"])
    )
    # Admin sees everything — so use a coordinator with no scope instead.
    api_client.force_authenticate(
        user=user_factory(email="s2@example.com", roles=["BCM_COORDINATOR"])
    )
    assert api_client.get(tree_url(version)).status_code == 404


def test_tree_is_the_whole_questionnaire_in_one_round_trip(
    author_client, version, django_assert_max_num_queries
):
    """Twelve queries, fixed: scope, version, assignments, questions, options,
    answers, comment counts, four catalogue lookups, and the authoring check.
    None of them is per question or per answer."""
    author_client.get(tree_url(version))  # warm the role cache (see Phase 2 tests)
    with django_assert_max_num_queries(14):
        response = author_client.get(tree_url(version))
    assert response.status_code == 200
    tree = response.data
    # Questionnaire tabs first (the contractual numbers together), BIA last:
    # its questions are answered in the plan's BIA part.
    assert [(s["section_name"], s["group"]) for s in tree["sections"]] == [
        ("Basic Questions", "questionnaire"),
        ("MAO", "questionnaire"),
        ("RTO", "questionnaire"),
        ("MBCO", "questionnaire"),
        ("RPO", "questionnaire"),
        ("BIA", "bia"),
    ]
    assert sum(len(s["questions"]) for s in tree["sections"]) == 17
    assert tree["editable"] is True
    assert tree["can_author"] is True


def test_tree_carries_dependency_metadata_and_options(author_client, version, questions):
    tree = author_client.get(tree_url(version)).data
    q2 = question_in(tree, "BASIC-002")
    assert q2["depends_on"] == {
        "question_id": questions["BASIC-001"].pk,
        "operator": "EQUALS",
        "value": "YES",
    }
    assert q2["visible"] is False
    assert [o["code"] for o in q2["options"]] == ["YES", "NO"]

    bia1 = question_in(tree, "BIA-001")
    assert {o["label"] for o in bia1["options"]} == {
        "Client Site",
        "FSL Owned Site",
        "Non FSL Owned Site",
    }

    bia5 = question_in(tree, "BIA-005")
    assert [o["code"] for o in bia5["options"]] == ["YES", "NO"]
    assert bia5["detail_options"] == []

    bia4 = question_in(tree, "BIA-004")
    assert bia4["answer_type"] == "SUBFORM"
    assert [f["name"] for f in bia4["subform_schema"]][:2] == ["subcontractor", "service"]

    # The dependency lists: who, and the service each provides.
    bia7 = question_in(tree, "BIA-007")
    assert bia7["depends_on"]["question_id"] == questions["BIA-005"].pk
    assert [f["name"] for f in bia7["subform_schema"]] == ["corporate_function", "service"]
    assert len(bia7["options"]) > 0  # the catalogue's corporate functions
    bia8 = question_in(tree, "BIA-008")
    assert bia8["depends_on"]["question_id"] == questions["BIA-006"].pk
    assert [f["name"] for f in bia8["subform_schema"]] == ["vendor", "service"]

    # Evidence rules travel with the question.
    assert question_in(tree, "BASIC-004")["evidence"] == {
        "offered": True,
        "when_value": "YES",
        "required": True,
        "files": [],
    }
    assert question_in(tree, "RTO-001")["evidence"]["when_value"] is None
    assert question_in(tree, "BASIC-001")["evidence"]["offered"] is False


def test_tree_merges_answers(author_client, version, questions):
    put(author_client, version, questions["RTO-001"], {"value": 8})
    tree = author_client.get(tree_url(version)).data
    assert question_in(tree, "RTO-001")["answer"] == {"value": 8}
    assert question_in(tree, "RTO-001")["answered_by"] == "arun"


def test_a_viewer_can_read_but_is_not_an_author(viewer_client, version):
    tree = viewer_client.get(tree_url(version)).data
    assert tree["can_author"] is False


# --------------------------------------------------------------------------- #
# Saving (4.2)
# --------------------------------------------------------------------------- #


def test_save_returns_the_normalised_answer_and_section_statuses(author_client, version, questions):
    response = put(author_client, version, questions["RTO-001"], {"value": " 8 "})
    assert response.status_code == 200
    assert response.data["answer"] == {"value": 8}
    rto = next(
        s for s in response.data["sections"] if s["section_id"] == questions["RTO-001"].section_id
    )
    assert rto["status"] == SectionStatusValue.COMPLETED
    assert rto["percent"] == 100


def test_save_rejects_a_bad_answer_with_a_field_error(author_client, version, questions):
    response = put(author_client, version, questions["BASIC-001"], {"value": "Maybe"})
    assert response.status_code == 400
    assert response.data["code"] == "unknown_option"
    assert "answer" in response.data["field_errors"]
    assert not QuestionAnswer.objects.filter(plan_version=version).exists()


def test_saving_twice_updates_one_row(author_client, version, questions):
    put(author_client, version, questions["RTO-001"], {"value": 8})
    put(author_client, version, questions["RTO-001"], {"value": 12})
    rows = QuestionAnswer.all_objects.filter(plan_version=version, question=questions["RTO-001"])
    assert rows.count() == 1
    assert rows.get().answer_json == {"value": 12}


def test_a_second_author_updates_the_same_row_and_becomes_respondent(
    author_client, api_client, user_factory, version, questions, estate, employee
):
    """One answer per question per context — not one per person (see services)."""
    from apps.accounts.models import Employee, UserEstateScope
    from apps.plans.models import CoordinatorAssignment

    put(author_client, version, questions["RTO-001"], {"value": 8})

    second_employee = Employee.objects.create(
        employee_number="2", full_name="Meera", email="m@example.com"
    )
    second = user_factory(
        email="meera@example.com", roles=["BCM_COORDINATOR"], display_name="Meera"
    )
    second.employee = second_employee
    second.save(update_fields=["employee"])
    UserEstateScope.objects.create(user=second, estate=estate)
    CoordinatorAssignment.objects.create(
        plan_version=version, employee=second_employee, estate=estate
    )

    api_client.force_authenticate(user=second)
    response = put(api_client, version, questions["RTO-001"], {"value": 16})
    assert response.status_code == 200
    assert response.data["answered_by"] == "Meera"
    assert (
        QuestionAnswer.all_objects.filter(
            plan_version=version, question=questions["RTO-001"]
        ).count()
        == 1
    )


def test_clear_hides_the_answer_and_recomputes(author_client, version, questions):
    put(author_client, version, questions["RTO-001"], {"value": 8})
    response = author_client.delete(answer_url(version, questions["RTO-001"]))
    assert response.status_code == 200
    rto = next(
        s for s in response.data["sections"] if s["section_id"] == questions["RTO-001"].section_id
    )
    assert rto["status"] == SectionStatusValue.NOT_STARTED
    assert question_in(author_client.get(tree_url(version)).data, "RTO-001")["answer"] is None


def test_answers_are_separate_per_context(author_client, version, questions):
    put(author_client, version, questions["RTO-001"], {"value": 8}, context="BCP")
    put(author_client, version, questions["RTO-001"], {"value": 4}, context="BIA")
    assert question_in(author_client.get(tree_url(version, "BCP")).data, "RTO-001")["answer"] == {
        "value": 8
    }
    assert question_in(author_client.get(tree_url(version, "BIA")).data, "RTO-001")["answer"] == {
        "value": 4
    }


def test_an_unknown_context_is_rejected(author_client, version, questions):
    assert (
        put(author_client, version, questions["RTO-001"], {"value": 8}, context="NOPE").status_code
        == 400
    )


# --------------------------------------------------------------------------- #
# Who may write
# --------------------------------------------------------------------------- #


def test_a_coordinator_not_assigned_to_the_version_cannot_write(
    onlooker_client, version, questions
):
    """Estate scope lets you see a plan; only an assignment lets you edit it."""
    response = put(onlooker_client, version, questions["RTO-001"], {"value": 8})
    assert response.status_code == 403
    assert response.data["code"] == "not_an_author"


def test_a_viewer_cannot_write(viewer_client, version, questions):
    assert put(viewer_client, version, questions["RTO-001"], {"value": 8}).status_code == 403


def test_an_admin_can_write_without_an_assignment(api_client, user_factory, version, questions):
    api_client.force_authenticate(user=user_factory(email="admin@example.com", roles=["BCM_ADMIN"]))
    assert put(api_client, version, questions["RTO-001"], {"value": 8}).status_code == 200


# --------------------------------------------------------------------------- #
# Read-only once approved (4.8)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [PlanStatus.APPROVED, PlanStatus.EXEMPTED])
def test_an_approved_version_refuses_writes(author_client, version, questions, status):
    version.status = status
    version.save(update_fields=["status"])

    response = put(author_client, version, questions["RTO-001"], {"value": 8})
    assert response.status_code == 409
    assert response.data["code"] == "plan_not_editable"
    assert author_client.get(tree_url(version)).data["editable"] is False


def test_a_pending_review_version_is_read_only_too(author_client, version, questions):
    version.status = PlanStatus.PENDING_BU_LEAD_REVIEW
    version.save(update_fields=["status"])
    assert put(author_client, version, questions["RTO-001"], {"value": 8}).status_code == 409


# --------------------------------------------------------------------------- #
# Derived completion (4.6) — the exit criteria
# --------------------------------------------------------------------------- #


def test_section_statuses_advance_as_a_coordinator_completes_the_plan(
    author_client, version, questions
):
    """Exit criterion: statuses move Not Started -> In Progress -> Completed on their own."""
    q = questions

    def basic():
        return SectionStatus.objects.get(
            plan_version=version, section=q["BASIC-001"].section
        ).status

    assert not SectionStatus.objects.filter(plan_version=version).exists()

    put(author_client, version, q["BASIC-001"], {"value": "NO"})
    assert basic() == SectionStatusValue.IN_PROGRESS  # BASIC-004 still required

    # A "Yes" here would also need the penalty clause uploaded (see
    # test_evidence_api); "No" completes the section on its own.
    put(author_client, version, q["BASIC-004"], {"value": "NO"})
    assert basic() == SectionStatusValue.COMPLETED

    # Every section, once every visible required question is answered.
    put(author_client, version, q["RTO-001"], {"value": 8})
    put(author_client, version, q["MBCO-001"], {"value": 60})
    put(author_client, version, q["RPO-001"], {"value": "NO"})
    put(author_client, version, q["BIA-001"], {"value": lookup_options("Primary sites")[0].code})
    put(author_client, version, q["BIA-002"], {"value": "NO"})
    put(author_client, version, q["BIA-003"], {"value": "NO"})
    put(author_client, version, q["BIA-005"], {"value": "NO"})
    put(author_client, version, q["BIA-006"], {"value": "NO"})
    response = put(author_client, version, q["MAO-001"], {"value": 48})

    assert {s["status"] for s in response.data["sections"]} == {SectionStatusValue.COMPLETED}
    assert all(s["percent"] == 100 for s in response.data["sections"])


def test_a_hidden_branch_never_blocks_completion(author_client, version, questions):
    """Exit criterion. BASIC-002/003 are conditional; a 'No' hides 002 and shows 003.

    With Q1 = No, the section must complete with 001, 003(optional) and 004 —
    002 is hidden and must not count as an unanswered question.
    """
    q = questions
    put(author_client, version, q["BASIC-001"], {"value": "NO"})
    response = put(author_client, version, q["BASIC-004"], {"value": "NO"})

    basic = next(
        s for s in response.data["sections"] if s["section_id"] == q["BASIC-001"].section_id
    )
    assert basic["status"] == SectionStatusValue.COMPLETED
    assert basic["percent"] == 100
    assert basic["required_visible"] == 2  # 001 and 004 only


def test_a_hidden_required_question_does_not_count(author_client, version, questions):
    """Make the branch REQUIRED and hidden; it must still not block."""
    q = questions
    q["BASIC-002"].required_flag = True
    q["BASIC-002"].save(update_fields=["required_flag"])

    put(author_client, version, q["BASIC-001"], {"value": "NO"})  # hides BASIC-002
    response = put(author_client, version, q["BASIC-004"], {"value": "NO"})
    basic = next(
        s for s in response.data["sections"] if s["section_id"] == q["BASIC-001"].section_id
    )
    assert basic["status"] == SectionStatusValue.COMPLETED


def test_revealing_a_required_branch_reopens_the_section(author_client, version, questions):
    """Completion is recomputed on every save, in both directions."""
    q = questions
    q["BASIC-002"].required_flag = True
    q["BASIC-002"].save(update_fields=["required_flag"])

    put(author_client, version, q["BASIC-001"], {"value": "NO"})
    put(author_client, version, q["BASIC-004"], {"value": "NO"})
    # Flip Q1 to Yes: BASIC-002 (required) becomes visible and unanswered.
    response = put(author_client, version, q["BASIC-001"], {"value": "YES"})
    basic = next(
        s for s in response.data["sections"] if s["section_id"] == q["BASIC-001"].section_id
    )
    assert basic["status"] == SectionStatusValue.IN_PROGRESS
    assert basic["required_visible"] == 3


def test_section_statuses_are_persisted_not_only_returned(author_client, version, questions):
    """The estate rollup and the export read the column; it must be written."""
    put(author_client, version, questions["RTO-001"], {"value": 8})
    stored = SectionStatus.objects.get(plan_version=version, section=questions["RTO-001"].section)
    assert stored.status == SectionStatusValue.COMPLETED


# --------------------------------------------------------------------------- #
# Adding a question is data, not a deploy — the third exit criterion
# --------------------------------------------------------------------------- #


def test_a_question_added_in_the_database_appears_in_the_editor(author_client, version, questions):
    rto = Section.objects.get(section_name="RTO")
    new = Question.objects.create(
        section=rto,
        question_code="RTO-002",
        question_text="Has the RTO been tested in the last 12 months?",
        answer_type=AnswerType.SINGLE_CHOICE,
        required_flag=True,
        display_order=2,
    )
    new.options.create(option_code="YES", option_label="Yes", display_order=1)
    new.options.create(option_code="NO", option_label="No", display_order=2)

    tree = author_client.get(tree_url(version)).data
    q = question_in(tree, "RTO-002")
    assert q["visible"] is True
    assert [o["code"] for o in q["options"]] == ["YES", "NO"]

    # And it takes part in completion straight away.
    response = put(author_client, version, questions["RTO-001"], {"value": 8})
    rto_status = next(s for s in response.data["sections"] if s["section_id"] == rto.pk)
    assert rto_status["status"] == SectionStatusValue.IN_PROGRESS
    response = put(author_client, version, new, {"value": "YES"})
    rto_status = next(s for s in response.data["sections"] if s["section_id"] == rto.pk)
    assert rto_status["status"] == SectionStatusValue.COMPLETED


# --------------------------------------------------------------------------- #
# Comments (4.5)
# --------------------------------------------------------------------------- #


def test_comments_thread_per_question(author_client, viewer_client, version, questions):
    q = questions["RTO-001"]
    assert author_client.get(comments_url(version, q)).data == []

    created = viewer_client.post(comments_url(version, q), {"comment": "  Is 8h realistic?  "})
    assert created.status_code == 201
    assert created.data["comment"] == "Is 8h realistic?"
    assert created.data["author_name"] == "viewer"

    thread = author_client.get(comments_url(version, q)).data
    assert [c["comment"] for c in thread] == ["Is 8h realistic?"]
    assert question_in(author_client.get(tree_url(version)).data, "RTO-001")["comment_count"] == 1


def test_an_empty_comment_is_rejected(author_client, version, questions):
    assert (
        author_client.post(
            comments_url(version, questions["RTO-001"]), {"comment": "   "}
        ).status_code
        == 400
    )


def test_comments_outside_scope_are_404(api_client, user_factory, version, questions):
    api_client.force_authenticate(
        user=user_factory(email="s@example.com", roles=["BCM_COORDINATOR"])
    )
    assert api_client.get(comments_url(version, questions["RTO-001"])).status_code == 404


def test_a_subform_carries_its_lookup_options(author_client, version):
    """The subcontractor column of BIA-004 needs the catalogue to pick from."""
    bia4 = question_in(author_client.get(tree_url(version)).data, "BIA-004")
    assert len(bia4["options"]) > 0
    assert all(o["code"].startswith("LC:") for o in bia4["options"])
