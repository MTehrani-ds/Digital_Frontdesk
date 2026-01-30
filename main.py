from __future__ import annotations

import logging
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, ConfigDict


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
    # harden against None
    if state.collected is None:
        state.collected = Collected()

    text = user_text.strip()
    t = text.lower()

    # Phone: naive parse
    digits = "".join(ch for ch in text if ch.isdigit() or ch == "+")
    if len(digits.replace("+", "")) >= 9 and not state.collected.phone:
        state.collected.phone = digits

    # Best time: naive phrases
    if (
        not state.collected.best_time
        and any(x in t for x in ["morning", "afternoon", "evening", "today", "tomorrow", "anytime"])
    ):
        state.collected.best_time = text

    # Name: case-safe, no split indexing
    if not state.collected.name:
        patterns = ["my name is ", "i am ", "i'm "]
        for pat in patterns:
            idx = t.find(pat)
            if idx != -1:
                name = text[idx + len(pat):].strip()
                for stop in [".", ",", ";", " and "]:
                    if stop in name:
                        name = name.split(stop, 1)[0].strip()
                if len(name) >= 2:
                    state.collected.name = name[:80]
                break

    return state


# ---------------------------
# State machine
# ---------------------------

def next_reply(practice_name: str, user_text: str, state: SessionState) -> tuple[str, SessionState]:
    # Policy gate at any time
    if is_medical_or_medication_question(user_text):
        state.step = Step.LIMITED_RESPONSE
        return limited_response_policy(practice_name), state

    # Collect contact for callback
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
                + ". You can reply in one message like: “My name is …, phone …, best time …”.",
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

    # Already handed off
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
