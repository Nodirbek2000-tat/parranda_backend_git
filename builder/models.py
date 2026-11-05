"""Konstruktor (API_CONTRACT_V4): Super Admin configures roles, forms, tasks, processes and document templates
without a developer.

One generic :class:`Blueprint` row per configured object (``kind`` + JSON ``data``) keeps the design light:
status lifecycle (draft → test → active → archived), versioning, pending changes awaiting confirmation and
revert are implemented once for every kind. Runtime rows (:class:`FormSubmission`, :class:`TaskInstance`,
:class:`ProcessRun`) snapshot the definition they were created from, so later edits never break old data.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone


def _user_fk(verbose_name: str, *, null: bool = True, related_name: str = "+") -> models.ForeignKey:
    return models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name=related_name,
        null=null,
        blank=null,
        verbose_name=verbose_name,
    )


class BlueprintKind(models.TextChoices):
    ROLE = "role", "Rol va vakolatlar"
    FORM = "form", "Forma"
    TASK = "task", "Vazifa shabloni"
    PROCESS = "process", "Jarayon"
    DOCUMENT = "document", "Hujjat shabloni"


class BlueprintStatus(models.TextChoices):
    DRAFT = "draft", "Qoralama"
    TEST = "test", "Sinov rejimi"
    ACTIVE = "active", "Faol"
    ARCHIVED = "archived", "Arxivlangan"


class Blueprint(models.Model):
    """A configured object. ``data`` is the live definition (schema per ``kind``, see ``builder.schemas``).

    An edit of an ACTIVE blueprint is parked in ``pending_data`` and takes effect only after
    ``activate`` (Super Admin confirmation, TZ §8 "Muhim o‘zgarishlar tasdiqlashdan keyin kuchga kirsin").
    """

    kind = models.CharField("Turi", max_length=16, choices=BlueprintKind.choices, db_index=True)
    name = models.CharField("Nomi", max_length=200)
    description = models.CharField("Tavsif", max_length=1000, blank=True, default="")
    status = models.CharField(
        "Holati", max_length=16, choices=BlueprintStatus.choices, default=BlueprintStatus.DRAFT, db_index=True
    )
    version = models.PositiveIntegerField("Versiya", default=1)
    data = models.JSONField("Ta’rif", default=dict)
    #: ``{"name": ..., "description": ..., "data": {...}, "comment": ...}`` awaiting confirmation.
    pending = models.JSONField("Tasdiq kutayotgan o‘zgarish", null=True, blank=True)
    pending_by = _user_fk("O‘zgarish muallifi")
    pending_at = models.DateTimeField("O‘zgarish vaqti", null=True, blank=True)
    created_by = _user_fk("Yaratgan")
    created_at = models.DateTimeField("Yaratilgan", default=timezone.now, editable=False)
    updated_at = models.DateTimeField("O‘zgartirilgan", auto_now=True)
    activated_by = _user_fk("Faollashtirgan")
    activated_at = models.DateTimeField("Faollashtirilgan", null=True, blank=True)

    class Meta:
        verbose_name = "Konstruktor obyekti"
        verbose_name_plural = "Konstruktor obyektlari"
        ordering = ("kind", "name", "id")

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: {self.name}"


class ConfigVersion(models.Model):
    """History of every configuration change (TZ §8): blueprints, notification rules, norms …

    ``object_type`` is ``"blueprint"``, ``"notification_setting"``, ``"feed_norm"`` … and ``object_id`` the row
    pk (notification settings use their numeric pk too). ``data`` is the full snapshot after the change.
    """

    object_type = models.CharField("Obyekt turi", max_length=32)
    object_id = models.BigIntegerField("Obyekt ID")
    version = models.PositiveIntegerField("Versiya")
    name = models.CharField("Nomi", max_length=200, blank=True, default="")
    data = models.JSONField("Holat", default=dict)
    status = models.CharField("Holati", max_length=16, blank=True, default="")
    comment = models.CharField("Izoh", max_length=500, blank=True, default="")
    created_by = _user_fk("Muallif")
    created_at = models.DateTimeField("Vaqti", default=timezone.now, editable=False)

    class Meta:
        verbose_name = "Sozlama versiyasi"
        verbose_name_plural = "Sozlamalar tarixi"
        ordering = ("object_type", "object_id", "-version")
        constraints = [
            models.UniqueConstraint(fields=("object_type", "object_id", "version"), name="builder_config_version_uniq")
        ]
        indexes = [models.Index(fields=("object_type", "object_id"), name="builder_cfgver_obj_idx")]


class UserAccess(models.Model):
    """Role profile + territorial scope of one user (TZ §1). No row = system role defaults, no limits."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="access", verbose_name="Foydalanuvchi"
    )
    profile = models.ForeignKey(
        Blueprint,
        on_delete=models.PROTECT,
        related_name="user_accesses",
        null=True,
        blank=True,
        limit_choices_to={"kind": BlueprintKind.ROLE},
        verbose_name="Rol profili",
    )
    #: Empty list = every district.
    districts = models.JSONField("Hududlar", default=list, blank=True)
    households = models.ManyToManyField("farm.Household", blank=True, related_name="+", verbose_name="Xonadonlar")
    updated_by = _user_fk("O‘zgartirgan")
    updated_at = models.DateTimeField("O‘zgartirilgan", auto_now=True)

    class Meta:
        verbose_name = "Foydalanuvchi vakolati"
        verbose_name_plural = "Foydalanuvchi vakolatlari"


class _Coded(models.Model):
    CODE_PREFIX = ""

    class Meta:
        abstract = True

    @property
    def code(self) -> str:
        return f"{self.CODE_PREFIX}-{self.pk:06d}" if self.pk else ""

    def __str__(self) -> str:
        return self.code


class ProcessRunStatus(models.TextChoices):
    RUNNING = "RUNNING", "Jarayonda"
    DONE = "DONE", "Yakunlandi"
    CANCELLED = "CANCELLED", "Bekor qilindi"


class ProcessRun(_Coded):
    """One household going through a configured process (TZ §4)."""

    CODE_PREFIX = "JR"

    process = models.ForeignKey(
        Blueprint, on_delete=models.PROTECT, related_name="runs", verbose_name="Jarayon",
        limit_choices_to={"kind": BlueprintKind.PROCESS},
    )
    process_version = models.PositiveIntegerField("Jarayon versiyasi")
    #: Snapshot of ``process.data["stages"]`` at start (later edits do not change a running process).
    stages = models.JSONField("Bosqichlar", default=list)
    household = models.ForeignKey(
        "farm.Household", on_delete=models.PROTECT, related_name="process_runs", null=True, blank=True,
        verbose_name="Xonadon",
    )
    current_index = models.PositiveSmallIntegerField("Joriy bosqich", default=0)
    status = models.CharField(
        "Holati", max_length=16, choices=ProcessRunStatus.choices, default=ProcessRunStatus.RUNNING, db_index=True
    )
    is_test = models.BooleanField("Sinov", default=False)
    started_by = _user_fk("Boshlagan")
    started_at = models.DateTimeField("Boshlangan", default=timezone.now, editable=False)
    finished_at = models.DateTimeField("Yakunlangan", null=True, blank=True)
    cancel_reason = models.CharField("Bekor qilish sababi", max_length=500, blank=True, default="")

    class Meta:
        verbose_name = "Jarayon"
        verbose_name_plural = "Jarayonlar"
        ordering = ("-started_at", "-id")


class TaskStatus(models.TextChoices):
    OPEN = "OPEN", "Bajarilmoqda"
    SUBMITTED = "SUBMITTED", "Tasdiq kutilmoqda"
    RETURNED = "RETURNED", "Qaytarildi"
    APPROVED = "APPROVED", "Tasdiqlandi"
    CANCELLED = "CANCELLED", "Bekor qilindi"


class TaskInstance(_Coded):
    """A task created from a task template (TZ §3), standalone or as a process stage."""

    CODE_PREFIX = "VZ"

    template = models.ForeignKey(
        Blueprint, on_delete=models.PROTECT, related_name="tasks", verbose_name="Vazifa shabloni",
        limit_choices_to={"kind": BlueprintKind.TASK},
    )
    template_version = models.PositiveIntegerField("Shablon versiyasi")
    title = models.CharField("Nomi", max_length=200)
    #: ``"VET"`` (system role) or ``"profile:<id>"`` (role profile).
    executor = models.CharField("Bajaruvchi rol", max_length=40)
    approver = models.CharField("Tasdiqlovchi rol", max_length=40, blank=True, default="")
    assigned_to = _user_fk("Bajaruvchi xodim", related_name="builder_tasks")
    household = models.ForeignKey(
        "farm.Household", on_delete=models.PROTECT, related_name="builder_tasks", null=True, blank=True,
        verbose_name="Xonadon",
    )
    process_run = models.ForeignKey(
        ProcessRun, on_delete=models.PROTECT, related_name="tasks", null=True, blank=True, verbose_name="Jarayon"
    )
    stage_index = models.PositiveSmallIntegerField("Bosqich", null=True, blank=True)
    #: Snapshot of the template definition (form fields, document id, condition …) at creation.
    definition = models.JSONField("Ta’rif", default=dict)
    starts_on = models.DateField("Boshlanish sanasi", default=timezone.localdate)
    due_on = models.DateField("Tugash sanasi", null=True, blank=True)
    status = models.CharField(
        "Holati", max_length=16, choices=TaskStatus.choices, default=TaskStatus.OPEN, db_index=True
    )
    submitted_by = _user_fk("Topshirgan")
    submitted_at = models.DateTimeField("Topshirilgan", null=True, blank=True)
    decided_by = _user_fk("Qaror qilgan")
    decided_at = models.DateTimeField("Qaror vaqti", null=True, blank=True)
    decision_note = models.CharField("Izoh", max_length=1000, blank=True, default="")
    is_test = models.BooleanField("Sinov", default=False)
    created_by = _user_fk("Yaratgan")
    created_at = models.DateTimeField("Yaratilgan", default=timezone.now, editable=False)
    updated_at = models.DateTimeField("O‘zgartirilgan", auto_now=True)

    class Meta:
        verbose_name = "Vazifa"
        verbose_name_plural = "Vazifalar"
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=("status", "executor"), name="builder_task_status_exec_idx")]


class FormSubmission(_Coded):
    """Values entered into a dynamic form (TZ §2). ``fields`` snapshots the field definitions used."""

    CODE_PREFIX = "FM"

    form = models.ForeignKey(
        Blueprint, on_delete=models.PROTECT, related_name="submissions", verbose_name="Forma",
        limit_choices_to={"kind": BlueprintKind.FORM},
    )
    form_version = models.PositiveIntegerField("Forma versiyasi")
    fields = models.JSONField("Maydonlar", default=list)
    data = models.JSONField("Qiymatlar", default=dict)
    household = models.ForeignKey(
        "farm.Household", on_delete=models.PROTECT, related_name="form_submissions", null=True, blank=True,
        verbose_name="Xonadon",
    )
    task = models.ForeignKey(
        TaskInstance, on_delete=models.PROTECT, related_name="submissions", null=True, blank=True,
        verbose_name="Vazifa",
    )
    is_test = models.BooleanField("Sinov", default=False)
    submitted_by = _user_fk("Kiritgan")
    created_at = models.DateTimeField("Kiritilgan", default=timezone.now, editable=False)
    updated_at = models.DateTimeField("O‘zgartirilgan", auto_now=True)

    class Meta:
        verbose_name = "Forma ma’lumoti"
        verbose_name_plural = "Forma ma’lumotlari"
        ordering = ("-created_at", "-id")
