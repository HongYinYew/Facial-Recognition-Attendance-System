# migrate_data.py
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import datetime

# --- IMPORT YOUR MODELS TO CREATE TABLES ---
# Make sure models.py is in the same folder
from models import Base, User, Event, Attendance, UserFace
# -------------------------------------------

# 1. CONFIGURATION
# OLD DB: MySQL (Adjust user/pass/db name if needed)
OLD_DB_URL = "mysql+pymysql://root:123456@localhost/fras_dev" 
# NEW DB: PostgreSQL (Docker)
NEW_DB_URL = "postgresql+psycopg://postgres:password@localhost:5432/attendance_db"

# 2. SETUP CONNECTIONS
old_engine = create_engine(OLD_DB_URL)
new_engine = create_engine(NEW_DB_URL)

def migrate():
    print("--- INITIALIZING NEW DATABASE ---")
    
    # --- FIX: ENABLE VECTOR EXTENSION MANUALLY ---
    try:
        with new_engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
            print("✅ PGVector extension enabled.")
    except Exception as e:
        print(f"⚠️ Warning enabling vector extension (might already exist): {e}")
    # ---------------------------------------------

    # --- CREATE TABLES ---
    print("Creating tables in the new database...")
    try:
        Base.metadata.create_all(bind=new_engine)
        print("✅ Tables created successfully.")
    except Exception as e:
        print(f"❌ Error creating tables: {e}")
        return

    # --- DATA MIGRATION ---
    with old_engine.connect() as old_conn, new_engine.connect() as new_conn:
        print("--- MIGRATING USERS ---")
        users = old_conn.execute(text("SELECT uuid, name, englishName, phoneNumber FROM users"))
        
        for u in users:
            # Check if user already exists
            exists = new_conn.execute(text("SELECT 1 FROM users WHERE id = :id"), {"id": u.uuid}).first()
            if not exists:
                query = text("INSERT INTO users (id, name, english_name, phone, has_image, password_hash) VALUES (:id, :name, :en, :ph, :img, :pw)")
                new_conn.execute(query, {
                    "id": u.uuid, 
                    "name": u.name, 
                    "en": u.englishName, 
                    "ph": u.phoneNumber, 
                    "img": False, 
                    "pw": "$2b$12$B7BceHBzafVEFLpIaZ0m7eZYH5jGnY.RP9QPRLczzVKMIaeZ5GLoW"
                })
        
        print("--- MIGRATING EVENTS ---")
        events = old_conn.execute(text("SELECT * FROM events"))
        for e in events:
            exists = new_conn.execute(text("SELECT 1 FROM events WHERE id = :id"), {"id": e.eventId}).first()
            if not exists:
                query = text("INSERT INTO events (id, name, start_time, end_time, venue, venue_other) VALUES (:id, :name, :st, :et, :v, :vo)")
                new_conn.execute(query, {
                    "id": e.eventId, 
                    "name": e.name, 
                    "st": e.datetime, 
                    "et": e.datetime,
                    "v": e.location, 
                    "vo": None
                })

        print("--- MIGRATING ATTENDANCE ---")
        attendance = old_conn.execute(text("SELECT * FROM attendance_log"))
        for a in attendance:
            exists = new_conn.execute(text("SELECT 1 FROM attendance WHERE id = :id"), {"id": a.id}).first()
            if not exists:
                query = text("INSERT INTO attendance (id, user_id, event_id, timestamp, method) VALUES (:id, :uid, :eid, :ts, :meth)")
                new_conn.execute(query, {
                    "id": a.id, 
                    "uid": a.uuid, 
                    "eid": a.eventId, 
                    "ts": a.timestamp, 
                    "meth": "UNKNOWN"
                })
        
        new_conn.commit()
        print("✅ Migration Complete!")

        print("--- FIXING DATABASE SEQUENCES ---")
        with new_conn.connect() as conn:
            # 1. Fix Events Table
            print("Fixing 'events' table sequence...")
            # This SQL gets the max ID and sets the sequence to next value
            conn.execute(text("SELECT setval(pg_get_serial_sequence('events', 'id'), coalesce(max(id), 0) + 1, false) FROM events"))
            
            # 2. Fix Attendance Table
            print("Fixing 'attendance' table sequence...")
            conn.execute(text("SELECT setval(pg_get_serial_sequence('attendance', 'id'), coalesce(max(id), 0) + 1, false) FROM attendance"))
            
            # 3. Fix User Faces Table
            print("Fixing 'user_faces' table sequence...")
            conn.execute(text("SELECT setval(pg_get_serial_sequence('user_faces', 'id'), coalesce(max(id), 0) + 1, false) FROM user_faces"))
            
            conn.commit()
            print("✅ Success! New records will now start from the correct number.")

if __name__ == "__main__":
    migrate()