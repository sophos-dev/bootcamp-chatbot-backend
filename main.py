import os
import json
import requests
from typing import List
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from sqlalchemy.orm import Session
from sqlalchemy import func

from database.db import get_db
from database.models import Conversation, Message

load_dotenv()

app = FastAPI(title="Study Buddy Backend")

ALLOWED_MODELS = [m.strip() for m in os.getenv("ALLOWED_MODELS", "openai/gpt-4o-mini,openai/gpt-4o").split(",")]
API_KEY = os.getenv("RODIUM_API_KEY")
RODIUM_API_URL = "https://api.rodiumai.io/v1/chat/completions"

def load_system_prompt() -> str:
    path = os.path.join("prompts", "system.md")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return "Tu es Study Buddy, un tuteur pédagogique."

class ChatRequest(BaseModel):
    conversation_id: int
    message: str
    model: str
    role: str = "user"  # "user" ou "note"

def get_next_seq(db: Session, conversation_id: int) -> int:
    max_seq = db.query(func.max(Message.seq)).filter(Message.conversation_id == conversation_id).scalar()
    return (max_seq or 0) + 1

def prepare_llm_messages(db_messages: List[Message]) -> List[dict]:
    llm_messages = [{"role": "system", "content": load_system_prompt()}]
    for msg in db_messages:
        # EXCLUSION : Le rôle "note" est filtré et JAMAIS envoyé au LLM
        if msg.role in ["user", "assistant", "system"]:
            llm_messages.append({"role": msg.role, "content": msg.content})
    return llm_messages

# --- ROUTES REQUISES PAR LE FRONTEND ---

@app.get("/models")
def get_models():
    return {"models": ALLOWED_MODELS}

@app.get("/conversations")
def list_conversations(db: Session = Depends(get_db)):
    convs = db.query(Conversation).order_by(Conversation.id.desc()).all()
    result = []
    for c in convs:
        first_msg = (
            db.query(Message)
            .filter(Message.conversation_id == c.id)
            .order_by(Message.seq.asc())
            .first()
        )
        preview = first_msg.content[:30] + "..." if first_msg else None
        result.append({
            "id": c.id,
            "created_at": c.created_at.isoformat() if c.created_at else "",
            "preview": preview
        })
    return result

@app.post("/conversations")
def create_conversation(db: Session = Depends(get_db)):
    new_conv = Conversation()
    db.add(new_conv)
    db.commit()
    db.refresh(new_conv)
    return {"conversation_id": new_conv.id}

@app.get("/conversations/{conversation_id}/messages")
def get_messages(conversation_id: int, db: Session = Depends(get_db)):
    msgs = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.seq.asc())
        .all()
    )
    return [
        {
            "seq": m.seq,
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at.isoformat() if m.created_at else ""
        }
        for m in msgs
    ]

# --- ENDPOINT STREAMING ---

@app.post("/chat/stream")
def chat_stream(req: ChatRequest, db: Session = Depends(get_db)):
    if req.model not in ALLOWED_MODELS:
        raise HTTPException(
            status_code=400, 
            detail=f"Modèle '{req.model}' non autorisé. Choisissez parmi : {ALLOWED_MODELS}"
        )

    conv = db.query(Conversation).filter(Conversation.id == req.conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation introuvable.")

    user_seq = get_next_seq(db, req.conversation_id)
    user_msg_db = Message(
        conversation_id=req.conversation_id,
        seq=user_seq,
        role=req.role,
        content=req.message
    )
    db.add(user_msg_db)
    db.commit()

    if req.role == "note":
        def note_stream():
            yield f"data: {json.dumps({'content': '📝 Note enregistrée avec succès.'})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(note_stream(), media_type="text/event-stream")

    existing_messages = (
        db.query(Message)
        .filter(Message.conversation_id == req.conversation_id)
        .order_by(Message.seq.asc())
        .all()
    )
    llm_messages = prepare_llm_messages(existing_messages)

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": req.model,
        "messages": llm_messages,
        "stream": True
    }

    def event_generator():
        full_response = ""
        try:
            with requests.post(RODIUM_API_URL, json=payload, headers=headers, stream=True) as resp:
                if resp.status_code != 200:
                    yield f"data: {json.dumps({'error': f'Erreur API (Code {resp.status_code})'})}\n\n"
                    return

                for line in resp.iter_lines():
                    if line:
                        line_str = line.decode("utf-8")
                        if line_str.startswith("data: "):
                            data_str = line_str[6:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                                delta = chunk["choices"][0]["delta"].get("content", "")
                                if delta:
                                    full_response += delta
                                    yield f"data: {json.dumps({'content': delta})}\n\n"
                            except Exception:
                                pass

            if full_response:
                assistant_seq = get_next_seq(db, req.conversation_id)
                assistant_msg_db = Message(
                    conversation_id=req.conversation_id,
                    seq=assistant_seq,
                    role="assistant",
                    content=full_response
                )
                db.add(assistant_msg_db)
                db.commit()

            yield "data: [DONE]\n\n"

        except Exception as e:
            db.rollback()
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")