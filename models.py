from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from database import Base
from pgvector.sqlalchemy import Vector
import uuid
import datetime

class User(Base):
    __tablename__ = "users"
    
    id = Column(String, primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    name = Column(String)
    
    # CHANGED: Removed 'unique=True'. Now duplicates/empty values are allowed.
    english_name = Column(String, index=True, nullable=True) 
    
    phone = Column(String)
    has_image = Column(Boolean, default=False)
    password_hash = Column(String, nullable=True) 
    is_admin = Column(Boolean, default=False)
    
    faces = relationship("UserFace", back_populates="user")

class UserFace(Base):
    __tablename__ = "user_faces"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"))
    embedding = Column(Vector(128)) 
    user = relationship("User", back_populates="faces")

class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    venue = Column(String)
    venue_other = Column(String, nullable=True)

class Attendance(Base):
    __tablename__ = "attendance"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"))
    event_id = Column(Integer, ForeignKey("events.id"))
    timestamp = Column(DateTime, default=datetime.datetime.now)
    method = Column(String)
    user = relationship("User")
    event = relationship("Event")