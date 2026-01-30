from __future__ import annotations

import logging
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, ConfigDict

# ---------------------------
# Clinic configuration (demo placeholders)
# ---------------------------

CLINIC = {
    "name": "Example Dental Clinic",
    "address": "Example Street 12, 1010 Vienna",
    "phone": "+43 1 234 5678",
    "emergency_note": (
        "If you have severe swelling, fever, heavy bleeding, trouble swallowing/breathing, "
        "or rapidly worsening pain, seek urgent care immediately."
    ),
    "hours": "Mon–Fri 08:00–18:00",
    "parking": "Street parking nearby. Nearest garage: Example Garage (3 min walk).",
    "public_transport": "U1/U3 to Stephansplatz, then 5 min walk (demo text).",
    "cancellation_policy": "Please cancel or reschedule at least 24 hours in advance.",
    "insurance": "We accept public insurance and private pay (demo).",
    "services": [
        "Check-ups & consultations",
        "Professional cleaning",
        "Fillings",
        "Root canal treatment (by assessment)",
        "Crowns/bridges",
        "Implants (by assessment)",
        "Kids dentistry",
        "Emergency pain consultations",
    ],
    "what_to_bring": "E-card/insurance card, photo ID, medication list (if any), and prior dental records if available.",
}


# ---------------------------
# FAQ router (starter set)
# ---------------------------

FAQ = [
    {
        "key": "hours",
        "keywords": ["hours", "opening", "open", "close", "closing", "weekend", "saturday", "sunday", "today", "tomorrow"],
        "answer": lambda: f"Our opening hours are: {CLINIC['hours']}.",
    },
    {
        "key": "location_parking",
        "keywords": ["address", "location", "where", "parking", "park", "garage", "public transport", "tram", "metro", "u-bahn", "bus"],
        "answer": lambda: (
            f"Address: {CLINIC['address']}.\n"
            f"Parking: {CLINIC['parking']}\n"
            f"Public transport: {CLINIC['public_transport']}"
        ),
    },
    {
        "key": "booking",
        "keywords": ["appointment", "book", "booking", "schedule", "available", "availability"],
        "answer": lambda: (
            "I can help arrange an appointment request. "
            "Please share your **name**, **phone number**, and the **best time window** to reach you."
        ),
        "forces_contact_flow": True,
    },
    {
        "key": "reschedule_cancel",
        "keywords": ["reschedule", "change appointment", "move appointment", "cancel", "cancellation"],
        "answer": lambda: (
            f"To cancel or reschedule, please share your **name**, **phone number**, and your preferred new time window.\n\n"
            f"Policy: {CLINIC['cancellation_policy']}"
        ),
        "forces_contact_flow": True,
    },
    {
        "key": "emergency",
        "keywords": ["emergency", "urgent", "swelling", "bleeding", "fever", "can’t breathe", "can't breathe", "hard to swallow", "severe pain"],
        "answer": lambda: (
            f"{CLINIC['emergency_note']}\n\n"
            f"If you want a same-day assessment, share your **name**, **phone number**, and **best time** to call."
        ),
        "forces_contact_flow": True,
    },
    {
        "key": "services",
        "keywords": ["services", "do you do", "offer", "cleaning", "filling", "implant", "braces", "root canal", "kids", "child"],
        "answer": lambda: (
            "We offer:\n- " + "\n- ".join(CLINIC["services"]) +
            "\n\nIf you'd like, tell me what you need and I can arrange a callback."
        ),
    },
    {
        "key": "pricing_insurance",
        "keywords": ["price", "pricing", "cost", "how much", "insurance", "kassa", "private", "payment"],
        "answer": lambda: (
            f"Pricing depends on the service and insurance coverage. {CLINIC['insurance']}\n"
            "If you tell me which service you’re asking about (e.g., cleaning, filling, implant), "
            "I can arrange a callback with an estimated range."
        ),
    },
    {
        "key": "what_to_bring",
        "keywords": ["what to bring", "bring", "documents", "paperwork", "e-card", "id", "records", "first visit", "new patient"],
        "answer": lambda: f"For your visit, please bring: {CLINIC['what_to_bring']}",
    },
]

# ---------------------------
# Models: Policy + State machine
# ---------------------------

class Step(str, Enum):
    LIMITED_RESPONSE = "LIMITED_RESPONSE"
    COLLECT_CONTACT = "COLLECT_CONTACT"
    HANDOFF = "HANDOFF"


class Collected(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    best_time: Optional[str] = None


class SessionState(BaseModel):
    step: Step = Step.LIMITED_RESPONSE
    procedure: Optional[str] = None
    intent: Optional[str] = None
    collected: Collected = Field(default_factory=Collected)


class IncomingMessage(BaseModel):
    # Keep your "Postman shape" stable
    session_id: str
    user_message: str
    channel: str = "webchat"
    practice_name: str = "Example Dental Clinic"
    prior_state: Optional[SessionState] = None
    msg: Optional[str] = None
    state: Optional[SessionState] = None


class OutgoingMessage(BaseModel):
    session_id: str
    channel: str
    practice_name: str
    user_message: str
    reply: str
    state: SessionState
    # New: ticket info returned when a callback task is created
    ticket: Optional[Dict[str, Any]] = None


# ---------------------------
# Ticketing (Path A) models
# ---------------------------

class Ticket(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    ticket_id: str
    created_at: str
    session_id: str
    practice_name: str
    name: str
    phone: str
    best_time: str
    summary: str
    status: str = "OPEN"


TICKET_DB: List[Ticket] = []
TICKET_COUNTER = 0


def create_ticket(session_id: str, practice_name: str, state: SessionState, summary: str) -> Ticket:
    global TICKET_COUNTER
    TICKET_COUNTER += 1

    ticket = Ticket(
        ticket_id=f"T-{TICKET_COUNTER:04d}",
        created_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
        session_id=session_id,
        practice_name=practice_name,
        name=state.collected.name or "",
        phone=state.collected.phone or "",
        best_time=state.collected.best_time or "",
        summary=summary,
        status="OPEN",
    )
    TICKET_DB.insert(0, ticket)  # newest first
    return ticket


def match_faq_intent(text: str) -> Optional[dict]:
    t = text.lower()
    # simple keyword match; first hit wins
    for item in FAQ:
        if any(k in t for k in item["keywords"]):
            return item
    return None


# ---------------------------
# App + global error handler
# ---------------------------

app = FastAPI(title="Dental Clinic Agentic Demo", version="0.3.0")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logging.exception("Unhandled server error")
    return JSONResponse(status_code=500, content={"error": "internal_error", "detail": str(exc)})


# ---------------------------
# In-memory session store
# ---------------------------

SESSION_DB: Dict[str, SessionState] = {}


def get_state(payload: IncomingMessage) -> SessionState:
    # Priority: payload.state -> payload.prior_state -> stored -> default
    if payload.state is not None:
        return payload.state
    if payload.prior_state is not None:
        return payload.prior_state
    if payload.session_id in SESSION_DB:
        return SESSION_DB[payload.session_id]
    return SessionState()


def save_state(session_id: str, state: SessionState) -> None:
    SESSION_DB[session_id] = state


# ---------------------------
# Policy layer (industry-safe)
# ---------------------------

def is_medical_or_medication_question(text: str) -> bool:
    t = text.lower()
    keywords = [
        "antibiotic", "antibiotics", "amoxicillin", "penicillin", "clindamycin",
        "medicine", "medication", "dose", "dosage", "should i",
        "ibuprofen", "painkiller", "prescription",
    ]
    return any(k in t for k in keywords)


def limited_response_policy(practice_name: str) -> str:
    return (
        f"Thanks for your question. I can’t recommend specific medication (including antibiotics) "
        f"without a clinician evaluating your situation.\n\n"
        f"If you have severe swelling, fever, trouble swallowing/breathing, or rapidly worsening pain, "
        f"please seek urgent care immediately.\n\n"
        f"If not urgent: the safest next step is to speak with a dentist from {practice_name}. "
        f"Can I take your **name** and **phone number**, and the **best time** to call you back?"
    )


# ---------------------------
# Lightweight extraction (demo-safe)
# ---------------------------

def update_collected_from_text(state: SessionState, user_text: str) -> SessionState:
    """
    Demo-safe extraction (no AI):
    - Supports: "My name is Alex", "I'm Alex", "I am Alex"
    - Supports label style: "Name: Alex, Phone: +43..., Best time: tomorrow morning"
    - Phone: extracts digits/+ (simple)
    - Best time: extracts the part after "best time" or uses short phrase fallback
    """
    # Harden against None
    if state.collected is None:
        state.collected = Collected()

    text = user_text.strip()
    tl = text.lower()

    # -----------------------
    # PHONE (simple)
    # -----------------------
    if not state.collected.phone:
        # Try label-based first: "phone: +43..."
        for key in ["phone:", "phone=", "phone-", "tel:", "tel=", "tel-", "mobile:", "mobile=", "mobile-"]:
            pos = tl.find(key)
            if pos != -1:
                candidate = text[pos + len(key):].strip()
                # cut at next field label or punctuation
                lowcand = candidate.lower()
                for stop in [" best time", " name", ",", ";", "."]:
                    idx2 = lowcand.find(stop)
                    if idx2 != -1:
                        candidate = candidate[:idx2].strip()
                        lowcand = candidate.lower()
                digits = "".join(ch for ch in candidate if ch.isdigit() or ch == "+")
                if len(digits.replace("+", "")) >= 9:
                    state.collected.phone = digits
                break

    # fallback: any phone-like digits in whole message
    if not state.collected.phone:
        digits = "".join(ch for ch in text if ch.isdigit() or ch == "+")
        if len(digits.replace("+", "")) >= 9:
            state.collected.phone = digits

    # -----------------------
    # BEST TIME (extract only relevant fragment)
    # -----------------------
    if not state.collected.best_time:
        marker = "best time"
        if marker in tl:
            start = tl.find(marker)
            best = text[start:].strip()

            # remove the marker and separators
            best_low = best.lower()
            if best_low.startswith("best time"):
                best = best[len("best time"):].lstrip(" :,-=").strip()

            # cut at next field label or sentence stop
            lowbest = best.lower()
            for stop in [" phone", " name", ".", ";", "|"]:
                idx2 = lowbest.find(stop)
                if idx2 != -1:
                    best = best[:idx2].strip()
                    lowbest = best.lower()

            if best:
                state.collected.best_time = best
        else:
            # fallback: common short phrases
            for phrase in [
                "tomorrow morning", "tomorrow afternoon", "tomorrow evening",
                "today morning", "today afternoon", "today evening",
                "tomorrow", "today", "anytime"
            ]:
                if phrase in tl:
                    state.collected.best_time = phrase
                    break

    # -----------------------
    # NAME (robust)
    # -----------------------
    if not state.collected.name:
        # 1) Label-based: "name: Alex"
        # Also handles: "Name: Alex, Phone: ..."
        for sep in [":", "=", "-"]:
            key = "name" + sep
            pos = tl.find(key)
            if pos != -1:
                candidate = text[pos + len(key):].strip()

                lowcand = candidate.lower()
                for stop in [" phone", " best time", ",", ";", "."]:
                    idx2 = lowcand.find(stop)
                    if idx2 != -1:
                        candidate = candidate[:idx2].strip()
                        lowcand = candidate.lower()

                if len(candidate) >= 2:
                    state.collected.name = candidate[:80]
                break

    if not state.collected.name:
        # 2) Phrase-based: "my name is / i'm / i am"
        for pat in ["my name is ", "i am ", "i'm "]:
            idx = tl.find(pat)
            if idx != -1:
                name = text[idx + len(pat):].strip()

                lowname = name.lower()
                for stop in [" phone", " best time", ".", ",", ";", " and "]:
                    idx2 = lowname.find(stop)
                    if idx2 != -1:
                        name = name[:idx2].strip()
                        lowname = name.lower()

                if len(name) >= 2:
                    state.collected.name = name[:80]
                break

    return state



# ---------------------------
# State machine
# ---------------------------

def next_reply(practice_name: str, user_text: str, state: SessionState) -> tuple[str, SessionState]:
    """
    Policy-first routing:
    1) Block medical/medication advice -> safe limited response + collect callback details
    2) Answer safe front-desk FAQs (hours/location/services/etc.)
       - some intents force contact flow (booking/cancel/emergency) to create a callback ticket
    3) If in contact flow, extract name/phone/best time and transition to HANDOFF when complete
    """

    # 1) Policy gate at any time (no medical or medication advice)
    if is_medical_or_medication_question(user_text):
        state.step = Step.LIMITED_RESPONSE
        return limited_response_policy(practice_name), state

    # 2) Front-desk FAQ router (safe topics only)
    faq = match_faq_intent(user_text)
    if faq:
        answer = faq["answer"]()
        forces_contact = bool(faq.get("forces_contact_flow"))

        # If FAQ is booking/cancel/emergency: enter contact-collection flow
        if forces_contact:
            state.step = Step.COLLECT_CONTACT

            # Try to extract contact info from the same message (e.g., "Name:..., Phone:...")
            state = update_collected_from_text(state, user_text)

            missing = []
            if not state.collected.name:
                missing.append("name")
            if not state.collected.phone:
                missing.append("phone number")
            if not state.collected.best_time:
                missing.append("best time to call")

            if missing:
                answer += "\n\nTo proceed, I still need your " + ", ".join(missing) + "."
                return answer, state

            # If we already have everything, hand off immediately
            state.step = Step.HANDOFF
            return (
                f"{answer}\n\n"
                f"Thanks, {state.collected.name}. I’ve captured your details.\n\n"
                f"Phone: {state.collected.phone}\n"
                f"Best time: {state.collected.best_time}\n\n"
                f"Someone from {practice_name} will contact you shortly.",
                state,
            )

        # If it's a simple FAQ and we are not collecting contact, just answer
        if state.step != Step.COLLECT_CONTACT:
            return answer, state

        # If we are already collecting contact, answer FAQ AND remind missing fields
        missing = []
        if not state.collected.name:
            missing.append("name")
        if not state.collected.phone:
            missing.append("phone number")
        if not state.collected.best_time:
            missing.append("best time to call")
        if missing:
            answer += "\n\nTo proceed, I still need your " + ", ".join(missing) + "."
        return answer, state

    # 3) Contact collection flow (if already active)
    if state.step in (Step.LIMITED_RESPONSE, Step.COLLECT_CONTACT):
        state = update_collected_from_text(state, user_text)

        missing = []
        if not state.collected.name:
            missing.append("name")
        if not state.collected.phone:
            missing.append("phone number")
        if not state.collected.best_time:
            missing.append("best time to call")

        if missing:
            state.step = Step.COLLECT_CONTACT
            return (
                "To arrange a callback, I still need your " + ", ".join(missing)
                + ". You can reply in one message like: “Name: …, Phone: …, Best time: …”.",
                state,
            )

        state.step = Step.HANDOFF
        return (
            f"Thanks, {state.collected.name}. I’ve captured your details.\n\n"
            f"Phone: {state.collected.phone}\n"
            f"Best time: {state.collected.best_time}\n\n"
            f"Someone from {practice_name} will contact you shortly.",
            state,
        )

    # 4) Already handed off
    return (
        f"Thanks — your request is with the team at {practice_name}.",
        state,
    )

# ---------------------------
# Routes
# ---------------------------

@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/webchat/message", response_model=OutgoingMessage)
def webchat_message(payload: IncomingMessage) -> OutgoingMessage:
    old_state = get_state(payload)
    old_step = old_state.step  # ✅ snapshot BEFORE mutation

    reply, new_state = next_reply(payload.practice_name, payload.user_message, old_state)
    save_state(payload.session_id, new_state)

    ticket_info: Optional[Dict[str, Any]] = None

    # ✅ use old_step instead of old_state.step
    if old_step != Step.HANDOFF and new_state.step == Step.HANDOFF:
        summary = f"Callback requested. Latest user message: {payload.user_message[:140]}"
        ticket = create_ticket(payload.session_id, payload.practice_name, new_state, summary)
        ticket_info = {
            "ticket_id": ticket.ticket_id,
            "status": ticket.status,
            "created_at": ticket.created_at,
        }
        reply = reply + f"\n\n✅ Callback ticket created: {ticket.ticket_id}"

    return OutgoingMessage(
        session_id=payload.session_id,
        channel=payload.channel,
        practice_name=payload.practice_name,
        user_message=payload.user_message,
        reply=reply,
        state=new_state,
        ticket=ticket_info,
    )



@app.post("/admin/reset_session/{session_id}")
def reset_session(session_id: str) -> Dict[str, Any]:
    SESSION_DB.pop(session_id, None)

    # remove tickets linked to this session (demo convenience)
    global TICKET_DB
    TICKET_DB = [t for t in TICKET_DB if t.session_id != session_id]

    return {"ok": True, "session_id": session_id}


@app.get("/staff", response_class=HTMLResponse)
def staff_dashboard():
    rows = []
    for t in TICKET_DB[:50]:
        rows.append(f"""
        <tr>
          <td>{t.ticket_id}</td>
          <td>{t.created_at}</td>
          <td>{t.name}</td>
          <td>{t.phone}</td>
          <td>{t.best_time}</td>
          <td>{t.summary}</td>
          <td>{t.status}</td>
        </tr>
        """)

    html = f"""
    <html>
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width,initial-scale=1" />
      <title>Staff Dashboard</title>
      <style>
        body {{ font-family: Arial, sans-serif; background:#f5f5f5; margin:0; padding:24px; }}
        .card {{ background:#fff; border-radius:14px; padding:16px; box-shadow: 0 2px 10px rgba(0,0,0,.08); }}
        table {{ width:100%; border-collapse: collapse; }}
        th, td {{ border-bottom: 1px solid #eee; text-align:left; padding:10px; font-size:14px; vertical-align: top; }}
        th {{ background:#fafafa; }}
        .top {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; gap:12px; flex-wrap: wrap; }}
        a.button {{ padding:8px 12px; border:1px solid #ddd; border-radius:10px; text-decoration:none; color:#333; background:#fff; }}
        .hint {{ color:#666; font-size:12px; }}
      </style>
    </head>
    <body>
      <div class="card">
        <div class="top">
          <div>
            <h3 style="margin:0;">Dental Clinic — Callback Tasks</h3>
            <div class="hint">This is a demo inbox (in-memory). Refresh to see new tickets.</div>
          </div>
          <div>
            <a class="button" href="/">Open Chat</a>
            <a class="button" href="/staff">Refresh</a>
          </div>
        </div>
        <table>
          <thead>
            <tr>
              <th>Ticket</th>
              <th>Created (UTC)</th>
              <th>Name</th>
              <th>Phone</th>
              <th>Best time</th>
              <th>Summary</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {''.join(rows) if rows else '<tr><td colspan="7">No tickets yet.</td></tr>'}
          </tbody>
        </table>
      </div>
    </body>
    </html>
    """
    return html


@app.get("/", response_class=HTMLResponse)
def home():
    # Serves your customer-friendly chat UI
    try:
        with open("chat.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return (
            "<h3>chat.html not found</h3>"
            "<p>Create <b>chat.html</b> next to <b>main.py</b> to use the chat UI.</p>"
            "<p>You can still test the API via <a href='/docs'>/docs</a>.</p>"
        )
