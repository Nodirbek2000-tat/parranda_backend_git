"""Notification texts and recipients of the vet app (API_CONTRACT_V3 §9.4).

Every function is called inside the business transaction; rows are queued after the commit by
:func:`notifications.services.notify_on_commit`. Reminder jobs (``vet/reminders.py``) call :func:`notify`
directly (they run in their own transaction and need the number of queued rows).

Body rows: ``Xonadon``, ``Mas’ul``, ``Partiya``, ``Veterinar``, ``Sana``, then the object specific rows
(``Maqsad`` / ``Vaksina`` / ``Doza`` / ``Kasallik`` / ``Holat`` / ``Xulosa``); op id = the vet code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from common.formatting import fmt_date, fmt_number
from common.roles import Role
from notifications import registry
from notifications.compose import Row, compose, excerpt, household_rows
from notifications.services import notify, notify_on_commit, role_users

if TYPE_CHECKING:
    from .models import DiseaseRecord, Treatment, Vaccination, VetRequest, VetVisit

MY_VET = "/my/vet"
VACCINATIONS_PATH = "/vet?tab=vaccinations"
OVERDUE_VACCINATIONS_PATH = "/vet?tab=vaccinations&overdue=1"
DISEASES_PATH = "/vet?tab=diseases"
REQUESTS_PATH = "/vet/requests"


def assigned_vet(obj: Any):
    """The object's vet, or every active VET when nobody is assigned (§9.2)."""
    return obj.vet if obj.vet_id else list(role_users(Role.VET))


def _common_rows(obj: Any, *, day=None) -> list[Row]:
    rows: list[Row] = [*household_rows(obj.household)]
    batch = getattr(obj, "batch", None)
    if batch is not None:
        rows.append(("Partiya", batch.code))
    vet = obj.vet if getattr(obj, "vet_id", None) else None
    rows.append(("Veterinar", vet.full_name if vet is not None else "Biriktirilmagan"))
    if day is not None:
        rows.append(("Sana", fmt_date(day)))
    return rows


def _visit_rows(visit: VetVisit) -> list[Row]:
    return [*_common_rows(visit, day=visit.planned_date), ("Maqsad", visit.purpose)]


def _vaccination_rows(vaccination: Vaccination, *, day=None) -> list[Row]:
    rows = [
        *_common_rows(vaccination, day=day or vaccination.planned_date),
        ("Vaksina", vaccination.vaccine_name),
        ("Doza", vaccination.dose),
    ]
    if vaccination.birds_planned:
        rows.append(("Parranda", f"{fmt_number(vaccination.birds_planned)} dona"))
    return rows


# --- Visits ----------------------------------------------------------------------------------


def visit_key(visit: VetVisit) -> str:
    """``{id}.{planned_date:%Y%m%d}.{vet_id}``: a changed date or vet notifies again (§9.4)."""
    return f"{visit.pk}.{visit.planned_date:%Y%m%d}.{visit.vet_id or 0}"


def visit_planned(visit: VetVisit) -> None:
    text = compose("vet_visit_planned", _visit_rows(visit), "Rejalashtirildi", visit.code)
    notify_on_commit(
        "vet_visit_planned",
        key=visit_key(visit),
        text=text,
        staff_path=f"/vet/visits/{visit.pk}",
        owner_path=MY_VET,
        related_op=visit.code,
        contextual={registry.ASSIGNED_VET: assigned_vet(visit), registry.HOUSEHOLD_OWNER: visit.household.owner},
        object_type="VetVisit",
        object_id=visit.pk,
    )


def visit_cancelled(visit: VetVisit) -> None:
    rows = [*_visit_rows(visit), ("Sabab", visit.cancel_reason)]
    text = compose("vet_visit_cancelled", rows, "Bekor qilindi", visit.code)
    notify_on_commit(
        "vet_visit_cancelled",
        key=visit.pk,
        text=text,
        staff_path=f"/vet/visits/{visit.pk}",
        owner_path=MY_VET,
        related_op=visit.code,
        contextual={registry.ASSIGNED_VET: assigned_vet(visit), registry.HOUSEHOLD_OWNER: visit.household.owner},
        object_type="VetVisit",
        object_id=visit.pk,
    )


def visit_concluded(visit: VetVisit) -> None:
    rows = [
        *_common_rows(visit, day=visit.done_at),
        ("Holat", visit.get_health_status_display()),
        ("Tekshirildi", f"{fmt_number(visit.birds_checked)} dona" if visit.birds_checked else None),
        ("Xulosa", excerpt(visit.conclusion)),
        ("Tavsiyalar", excerpt(visit.recommendations)),
    ]
    text = compose("vet_conclusion", rows, "O‘tkazildi", visit.code)
    notify_on_commit(
        "vet_conclusion",
        key=visit.pk,
        text=text,
        staff_path=f"/vet/visits/{visit.pk}",
        owner_path=MY_VET,
        related_op=visit.code,
        contextual={registry.HOUSEHOLD_OWNER: visit.household.owner},
        object_type="VetVisit",
        object_id=visit.pk,
    )


def visit_today_rows(visit: VetVisit) -> list[Row]:
    return _visit_rows(visit)


def visit_today(visit: VetVisit, today) -> list:
    """Reminder (§16.1): queued now, key ``{id}.{YYYYMMDD}``."""
    text = compose("vet_visit_today", _visit_rows(visit), "Bugun", visit.code)
    return notify(
        "vet_visit_today",
        key=f"{visit.pk}.{today:%Y%m%d}",
        text=text,
        staff_path=f"/vet/visits/{visit.pk}",
        owner_path=MY_VET,
        related_op=visit.code,
        contextual={registry.ASSIGNED_VET: _freeze(assigned_vet(visit))},
        object_type="VetVisit",
        object_id=visit.pk,
    )


# --- Vaccinations ------------------------------------------------------------------------------


def vaccination_key(vaccination: Vaccination) -> str:
    """``{id}.{planned_date:%Y%m%d}.{vet_id}``: a rescheduled or reassigned vaccination notifies again."""
    return f"{vaccination.pk}.{vaccination.planned_date:%Y%m%d}.{vaccination.vet_id or 0}"


def _reminder_key(vaccination: Vaccination, stage: str) -> str:
    """Reminder keys carry the planned date: after a reschedule the new date gets its own due/today/overdue."""
    return f"{vaccination.pk}.{vaccination.planned_date:%Y%m%d}.{stage}"


def vaccination_planned(vaccination: Vaccination) -> None:
    text = compose("vaccination_planned", _vaccination_rows(vaccination), "Rejalashtirildi", vaccination.code)
    notify_on_commit(
        "vaccination_planned",
        key=vaccination_key(vaccination),
        text=text,
        staff_path=VACCINATIONS_PATH,
        owner_path=MY_VET,
        related_op=vaccination.code,
        contextual={
            registry.ASSIGNED_VET: assigned_vet(vaccination),
            registry.HOUSEHOLD_OWNER: vaccination.household.owner,
        },
        object_type="Vaccination",
        object_id=vaccination.pk,
    )


def vaccination_done(vaccination: Vaccination) -> None:
    rows = [
        *_common_rows(vaccination, day=vaccination.done_date),
        ("Vaksina", vaccination.vaccine_name),
        ("Doza", vaccination.dose),
        ("Emlandi", f"{fmt_number(vaccination.birds_vaccinated)} dona" if vaccination.birds_vaccinated else None),
        ("Natija", excerpt(vaccination.result)),
    ]
    text = compose("vaccination_done", rows, "O‘tkazildi", vaccination.code)
    notify_on_commit(
        "vaccination_done",
        key=vaccination.pk,
        text=text,
        staff_path=VACCINATIONS_PATH,
        owner_path=MY_VET,
        related_op=vaccination.code,
        contextual={registry.HOUSEHOLD_OWNER: vaccination.household.owner},
        object_type="Vaccination",
        object_id=vaccination.pk,
    )


def vaccination_cancelled(vaccination: Vaccination) -> None:
    rows = [*_vaccination_rows(vaccination), ("Sabab", vaccination.cancel_reason)]
    text = compose("vaccination_cancelled", rows, "Bekor qilindi", vaccination.code)
    notify_on_commit(
        "vaccination_cancelled",
        key=vaccination.pk,
        text=text,
        staff_path=VACCINATIONS_PATH,
        owner_path=MY_VET,
        related_op=vaccination.code,
        contextual={
            registry.ASSIGNED_VET: assigned_vet(vaccination),
            registry.HOUSEHOLD_OWNER: vaccination.household.owner,
        },
        object_type="Vaccination",
        object_id=vaccination.pk,
    )


def _freeze(value):
    return [value] if not isinstance(value, list) else value


def vaccination_due(vaccination: Vaccination, today) -> list:
    """Reminder: ``{id}.{date}.due`` once, ``{id}.{date}.today`` on the planned day (§16.1)."""
    is_today = vaccination.planned_date == today
    text = compose(
        "vaccination_due", _vaccination_rows(vaccination), "Bugun" if is_today else "Yaqinlashmoqda", vaccination.code
    )
    return notify(
        "vaccination_due",
        key=_reminder_key(vaccination, "today" if is_today else "due"),
        text=text,
        staff_path=VACCINATIONS_PATH,
        owner_path=MY_VET,
        related_op=vaccination.code,
        contextual={
            registry.ASSIGNED_VET: _freeze(assigned_vet(vaccination)),
            registry.HOUSEHOLD_OWNER: vaccination.household.owner,
        },
        object_type="Vaccination",
        object_id=vaccination.pk,
    )


def vaccination_overdue(vaccination: Vaccination, days: int) -> list:
    """Reminder on day 1, 7, 14 and 30 after the planned date: key ``{id}.{date}.od{days}``."""
    rows = [*_vaccination_rows(vaccination), ("Kechikish", f"{days} kun")]
    text = compose("vaccination_overdue", rows, "Muddati o‘tdi", vaccination.code)
    return notify(
        "vaccination_overdue",
        key=_reminder_key(vaccination, f"od{days}"),
        text=text,
        staff_path=OVERDUE_VACCINATIONS_PATH,
        owner_path=MY_VET,
        related_op=vaccination.code,
        contextual={registry.ASSIGNED_VET: _freeze(assigned_vet(vaccination))},
        object_type="Vaccination",
        object_id=vaccination.pk,
    )


# --- Diseases and treatments -------------------------------------------------------------------


def disease_registered(disease: DiseaseRecord) -> None:
    rows = [
        *_common_rows(disease, day=disease.diagnosed_date),
        ("Kasallik", disease.disease_name),
        ("Holatlar", f"{fmt_number(disease.cases)} ta"),
        ("Belgilar", excerpt(disease.symptoms)),
    ]
    text = compose("disease_registered", rows, disease.get_status_display(), disease.code)
    notify_on_commit(
        "disease_registered",
        key=disease.pk,
        text=text,
        staff_path=DISEASES_PATH,
        owner_path=MY_VET,
        related_op=disease.code,
        contextual={registry.HOUSEHOLD_OWNER: disease.household.owner},
        object_type="DiseaseRecord",
        object_id=disease.pk,
    )


def treatment_prescribed(treatment: Treatment) -> None:
    period = fmt_date(treatment.start_date)
    if treatment.end_date:
        period = f"{period} – {fmt_date(treatment.end_date)}"
    rows = [
        *_common_rows(treatment),
        ("Sana", period),
        ("Kasallik", treatment.disease.disease_name if treatment.disease_id else None),
        ("Preparat", treatment.drug_name),
        ("Doza", treatment.dosage),
        ("Ko‘rsatma", excerpt(treatment.instructions)),
    ]
    text = compose("treatment_prescribed", rows, "Tavsiya berildi", treatment.code)
    notify_on_commit(
        "treatment_prescribed",
        key=treatment.pk,
        text=text,
        staff_path=DISEASES_PATH,
        owner_path=MY_VET,
        related_op=treatment.code,
        contextual={registry.HOUSEHOLD_OWNER: treatment.household.owner},
        object_type="Treatment",
        object_id=treatment.pk,
    )


# --- Owner requests ----------------------------------------------------------------------------


def request_created(request: VetRequest) -> None:
    rows = [*household_rows(request.household), ("Sana", fmt_date(request.created_at)), ("Murojaat", excerpt(request.text))]
    text = compose("vet_request_created", rows, request.get_status_display(), request.code)
    notify_on_commit(
        "vet_request_created",
        key=request.pk,
        text=text,
        staff_path=REQUESTS_PATH,
        owner_path=MY_VET,
        related_op=request.code,
        object_type="VetRequest",
        object_id=request.pk,
    )


def request_updated(request: VetRequest) -> None:
    """Take / staff close: key ``{id}.{status}`` (the owner gets one message per status)."""
    rows = [
        *household_rows(request.household),
        ("Veterinar", request.vet.full_name if request.vet_id else None),
        ("Murojaat", excerpt(request.text)),
        ("Javob", excerpt(request.response)),
    ]
    text = compose("vet_request_updated", rows, request.get_status_display(), request.code)
    notify_on_commit(
        "vet_request_updated",
        key=f"{request.pk}.{request.status}",
        text=text,
        staff_path=REQUESTS_PATH,
        owner_path=MY_VET,
        related_op=request.code,
        contextual={registry.HOUSEHOLD_OWNER: request.household.owner},
        object_type="VetRequest",
        object_id=request.pk,
    )
