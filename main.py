import cv2
import numpy as np
import face_recognition
import pandas as pd
import asyncio
import uuid
import io
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from typing import List
from database import SessionLocal, engine, Base
from models import User, Event, Attendance, UserFace
from sqlalchemy import text
from fastapi import FastAPI, Request, Form, File, UploadFile, Depends, HTTPException, WebSocket, WebSocketDisconnect, status, Response, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

# PDF Generation
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

# Security & DB
from passlib.context import CryptContext
from jose import JWTError, jwt
from sqlalchemy.orm import Session
from sqlalchemy import create_engine, text
from pgvector.sqlalchemy import Vector

# --- CONFIGURATION ---
SECRET_KEY = "CHANGE_THIS_TO_A_SUPER_SECRET_KEY"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

# --- SECURITY UTILS ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

def verify_password(plain, hashed):
    if plain is None: return False
    return pwd_context.verify(plain[:72], hashed)

def get_password_hash(password): return pwd_context.hash(password)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# --- DB INIT ---
try:
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
        print("PGVector extension enabled successfully.")
except Exception as e:
    print(f"Warning: Could not enable pgvector extension: {e}")

try:
    Base.metadata.create_all(bind=engine)
except Exception as e:
    print(f"DB Init Error: {e}")

# --- APP SETUP ---
app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- WEBSOCKET MANAGER ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, event_id: int):
        await websocket.accept()
        if event_id not in self.active_connections:
            self.active_connections[event_id] = []
        self.active_connections[event_id].append(websocket)

    def disconnect(self, websocket: WebSocket, event_id: int):
        if event_id in self.active_connections:
            if websocket in self.active_connections[event_id]:
                self.active_connections[event_id].remove(websocket)

    async def broadcast_attendance(self, event_id: int, user_data: dict):
        if event_id in self.active_connections:
            for connection in self.active_connections[event_id]:
                await connection.send_json(user_data)

manager = ConnectionManager()

# --- DEPENDENCIES ---
def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None: raise HTTPException(status_code=401)
    except JWTError:
        raise HTTPException(status_code=401)
    user = db.query(User).filter(User.english_name == username).first()
    if user is None: raise HTTPException(status_code=401)
    return user

async def get_current_user_from_cookie(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("access_token")
    if not token: return None
    try:
        scheme, _, param = token.partition(" ")
        return await get_current_user(param, db)
    except: return None

# NEW: Dependency to check for Admin status
async def get_current_admin_user(user: User = Depends(get_current_user_from_cookie)):
    if not user: return None # Will trigger redirect in route
    if not user.is_admin: return None # Treat non-admin as unauthorized for protected routes
    return user

# --- HELPER FUNCTIONS ---
def check_blur_variance(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    score = cv2.Laplacian(gray, cv2.CV_64F).var()
    return score > 100 

def compute_embedding(image):
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    boxes = face_recognition.face_locations(rgb)
    if not boxes: return None
    encodings = face_recognition.face_encodings(rgb, boxes)
    return encodings[0] if encodings else None

# --- AUTH ROUTES ---
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/token")
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.english_name == form_data.username).first()
    if not user or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    
    access_token = create_access_token(data={"sub": user.english_name})
    response = RedirectResponse(url="/", status_code=303) # Redirect to Dashboard instead of Admin
    response.set_cookie(key="access_token", value=f"Bearer {access_token}", httponly=True)
    return response

@app.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("access_token")
    return resp

# --- PAGE ROUTES ---
@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request, 
    db: Session = Depends(get_db), 
    user: User = Depends(get_current_user_from_cookie)
):
    total_members = db.query(User).count()
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    attendance_today = db.query(Attendance.user_id).filter(Attendance.timestamp >= today_start).distinct().count()
    now = datetime.now()
    events_today = db.query(Event).filter(Event.start_time >= today_start, Event.start_time < today_end).count()
    upcoming_events = db.query(Event).filter(Event.start_time > now).count()
    past_events = db.query(Event).filter(Event.start_time < now).count()

    return templates.TemplateResponse("home.html", {
        "request": request, 
        "total_members": total_members, 
        "attendance_today": attendance_today,
        "events_today": events_today,
        "upcoming_events": upcoming_events,
        "past_events": past_events,
        "user": user,
        "date": datetime.now().strftime("%m-%d-%Y")
    })

# PROTECTED: ADMIN ONLY
@app.get("/admin", response_class=HTMLResponse)
async def admin_hub(request: Request, user: User = Depends(get_current_admin_user), db: Session = Depends(get_db)):
    if not user: return RedirectResponse("/") # Redirect non-admins to Dashboard
    
    users = db.query(User).order_by(User.name.asc()).all()
    now = datetime.now()
    upcoming_events = db.query(Event).filter(Event.start_time >= now).order_by(Event.start_time.asc()).all()
    past_events = db.query(Event).filter(Event.start_time < now).order_by(Event.start_time.desc()).all()
    
    return templates.TemplateResponse("admin_hub.html", {
        "request": request, "users": users, "upcoming_events": upcoming_events, "past_events": past_events, "user": user
    })

# PROTECTED: ADMIN ONLY
@app.get("/events", response_class=HTMLResponse)
async def events_panel(
    request: Request, 
    user: User = Depends(get_current_user_from_cookie), 
    db: Session = Depends(get_db)
):
    if not user: return RedirectResponse("/login")
    
    now = datetime.now()
    # Upcoming: Sorted by soonest first
    upcoming_events = db.query(Event).filter(Event.start_time >= now).order_by(Event.start_time.asc()).all()
    # Past: Sorted by newest first
    past_events = db.query(Event).filter(Event.start_time < now).order_by(Event.start_time.desc()).all()
    
    return templates.TemplateResponse("events.html", {
        "request": request, 
        "upcoming_events": upcoming_events, 
        "past_events": past_events, 
        "user": user
    })

# PROTECTED: ADMIN ONLY
@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, user: User = Depends(get_current_admin_user)):
    if not user: return RedirectResponse("/")
    return templates.TemplateResponse("register.html", {"request": request, "user": user})

# PROTECTED: ADMIN ONLY
@app.get("/reports", response_class=HTMLResponse)
async def report_page(
    request: Request, 
    event_ids: List[str] = Query(None),
    user_ids: List[str] = Query(None),
    user: User = Depends(get_current_admin_user), 
    db: Session = Depends(get_db)
):
    if not user: return RedirectResponse("/")
    
    query = db.query(Attendance).join(Event).join(User)
    if event_ids:
        e_ids = [int(e) for e in event_ids if e.isdigit()]
        if e_ids: query = query.filter(Attendance.event_id.in_(e_ids))
    if user_ids:
        query = query.filter(Attendance.user_id.in_(user_ids))
    
    records = query.order_by(Attendance.timestamp.desc()).all()
    all_events = db.query(Event).order_by(Event.start_time.desc()).all()
    all_users = db.query(User).order_by(User.name.asc()).all()
    
    return templates.TemplateResponse("reports.html", {
        "request": request, 
        "events": all_events, 
        "users": all_users, 
        "attendance_records": records,
        "selected_events": event_ids or [],
        "selected_users": user_ids or [],
        "user": user
    })

# PROTECTED: ADMIN ONLY
@app.get("/manual/{event_id}", response_class=HTMLResponse)
async def manual_page(request: Request, event_id: int, user: User = Depends(get_current_admin_user), db: Session = Depends(get_db)):
    if not user: return RedirectResponse("/")
    event = db.query(Event).filter(Event.id == event_id).first()
    users = db.query(User).order_by(User.name.asc()).all()
    attended_ids = [a.user_id for a in db.query(Attendance).filter(Attendance.event_id == event_id).all()]
    return templates.TemplateResponse("manual.html", {"request": request, "event": event, "users": users, "attended_ids": attended_ids, "user": user})

# PROTECTED: ADMIN ONLY
@app.get("/attendance/{event_id}", response_class=HTMLResponse)
async def attendance_page(request: Request, event_id: int, user: User = Depends(get_current_admin_user), db: Session = Depends(get_db)):
    if not user: return RedirectResponse("/")
    event = db.query(Event).filter(Event.id == event_id).first()
    return templates.TemplateResponse("attendance.html", {"request": request, "event": event, "user": user})

# PROTECTED: ADMIN ONLY
@app.get("/admin/enroll_face", response_class=HTMLResponse)
async def enroll_face_page(request: Request, user: User = Depends(get_current_admin_user), db: Session = Depends(get_db)):
    if not user: return RedirectResponse("/")
    users = db.query(User).order_by(User.name.asc()).all()
    return templates.TemplateResponse("enroll_face.html", {"request": request, "users": users, "user": user})

# --- EDIT & DELETE ACTIONS (ADMIN ONLY) ---
@app.get("/admin/edit_user/{user_id}", response_class=HTMLResponse)
async def edit_user_page(request: Request, user_id: str, user: User = Depends(get_current_admin_user), db: Session = Depends(get_db)):
    if not user: return RedirectResponse("/")
    target_user = db.query(User).filter(User.id == user_id).first()
    return templates.TemplateResponse("edit_user.html", {"request": request, "user": target_user, "current_user": user})

@app.post("/api/edit_user/{user_id}")
async def edit_user_action(user_id: str, name: str = Form(...), english_name: str = Form(...), phone: str = Form(...), user: User = Depends(get_current_admin_user), db: Session = Depends(get_db)):
    if not user: return RedirectResponse("/")
    target_user = db.query(User).filter(User.id == user_id).first()
    if target_user:
        target_user.name = name
        target_user.english_name = english_name
        target_user.phone = phone
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)

@app.get("/admin/edit_event/{event_id}", response_class=HTMLResponse)
async def edit_event_page(request: Request, event_id: int, user: User = Depends(get_current_admin_user), db: Session = Depends(get_db)):
    if not user: return RedirectResponse("/")
    event = db.query(Event).filter(Event.id == event_id).first()
    duration = int((event.end_time - event.start_time).total_seconds() / 60)
    return templates.TemplateResponse("edit_event.html", {"request": request, "event": event, "duration": duration, "user": user})

@app.post("/api/edit_event/{event_id}")
async def edit_event_action(event_id: int, name: str = Form(...), venue: str = Form(...), date: str = Form(...), time: str = Form(...), duration_min: int = Form(...), user: User = Depends(get_current_admin_user), db: Session = Depends(get_db)):
    if not user: return JSONResponse({"status": "error", "message": "Unauthorized"}, 403)
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event: return JSONResponse({"status": "error", "message": "Not found"}, 404)
    new_start = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    new_end = new_start + timedelta(minutes=duration_min)
    clash = db.query(Event).filter(Event.id != event_id, Event.venue == venue, Event.end_time > new_start, Event.start_time < new_end).first()
    if clash: return JSONResponse({"status": "error", "message": "Venue Clash!"}, 400)
    event.name = name
    event.venue = venue
    event.start_time = new_start
    event.end_time = new_end
    db.commit()
    return JSONResponse({"status": "success", "message": "Updated"})

@app.post("/api/delete_user/{user_id}")
async def delete_user(user_id: str, user: User = Depends(get_current_admin_user), db: Session = Depends(get_db)):
    if not user: return JSONResponse({"status": "error", "message": "Unauthorized"}, 403)
    target = db.query(User).filter(User.id == user_id).first()
    if target:
        db.query(Attendance).filter(Attendance.user_id == user_id).delete()
        db.query(UserFace).filter(UserFace.user_id == user_id).delete()
        db.delete(target)
        db.commit()
        return JSONResponse({"status": "success", "message": "Deleted"})
    return JSONResponse({"status": "error", "message": "Not found"}, 404)

@app.post("/api/delete_event/{event_id}")
async def delete_event(event_id: int, user: User = Depends(get_current_admin_user), db: Session = Depends(get_db)):
    if not user: return JSONResponse({"status": "error", "message": "Unauthorized"}, 403)
    target = db.query(Event).filter(Event.id == event_id).first()
    if target:
        db.query(Attendance).filter(Attendance.event_id == event_id).delete()
        db.delete(target)
        db.commit()
        return JSONResponse({"status": "success", "message": "Deleted"})
    return JSONResponse({"status": "error", "message": "Not found"}, 404)

# --- ACTION APIs (ADMIN ONLY) ---
@app.post("/api/create_event")
async def create_event(
    name: str = Form(...), 
    venue: str = Form(...), 
    venue_other: str = Form(None), 
    date: str = Form(...), 
    time: str = Form(...), 
    duration_min: int = Form(...), 
    user: User = Depends(get_current_user_from_cookie), 
    db: Session = Depends(get_db)
):
    if not user: return JSONResponse({"status": "error", "message": "Not authenticated"}, 401)
    
    # 1. Safe Date Parsing
    try:
        start_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        return JSONResponse({"status": "error", "message": "Invalid Date/Time format. Use YYYY-MM-DD."}, 400)
        
    end_dt = start_dt + timedelta(minutes=duration_min)
    final_venue = venue if venue != "Others" else venue_other
    
    # 2. Clash Check
    clash = db.query(Event).filter(
        Event.venue == final_venue, 
        Event.end_time > start_dt, 
        Event.start_time < end_dt
    ).first()
    
    if clash: 
        return JSONResponse({"status": "error", "message": f"Venue Clash with event: {clash.name}"}, 400)
        
    new_event = Event(name=name, start_time=start_dt, end_time=end_dt, venue=final_venue, venue_other=venue_other)
    db.add(new_event)
    db.commit()
    return {"status": "success", "message": "Event Created Successfully"}

@app.post("/api/register_user")
def register_user(name: str = Form(...), english_name: str = Form(...), phone: str = Form(...), image_file: UploadFile = File(None), user: User = Depends(get_current_admin_user), db: Session = Depends(get_db)):
    if not user: return JSONResponse({"status": "error", "message": "Unauthorized"}, 403)
    user_uuid = str(uuid.uuid4())
    new_user = User(id=user_uuid, name=name, english_name=english_name, phone=phone, has_image=False)
    db.add(new_user)
    db.commit()
    if image_file and image_file.filename:
        contents = image_file.file.read()
        nparr = np.frombuffer(contents, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        encoding = compute_embedding(img)
        if encoding is not None:
            new_face = UserFace(user_id=user_uuid, embedding=encoding.tolist())
            db.add(new_face)
            new_user.has_image = True
            db.commit()
    return {"status": "success", "uuid": user_uuid}

@app.post("/api/mark_manual")
async def mark_manual(user_id: str = Form(...), event_id: int = Form(...), action: str = Form(...), user: User = Depends(get_current_admin_user), db: Session = Depends(get_db)):
    if not user: return JSONResponse({"status": "error", "message": "Unauthorized"}, 403)
    existing = db.query(Attendance).filter(Attendance.user_id == user_id, Attendance.event_id == event_id).first()
    if action == 'mark' and not existing:
        db.add(Attendance(user_id=user_id, event_id=event_id, method="MANUAL"))
        db.commit()
        await manager.broadcast_attendance(event_id, {"user_id": user_id, "status": "marked"})
    elif action == 'unmark' and existing:
        db.delete(existing)
        db.commit()
    return {"status": "success"}

@app.post("/api/recognize_attendance")
async def recognize_attendance(event_id: int = Form(...), image_file: UploadFile = File(...), db: Session = Depends(get_db)):
    # Note: Recognizing attendance is PUBLIC action (kiosk mode), so no admin check needed here usually.
    # If you want to restrict it to admin logged in device, add 'user: User = Depends(get_current_admin_user)'
    contents = await image_file.read()
    nparr = np.frombuffer(contents, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if not check_blur_variance(img): return {"status": "error", "message": "Image too blurry. Hold still."}
    user_found = None
    method = "UNKNOWN"
    detector = cv2.QRCodeDetector()
    data, bbox, _ = detector.detectAndDecode(img)
    if data:
        user = db.query(User).filter(User.id == data).first()
        if user: user_found = user; method = "QR"
    if not user_found:
        encoding = compute_embedding(img)
        if encoding is not None:
            embedding_list = encoding.tolist()
            match = db.query(UserFace).order_by(UserFace.embedding.l2_distance(embedding_list)).limit(1).first()
            if match:
                dist = np.linalg.norm(np.array(match.embedding) - encoding)
                if dist < 0.5: user_found = match.user; method = "FACE"
    if user_found:
        existing = db.query(Attendance).filter(Attendance.user_id == user_found.id, Attendance.event_id == event_id).first()
        if not existing:
            db.add(Attendance(user_id=user_found.id, event_id=event_id, method=method))
            db.commit()
            await manager.broadcast_attendance(event_id, {"user_id": user_found.id, "status": "marked"})
            return {"status": "success", "message": f"Welcome {user_found.english_name}", "user_name": user_found.english_name}
        return {"status": "info", "message": f"Already marked: {user_found.english_name}"}
    return {"status": "info", "message": "Scanning..."}

@app.post("/api/update_face")
def update_face(user_id: str = Form(...), image_file: UploadFile = File(...), user: User = Depends(get_current_admin_user), db: Session = Depends(get_db)):
    if not user: return JSONResponse({"status": "error", "message": "Unauthorized"}, 403)
    contents = image_file.file.read()
    nparr = np.frombuffer(contents, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if not check_blur_variance(img): return {"status": "error", "message": "Image too blurry to enroll. Try again."}
    encoding = compute_embedding(img)
    if encoding is not None:
        new_face = UserFace(user_id=user_id, embedding=encoding.tolist())
        db.add(new_face)
        user_target = db.query(User).filter(User.id == user_id).first()
        user_target.has_image = True
        db.commit()
        return {"status": "success", "message": "Face enrolled securely."}
    return {"status": "error", "message": "No face detected. Try adjusting light."}

@app.get("/api/download_report")
async def download_report(
    event_ids: List[str] = Query(None),
    user_ids: List[str] = Query(None),
    format: str = Query("excel"),
    user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    if not user: return RedirectResponse("/")
    
    query = db.query(Attendance).join(Event).join(User)
    if event_ids:
        e_ids = [int(e) for e in event_ids if e.isdigit()]
        if e_ids: query = query.filter(Attendance.event_id.in_(e_ids))
    if user_ids:
        query = query.filter(Attendance.user_id.in_(user_ids))
        
    records = query.order_by(Attendance.timestamp.desc()).all()
    
    data_rows = []
    for r in records:
        data_rows.append({
            "Date & Time": r.timestamp.strftime('%m-%d-%Y %I:%M %p'),
            "Event": r.event.name,
            "Member": r.user.english_name,
            "Full Name": r.user.name,
            "Method": r.method
        })

    if format == "excel":
        df = pd.DataFrame(data_rows)
        stream = io.BytesIO()
        with pd.ExcelWriter(stream) as writer: df.to_excel(writer, index=False)
        stream.seek(0)
        return HTMLResponse(content=stream.getvalue(), headers={'Content-Disposition': 'attachment; filename="report.xlsx"'}, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    
    elif format == "pdf":
        stream = io.BytesIO()
        doc = SimpleDocTemplate(stream, pagesize=letter)
        elements = []
        styles = getSampleStyleSheet()
        elements.append(Paragraph("Attendance Report", styles['Title']))
        elements.append(Spacer(1, 12))
        table_data = [["Date & Time", "Event", "Member", "Method"]]
        for row in data_rows:
            table_data.append([row["Date & Time"], row["Event"], row["Member"], row["Method"]])
        t = Table(table_data)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ]))
        elements.append(t)
        doc.build(elements)
        stream.seek(0)
        return HTMLResponse(content=stream.getvalue(), headers={'Content-Disposition': 'attachment; filename="report.pdf"'}, media_type="application/pdf")

@app.websocket("/ws/attendance/{event_id}")
async def websocket_endpoint(websocket: WebSocket, event_id: int):
    await manager.connect(websocket, event_id)
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, event_id)