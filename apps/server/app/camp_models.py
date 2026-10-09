"""English camp records. Imported by models so the existing migration script creates them."""
from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from app.database import Base


class Camp(Base):
    __tablename__ = "attendance_camps"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    version = Column(Integer, nullable=False, default=0)


class CampMember(Base):
    __tablename__ = "attendance_camp_members"
    __table_args__ = (UniqueConstraint("camp_id", "user_id"),)
    id = Column(Integer, primary_key=True)
    camp_id = Column(Integer, ForeignKey("attendance_camps.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    affiliation = Column(String(120), nullable=False)
    active = Column(Boolean, nullable=False, default=True)


class CampSession(Base):
    __tablename__ = "attendance_camp_sessions"
    __table_args__ = (UniqueConstraint("camp_id", "date"), UniqueConstraint("camp_id", "number"))
    id = Column(Integer, primary_key=True)
    camp_id = Column(Integer, ForeignKey("attendance_camps.id"), nullable=False, index=True)
    number = Column(Integer, nullable=False)
    date = Column(Date, nullable=False)
    title = Column(String(120), nullable=False)
    status = Column(String(16), nullable=False, default="scheduled")
    qr_token = Column(String(100), nullable=False, unique=True)
    opened_at = Column(DateTime(timezone=True))
    closed_at = Column(DateTime(timezone=True))


class CampAttendance(Base):
    __tablename__ = "attendance_camp_records"
    __table_args__ = (UniqueConstraint("session_id", "user_id"),)
    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("attendance_camp_sessions.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(120), nullable=False)
    affiliation = Column(String(120), nullable=False)
    source = Column(String(16), nullable=False)
    checked_at = Column(DateTime(timezone=True), nullable=False)
    cancelled_at = Column(DateTime(timezone=True))


class CampAudit(Base):
    __tablename__ = "attendance_camp_audit"
    id = Column(Integer, primary_key=True)
    camp_id = Column(Integer, ForeignKey("attendance_camps.id"), nullable=False, index=True)
    session_id = Column(Integer, ForeignKey("attendance_camp_sessions.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"))
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    action = Column(String(30), nullable=False)
    reason = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
