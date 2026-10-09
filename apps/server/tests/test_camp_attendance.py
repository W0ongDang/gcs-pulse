import asyncio
import hashlib
from datetime import datetime, timezone
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware
from app.database import Base, get_db
from app.models import User, ApiToken
from app.camp_models import CampAttendance
from app.routers.camp_attendance import router
from app.camp_stats import aggregate

ADMIN = "fixture-admin-token-not-a-real-secret"
DAY = "2026-10-09T12:00:00+09:00"


@pytest_asyncio.fixture
async def client(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "camp.db"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as db:
        db.add_all([User(id=i, email=f"student{i}@example.test", name=f"학생{i}", roles=["admin"] if i == 1 else ["user"])
                    for i in range(1, 9)])
        db.add(ApiToken(user_id=1, token_hash=hashlib.sha256(ADMIN.encode()).hexdigest()))
        await db.commit()
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-only-session-secret")
    app.include_router(router)
    async def session_db():
        async with factory() as db:
            yield db
    app.dependency_overrides[get_db] = session_db
    @app.get("/fixture/login/{uid}")
    async def login(uid: int, request: Request):
        request.session["user"] = {"email": f"student{uid}@example.test"}
        request.session["csrf_token"] = "fixture-csrf"
        return {"ok": True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        c.fixture_db = factory
        yield c
    await engine.dispose()


def admin_headers(now=DAY):
    return {"Authorization": "Bearer " + ADMIN, "X-Test-Now": now}


async def create_open(client, now=DAY):
    created = await client.post("/camp-attendance/camps", json={"name": "영어캠프", "members": [
        {"user_id": i, "affiliation": "가천대학교"} for i in range(2, 9)]}, headers=admin_headers(now))
    assert created.status_code == 200, created.text
    camp_id = created.json()["id"]
    r = await client.post(f"/camp-attendance/camps/{camp_id}/sessions", json={"date": now[:10], "title": "English"}, headers=admin_headers(now))
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    r = await client.put(f"/camp-attendance/camps/{camp_id}/sessions/{sid}/status", json={"status": "open"}, headers=admin_headers(now))
    assert r.status_code == 200, r.text
    state = (await client.get(f"/camp-attendance/camps/{camp_id}/admin", headers=admin_headers(now))).json()
    return camp_id, sid, state["sessions"][0]["qr_token"]


async def student_checkin(client, token, uid=2, now=DAY):
    await client.get(f"/fixture/login/{uid}")
    return await client.post(f"/camp-attendance/check-in/{token}", json={}, headers={"X-CSRF-Token": "fixture-csrf", "X-Test-Now": now})


@pytest.mark.asyncio
async def test_checkin_duplicate_close_and_public_privacy(client):
    camp, sid, token = await create_open(client)
    before = await client.get(f"/camp-attendance/camps/{camp}/public", headers={"X-Test-Now": DAY})
    assert before.json()["session"]["check_in_url"].endswith(token)
    first = await student_checkin(client, token)
    assert first.status_code == 200 and first.json()["status"] == "success"
    assert datetime.fromisoformat(first.json()["checked_at"]).astimezone(timezone.utc).hour == 3
    again = await student_checkin(client, token)
    assert again.json()["status"] == "duplicate"
    assert again.json()["checked_at"] == first.json()["checked_at"]
    close = await client.put(f"/camp-attendance/camps/{camp}/sessions/{sid}/status", json={"status": "closed"}, headers=admin_headers())
    assert close.status_code == 200
    assert (await student_checkin(client, token)).status_code == 409
    after = (await client.get(f"/camp-attendance/camps/{camp}/public", headers={"X-Test-Now": DAY})).json()
    assert after["session"]["status"] == "closed" and "check_in_url" not in after["session"]
    assert after["result"]["winner"]["name"] == "학생2"
    assert after["result"]["average"] == 1 and after["result"]["count"] == 1
    assert "records" not in after and "members" not in after
    assert "user_id" not in after["result"]["winner"]
    assert token not in str(after)


@pytest.mark.asyncio
async def test_authorization_csrf_and_previous_date(client):
    camp, sid, token = await create_open(client)
    await client.get("/fixture/login/2")
    assert (await client.get(f"/camp-attendance/camps/{camp}/admin")).status_code == 403
    assert (await client.post(f"/camp-attendance/check-in/{token}", json={})).status_code == 403
    assert (await student_checkin(client, token, now="2026-10-10T00:01:00+09:00")).status_code == 409
    assert (await student_checkin(client, "invalid-token")).status_code == 404
    await client.get("/fixture/login/1")
    assert (await client.post(f"/camp-attendance/check-in/{token}", headers={"X-CSRF-Token":"fixture-csrf", "X-Test-Now":DAY}, json={})).status_code == 403
    assert (await client.get(f"/camp-attendance/camps/{camp}/admin", headers={"Authorization":"Bearer invalid"})).status_code == 401


@pytest.mark.asyncio
async def test_correction_winner_recompute_and_audit(client):
    camp, sid, token = await create_open(client)
    await student_checkin(client, token, uid=2)
    await student_checkin(client, token, uid=3, now="2026-10-09T12:01:00+09:00")
    endpoint=f"/camp-attendance/camps/{camp}/sessions/{sid}/corrections"
    assert (await client.post(endpoint, json={"user_id":2,"action":"cancel","reason":" "}, headers=admin_headers())).status_code == 422
    assert (await client.post(endpoint, json={"user_id":2,"action":"cancel","reason":"잘못된 출석"}, headers=admin_headers())).status_code == 200
    assert (await student_checkin(client, token, uid=2)).status_code == 409
    assert (await client.post(endpoint, json={"user_id":2,"action":"add","reason":"현장 확인 후 복구"}, headers=admin_headers())).status_code == 200
    await client.put(f"/camp-attendance/camps/{camp}/sessions/{sid}/status", json={"status":"closed"}, headers=admin_headers())
    result=(await client.get(f"/camp-attendance/camps/{camp}/public",headers={"X-Test-Now":DAY})).json()["result"]
    assert result["winner"]["name"] == "학생3" and result["count"] == 2
    state=(await client.get(f"/camp-attendance/camps/{camp}/admin",headers=admin_headers())).json()
    assert any(a["reason"] == "현장 확인 후 복구" and a["actor_id"] == 1 for a in state["audit"])


@pytest.mark.asyncio
async def test_concurrent_requests_make_one_record(client):
    camp, sid, token = await create_open(client)
    await client.get("/fixture/login/2")
    responses = await asyncio.gather(*[client.post(f"/camp-attendance/check-in/{token}",json={},headers={"X-CSRF-Token":"fixture-csrf","X-Test-Now":DAY}) for _ in range(6)])
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert sum(r.json()["status"] == "success" for r in responses) == 1
    state=(await client.get(f"/camp-attendance/camps/{camp}/admin",headers=admin_headers())).json()
    assert len(state["records"]) == 1


@pytest.mark.asyncio
async def test_empty_close_and_duplicate_date(client):
    camp,sid,token=await create_open(client)
    duplicate=await client.post(f"/camp-attendance/camps/{camp}/sessions",json={"date":"2026-10-09","title":"Duplicate"},headers=admin_headers())
    assert duplicate.status_code == 409
    await client.put(f"/camp-attendance/camps/{camp}/sessions/{sid}/status",json={"status":"closed"},headers=admin_headers())
    result=(await client.get(f"/camp-attendance/camps/{camp}/public",headers={"X-Test-Now":DAY})).json()["result"]
    assert result["winner"] is None and result["count"] == 0 and result["average"] == 0
    assert result["newcomers"] == [] and result["leaderboard"] == []


def test_ratio_ties_newcomers_and_exclude_open_session():
    sessions=[{"id":i,"number":i,"date":f"2026-10-0{i}","status":"closed" if i<6 else "open"} for i in range(1,7)]
    records=[]
    for sid,ids in [(1,[2,3]),(2,[3,2]),(3,[2,3]),(4,[4]),(5,[4]),(6,[2])]:
        for offset,uid in enumerate(ids):
            records.append({"id":len(records)+1,"session_id":sid,"user_id":uid,"name":str(uid),"affiliation":"school","source":"qr","checked_at":f"2026-10-0{sid}T03:0{offset}:00+00:00"})
    members=[{"id":i,"name":str(i),"affiliation":"school"} for i in [2,3,4]]
    stats=aggregate(sessions,records,members,5)
    assert stats["closed_count"] == 5
    assert next(m for m in stats["individuals"] if m["id"]==2)["ratio"] == .6
    assert stats["average"] == 8/5 and stats["newcomers"] == []
    records[2]["source"]="manual"
    tied=aggregate(sessions,records,members)
    assert next(m for m in tied["leaderboard"] if m["id"]==2)["first_count"] == 3
    empty=aggregate([],[],members)
    assert all(m["ratio"] is None for m in empty["individuals"])


@pytest.mark.asyncio
async def test_close_racing_checkin_and_revoked_token(client):
    camp,sid,token=await create_open(client)
    await client.get('/fixture/login/2')
    results=await asyncio.gather(
        client.post(f'/camp-attendance/check-in/{token}',json={},headers={'X-CSRF-Token':'fixture-csrf','X-Test-Now':DAY}),
        client.put(f'/camp-attendance/camps/{camp}/sessions/{sid}/status',json={'status':'closed'},headers=admin_headers()))
    assert results[0].status_code in (200,409) and results[1].status_code==200
    state=(await client.get(f'/camp-attendance/camps/{camp}/admin',headers=admin_headers())).json()
    assert len(state['records']) == (1 if results[0].status_code==200 else 0)
    assert (await student_checkin(client,token,uid=3)).status_code==409
    async with client.fixture_db() as db:
        api_token=(await db.execute(select(ApiToken))).scalar_one()
        api_token.revoked_at=datetime.now(timezone.utc)
        await db.commit()
    assert (await client.get('/camp-attendance/identity',headers=admin_headers())).status_code==401


@pytest.mark.asyncio
async def test_remove_roster_member_preserves_history(client):
    camp,sid,token=await create_open(client)
    await student_checkin(client,token,uid=2)
    update=await client.put(f'/camp-attendance/camps/{camp}/members',json=[{'user_id':3,'affiliation':'새 소속'}],headers=admin_headers())
    assert update.status_code==200
    assert (await student_checkin(client,token,uid=2)).status_code==403
    state=(await client.get(f'/camp-attendance/camps/{camp}/admin',headers=admin_headers())).json()
    assert len(state['records'])==1 and state['records'][0]['name']=='학생2'
    assert next(m for m in state['members'] if m['id']==2)['active'] is False
