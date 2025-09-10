import os
import sys
import threading
import traceback
import sqlite3
import os
from datetime import datetime
from flask import Flask, request, abort
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator

# Force unbuffered logging so print() always shows in Render logs
sys.stdout.reconfigure(line_buffering=True)

app = Flask(__name__)
print("[STARTUP] Webhook service started and ready (SQLite).")

# ==========================
# 🔐 Environment Config
# ==========================
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
DB_PATH = os.path.join(os.path.dirname(__file__), "customers.db")


# ==========================
# 📑 SQLite Setup
# ==========================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Customers table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        phone TEXT UNIQUE,
        dnc BOOLEAN DEFAULT 0,
        optin_date TEXT,
        optin_source TEXT,
        optout_date TEXT
    )
    """)

    # Messages table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        name TEXT,
        phone TEXT,
        type TEXT,
        message TEXT,
        status TEXT,
        error TEXT,
        sid TEXT UNIQUE
    )
    """)

    conn.commit()
    conn.close()
    print("[DB] ✅ Tables ensured (customers, messages)")

def get_db_connection():
    # Allow multithread access + wait if DB is busy
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def iso_now():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"

def normalize_e164(wa_from: str) -> str:
    return (wa_from or "").replace("whatsapp:", "").strip()

# Ensure DB is ready at startup
init_db()

# ==========================
# 🔒 Twilio Validation
# ==========================
validator = RequestValidator(TWILIO_AUTH_TOKEN)
def is_valid_twilio_request(req) -> bool:
    if not TWILIO_AUTH_TOKEN:  # allow if no token set (dev mode)
        return True
    signature = req.headers.get("X-Twilio-Signature", "")
    return validator.validate(req.url, req.form.to_dict(), signature)

# ==========================
# 📩 Background handlers
# ==========================
def handle_unsubscribe(phone):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Try to update first
        cur.execute(
            "UPDATE customers SET dnc=1, optout_date=? WHERE phone=?",
            (iso_now(), phone)
        )
        if cur.rowcount == 0:
            # If not found, insert new row
            cur.execute(
                "INSERT INTO customers (phone, dnc, optout_date) VALUES (?, 1, ?)",
                (phone, iso_now())
            )
        conn.commit()
        conn.close()
        print(f"[UNSUBSCRIBE] ✅ Phone {phone} set to DNC=1")
    except Exception as e:
        print("[ERROR] handle_unsubscribe failed:", e)
        traceback.print_exc()
print("[DB DEBUG] DB path in webhook:", os.path.abspath(DB_PATH))


def handle_resubscribe(phone):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Try to update first
        cur.execute(
            "UPDATE customers SET dnc=0, optin_source=?, optin_date=? WHERE phone=?",
            ("Resubscribe", iso_now(), phone)
        )
        if cur.rowcount == 0:
            # If not found, insert new row
            cur.execute(
                "INSERT INTO customers (phone, dnc, optin_source, optin_date) VALUES (?, 0, ?, ?)",
                (phone, "Resubscribe", iso_now())
            )
        conn.commit()
        conn.close()
        print(f"[RESUBSCRIBE] ✅ Phone {phone} reactivated")
    except Exception as e:
        print("[ERROR] handle_resubscribe failed:", e)
        traceback.print_exc()


def update_message_status(sid, status, error):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE messages SET status=?, error=? WHERE sid=?",
            (status, error, sid)
        )
        if cur.rowcount == 0:
            # If no existing row, insert new record
            cur.execute(
                "INSERT INTO messages (date, status, error, sid) VALUES (?, ?, ?, ?)",
                (iso_now(), status, error, sid)
            )
        conn.commit()
        conn.close()
        print(f"[STATUS] ✅ Updated/Inserted SID={sid} → {status} ({error})")
    except Exception as e:
        print("[ERROR] update_message_status failed:", e)
        traceback.print_exc()

# ==========================
# 📩 Inbound Handler
# ==========================
@app.post("/twilio/inbound")
def inbound():
    if not is_valid_twilio_request(request):
        abort(403)

    from_num = normalize_e164(request.form.get("From"))
    body = (request.form.get("Body") or "").strip().upper()
    sid = (request.form.get("MessageSid") or "").strip()
    resp = MessagingResponse()

    print(f"[INBOUND] Message from {from_num}: {body}")

    # Save inbound message in DB
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO messages (date, phone, type, message, status, sid)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (iso_now(), from_num, "inbound", body, "received", sid))
        conn.commit()
        conn.close()
        print(f"[DB] ✅ Inbound message logged for {from_num}")
    except Exception as e:
        print("[ERROR] Failed to insert inbound message:", e)

    # Unsubscribe
    if body in {"SALIR", "UNSUBSCRIBE", "CANCEL", "END", "STOP", "BAJA", "ALTO"}:
        resp.message(
            "❌ You’ve been unsubscribed from Sardaar Ji promotions. "
            "Reply START to resubscribe. / "
            "❌ Has sido dado de baja de Sardaar Ji. "
            "Responde START para suscribirte de nuevo."
        )
        threading.Thread(target=handle_unsubscribe, args=(from_num,)).start()
        return str(resp)

    # Resubscribe
    if body in {"START", "YES", "SI"}:
        resp.message("✅ Subscribed / ✅ Suscripción activada")
        threading.Thread(target=handle_resubscribe, args=(from_num,)).start()
        return str(resp)

    # Default fallback
    resp.message("🍛 Thanks for contacting Sardaar Ji Indian Cuisine Panama!")
    return str(resp)

# ==========================
# 📦 Delivery Status Handler
# ==========================
@app.post("/twilio/status")
def status_callback():
    if not is_valid_twilio_request(request):
        abort(403)

    data = request.form.to_dict()
    sid = (data.get("MessageSid") or data.get("SmsSid") or "").strip()
    status = (data.get("MessageStatus") or "").strip()
    error_code = (data.get("ErrorCode") or "").strip()
    error_message = (data.get("ErrorMessage") or "").strip()

    print(f"[STATUS] SID={sid} Status={status} Error={error_code} {error_message}")

    if sid:
        threading.Thread(
            target=update_message_status,
            args=(sid, status, error_code or error_message)
        ).start()

    return "OK", 200

# ==========================
# 🌐 Health Check
# ==========================
@app.get("/")
def health_check():
    return {
        "status": "ok",
        "service": "sardaarji-whatsapp-webhook",
        "time": datetime.utcnow().isoformat() + "Z"
    }

# ==========================
# 🚀 Entrypoint
# ==========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
