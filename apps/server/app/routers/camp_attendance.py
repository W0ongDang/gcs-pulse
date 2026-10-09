from datetime import date, datetime, timezone
import secrets
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.models import User
from app.camp_models import Camp, CampMember, CampSession, CampAttendance, CampAudit
from app.camp_stats import aggregate
from app.dependencies import load_session_user_or_401, require_privileged_api_role, is_bearer_request, verify_csrf
from app.routers.snippet_access import load_bearer_identity_or_401, get_request_now
from app.core.config import settings

router = APIRouter(prefix="/camp-attendance", tags=["camp-attendance"], dependencies=[Depends(verify_csrf)])


class MemberInput(BaseModel):
    user_id: int = Field(gt=0)
    affiliation: str = Field(min_length=1, max_length=120)


class CampInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    members: list[MemberInput] = Field(default_factory=list, max_length=5000)


class SessionInput(BaseModel):
    date: date
    title: str = Field(min_length=1, max_length=120)


class StatusInput(BaseModel):
    status: str


class CorrectionInput(BaseModel):
    user_id: int = Field(gt=0)
    action: str
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def trim_reason(cls, v):
        if not v.strip():
            raise ValueError("정정 사유를 입력해 주세요")
        return v.strip()


def iso(v):
    if isinstance(v, datetime):
        return v.replace(tzinfo=v.tzinfo or timezone.utc).isoformat()
    return v.isoformat() if v else None


async def identity(request: Request, db=Depends(get_db)):
    if is_bearer_request(request):
        auth = await load_bearer_identity_or_401(request, db, invalid_detail="Invalid API token",
                require_snippet_role=False, include_consents=False)
        if auth.api_token.revoked_at:
            raise HTTPException(401, "취소된 API 토큰입니다")
        return auth.user
    return await load_session_user_or_401(request, db)


async def admin(user=Depends(identity)):
    require_privileged_api_role(user)
    return user


async def camp_or_404(db, camp_id, lock=False):
    if lock:
        # Updating the parent serializes attendance, close and corrections on both
        # PostgreSQL and SQLite. Status is checked only after this lock is acquired.
        await db.execute(update(Camp).where(Camp.id == camp_id).values(version=Camp.version + 1))
    camp = await db.get(Camp, camp_id, populate_existing=True)
    if not camp:
        raise HTTPException(404, "캠프를 찾을 수 없습니다")
    return camp


async def snapshot(db, camp_id):
    camp = await camp_or_404(db, camp_id)
    sessions = (await db.execute(select(CampSession).where(CampSession.camp_id == camp_id)
                                .order_by(CampSession.date, CampSession.number))).scalars().all()
    rows = (await db.execute(select(CampAttendance).join(CampSession)
                            .where(CampSession.camp_id == camp_id))).scalars().all()
    members = (await db.execute(select(CampMember, User).join(User, User.id == CampMember.user_id)
                               .where(CampMember.camp_id == camp_id))).all()
    return {"camp": {"id": camp.id, "name": camp.name}, "version": camp.version,
            "sessions": [{"id": s.id, "number": s.number, "date": iso(s.date), "title": s.title,
                          "status": s.status, "qr_token": s.qr_token, "opened_at": iso(s.opened_at),
                          "closed_at": iso(s.closed_at)} for s in sessions],
            "records": [{"id": r.id, "session_id": r.session_id, "user_id": r.user_id, "name": r.name,
                         "affiliation": r.affiliation, "source": r.source, "checked_at": iso(r.checked_at),
                         "cancelled_at": iso(r.cancelled_at)} for r in rows],
            "members": [{"id": u.id, "name": u.name or "이름 미등록", "affiliation": m.affiliation,
                         "active": m.active} for m, u in members]}


@router.get("/identity")
async def whoami(user=Depends(admin)):
    return {"id": user.id, "name": user.name, "roles": user.roles}


@router.get("/users")
async def users(user=Depends(admin), db=Depends(get_db)):
    rows = (await db.execute(select(User).options(selectinload(User.team)).order_by(User.name))).scalars().all()
    return [{"id": u.id, "name": u.name or "이름 미등록", "affiliation": u.team.name if u.team else "소속 미등록"}
            for u in rows]


@router.post("/camps")
async def create_camp(payload: CampInput, user=Depends(admin), db=Depends(get_db)):
    if not payload.name.strip():
        raise HTTPException(422, "캠프 이름을 입력해 주세요")
    ids = [m.user_id for m in payload.members]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "명단에 중복 학생이 있습니다")
    found = (await db.execute(select(User.id).where(User.id.in_(ids)))).scalars().all()
    if len(found) != len(ids):
        raise HTTPException(422, "GCS 명단에 없는 학생입니다")
    camp = Camp(name=payload.name.strip())
    db.add(camp)
    await db.flush()
    for m in payload.members:
        db.add(CampMember(camp_id=camp.id, user_id=m.user_id, affiliation=m.affiliation.strip() or "소속 미등록"))
    await db.commit()
    return {"id": camp.id, "name": camp.name}


@router.get("/camps/{camp_id}/admin")
async def admin_state(camp_id: int, user=Depends(admin), db=Depends(get_db)):
    data = await snapshot(db, camp_id)
    data["stats"] = aggregate(data["sessions"], data["records"], data["members"])
    audits = (await db.execute(select(CampAudit).where(CampAudit.camp_id == camp_id)
                              .order_by(CampAudit.id.desc()).limit(100))).scalars().all()
    data["audit"] = [{"id": a.id, "session_id": a.session_id, "user_id": a.user_id, "actor_id": a.actor_id,
                      "action": a.action, "reason": a.reason, "created_at": iso(a.created_at)} for a in audits]
    return data


@router.put("/camps/{camp_id}/members")
async def set_members(camp_id: int, payload: list[MemberInput], user=Depends(admin), db=Depends(get_db)):
    await camp_or_404(db, camp_id, lock=True)
    ids = [m.user_id for m in payload]
    if len(set(ids)) != len(ids) or len(ids) > 5000:
        raise HTTPException(422, "잘못된 명단입니다")
    found = (await db.execute(select(User.id).where(User.id.in_(ids)))).scalars().all()
    if len(found) != len(ids):
        raise HTTPException(422, "GCS 명단에 없는 학생입니다")
    existing = {m.user_id: m for m in (await db.execute(select(CampMember).where(CampMember.camp_id == camp_id))).scalars()}
    for m in existing.values():
        m.active = False
    for m in payload:
        row = existing.get(m.user_id)
        if row:
            row.active, row.affiliation = True, m.affiliation.strip() or "소속 미등록"
        else:
            db.add(CampMember(camp_id=camp_id, user_id=m.user_id, affiliation=m.affiliation.strip() or "소속 미등록"))
    await db.commit()
    return {"ok": True}


@router.post("/camps/{camp_id}/sessions")
async def create_session(camp_id: int, payload: SessionInput, user=Depends(admin), db=Depends(get_db)):
    await camp_or_404(db, camp_id, lock=True)
    sessions = (await db.execute(select(CampSession).where(CampSession.camp_id == camp_id))).scalars().all()
    if any(s.date == payload.date for s in sessions):
        raise HTTPException(409, "하루에 한 회차만 추가할 수 있습니다")
    if not payload.title.strip():
        raise HTTPException(422, "회차 이름을 입력해 주세요")
    row = CampSession(camp_id=camp_id, number=max([s.number for s in sessions], default=0) + 1,
                      date=payload.date, title=payload.title.strip(), qr_token=secrets.token_urlsafe(32))
    db.add(row)
    await db.commit()
    return {"id": row.id}


@router.put("/camps/{camp_id}/sessions/{session_id}/status")
async def set_status(camp_id: int, session_id: int, payload: StatusInput, request: Request,
                     user=Depends(admin), db=Depends(get_db)):
    await camp_or_404(db, camp_id, lock=True)
    row = await db.get(CampSession, session_id, populate_existing=True)
    if not row or row.camp_id != camp_id:
        raise HTTPException(404, "회차를 찾을 수 없습니다")
    now = get_request_now(request)
    if payload.status == "open":
        if row.status != "scheduled" or row.date != now.date():
            raise HTTPException(409, "오늘의 예정 회차만 개방할 수 있습니다")
        row.opened_at = now.astimezone(timezone.utc)
    elif payload.status == "closed":
        if row.status != "open":
            raise HTTPException(409, "접수 중인 회차만 마감할 수 있습니다")
        row.closed_at = now.astimezone(timezone.utc)
    else:
        raise HTTPException(422, "잘못된 회차 상태입니다")
    row.status = payload.status
    db.add(CampAudit(camp_id=camp_id, session_id=session_id, actor_id=user.id, action=payload.status,
                     reason="출석 개방" if payload.status == "open" else "출석 마감", created_at=now.astimezone(timezone.utc)))
    await db.commit()
    return {"ok": True}


@router.get("/camps/{camp_id}/public")
async def public_state(camp_id: int, request: Request, session_id: int | None = None, db=Depends(get_db)):
    data = await snapshot(db, camp_id)
    today = get_request_now(request).date().isoformat()
    selected = next((s for s in data["sessions"] if s["id"] == session_id), None) if session_id else None
    if session_id and (not selected or selected["status"] != "closed"):
        raise HTTPException(404, "마감 결과를 찾을 수 없습니다")
    if not selected:
        candidates = [s for s in data["sessions"] if s["date"] <= today]
        selected = candidates[-1] if candidates else None
    output = {"camp": data["camp"], "version": data["version"], "session": None, "result": None}
    if not selected:
        return output
    s = {k: v for k, v in selected.items() if k != "qr_token"}
    if selected["status"] == "open" and selected["date"] == today:
        s["check_in_url"] = settings.AUTH_SUCCESS_URL.rstrip("/") + "/camp/check-in/" + selected["qr_token"]
    elif selected["status"] == "open":
        s["status"] = "expired"
    if selected["status"] == "closed":
        stats = aggregate(data["sessions"], data["records"], data["members"], selected["id"])
        # Only the intentionally public names, affiliations and ranking metrics.
        def person(r):
            return {k: r[k] for k in ("name", "affiliation", "checked_at", "first_count", "attendance_count", "rank") if k in r}
        output["result"] = {"count": stats["count"], "average": stats["average"], "closed_count": stats["closed_count"],
                            "winner": person(stats["winner"]) if stats["winner"] else None,
                            "newcomers": [person(r) for r in stats["newcomers"]],
                            "leaderboard": [person(r) for r in stats["leaderboard"]]}
    output["session"] = s
    return output


@router.post("/check-in/{qr_token}")
async def check_in(qr_token: str, request: Request, user=Depends(identity), db=Depends(get_db)):
    session = (await db.execute(select(CampSession).where(CampSession.qr_token == qr_token))).scalar_one_or_none()
    if not session:
        raise HTTPException(404, "유효하지 않은 QR입니다")
    await camp_or_404(db, session.camp_id, lock=True)
    await db.refresh(session)
    now = get_request_now(request)
    if session.status != "open" or session.date != now.date():
        raise HTTPException(409, "출석이 마감되었거나 오늘의 QR이 아닙니다")
    member = (await db.execute(select(CampMember).where(CampMember.camp_id == session.camp_id,
                                                       CampMember.user_id == user.id, CampMember.active == True))).scalar_one_or_none()
    if not member:
        raise HTTPException(403, "캠프 명단에 등록되어 있지 않습니다. 관리자에게 문의해 주세요")
    row = (await db.execute(select(CampAttendance).where(CampAttendance.session_id == session.id,
                                                         CampAttendance.user_id == user.id))).scalar_one_or_none()
    duplicate = row is not None
    if row and row.cancelled_at:
        raise HTTPException(409, "관리자가 취소한 출석입니다. 관리자에게 문의해 주세요")
    if not row:
        row = CampAttendance(session_id=session.id, user_id=user.id, name=user.name or "이름 미등록",
                             affiliation=member.affiliation, source="qr", checked_at=now.astimezone(timezone.utc))
        db.add(row)
    await db.commit()
    return {"status": "duplicate" if duplicate else "success", "name": row.name, "affiliation": row.affiliation,
            "checked_at": iso(row.checked_at), "session": {"number": session.number, "title": session.title, "date": iso(session.date)}}


@router.post("/camps/{camp_id}/sessions/{session_id}/corrections")
async def correct(camp_id: int, session_id: int, payload: CorrectionInput, request: Request,
                  user=Depends(admin), db=Depends(get_db)):
    await camp_or_404(db, camp_id, lock=True)
    session = await db.get(CampSession, session_id)
    if not session or session.camp_id != camp_id:
        raise HTTPException(404, "회차를 찾을 수 없습니다")
    if session.status == "scheduled":
        raise HTTPException(409, "예정 회차는 정정할 수 없습니다")
    member = (await db.execute(select(CampMember).where(CampMember.camp_id == camp_id,
                                                       CampMember.user_id == payload.user_id))).scalar_one_or_none()
    student = await db.get(User, payload.user_id)
    if not member or not student:
        raise HTTPException(404, "캠프 명단에 없는 학생입니다")
    row = (await db.execute(select(CampAttendance).where(CampAttendance.session_id == session_id,
                                                         CampAttendance.user_id == payload.user_id))).scalar_one_or_none()
    now = get_request_now(request).astimezone(timezone.utc)
    if payload.action == "add":
        if row and not row.cancelled_at:
            raise HTTPException(409, "이미 출석했습니다")
        if row:
            row.cancelled_at, row.source, row.checked_at = None, "manual", now
            row.name, row.affiliation = student.name or "이름 미등록", member.affiliation
        else:
            db.add(CampAttendance(session_id=session_id, user_id=student.id, name=student.name or "이름 미등록",
                                  affiliation=member.affiliation, source="manual", checked_at=now))
    elif payload.action == "cancel":
        if not row or row.cancelled_at:
            raise HTTPException(409, "취소할 출석이 없습니다")
        row.cancelled_at = now
    else:
        raise HTTPException(422, "잘못된 정정 유형입니다")
    db.add(CampAudit(camp_id=camp_id, session_id=session_id, user_id=student.id, actor_id=user.id,
                     action=payload.action, reason=payload.reason, created_at=now))
    await db.commit()
    return {"ok": True}
