import os
import json
from typing import List
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from sqlalchemy.orm import Session
from sqlalchemy import func
from database.db import get_db, engine, Base  
from database.models import Conversation, Message
from openai import OpenAI

# override=True : le .env écrase les variables déjà définies dans le système
load_dotenv(override=True)

app = FastAPI(title="Study Buddy Backend")

Base.metadata.create_all(bind=engine)
ALLOWED_MODELS = [m.strip() for m in os.getenv("ALLOWED_MODELS", "openai/gpt-4o-mini,openai/gpt-4o").split(",")]
RODIUM_API_URL = "https://api.rodiumai.io/v1/chat/completions"

# Lecture de la clé : on garde tes deux noms possibles, mais on nettoie la valeur
# (espaces, retours à la ligne, guillemets) et on affiche d'où elle vient.
_key_name = "RODIUM_API_KEY" if os.getenv("RODIUM_API_KEY") else "RODIUMAI_API_KEY"
API_KEY = (os.getenv("RODIUM_API_KEY") or os.getenv("RODIUMAI_API_KEY") or "").strip().strip('"').strip("'")
print(f"[config] variable utilisée : {_key_name}")
print(f"[config] clé chargée : {API_KEY[:6]}...{API_KEY[-4:]} (longueur {len(API_KEY)})")
if os.getenv("RODIUM_API_KEY") and os.getenv("RODIUMAI_API_KEY") and os.getenv("RODIUM_API_KEY") != os.getenv("RODIUMAI_API_KEY"):
    print("[config] ATTENTION : RODIUM_API_KEY et RODIUMAI_API_KEY ont des valeurs différentes")

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

# Initialisation du client Rodium (avec la clé nettoyée ci-dessus)
client = OpenAI(
    api_key=API_KEY,
    base_url="https://api.rodiumai.io/v1",
)

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

    def event_generator():
        full_response = ""
        try:
            # Appel officiel via le SDK OpenAI pointant sur Rodium
            stream = client.chat.completions.create(
                model=req.model,
                messages=llm_messages,
                stream=True
            )

            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    full_response += delta
                    yield f"data: {json.dumps({'content': delta})}\n\n"

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
            # Log complet dans le terminal pour diagnostiquer le 401
            print(f"[erreur stream] {type(e).__name__}: {e}")
            print(f"[erreur stream] clé utilisée : {API_KEY[:6]}...{API_KEY[-4:]} | modèle : {req.model}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")