import os
import json
import threading
from datetime import datetime
from flask import Flask, request, abort
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = Flask(__name__)

# ==========================
# 🔐 Environment Config
# ==========================
SERVICE_ACCOUNT_JSON = os.environ["SERVICE_ACCOUNT_JSON"]   # full JSON string
SHEET_URL = os.environ["SHEET_URL"]                         # Google Sheet URL
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "") # optional (for signature validation)

# ==========================
# 📑 Google Sheets Setup
# ==========================
SCOPES = ["https://spreadsheets.google.com/feeds",
          "https://www.googleapis.com/auth/drive"]

creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open_by_url(SHEET_URL).sheet1  # first tab

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
    "phone": ["Phone", "phone"]
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
    headers = ws.row_values(1)
    cur = ws.row_values(row_idx); cur += [""] * (len(headers) - len(cur))
    hmap = {h: i for i, h in enumerate(headers)}
    for logical_key, value in updates_logical.items():
        actual = HEADER_MAP.get(logical_key)
        if actual in hmap:
            cur[hmap[actual]] = value
    rng = ws.range(row_idx, 1, row_idx, len(headers))
    for i, cell in enumerate(rng):
        cell.value = cur[i]
    ws.update_cells(rng, value_input_option="USER_ENTERED")

def iso_now():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"

def normalize_e164(wa_from: str) -> str:
    return (wa_from or "").replace("whatsapp:", "").strip()

def find_row_index_by_phone(e164: str):
    records = sheet.get_all_records()
    phone_header = HEADER_MAP.get("phone", "Phone")
    for idx, row in enumerate(records, start=2):
        phone = str(row.get(phone_header, "")).strip()
        if not phone:
            continue
        if phone == e164 or phone.replace("+","") == e164.replace("+",""):
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
        set_row_values(sheet, row_idx, {
            "dnc": "TRUE",
            "optout_date": iso_now()
        })
        print(f"[UNSUBSCRIBE] Updated row {row_idx}")
    except Exception as e:
        print("Error in unsubscribe:", e)

def handle_resubscribe(row_idx):
    try:
        set_row_values(sheet, row_idx, {
            "dnc": "FALSE",
            "optin_source": "Resubscribe",
            "optin_date": iso_now()
        })
        print(f"[RESUBSCRIBE] Updated row {row_idx}")
    except Exception as e:
        print("Error in resubscribe:", e)

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

    row_idx, _row = find_row_index_by_phone(from_num)
    if not row_idx:
        return str(resp)

    # Unsubscribe
    if body in {"SALIR", "UNSUBSCRIBE", "CANCEL", "END", "STOP", "BAJA", "ALTO"}:
        # Respond immediately
        resp.message(
            "❌ You’ve been unsubscribed from Sardaar Ji promotions. "
            "Reply START to resubscribe. / "
            "❌ Has sido dado de baja de Sardaar Ji. "
            "Responde START para suscribirte de nuevo."
        )
        # Background update
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
# 🚀 Entrypoint
# ==========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
