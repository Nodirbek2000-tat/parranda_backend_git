"""v2.1 §7 reports (ADMIN, SUPERADMIN): GET /api/reports/{overview|cases|staff|finance}.

Every report takes `?date_from=&date_to=` (case created_at, local dates, inclusive) and `?format=xlsx` (openpyxl).
A stage is late when it was submitted after its due_at, or is still open past due_at.
"""

import io
from collections import defaultdict
from datetime import date

from django.db.models import Count, Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.auth import IsAdmin
from accounts.models import User

from . import stages as chain
from .finance import COST_KEYS, case_finance
from .models import (Case, CaseStage, CaseStatus, Request, RequestStatus, StageEvent, StageStatus, VetVisit,
                     VisitStatus)
from .serializers import user_brief
from .services import OPEN

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class IgnoreFormatNegotiation(DefaultContentNegotiation):
    """`?format=xlsx` selects our Excel export, not a DRF renderer (DRF would answer 404)."""

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


# --- Helpers ------------------------------------------------------------------------------------

def _parse_date(value, name):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValidationError({name: ["Sana YYYY-MM-DD ko‘rinishida bo‘lishi kerak."]}) from None


def filtered_cases(params):
    date_from, date_to = _parse_date(params.get("date_from"), "date_from"), _parse_date(params.get("date_to"), "date_to")
    qs = Case.objects.all()
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    return qs, date_from, date_to


def timing(stage, now) -> str | None:
    """on_time / late (finished after due) / open_late (open past due) / open; None without a deadline."""
    if stage.due_at is None:
        return None
    if stage.status in OPEN:
        return "open_late" if stage.due_at < now else "open"
    if stage.submitted_at is None:
        return None
    return "late" if stage.submitted_at > stage.due_at else "on_time"


def delay_hours(stage, now) -> float:
    if stage.due_at is None:
        return 0.0
    if stage.status in OPEN:
        end = now
    elif stage.submitted_at is not None:
        end = stage.submitted_at
    else:
        return 0.0
    return max(0.0, (end - stage.due_at).total_seconds() / 3600)


def _avg(values):
    return round(sum(values) / len(values), 1) if values else None


def _hours(delta):
    return delta.total_seconds() / 3600


def _with_finance(cases):
    return [(case, case_finance(case)) for case in cases]


# --- Report data --------------------------------------------------------------------------------

def overview(params) -> dict:
    cases_qs, date_from, date_to = filtered_cases(params)
    now = timezone.now()
    today = timezone.localdate()
    totals = cases_qs.aggregate(
        total=Count("id"),
        in_progress=Count("id", filter=Q(status=CaseStatus.IN_PROGRESS)),
        done=Count("id", filter=Q(status=CaseStatus.DONE)),
        rejected=Count("id", filter=Q(status=CaseStatus.REJECTED)),
    )
    stages = list(CaseStage.objects.filter(case__in=cases_qs).select_related("case"))
    per_stage = {st.number: {"number": st.number, "key": st.key, "title": st.title, "total": 0, "active": 0,
                             "submitted": 0, "returned": 0, "overdue": 0} for st in chain.STAGES}
    per_timing = {st.number: {"number": st.number, "title": st.title, "on_time": 0, "late": 0, "open_late": 0}
                  for st in chain.STAGES}
    timing_total = {"on_time": 0, "late": 0, "open_late": 0}
    for st in stages:
        if st.case.status == CaseStatus.IN_PROGRESS and st.number == st.case.current_stage:
            row = per_stage[st.number]
            row["total"] += 1
            if st.status == StageStatus.ACTIVE:
                row["active"] += 1
            elif st.status == StageStatus.SUBMITTED:
                row["submitted"] += 1
            elif st.status == StageStatus.RETURNED:
                row["returned"] += 1
            if st.is_overdue:
                row["overdue"] += 1
        kind = timing(st, now)
        if kind in timing_total:
            timing_total[kind] += 1
            per_timing[st.number][kind] += 1

    requests = Request.objects.filter(case__in=cases_qs)
    by_status = dict(requests.values_list("status").annotate(n=Count("id")).order_by())
    by_kind = dict(requests.values_list("kind").annotate(n=Count("id")).order_by())
    visits = VetVisit.objects.filter(case__in=cases_qs)

    fin = {"done": {"cases": 0, "cost": 0, "revenue": 0, "profit": 0}, "in_progress": {"cases": 0, "cost": 0}}
    for case, f in _with_finance(cases_qs.prefetch_related("stages").select_related("package")):
        if case.status == CaseStatus.DONE:
            fin["done"]["cases"] += 1
            fin["done"]["cost"] += f["total_cost"]
            fin["done"]["revenue"] += f["revenue"] or 0
            fin["done"]["profit"] += f["profit"] if f["profit"] is not None else -f["total_cost"]
        elif case.status == CaseStatus.IN_PROGRESS:
            fin["in_progress"]["cases"] += 1
            fin["in_progress"]["cost"] += f["total_cost"]

    return {
        "date_from": date_from, "date_to": date_to,
        "cases": totals,
        "per_stage": list(per_stage.values()),
        "timing": timing_total,
        "per_stage_timing": list(per_timing.values()),
        "requests": {
            "total": sum(by_status.values()),
            "by_status": [{"status": value, "status_display": label, "count": by_status.get(value, 0)}
                          for value, label in RequestStatus.choices],
            "by_kind": {"FEED": by_kind.get("FEED", 0), "VET": by_kind.get("VET", 0)},
        },
        "visits": {
            "planned": visits.filter(status=VisitStatus.PLANNED, planned_date__gte=today).count(),
            "done": visits.filter(status=VisitStatus.DONE).count(),
            "overdue": visits.filter(status=VisitStatus.PLANNED, planned_date__lt=today).count(),
            "cancelled": visits.filter(status=VisitStatus.CANCELLED).count(),
        },
        "finance": fin,
    }


def cases_report(params) -> list[dict]:
    cases_qs, _, _ = filtered_cases(params)
    now = timezone.now()
    rows = []
    cases = cases_qs.select_related("package").prefetch_related("stages__submitted_by").order_by("id")
    for case, f in _with_finance(cases):
        stages = list(case.stages.all())
        late = [st for st in stages if timing(st, now) in ("late", "open_late")]
        worst = max(late, key=lambda st: delay_hours(st, now), default=None)
        end = case.done_at or now
        rows.append({
            "id": case.pk, "code": case.code, "name": case.name, "owner_full_name": case.owner_full_name,
            "district": case.district, "status": case.status, "status_display": case.get_status_display(),
            "current_stage": case.current_stage, "current_stage_title": chain.get(case.current_stage).title,
            "created_at": case.created_at,
            "days_in_chain": max(0, (end - case.created_at).days),
            "late_stages": len(late),
            "worst_delay": None if worst is None else {
                "stage": worst.number, "title": chain.get(worst.number).title,
                "hours": round(delay_hours(worst, now), 1), "executor_role": chain.get(worst.number).executor,
                "who": None if worst.status in OPEN else user_brief(worst.submitted_by),
            },
            "cost": f["total_cost"], "revenue": f["revenue"], "profit": f["profit"],
        })
    return rows


def staff_report(params) -> list[dict]:
    cases_qs, _, _ = filtered_cases(params)
    now = timezone.now()
    today = timezone.localdate()
    data = defaultdict(lambda: {"stages_submitted": 0, "submit_hours": [], "late": [], "approvals": 0,
                                "decision_hours": [], "requests": set(), "visits_done": 0, "visits_overdue": 0})
    for st in CaseStage.objects.filter(case__in=cases_qs).select_related("case"):
        if st.submitted_by_id and st.submitted_at:
            row = data[st.submitted_by_id]
            row["stages_submitted"] += 1
            if st.activated_at and st.number != 1:
                row["submit_hours"].append(_hours(st.submitted_at - st.activated_at))
            if timing(st, now) == "late":
                row["late"].append({"case_id": st.case_id, "code": st.case.code, "stage": st.number,
                                    "title": chain.get(st.number).title, "hours": round(delay_hours(st, now), 1)})
        if (st.decided_by_id and st.status == StageStatus.DONE and st.decided_at and st.submitted_at
                and st.decided_at >= st.submitted_at):
            data[st.decided_by_id]["decision_hours"].append(_hours(st.decided_at - st.submitted_at))
    approvals = (StageEvent.objects.filter(case__in=cases_qs, action=StageEvent.APPROVE, actor__isnull=False)
                 .values_list("actor_id").annotate(n=Count("id")).order_by())
    for actor_id, n in approvals:
        data[actor_id]["approvals"] = n
    fields = ("logistics_by_id", "admin_by_id", "delivered_by_id", "done_by_id", "rejected_by_id")
    for row in Request.objects.filter(case__in=cases_qs).values("id", *fields):
        for name in fields:
            if row[name]:
                data[row[name]]["requests"].add(row["id"])
    visits = VetVisit.objects.filter(case__in=cases_qs, vet__isnull=False)
    for vet_id, n in visits.filter(status=VisitStatus.DONE).values_list("vet_id").annotate(n=Count("id")).order_by():
        data[vet_id]["visits_done"] = n
    overdue = visits.filter(status=VisitStatus.PLANNED, planned_date__lt=today)
    for vet_id, n in overdue.values_list("vet_id").annotate(n=Count("id")).order_by():
        data[vet_id]["visits_overdue"] = n

    users = {u.pk: u for u in User.objects.filter(pk__in=data.keys())}
    result = []
    for uid, row in data.items():
        user = users.get(uid)
        if user is None:
            continue
        result.append({
            "user": user_brief(user),
            "stages_submitted": row["stages_submitted"],
            "avg_submit_hours": _avg(row["submit_hours"]),
            "late_count": len(row["late"]),
            "late": row["late"],
            "approvals": row["approvals"],
            "avg_decision_hours": _avg(row["decision_hours"]),
            "requests_handled": len(row["requests"]),
            "visits_done": row["visits_done"],
            "visits_overdue": row["visits_overdue"],
        })
    result.sort(key=lambda r: (r["user"]["role"], r["user"]["full_name"]))
    return result


TOTAL_KEYS = COST_KEYS + ("total_cost", "revenue", "profit")


def finance_report(params) -> dict:
    cases_qs, _, _ = filtered_cases(params)
    rows = []
    totals = dict.fromkeys(TOTAL_KEYS, 0)
    cases = cases_qs.select_related("package").prefetch_related("stages").order_by("id")
    for case, f in _with_finance(cases):
        rows.append({"id": case.pk, "code": case.code, "name": case.name, "status": case.status,
                     "status_display": case.get_status_display(), **f})
        for key in TOTAL_KEYS:
            totals[key] += f[key] or 0
    return {"rows": rows, "totals": totals}


# --- Excel --------------------------------------------------------------------------------------

def _cell(value):
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v}" for k, v in value.items())
    if hasattr(value, "tzinfo") and getattr(value, "tzinfo", None) is not None:
        return timezone.localtime(value).replace(tzinfo=None)
    return value


def xlsx_response(name: str, sheets) -> HttpResponse:
    """sheets = [(title, headers, rows)]."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    wb.remove(wb.active)
    for title, headers, rows in sheets:
        ws = wb.create_sheet(title[:31])
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F5130")
        for row in rows:
            ws.append([_cell(v) for v in row])
        for i, header in enumerate(headers, start=1):
            width = max([len(str(header))] + [len(str(_cell(r[i - 1]) or "")) for r in rows[:200]]) + 2
            ws.column_dimensions[ws.cell(1, i).column_letter].width = min(width, 60)
        ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    response = HttpResponse(buf.getvalue(), content_type=XLSX)
    response["Content-Disposition"] = f'attachment; filename="hisobot-{name}-{timezone.localdate():%Y%m%d}.xlsx"'
    return response


def _user(u):
    return f"{u['full_name']} ({u['role_display']})" if u else ""


def overview_sheets(d):
    c, fin = d["cases"], d["finance"]
    summary = [
        ("Davr", f"{d['date_from'] or '…'} — {d['date_to'] or '…'}"),
        ("Jami xonadonlar", c["total"]), ("Jarayonda", c["in_progress"]), ("Yakunlangan", c["done"]),
        ("Rad etilgan", c["rejected"]),
        ("Bosqichlar o‘z vaqtida", d["timing"]["on_time"]), ("Kechikkan (bajarilgan)", d["timing"]["late"]),
        ("Kechikmoqda (ochiq)", d["timing"]["open_late"]),
        ("So‘rovlar jami", d["requests"]["total"]), ("Yem so‘rovlari", d["requests"]["by_kind"]["FEED"]),
        ("Veterinar so‘rovlari", d["requests"]["by_kind"]["VET"]),
        ("Tashriflar: rejada", d["visits"]["planned"]), ("Tashriflar: bajarilgan", d["visits"]["done"]),
        ("Tashriflar: muddati o‘tgan", d["visits"]["overdue"]), ("Tashriflar: bekor", d["visits"]["cancelled"]),
        ("Yakunlanganlar xarajati", fin["done"]["cost"]), ("Yakunlanganlar daromadi", fin["done"]["revenue"]),
        ("Yakunlanganlar foydasi", fin["done"]["profit"]), ("Jarayondagilar xarajati", fin["in_progress"]["cost"]),
    ]
    per_stage = [[r["number"], r["title"], r["total"], r["active"], r["submitted"], r["returned"], r["overdue"]]
                 for r in d["per_stage"]]
    timing_rows = [[r["number"], r["title"], r["on_time"], r["late"], r["open_late"]] for r in d["per_stage_timing"]]
    requests = [[r["status_display"], r["count"]] for r in d["requests"]["by_status"]]
    return [
        ("Umumiy", ["Ko‘rsatkich", "Qiymat"], [list(r) for r in summary]),
        ("Bosqichlar", ["#", "Bosqich", "Jami", "Faol", "Tasdiq kutmoqda", "Qaytarilgan", "Muddati o‘tgan"], per_stage),
        ("Muddatlar", ["#", "Bosqich", "O‘z vaqtida", "Kechikkan", "Kechikmoqda"], timing_rows),
        ("So‘rovlar", ["Holat", "Soni"], requests),
    ]


def cases_sheets(rows):
    headers = ["Kod", "Xonadon", "Egasi", "Tuman", "Holat", "Joriy bosqich", "Yaratilgan", "Zanjirdagi kunlar",
               "Kechikkan bosqichlar", "Eng katta kechikish", "Kechikish (soat)", "Kim", "Xarajat", "Daromad",
               "Foyda"]
    out = []
    for r in rows:
        w = r["worst_delay"] or {}
        out.append([r["code"], r["name"], r["owner_full_name"], r["district"], r["status_display"],
                    f"{r['current_stage']}. {r['current_stage_title']}", r["created_at"], r["days_in_chain"],
                    r["late_stages"], f"{w['stage']}. {w['title']}" if w else "", w.get("hours"),
                    _user(w.get("who")) if w else "", r["cost"], r["revenue"], r["profit"]])
    return [("Xonadonlar", headers, out)]


def staff_sheets(rows):
    headers = ["Xodim", "Rol", "Bajargan bosqichlar", "O‘rtacha bajarish (soat)", "Kechikkanlar",
               "Kechikkan bosqichlar", "Tasdiqlar", "O‘rtacha tasdiqlash (soat)", "So‘rovlar", "Tashrif bajarildi",
               "Tashrif muddati o‘tgan"]
    out = [[r["user"]["full_name"], r["user"]["role_display"], r["stages_submitted"], r["avg_submit_hours"],
            r["late_count"], "; ".join(f"{x['code']} #{x['stage']} (+{x['hours']} soat)" for x in r["late"]),
            r["approvals"], r["avg_decision_hours"], r["requests_handled"], r["visits_done"], r["visits_overdue"]]
           for r in rows]
    return [("Xodimlar", headers, out)]


def finance_sheets(d):
    headers = ["Kod", "Xonadon", "Holat", "Paket", "Jo‘jalar", "Yem", "Veterinariya", "Boshqa", "Jami xarajat",
               "Daromad", "Foyda", "Parranda", "1 parranda tannarxi", "Vazn (kg)", "1 kg narxi"]
    out = [[r["code"], r["name"], r["status_display"], r["package_cost"], r["chicks_cost"], r["feed_cost"],
            r["vet_cost"], r["other_cost"], r["total_cost"], r["revenue"], r["profit"], r["birds"],
            r["per_bird_cost"], r["kg"], r["price_per_kg"]] for r in d["rows"]]
    t = d["totals"]
    out.append(["JAMI", "", "", t["package_cost"], t["chicks_cost"], t["feed_cost"], t["vet_cost"], t["other_cost"],
                t["total_cost"], t["revenue"], t["profit"], None, None, None, None])
    return [("Moliya", headers, out)]


# --- Views --------------------------------------------------------------------------------------

REPORTS = {
    "overview": (overview, overview_sheets),
    "cases": (cases_report, cases_sheets),
    "staff": (staff_report, staff_sheets),
    "finance": (finance_report, finance_sheets),
}


class ReportView(APIView):
    permission_classes = [IsAdmin]
    content_negotiation_class = IgnoreFormatNegotiation
    report = ""

    def get(self, request):
        build, sheets = REPORTS[self.report]
        data = build(request.query_params)
        if request.query_params.get("format") == "xlsx":
            return xlsx_response(self.report, sheets(data))
        return Response(data)
