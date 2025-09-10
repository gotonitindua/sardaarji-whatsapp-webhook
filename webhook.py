import os
import sys
import json
import threading
import traceback
from datetime import datetime
from flask import Flask, request, abort
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Force unbuffered logging so print() always shows in Render logs
sys.stdout.reconfigure(line_buffering=True)

app = Flask(__name__)
print("[STARTUP] Webhook service started and ready.")

# ==========================
# 🔐 Environment Config
# ==========================
SERVICE_ACCOUNT_JSON = os.environ["SERVICE_ACCOUNT_JSON"]
SHEET_URL = os.environ["SHEET_URL"]
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")

# ==========================
# 📑 Google Sheets Setup
# ==========================
SCOPES = ["https://spreadsheets.google.com/feeds",
          "https://www.googleapis.com/auth/drive"]

creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open_by_url(SHEET_URL).sheet1

# ---------------------------------------
# Header aliasing for mixed names
# ---------------------------------------
def _normalize(h: str) -> str:
    return "".join(ch for ch in h.lower() if ch.isalnum())

_HEADER_LOGICALS = {
    "dnc": ["do_not contact", "do not contact", "do_not_contact", "donotcontact"],
    "optin_date": ["opt in date", "opt_in date", "opt_in_date", "optindate"],
    "optin_source": ["opt_in source", "opt in source", "optinsource"],
    "optout_date": ["opt out date", "opt_out date", "opt_out_date", "optoutdate"],
    "phone": ["Phone", "phone"],
    "status": ["Status", "status"],
    "error": ["Error", "error"],
    "sid": ["SID", "sid"]
}

def _build_header_map(ws):
    headers = ws.row_values(1)
    norm_to_actual = {_normalize(h): h for h in headers}
    result = {}
    for key, variants in _HEADER_LOGICALS.items():
        for v in variants:
            n = _normalize(v)
            if n in norm_to_actual:
                result[key] = norm_to_actual[n]
                break
    return result

HEADER_MAP = _build_header_map(sheet)

def set_row_values(ws, row_idx: int, updates_logical: dict):
    """
    Generic updater: works for Customer sheet (dnc, optin_date, etc.)
    and Message Log sheet (Status, Error, SID).
    """
    headers = ws.row_values(1)
    cur = ws.row_values(row_idx)
    cur += [""] * (len(headers) - len(cur))
    hmap = {h: i for i, h in enumerate(headers)}

    for logical_key, value in updates_logical.items():
        # Try to map via HEADER_MAP
        actual = None
        if logical_key in HEADER_MAP:
            actual = HEADER_MAP[logical_key]
        else:
            # fallback: look directly in headers
            if logical_key in headers:
                actual = logical_key
        if actual and actual in hmap:
            cur[hmap[actual]] = value

    rng = ws.range(row_idx, 1, row_idx, len(headers))
    for i, cell in enumerate(rng):
        cell.value = cur[i]
    ws.update_cells(rng, value_input_option="USER_ENTERED")

def iso_now():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"

def normalize_e164(wa_from: str) -> str:
    return (wa_from or "").replace("whatsapp:", "").strip()

# ==========================
# 📌 FIXED phone matching
# ==========================
def find_row_index_by_phone(e164: str):
    records = sheet.get_all_records()
    phone_header = HEADER_MAP.get("phone", "Phone")

    # Normalize incoming number
    clean_incoming = e164.replace("+", "").replace(" ", "").replace("-", "")

    for idx, row in enumerate(records, start=2):
        phone = str(row.get(phone_header, "")).strip()
        if not phone:
            continue
        clean_stored = phone.replace("+", "").replace(" ", "").replace("-", "")
        if clean_incoming == clean_stored:
            return idx, row
        if clean_incoming.endswith(clean_stored):
            return idx, row
        if clean_stored.endswith(clean_incoming):
            return idx, row
    return None, None

# ==========================
# 🔒 Twilio Validation
# ==========================
validator = RequestValidator(TWILIO_AUTH_TOKEN)
def is_valid_twilio_request(req) -> bool:
    if not TWILIO_AUTH_TOKEN:
        return True
    signature = req.headers.get("X-Twilio-Signature", "")
    return validator.validate(req.url, req.form.to_dict(), signature)

# ==========================
# 📩 Background handlers
# ==========================
def handle_unsubscribe(row_idx):
    try:
        print(f"[DEBUG] Entering handle_unsubscribe for row {row_idx}")
        set_row_values(sheet, row_idx, {
            "dnc": "TRUE",
            "optout_date": iso_now()
        })
        print(f"[UNSUBSCRIBE] Updated row {row_idx} successfully")
    except Exception as e:
        print("[ERROR] handle_unsubscribe failed:", e)
        traceback.print_exc()

def handle_resubscribe(row_idx):
    try:
        print(f"[DEBUG] Entering handle_resubscribe for row {row_idx}")
        set_row_values(sheet, row_idx, {
            "dnc": "FALSE",
            "optin_source": "Resubscribe",
            "optin_date": iso_now()
        })
        print(f"[RESUBSCRIBE] Updated row {row_idx} successfully")
    except Exception as e:
        print("[ERROR] handle_resubscribe failed:", e)
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

    row_idx, _row = find_row_index_by_phone(from_num)
    if not row_idx:
        print(f"[WARN] No row found for {from_num}")
        return str(resp)

    # Unsubscribe
    if body in {"SALIR", "UNSUBSCRIBE", "CANCEL", "END", "STOP", "BAJA", "ALTO"}:
        resp.message(
            "❌ You’ve been unsubscribed from Sardaar Ji promotions. "
            "Reply START to resubscribe. / "
            "❌ Has sido dado de baja de Sardaar Ji. "
            "Responde START para suscribirte de nuevo."
        )
        threading.Thread(target=handle_unsubscribe, args=(row_idx,)).start()
        return str(resp)

    # Resubscribe
    if body in {"START", "YES", "SI"}:
        resp.message("✅ Subscribed / ✅ Suscripción activada")
        threading.Thread(target=handle_resubscribe, args=(row_idx,)).start()
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
    to_number = normalize_e164(data.get("To"))

    print(f"[STATUS] SID={sid} To={to_number} Status={status} Error={error_code} {error_message}")

    try:
        sh = gc.open_by_url(SHEET_URL)
        ws = sh.worksheet("Message Log")

        # Ensure headers are correct
        required_headers = ["Date","Name","Phone","Type","Message","Status","Error","SID"]
        headers = ws.row_values(1)
        if headers != required_headers:
            ws.update("A1:H1", [required_headers])
            headers = required_headers

        # Column indexes
        sid_col = headers.index("SID") + 1
        status_col = headers.index("Status") + 1
        error_col = headers.index("Error") + 1

        records = ws.get_all_records()
        updated = False

        for i, r in enumerate(records, start=2):  # start=2 for row index
            sid_in_sheet = str(r.get("SID", "")).strip()
            if sid_in_sheet == sid:
                ws.update_cell(i, status_col, status)
                ws.update_cell(i, error_col, error_code or error_message)
                print(f"[STATUS] ✅ Updated row {i} for SID {sid} → {status}")
                updated = True
                break

        if not updated:
            ws.append_row([
                datetime.now().strftime("%d-%m-%Y %H:%M"),
                "", to_number, "Status Update",
                "", status, error_code or error_message, sid
            ])
            print(f"[STATUS] ➕ Appended new row for SID {sid}")

    except Exception as e:
        print("[ERROR] Failed to update status in sheet:", e)
        traceback.print_exc()

    return "OK", 200


# ==========================
# 🚀 Entrypoint
# ==========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
