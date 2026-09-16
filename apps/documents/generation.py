"""
Plan document generation (Phase 7.1-7.4).

One `build_plan_payload` gathers everything a version holds — the questionnaire
with answers, BIA sections, risks and actions, recovery strategy, coordinators,
history — and two renderers turn that same payload into a Word document
(python-docx) and a PDF (reportlab). Both are pure Python, which is what keeps
generation runnable on a Windows worker without a GTK or Office install.

Outputs are content-addressed (AD-7): the file's SHA-256 is the storage key,
so regenerating an unchanged plan reuses the existing `Document` row rather
than writing a duplicate. Each generation is attached to the version through
`entity_documents` with a `document_type` of GENERATED_PLAN_DOCX / _PDF;
earlier generations are kept, so an approved document is never overwritten.

`RENDERER_VERSION` is the layout version. It must match the active
`DocumentTemplate` row for BCP_PLAN; `test_renderer_version_matches_template`
fails otherwise, so a layout change cannot ship without a traceable record.
"""

from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.assessments.models import (
    AnswerContext,
    BiaCriticalContact,
    BiaServiceDescription,
    NetworkRequirement,
)
from apps.documents.models import Document, DocumentTemplate, EntityDocument
from apps.plans.models import CoordinatorAssignment, PlanStatusHistory, PlanVersion
from apps.risk.models import RecoveryStrategy, Risk

logger = logging.getLogger(__name__)

TEMPLATE_CODE = "BCP_PLAN"
RENDERER_VERSION = 1

DOCX_TYPE = "GENERATED_PLAN_DOCX"
PDF_TYPE = "GENERATED_PLAN_PDF"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"


# --------------------------------------------------------------------------- #
# Payload
# --------------------------------------------------------------------------- #


def _answer_text(question, answer) -> str:
    """A human-readable rendering of an answer, from codes back to labels."""
    from apps.questionnaire.answers import detail_options_for, options_for, subform_schema

    if answer is None:
        return "Not answered"
    if "rows" in answer:
        rows = answer["rows"]
        if not rows:
            return "None"
        labels = {o.code: o.label for o in options_for(question)}
        fields = {f["name"]: f["label"] for f in subform_schema(question)}
        return "; ".join(
            ", ".join(f"{fields.get(k, k)}: {labels.get(v, v)}" for k, v in row.items())
            for row in rows
        )
    value = answer.get("value")
    labels = {o.code: o.label for o in options_for(question)}
    if isinstance(value, list):
        text = ", ".join(labels.get(v, v) for v in value)
    else:
        text = labels.get(value, str(value))
    detail = answer.get("detail")
    if detail:
        detail_labels = {o.code: o.label for o in detail_options_for(question)}
        text += " — " + ", ".join(detail_labels.get(d, d) for d in detail)
    return text


def build_plan_payload(version: PlanVersion) -> dict:
    """Everything the document needs, as plain data."""
    from apps.questionnaire.services import build_questionnaire

    plan = version.plan
    cost_code = plan.cost_code
    tree = build_questionnaire(version, AnswerContext.BCP)
    questions_by_id = {}
    from apps.questionnaire.models import Question

    for q in Question.objects.filter(
        pk__in=[q["question_id"] for s in tree["sections"] for q in s["questions"]]
    ):
        questions_by_id[q.pk] = q

    sections = []
    for section in tree["sections"]:
        sections.append(
            {
                "name": section["section_name"],
                "status": section["status"],
                "questions": [
                    {
                        "code": q["question_code"],
                        "text": q["question_text"],
                        "answer": _answer_text(questions_by_id[q["question_id"]], q["answer"]),
                        "answered_by": q["answered_by"] or "",
                    }
                    for q in section["questions"]
                    if q["visible"]
                ],
            }
        )

    def name(employee):
        return employee.full_name if employee else ""

    return {
        "generated_at": timezone.now(),
        "template": f"{TEMPLATE_CODE} v{RENDERER_VERSION}",
        "cost_code": cost_code.cost_code,
        "process": getattr(cost_code.process, "process_name", ""),
        "subprocess": getattr(cost_code.subprocess, "subprocess_name", ""),
        "estate": getattr(cost_code.estate, "estate_name", ""),
        "region": getattr(cost_code.region, "region_name", ""),
        "bu_lead": getattr(cost_code.bu_lead, "lead_name", ""),
        "version_number": version.version_number,
        "status": version.status,
        "approved_by": version.approved_by.display_name if version.approved_by_id else "",
        "approved_at": version.approved_at,
        "coordinators": [
            (
                f"{a.employee.full_name} ({a.coordinator_type})"
                if a.coordinator_type
                else a.employee.full_name
            )
            for a in CoordinatorAssignment.objects.filter(
                plan_version=version, active_flag=True
            ).select_related("employee")
        ],
        "sections": sections,
        "service_descriptions": [
            {
                "process": s.process.process_name,
                "subprocess": s.subprocess.subprocess_name if s.subprocess else "",
                "owner": name(s.owner_employee),
                "description": s.process_description,
                "mao": s.mao,
                "mbco": s.mbco,
                "rto": s.rto,
                "rpo": s.rpo,
            }
            for s in BiaServiceDescription.objects.filter(plan_version=version).select_related(
                "process", "subprocess", "owner_employee"
            )
        ],
        "critical_contacts": [
            {
                "person": name(c.employee),
                "type": c.contact_type,
                "shift": c.shift_timings,
                "phone": c.primary_phone,
                "alternate": c.alternate_phone,
                "seats": c.seat_count,
                "voice": c.voice_non_voice,
                "asset": c.asset_id,
            }
            for c in BiaCriticalContact.objects.filter(plan_version=version).select_related(
                "employee"
            )
        ],
        "network_requirements": [
            {
                "type": n.get_requirement_type_display(),
                "source": n.source_ip,
                "destination": n.destination_ip,
                "port": n.port_number,
                "connectivity": n.connectivity_type,
            }
            for n in NetworkRequirement.objects.filter(plan_version=version)
        ],
        "risks": [
            {
                "name": r.risk_name,
                "resource": r.resource_type,
                "owner": name(r.owner_employee),
                "likelihood": r.likelihood_rating,
                "severity": r.severity_rating,
                "control": r.control_effectiveness_rating,
                "inherent": r.inherent_risk_score,
                "residual": r.residual_risk_score,
                "level": r.risk_level,
                "actions": [
                    {
                        "type": a.get_action_type_display(),
                        "description": a.description,
                        "status": a.status,
                        "target": a.target_date.isoformat() if a.target_date else "",
                    }
                    for a in r.actions.all()
                ],
            }
            for r in Risk.objects.filter(plan_version=version)
            .select_related("owner_employee")
            .prefetch_related("actions")
        ],
        "strategies": [
            {
                "core": s.core_strategy,
                "tactical": s.tactical_strategy,
                "owner": name(s.owner_employee),
            }
            for s in RecoveryStrategy.objects.filter(plan_version=version).select_related(
                "owner_employee"
            )
        ],
        "history": [
            {
                "status": h.status,
                "at": h.changed_at,
                "by": h.changed_by.display_name if h.changed_by_id else "System",
                "comments": h.comments,
            }
            for h in PlanStatusHistory.objects.filter(plan_version=version)
            .select_related("changed_by")
            .order_by("changed_at")
        ],
    }


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #


def _fmt(value) -> str:
    if value is None or value == "":
        return "—"
    if hasattr(value, "strftime"):
        return (
            timezone.localtime(value).strftime("%d %b %Y %H:%M")
            if getattr(value, "tzinfo", None)
            else value.strftime("%d %b %Y")
        )
    return str(value)


def render_docx(payload: dict) -> bytes:
    from docx import Document as DocxDocument
    from docx.shared import Pt

    doc = DocxDocument()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)

    doc.add_heading(f"Business Continuity Plan — {payload['cost_code']}", level=0)
    meta = doc.add_table(rows=0, cols=2)
    meta.style = "Light Grid Accent 1"
    for label, value in (
        ("Process", payload["process"]),
        ("Subprocess", payload["subprocess"]),
        ("Estate", payload["estate"]),
        ("Region", payload["region"]),
        ("BU lead", payload["bu_lead"]),
        ("Version", payload["version_number"]),
        ("Status", payload["status"]),
        ("Approved by", payload["approved_by"]),
        ("Approved at", payload["approved_at"]),
        ("Coordinators", ", ".join(payload["coordinators"])),
        ("Generated", payload["generated_at"]),
        ("Template", payload["template"]),
    ):
        row = meta.add_row().cells
        row[0].text = label
        row[1].text = _fmt(value)

    doc.add_heading("Questionnaire", level=1)
    for section in payload["sections"]:
        doc.add_heading(f"{section['name']} ({section['status']})", level=2)
        for q in section["questions"]:
            p = doc.add_paragraph()
            p.add_run(f"{q['code']}  ").bold = True
            p.add_run(q["text"])
            answer = doc.add_paragraph(style="List Bullet")
            answer.add_run("Answer: ").bold = True
            answer.add_run(q["answer"])

    def table(title, columns, rows):
        doc.add_heading(title, level=1)
        if not rows:
            doc.add_paragraph("None recorded.")
            return
        t = doc.add_table(rows=1, cols=len(columns))
        t.style = "Light Grid Accent 1"
        for i, (label, _) in enumerate(columns):
            t.rows[0].cells[i].text = label
        for row in rows:
            cells = t.add_row().cells
            for i, (_, key) in enumerate(columns):
                cells[i].text = _fmt(row.get(key))

    table(
        "Service description and recovery targets",
        [
            ("Process", "process"),
            ("Subprocess", "subprocess"),
            ("Owner", "owner"),
            ("MAO", "mao"),
            ("MBCO", "mbco"),
            ("RTO", "rto"),
            ("RPO", "rpo"),
        ],
        payload["service_descriptions"],
    )
    table(
        "Critical contacts",
        [
            ("Person", "person"),
            ("Type", "type"),
            ("Shift", "shift"),
            ("Phone", "phone"),
            ("Seats", "seats"),
            ("Voice", "voice"),
            ("Asset", "asset"),
        ],
        payload["critical_contacts"],
    )
    table(
        "Network requirements",
        [
            ("Type", "type"),
            ("Source", "source"),
            ("Destination", "destination"),
            ("Port", "port"),
            ("Connectivity", "connectivity"),
        ],
        payload["network_requirements"],
    )

    doc.add_heading("Risk register", level=1)
    if not payload["risks"]:
        doc.add_paragraph("None recorded.")
    for r in payload["risks"]:
        doc.add_heading(f"{r['name']} — {r['level'] or 'unscored'}", level=2)
        doc.add_paragraph(
            f"Resource: {_fmt(r['resource'])} · Owner: {_fmt(r['owner'])} · "
            f"L {_fmt(r['likelihood'])} × S {_fmt(r['severity'])} − C {_fmt(r['control'])} "
            f"→ inherent {_fmt(r['inherent'])}, residual {_fmt(r['residual'])}"
        )
        for a in r["actions"]:
            doc.add_paragraph(
                f"{a['type']}: {a['description']} [{a['status'] or 'no status'}; target {a['target'] or '—'}]",
                style="List Bullet",
            )

    table(
        "Recovery strategy",
        [("Core", "core"), ("Tactical", "tactical"), ("Owner", "owner")],
        payload["strategies"],
    )

    doc.add_heading("Status history", level=1)
    for h in payload["history"]:
        doc.add_paragraph(
            f"{_fmt(h['at'])} — {h['status']} — {h['by']}{': ' + h['comments'] if h['comments'] else ''}"
        )

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def render_pdf(payload: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    story = [Paragraph(f"Business Continuity Plan — {payload['cost_code']}", styles["Title"])]

    def kv_table(pairs):
        data = [[Paragraph(f"<b>{k}</b>", body), Paragraph(_fmt(v), body)] for k, v in pairs]
        t = Table(data, colWidths=[40 * mm, 130 * mm])
        t.setStyle(
            TableStyle(
                [("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]
            )
        )
        return t

    story.append(
        kv_table(
            [
                ("Process", payload["process"]),
                ("Subprocess", payload["subprocess"]),
                ("Estate", payload["estate"]),
                ("Region", payload["region"]),
                ("BU lead", payload["bu_lead"]),
                ("Version", payload["version_number"]),
                ("Status", payload["status"]),
                ("Approved by", payload["approved_by"]),
                ("Approved at", payload["approved_at"]),
                ("Coordinators", ", ".join(payload["coordinators"])),
                ("Generated", payload["generated_at"]),
                ("Template", payload["template"]),
            ]
        )
    )
    story.append(Spacer(1, 6 * mm))

    story.append(Paragraph("Questionnaire", styles["Heading1"]))
    for section in payload["sections"]:
        story.append(Paragraph(f"{section['name']} ({section['status']})", styles["Heading2"]))
        for q in section["questions"]:
            story.append(Paragraph(f"<b>{q['code']}</b> {q['text']}", body))
            story.append(Paragraph(f"<i>Answer:</i> {q['answer']}", body))
            story.append(Spacer(1, 2 * mm))

    def grid(title, columns, rows):
        story.append(Paragraph(title, styles["Heading1"]))
        if not rows:
            story.append(Paragraph("None recorded.", body))
            return
        data = [[Paragraph(f"<b>{label}</b>", body) for label, _ in columns]]
        for row in rows:
            data.append([Paragraph(_fmt(row.get(key)), body) for _, key in columns])
        t = Table(data, repeatRows=1)
        t.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                ]
            )
        )
        story.append(t)

    grid(
        "Service description and recovery targets",
        [
            ("Process", "process"),
            ("Subprocess", "subprocess"),
            ("Owner", "owner"),
            ("MAO", "mao"),
            ("MBCO", "mbco"),
            ("RTO", "rto"),
            ("RPO", "rpo"),
        ],
        payload["service_descriptions"],
    )
    grid(
        "Critical contacts",
        [
            ("Person", "person"),
            ("Type", "type"),
            ("Shift", "shift"),
            ("Phone", "phone"),
            ("Seats", "seats"),
            ("Voice", "voice"),
            ("Asset", "asset"),
        ],
        payload["critical_contacts"],
    )
    grid(
        "Network requirements",
        [
            ("Type", "type"),
            ("Source", "source"),
            ("Destination", "destination"),
            ("Port", "port"),
            ("Connectivity", "connectivity"),
        ],
        payload["network_requirements"],
    )
    grid(
        "Risk register",
        [
            ("Risk", "name"),
            ("Resource", "resource"),
            ("Owner", "owner"),
            ("L", "likelihood"),
            ("S", "severity"),
            ("C", "control"),
            ("Inherent", "inherent"),
            ("Residual", "residual"),
            ("Level", "level"),
        ],
        payload["risks"],
    )
    grid(
        "Recovery strategy",
        [("Core", "core"), ("Tactical", "tactical"), ("Owner", "owner")],
        payload["strategies"],
    )

    story.append(Paragraph("Status history", styles["Heading1"]))
    for h in payload["history"]:
        story.append(
            Paragraph(
                f"{_fmt(h['at'])} — <b>{h['status']}</b> — {h['by']}{': ' + h['comments'] if h['comments'] else ''}",
                body,
            )
        )

    buffer = io.BytesIO()
    # invariant=1: no creation timestamp or random document ID in the file, so
    # the same plan renders to the same bytes and content addressing can
    # recognise an unchanged regeneration.
    SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        invariant=1,
        title=f"BCP plan {payload['cost_code']}",
    ).build(story)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Storage and records
# --------------------------------------------------------------------------- #


def media_root() -> Path:
    return Path(settings.MEDIA_ROOT)


def active_template() -> DocumentTemplate:
    """The current BCP_PLAN template row, created on first use."""
    template, _ = DocumentTemplate.objects.get_or_create(
        template_code=TEMPLATE_CODE,
        version_number=RENDERER_VERSION,
        defaults={"description": "Generated BCP plan document (docx + pdf)", "active_flag": True},
    )
    return template


def store_bytes(
    content: bytes, *, file_name: str, mime_type: str, template: DocumentTemplate, actor=None
) -> Document:
    """Write content-addressed to MEDIA_ROOT and return (or reuse) its Document row."""
    digest = hashlib.sha256(content).hexdigest()
    suffix = Path(file_name).suffix
    storage_key = f"generated/{digest[:2]}/{digest}{suffix}"

    existing = Document.objects.filter(storage_key=storage_key).first()
    if existing is not None:
        return existing

    path = media_root() / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)

    return Document.objects.create(
        file_name=file_name,
        storage_key=storage_key,
        mime_type=mime_type,
        file_size_bytes=len(content),
        checksum_sha256=digest,
        uploaded_by=actor,
        template=template,
    )


@transaction.atomic
def generate_for_version(version: PlanVersion, *, actor=None) -> list[EntityDocument]:
    """Render both formats and attach them to the version. Earlier outputs stay."""
    payload = build_plan_payload(version)
    template = active_template()
    stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
    base = f"BCP-{version.plan.cost_code.cost_code}-v{version.version_number}-{stamp}"

    attachments = []
    for content, suffix, mime, doc_type in (
        (render_docx(payload), ".docx", DOCX_MIME, DOCX_TYPE),
        (render_pdf(payload), ".pdf", PDF_MIME, PDF_TYPE),
    ):
        document = store_bytes(
            content, file_name=f"{base}{suffix}", mime_type=mime, template=template, actor=actor
        )
        attachment, _ = EntityDocument.objects.get_or_create(
            document=document,
            entity_type=EntityDocument.EntityType.PLAN_VERSION,
            entity_id=version.pk,
            document_type=doc_type,
        )
        attachments.append(attachment)

    logger.info("Generated %s documents for plan version %s", len(attachments), version.pk)
    return attachments


def documents_for_version(version: PlanVersion):
    """Every generated document for a version, newest first."""
    return (
        EntityDocument.objects.filter(
            entity_type=EntityDocument.EntityType.PLAN_VERSION,
            entity_id=version.pk,
            document_type__in=[DOCX_TYPE, PDF_TYPE],
        )
        .select_related("document", "document__template")
        .order_by("-document__uploaded_at", "-entity_document_id")
    )
