from sqlalchemy import Column, Integer, String, DateTime, Boolean, LargeBinary, ForeignKey
from sqlalchemy.orm import relationship
from database import Base
import uuid
import datetime

class User(Base):
    __tablename__ = "users"
    
    id = Column(String, primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, index=True)
    english_name = Column(String)
    phone = Column(String)
    has_image = Column(Boolean, default=False)
    
    # Relationship: One User -> Many Faces
    faces = relationship("UserFace", back_populates="user")

class UserFace(Base):
    __tablename__ = "user_faces"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"))
    encoding = Column(LargeBinary, nullable=False) # The math data
    created_at = Column(DateTime, default=datetime.datetime.now)
    
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