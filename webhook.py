import os
import sys
import threading
import traceback
import sqlite3
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
DB_PATH = "customers.db"

# ==========================
# 📑 SQLite Helpers
# ==========================
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def iso_now():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"

def normalize_e164(wa_from: str) -> str:
    return (wa_from or "").replace("whatsapp:", "").strip()

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
        cur.execute(
            "UPDATE customers SET dnc=1, optout_date=? WHERE phone=?",
            (iso_now(), phone)
        )
        conn.commit()
        conn.close()
        print(f"[UNSUBSCRIBE] ✅ Phone {phone} set to DNC=1")
    except Exception as e:
        print("[ERROR] handle_unsubscribe failed:", e)
        traceback.print_exc()

def handle_resubscribe(phone):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE customers SET dnc=0, optin_source=?, optin_date=? WHERE phone=?",
            ("Resubscribe", iso_now(), phone)
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
        conn.commit()
        conn.close()
        print(f"[STATUS] ✅ Updated SID={sid} → {status} ({error})")
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
    resp = MessagingResponse()

    print(f"[INBOUND] Message from {from_num}: {body}")

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
# 🌐 Health Check 1
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
