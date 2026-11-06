"""v2.1 §7 reports (ADMIN, SUPERADMIN): GET /api/reports/{overview|cases|staff|finance}.

Every report takes `?date_from=&date_to=` (case created_at, local dates, inclusive), `?district=` (tuman/shahar)
and `?format=xlsx` (openpyxl).
A stage is late when it was submitted after its due_at, or is still open past due_at.
"""

import io
from collections import defaultdict
from datetime import date, timedelta

from django.db.models import Count, Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.auth import IsAdmin
from accounts.models import User

from . import geo
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
    district = (params.get("district") or "").strip()
    if district:
        qs = qs.filter(geo.district_q(district))
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
        "date_from": date_from, "date_to": date_to, "district": (params.get("district") or "").strip() or None,
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
        "geo": geo_report(params, now),
        "dashboard": dashboard(cases_qs, now),
    }


def geo_report(params, now) -> dict:
    """The region map: every district with its numbers (dates applied, district filter NOT applied, so the map
    always shows the whole viloyat) and the households with a location (district filter applied)."""
    from .summary import summaries

    qs, _, _ = filtered_cases({"date_from": params.get("date_from"), "date_to": params.get("date_to")})
    cases = list(qs.select_related("package").prefetch_related("stages"))
    stats = summaries(cases)
    rows = {name: {"name": name, "latitude": lat, "longitude": lng, "total": 0, "in_progress": 0, "done": 0,
                   "rejected": 0, "overdue": 0, "birds": 0, "cost": 0, "revenue": 0}
            for name, lat, lng in geo.DISTRICTS}
    selected = geo.canonical(params.get("district") or "")
    points = []
    mahallas: dict[tuple, dict] = {}
    for case in cases:
        name = geo.canonical(case.district)
        row = rows.setdefault(name or geo.OTHER, {
            "name": geo.OTHER, "latitude": None, "longitude": None, "total": 0, "in_progress": 0, "done": 0,
            "rejected": 0, "overdue": 0, "birds": 0, "cost": 0, "revenue": 0})
        s = stats.get(case.pk) or {}
        current = next((st for st in case.stages.all() if st.number == case.current_stage), None)
        running = case.status == CaseStatus.IN_PROGRESS
        overdue = bool(running and current is not None and current.is_overdue)
        row["total"] += 1
        row[{"IN_PROGRESS": "in_progress", "DONE": "done", "REJECTED": "rejected"}[case.status]] += 1
        row["overdue"] += overdue
        row["birds"] += (s.get("birds") or 0) if running else 0
        row["cost"] += s.get("total_cost") or 0
        row["revenue"] += s.get("revenue") or 0 if case.status != CaseStatus.REJECTED else 0
        mfy_name = " ".join((case.mfy or "").split()) or "—"
        m = mahallas.setdefault((row["name"], mfy_name.lower()), {
            "district": row["name"], "mfy": mfy_name, "green": 0, "yellow": 0, "red": 0, "total": 0})
        m[toifa(case, current, now)] += 1
        m["total"] += 1
        if case.latitude is None or case.longitude is None:
            continue
        if params.get("district") and name != selected:
            continue
        points.append({
            "id": case.pk, "code": case.code, "cycle": case.cycle, "owner_full_name": case.owner_full_name,
            "district": name or case.district, "mfy": case.mfy,
            "latitude": float(case.latitude), "longitude": float(case.longitude),
            "status": case.status, "current_stage": case.current_stage,
            "stage_title": chain.get(case.current_stage).title, "is_overdue": overdue,
            "birds": s.get("birds"), "days_since_delivery": s.get("days_since_delivery"),
        })
    mfy_rows = sorted(mahallas.values(), key=lambda m: (m["district"], -m["total"], m["mfy"]))
    return {"selected": selected, "districts": list(rows.values()), "points": points, "mahallas": mfy_rows}


#: Toifa of a household: qizil = rejected or the current stage is overdue; sariq = needs attention (returned,
#: deadline within a day, or waiting for approval more than a day); yashil = on track or finished.
TOIFA_LABELS = {"green": "Yashil", "yellow": "Sariq", "red": "Qizil"}


def toifa(case, current, now) -> str:
    if case.status == CaseStatus.REJECTED:
        return "red"
    if case.status == CaseStatus.DONE or current is None:
        return "green"
    if current.is_overdue:
        return "red"
    if current.status == StageStatus.RETURNED:
        return "yellow"
    if current.status in OPEN and current.due_at is not None and current.due_at - now < timedelta(days=1):
        return "yellow"
    if current.status == StageStatus.SUBMITTED and current.submitted_at and now - current.submitted_at > timedelta(days=1):
        return "yellow"
    return "green"


def _months_back(today, count):
    """The first days of the last `count` months, oldest first (the current month last)."""
    year, month, out = today.year, today.month, []
    for _ in range(count):
        out.append(date(year, month, 1))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return out[::-1]


def dashboard(cases_qs, now) -> dict:
    """Numbers for the "Umumiy svod" page (district / date filters applied)."""
    from .summary import summaries

    cases = list(cases_qs.select_related("package").prefetch_related("stages"))
    stats = summaries(cases)
    today = timezone.localdate()
    months = _months_back(today, 6)
    monthly = {(m.year, m.month): {"month": m.isoformat()[:7], "created": 0, "delivered": 0, "growing_done": 0,
                                   "done": 0} for m in months}

    def bump(moment, key):
        if moment is None:
            return
        day = timezone.localdate(moment)
        row = monthly.get((day.year, day.month))
        if row is not None:
            row[key] += 1

    kpi = {"total": 0, "new_this_month": 0, "in_progress": 0, "growing": 0, "birds_now": 0, "done": 0,
           "overdue": 0, "total_cost": 0}
    toifas = {"green": 0, "yellow": 0, "red": 0}
    est_revenue = 0
    growing_lengths = []
    rows = []
    for case in cases:
        stages = {st.number: st for st in case.stages.all()}
        current = stages.get(case.current_stage)
        s = stats.get(case.pk) or {}
        running = case.status == CaseStatus.IN_PROGRESS
        kind = toifa(case, current, now)
        toifas[kind] += 1
        kpi["total"] += 1
        kpi["new_this_month"] += timezone.localdate(case.created_at) >= today.replace(day=1)
        kpi["in_progress"] += running
        kpi["done"] += case.status == CaseStatus.DONE
        kpi["overdue"] += bool(running and current is not None and current.is_overdue)
        kpi["total_cost"] += s.get("total_cost") or 0
        if running and case.current_stage == chain.GROWING_STAGE:
            kpi["growing"] += 1
        if running:
            kpi["birds_now"] += s.get("birds") or 0
            est_revenue += s.get("revenue") or 0
        bump(case.created_at, "created")
        delivery = stages.get(chain.DELIVERY_STAGE)
        if delivery is not None and delivery.status == StageStatus.DONE and not (delivery.data or {}).get("carried_over"):
            bump(delivery.decided_at or delivery.submitted_at, "delivered")
        growing = stages.get(chain.GROWING_STAGE)
        if growing is not None and growing.submitted_at:
            bump(growing.submitted_at, "growing_done")
            if growing.activated_at:
                growing_lengths.append((growing.submitted_at - growing.activated_at).total_seconds() / 86400)
        if case.status == CaseStatus.DONE:
            bump(case.done_at, "done")
        rows.append((case, current, s, kind))

    latest = []
    for case, current, s, kind in sorted(rows, key=lambda r: r[0].created_at, reverse=True)[:8]:
        latest.append({
            "id": case.pk, "code": case.code, "cycle": case.cycle, "owner_full_name": case.owner_full_name,
            "mfy": case.mfy, "district": geo.canonical(case.district) or case.district,
            "status": case.status, "status_display": case.get_status_display(),
            "current_stage": case.current_stage, "stage_title": chain.get(case.current_stage).title,
            "stage_status": current.status if current else None,
            "stage_overdue": bool(case.status == CaseStatus.IN_PROGRESS and current and current.is_overdue),
            "birds": s.get("birds"), "days_since_delivery": s.get("days_since_delivery"),
            "growing_days": s.get("growing_days"), "feed": s.get("feed") or [], "vet_visits": s.get("vet_visits") or 0,
            "total_cost": s.get("total_cost") or 0, "revenue": s.get("revenue"), "profit": s.get("profit"),
            "revenue_is_estimate": s.get("revenue_is_estimate", True),
            "toifa": kind, "toifa_display": TOIFA_LABELS[kind],
        })

    return {
        "kpi": kpi,
        "toifa": [{"key": key, "label": TOIFA_LABELS[key], "count": toifas[key]} for key in ("green", "yellow", "red")],
        "monthly": list(monthly.values()),
        "finance_extra": {
            "estimated_revenue_in_progress": est_revenue,
            "avg_growing_days": round(sum(growing_lengths) / len(growing_lengths), 1) if growing_lengths else None,
        },
        "latest": latest,
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
        ("Tuman / shahar", d.get("district") or "Barchasi"),
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
    districts = [[r["name"], r["total"], r["in_progress"], r["done"], r["rejected"], r["overdue"], r["birds"], r["cost"],
                  r["revenue"]] for r in d["geo"]["districts"]]
    return [
        ("Umumiy", ["Ko‘rsatkich", "Qiymat"], [list(r) for r in summary]),
        ("Bosqichlar", ["#", "Bosqich", "Jami", "Faol", "Tasdiq kutmoqda", "Qaytarilgan", "Muddati o‘tgan"], per_stage),
        ("Muddatlar", ["#", "Bosqich", "O‘z vaqtida", "Kechikkan", "Kechikmoqda"], timing_rows),
        ("So‘rovlar", ["Holat", "Soni"], requests),
        ("Tumanlar", ["Tuman / shahar", "Jami", "Jarayonda", "Yakunlangan", "Rad etilgan", "Muddati o‘tgan",
                      "Parranda (hozir)", "Xarajat", "Daromad"], districts),
        ("Mahallalar", ["Tuman / shahar", "Mahalla", "Qizil", "Sariq", "Yashil", "Jami"],
         [[m["district"], m["mfy"], m["red"], m["yellow"], m["green"], m["total"]] for m in d["geo"]["mahallas"]]),
        ("Oylar", ["Oy", "Yangi xonadon", "Jo‘ja yetkazildi", "Parvarish tugadi", "Yakunlandi"],
         [[r["month"], r["created"], r["delivered"], r["growing_done"], r["done"]] for r in d["dashboard"]["monthly"]]),
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
