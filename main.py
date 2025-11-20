import pickle
import cv2
import numpy as np
import face_recognition # Keep commented out if dlib is failing
import pandas as pd
from fastapi import FastAPI, Request, Form, File, UploadFile, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import uuid
import io

from database import SessionLocal, engine, Base
from models import User, Event, Attendance, UserFace

Base.metadata.create_all(bind=engine)

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

known_face_encodings = []
known_face_ids = []

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

def load_faces_from_db():
    global known_face_encodings, known_face_ids
    print("Loading faces from database...")
    db = SessionLocal()
    try:
        # Fetch all face records from the UserFace table
        all_faces = db.query(UserFace).all()
        
        known_face_encodings = []
        known_face_ids = []
        
        for face_record in all_faces:
            try:
                # Load the math data
                enc = pickle.loads(face_record.encoding)
                known_face_encodings.append(enc)
                # Link to the User object (face_record.user)
                known_face_ids.append(face_record.user)
            except Exception as e:
                print(f"Error loading face {face_record.id}: {e}")
                
        print(f"Cache Updated: {len(known_face_encodings)} face variations loaded.")
    finally:
        db.close()

@app.on_event("startup")
def startup_event():
    load_faces_from_db()

# --- ROUTES: PUBLIC & ATTENDANCE ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, db: Session = Depends(get_db)):
    total_members = db.query(User).count()
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    attendance_today = db.query(Attendance.user_id).filter(Attendance.timestamp >= today_start).distinct().count()
    now = datetime.now()
    upcoming_events = db.query(Event).filter(Event.start_time > now).count()
    passed_events = db.query(Event).filter(Event.end_time < now).count()
    total_events = db.query(Event).count()

    return templates.TemplateResponse("home.html", {
        "request": request, "total_members": total_members, "attendance_today": attendance_today,
        "upcoming_events": upcoming_events, "passed_events": passed_events, "total_events": total_events
    })

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

@app.get("/attendance/{event_id}", response_class=HTMLResponse)
async def attendance_page(request: Request, event_id: int, db: Session = Depends(get_db)):
    event = db.query(Event).filter(Event.id == event_id).first()
    return templates.TemplateResponse("attendance.html", {"request": request, "event": event})

@app.get("/manual/{event_id}", response_class=HTMLResponse)
async def manual_page(request: Request, event_id: int, db: Session = Depends(get_db)):
    event = db.query(Event).filter(Event.id == event_id).first()
    users = db.query(User).all()
    attended_ids = [a.user_id for a in db.query(Attendance).filter(Attendance.event_id == event_id).all()]
    return templates.TemplateResponse("manual.html", {"request": request, "event": event, "users": users, "attended_ids": attended_ids})

@app.get("/reports", response_class=HTMLResponse)
async def report_page(request: Request, db: Session = Depends(get_db)):
    events = db.query(Event).all()
    users = db.query(User).all()
    return templates.TemplateResponse("reports.html", {"request": request, "events": events, "users": users})

# --- ROUTES: ADMIN & MANAGEMENT ---

@app.get("/admin", response_class=HTMLResponse)
async def admin_hub(request: Request, db: Session = Depends(get_db)):
    users = db.query(User).all()
    events = db.query(Event).order_by(Event.start_time.desc()).limit(10).all()
    return templates.TemplateResponse("admin_hub.html", {"request": request, "users": users, "events": events})

@app.get("/events", response_class=HTMLResponse)
async def events_panel(request: Request, db: Session = Depends(get_db)):
    events = db.query(Event).order_by(Event.start_time.desc()).all()
    return templates.TemplateResponse("events.html", {"request": request, "events": events})

# NEW: ENROLL FACE PAGE
@app.get("/admin/enroll_face", response_class=HTMLResponse)
async def enroll_face_page(request: Request, db: Session = Depends(get_db)):
    # Fetch users and pre-load their faces relationship
    users = db.query(User).order_by(User.english_name).all()
    return templates.TemplateResponse("enroll_face.html", {"request": request, "users": users})

@app.get("/admin/edit_user/{user_id}", response_class=HTMLResponse)
async def edit_user_page(request: Request, user_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    return templates.TemplateResponse("edit_user.html", {"request": request, "user": user})

@app.post("/api/edit_user/{user_id}")
async def edit_user_action(user_id: str, name: str = Form(...), english_name: str = Form(...), phone: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.name = name
        user.english_name = english_name
        user.phone = phone
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)

@app.get("/admin/edit_event/{event_id}", response_class=HTMLResponse)
async def edit_event_page(request: Request, event_id: int, db: Session = Depends(get_db)):
    event = db.query(Event).filter(Event.id == event_id).first()
    # Calculate duration for the form
    duration = (event.end_time - event.start_time).total_seconds() / 60
    return templates.TemplateResponse("edit_event.html", {"request": request, "event": event, "duration": int(duration)})

# UPDATED: EDIT EVENT WITH CLASH CHECK
@app.post("/api/edit_event/{event_id}")
async def edit_event_action(
    event_id: int, 
    name: str = Form(...), 
    venue: str = Form(...), 
    date: str = Form(...), 
    time: str = Form(...), 
    duration_min: int = Form(...),
    db: Session = Depends(get_db)
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        return JSONResponse(content={"status": "error", "message": "Event not found"}, status_code=404)

    # Calculate New Times
    new_start = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    new_end = new_start + timedelta(minutes=duration_min)

    # Check Clash (Exclude current event ID)
    clash = db.query(Event).filter(
        Event.id != event_id, # Don't clash with self
        Event.venue == venue,
        Event.end_time > new_start,
        Event.start_time < new_end
    ).first()

    if clash:
        return JSONResponse(content={"status": "error", "message": f"Venue Clash with '{clash.name}'!"}, status_code=400)

    # Update
    event.name = name
    event.venue = venue
    event.start_time = new_start
    event.end_time = new_end
    db.commit()
    
    return JSONResponse(content={"status": "success", "message": "Event updated successfully"})

# NEW: UPDATE FACE API
@app.post("/api/update_face")
async def update_face(
    user_id: str = Form(...),
    image_file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user: return {"status": "error", "message": "User not found"}

    # Read Image
    contents = await image_file.read()
    nparr = np.frombuffer(contents, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    # Find Face
    encs = face_recognition.face_encodings(rgb)
    
    if encs:
        # STACKING LOGIC: Create a NEW entry in UserFace
        new_face_entry = UserFace(
            user_id=user.id, 
            encoding=pickle.dumps(encs[0])
        )
        db.add(new_face_entry)
        
        # Ensure the main User flag is set
        user.has_image = True
        
        db.commit()
        
        # Refresh Cache so the new angle works immediately
        load_faces_from_db()
        
        return {"status": "success", "message": f"New face angle added for {user.english_name}."}
    else:
        return {"status": "error", "message": "No face detected. Try again."}

# --- API ENDPOINTS (Standard) ---

@app.get("/api/live_attendance/{event_id}")
async def get_live_attendance(event_id: int, db: Session = Depends(get_db)):
    records = db.query(Attendance.user_id).filter(Attendance.event_id == event_id).all()
    ids = [r[0] for r in records]
    return {"attended_ids": ids}

@app.post("/api/register_user")
async def register_user(
    name: str = Form(...),
    english_name: str = Form(...),
    phone: str = Form(...),
    image_file: UploadFile = File(None),
    db: Session = Depends(get_db)
):
    user_uuid = str(uuid.uuid4())
    has_img = False
    encoding_bytes = None

    # 1. Process Image (If uploaded)
    if image_file and image_file.filename:
        try:
            contents = await image_file.read()
            nparr = np.frombuffer(contents, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            
            if img is not None:
                # Convert to RGB for face_recognition
                rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                encs = face_recognition.face_encodings(rgb)
                
                if encs:
                    # We found a face! Prepare the data
                    encoding_bytes = pickle.dumps(encs[0])
                    has_img = True
                else:
                    print("Warning: Image uploaded but no face detected.")
        except Exception as e:
            print(f"Error processing image during register: {e}")

    # 2. Create User Record (Note: No face_encoding passed here)
    new_user = User(
        id=user_uuid, 
        name=name, 
        english_name=english_name, 
        phone=phone, 
        has_image=has_img
    )
    db.add(new_user)
    db.commit() # Commit to establish the User ID first

    # 3. Create Face Record (If encoding exists)
    if encoding_bytes:
        new_face_entry = UserFace(
            user_id=user_uuid, 
            encoding=encoding_bytes
        )
        db.add(new_face_entry)
        db.commit()

    # 4. Refresh Global Cache (Important!)
    load_faces_from_db()

    return {"status": "success", "uuid": user_uuid}

@app.post("/api/create_event")
async def create_event(name: str = Form(...), venue: str = Form(...), venue_other: str = Form(None), date: str = Form(...), time: str = Form(...), duration_min: int = Form(...), db: Session = Depends(get_db)):
    start_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    end_dt = start_dt + timedelta(minutes=duration_min)
    final_venue = venue if venue != "Others" else venue_other
    
    if db.query(Event).filter(Event.venue == final_venue, Event.end_time > start_dt, Event.start_time < end_dt).first():
        return {"status": "error", "message": "Venue Clash Detected!"}
        
    new_event = Event(name=name, start_time=start_dt, end_time=end_dt, venue=final_venue, venue_other=venue_other)
    db.add(new_event)
    db.commit()
    return {"status": "success"}

@app.post("/api/mark_manual")
async def mark_manual(user_id: str = Form(...), event_id: int = Form(...), action: str = Form(...), db: Session = Depends(get_db)):
    existing = db.query(Attendance).filter(Attendance.user_id == user_id, Attendance.event_id == event_id).first()
    if action == 'mark' and not existing: db.add(Attendance(user_id=user_id, event_id=event_id, method="MANUAL"))
    elif action == 'unmark' and existing: db.delete(existing)
    db.commit()
    return {"status": "success"}

@app.post("/api/recognize_attendance")
async def recognize_attendance(
    event_id: int = Form(...), 
    image_file: UploadFile = File(...), 
    db: Session = Depends(get_db)
):
    contents = await image_file.read()
    nparr = np.frombuffer(contents, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    
    user_found = None
    method = "UNKNOWN"

    # 1. QR CODE
    detector = cv2.QRCodeDetector()
    data, bbox, _ = detector.detectAndDecode(img)
    if data:
        user = db.query(User).filter(User.id == data).first()
        if user: 
            user_found = user
            method = "QR"

    # 2. FACE RECOGNITION (Uses Cache)
    if not user_found and known_face_encodings:
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        face_locs = face_recognition.face_locations(rgb)
        unknown_encodings = face_recognition.face_encodings(rgb, face_locs)

        if unknown_encodings:
            # Compare found face against ALL enrolled variations in RAM
            matches = face_recognition.compare_faces(known_face_encodings, unknown_encodings[0], tolerance=0.5)
            
            if True in matches:
                first_match_index = matches.index(True)
                user_found = known_face_ids[first_match_index]
                method = "FACE"

    # 3. MARK ATTENDANCE
    if user_found:
        existing = db.query(Attendance).filter(
            Attendance.user_id == user_found.id, 
            Attendance.event_id == event_id
        ).first()
        
        if not existing:
            db.add(Attendance(user_id=user_found.id, event_id=event_id, method=method))
            db.commit()
            return {"status": "success", "message": f"Welcome, {user_found.english_name}!", "user_name": user_found.english_name}
        return {"status": "info", "message": f"Already marked: {user_found.english_name}"}

    return {"status": "info", "message": "Scanning..."}

@app.get("/api/download_report")
async def download_report(event_id: int = None, user_id: str = None, db: Session = Depends(get_db)):
    query = db.query(Attendance)
    if event_id: query = query.filter(Attendance.event_id == event_id)
    if user_id: query = query.filter(Attendance.user_id == user_id)
    
    data = [{"Event": r.event.name, "User": r.user.name, "Time": r.timestamp, "Method": r.method} for r in query.all()]
    df = pd.DataFrame(data)
    stream = io.BytesIO()
    with pd.ExcelWriter(stream) as writer: df.to_excel(writer, index=False)
    stream.seek(0)
    return HTMLResponse(content=stream.getvalue(), headers={'Content-Disposition': 'attachment; filename="report.xlsx"'}, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")