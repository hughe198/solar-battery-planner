"""Opt-in inverter control: preview, apply, remove and restore the planner's charge on a Fox ESS
inverter, a step-by-step write test, and the audit log of every change."""

from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.forecast import _system
from app.api.imports import get_weather_client
from app.api.plan import make_plan, next_day
from app.config import settings
from app.control import service, testing
from app.control.nightly import control_client
from app.control.schedule import Period
from app.crypto import decrypt
from app.database import get_db
from app.importers.fox_ess import FoxEssClient, FoxEssError
from app.models import (
    ControlAction,
    ControlActionKind,
    ControlStatus,
    ControlTrigger,
    InverterControl,
    System,
    User,
)
from app.schemas import (
    ApplyIn,
    ControlActionOut,
    ControlSettingsIn,
    ControlStatusOut,
    PeriodOut,
    ProposalOut,
    TestStepIn,
    TestStepOut,
)
from app.security import current_user
from app.weather.open_meteo import OpenMeteoClient, WeatherError

router = APIRouter(prefix="/api/control", tags=["control"])

LOG_LENGTH = 30


def get_control_client_factory() -> Callable[[str], FoxEssClient]:
    return control_client


def _clock(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def period_out(p: Period) -> PeriodOut:
    charge = p.work_mode == "ForceCharge"
    return PeriodOut(
        start=_clock(p.start),
        end=_clock(p.end + 1),
        work_mode=p.work_mode,
        charge_to_percent=p.fd_soc if charge else None,
        power_w=p.fd_pwr if charge else None,
        min_soc_percent=p.min_soc_on_grid,
    )


def _periods(groups: list | None) -> list[PeriodOut]:
    return [period_out(Period.from_fox(g)) for g in groups or []]


def _control(system: System) -> InverterControl:
    return system.inverter_control or InverterControl(system=system)


def _enabled(system: System) -> InverterControl:
    if system.fox_ess is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Connect Fox ESS first")
    control = system.inverter_control
    if control is None or not control.enabled:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Turn on inverter control first; it is off by default"
        )
    return control


def _client(system: System, factory: Callable[[str], FoxEssClient]) -> FoxEssClient:
    return factory(decrypt(system.fox_ess.api_key_encrypted))


def _has_good_manual_apply(db: Session, system: System) -> bool:
    return (
        db.scalar(
            select(ControlAction.id).where(
                ControlAction.system_id == system.id,
                ControlAction.kind == ControlActionKind.apply,
                ControlAction.trigger == ControlTrigger.manual,
                ControlAction.status == ControlStatus.ok,
            )
        )
        is not None
    )


def _status(db: Session, system: System) -> ControlStatusOut:
    control = system.inverter_control
    level = control.test_level if control else 0
    original = (control.original_schedule if control else None) or None
    actions = db.scalars(
        select(ControlAction)
        .where(ControlAction.system_id == system.id)
        .order_by(ControlAction.id.desc())
        .limit(LOG_LENGTH)
    ).all()
    enabled = bool(control and control.enabled and system.fox_ess)
    can_apply = enabled and level >= testing.UNLOCKS_APPLY
    return ControlStatusOut(
        connected=system.fox_ess is not None,
        enabled=enabled,
        auto_apply=bool(enabled and control.auto_apply),
        auto_apply_available=settings.auto_apply,
        consented_at=control.consented_at if control else None,
        test_level=level,
        test_steps=[
            TestStepOut(step=n, label=label, passed=level >= n)
            for n, label in testing.STEPS.items()
        ],
        can_apply=can_apply,
        can_auto_apply=can_apply and _has_good_manual_apply(db, system),
        has_original=original is not None,
        original_saved_at=control.original_saved_at if control else None,
        original=_periods(original.get("groups") if original else None),
        original_enabled=bool(original.get("enable")) if original else None,
        planner_periods=_periods(control.written_groups if control else None),
        test_periods=_periods(control.test_groups if control else None),
        actions=[ControlActionOut.model_validate(a) for a in actions],
    )


@router.get("", response_model=ControlStatusOut)
def control_status(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> ControlStatusOut:
    return _status(db, _system(user))


@router.put("", response_model=ControlStatusOut)
def update_settings(
    body: ControlSettingsIn, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> ControlStatusOut:
    """Opt in or out. Opting out stops all writes but doesn't change the inverter; remove or
    restore first to take the planner's periods out."""
    system = _system(user)
    control = _control(system)
    if body.enabled and system.fox_ess is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Connect Fox ESS first")
    if body.enabled and not control.enabled and not body.accept:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Confirm you understand the planner will change your inverter's settings",
        )
    if body.enabled and body.auto_apply and not control.auto_apply:
        if control.test_level < testing.UNLOCKS_APPLY or not _has_good_manual_apply(db, system):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Finish the write test and apply a plan by hand first",
            )
    was_enabled = control.enabled
    control.enabled = body.enabled
    control.auto_apply = body.enabled and body.auto_apply
    if body.enabled and not was_enabled:
        control.consented_at = datetime.now(timezone.utc)
        service._log(
            db,
            system,
            ControlActionKind.opt_in,
            ControlTrigger.manual,
            ControlStatus.ok,
            "Inverter control turned on.",
        )
    elif was_enabled and not body.enabled:
        service._log(
            db,
            system,
            ControlActionKind.opt_out,
            ControlTrigger.manual,
            ControlStatus.ok,
            "Inverter control turned off. The inverter was not changed.",
        )
    db.add(control)
    db.commit()
    return _status(db, system)


def _proposal(
    db: Session,
    system: System,
    control: InverterControl,
    client: FoxEssClient,
    weather_client: OpenMeteoClient,
    start_soc_percent: float | None,
) -> service.Proposal:
    try:
        plan = make_plan(db, system, weather_client, next_day(system), start_soc_percent)
    except WeatherError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    try:
        schedule = service.read_schedule(client, system.fox_ess.device_sn)
    except FoxEssError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    try:
        return service.propose(system, control, plan, schedule)
    except service.ControlError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.get("/preview", response_model=ProposalOut)
def preview(
    start_soc_percent: float | None = Query(default=None, ge=0, le=100),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    client_factory: Callable[[str], FoxEssClient] = Depends(get_control_client_factory),
    weather_client: OpenMeteoClient = Depends(get_weather_client),
) -> ProposalOut:
    """Exactly what applying the next charge would write. Reads the inverter; writes nothing."""
    system = _system(user)
    control = _enabled(system)
    p = _proposal(
        db, system, control, _client(system, client_factory), weather_client, start_soc_percent
    )
    return ProposalOut(
        day=p.day,
        mode=p.mode,
        planned_soc_percent=p.planned_soc_percent,
        charge_kwh=p.charge_kwh,
        start=p.start,
        stop=p.stop,
        target_soc_percent=p.target_soc_percent,
        power_w=p.power_w,
        min_soc_on_grid=p.min_soc_on_grid,
        schedule_enabled=p.schedule.enabled,
        max_periods=p.schedule.max_groups,
        current=[period_out(x) for x in p.schedule.periods],
        added=[period_out(x) for x in p.added],
        removed=[period_out(x) for x in p.removed],
        after=[period_out(x) for x in p.after],
        request={
            "deviceSN": system.fox_ess.device_sn,
            "groups": [x.to_fox() for x in p.after],
        },
        notes=p.notes,
        fingerprint=p.fingerprint,
    )


@router.post("/apply", response_model=ControlActionOut)
def apply_plan(
    body: ApplyIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    client_factory: Callable[[str], FoxEssClient] = Depends(get_control_client_factory),
    weather_client: OpenMeteoClient = Depends(get_weather_client),
) -> ControlAction:
    """Write the previewed schedule. Refused if the plan or the inverter's schedule has
    changed since the preview, so what is written is always what was shown."""
    system = _system(user)
    control = _enabled(system)
    if control.test_level < testing.UNLOCKS_APPLY:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Pass the write test up to step {testing.UNLOCKS_APPLY} first",
        )
    client = _client(system, client_factory)
    proposal = _proposal(db, system, control, client, weather_client, body.start_soc_percent)
    if proposal.fingerprint != body.fingerprint:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The plan or your inverter's schedule changed since the preview. Check the new "
            "preview before applying.",
        )
    action = service.apply(db, system, control, client, proposal, ControlTrigger.manual)
    db.commit()
    return action


@router.post("/clear", response_model=ControlActionOut)
def clear_periods(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    client_factory: Callable[[str], FoxEssClient] = Depends(get_control_client_factory),
) -> ControlAction:
    """Take the planner's force charge and test periods out, keeping the user's own."""
    system = _system(user)
    control = _enabled(system)
    action = service.clear(db, system, control, _client(system, client_factory))
    db.commit()
    return action


@router.post("/restore", response_model=ControlActionOut)
def restore_schedule(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    client_factory: Callable[[str], FoxEssClient] = Depends(get_control_client_factory),
) -> ControlAction:
    """Put back the schedule saved before the planner first wrote to the inverter."""
    system = _system(user)
    control = _enabled(system)
    action = service.restore(db, system, control, _client(system, client_factory))
    db.commit()
    return action


@router.post("/test", response_model=ControlActionOut)
def run_test_step(
    body: TestStepIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    client_factory: Callable[[str], FoxEssClient] = Depends(get_control_client_factory),
) -> ControlAction:
    """Run one step of the write test (see app.control.testing)."""
    system = _system(user)
    control = _enabled(system)
    try:
        action = testing.run_step(
            db,
            system,
            control,
            _client(system, client_factory),
            body.step,
            datetime.now(timezone.utc),
        )
    except service.ControlError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    db.commit()
    return action
