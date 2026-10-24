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
from database.db import get_db, engine, Base  
from database.models import Conversation, Message
from openai import OpenAI
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def send_email(to_email: str, subject: str, body: str) -> str:
    """Envoie un email via SMTP."""
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", 587))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    
    if not smtp_user or not smtp_password:
        return "Erreur : Identifiants SMTP non configurés dans le fichier .env."
    
    try:
        # Création du message
        msg = MIMEMultipart()
        msg["From"] = smtp_user
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        
        # Connexion au serveur SMTP et envoi
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, to_email, msg.as_string())
        server.quit()
        
        return f"Email envoyé avec succès à {to_email} !"
    except Exception as e:
        return f"Erreur technique lors de l'envoi de l'email : {str(e)}"

# override=True : le .env écrase les variables déjà définies dans le système
load_dotenv(override=True)

app = FastAPI(title="Study Buddy Backend - Agent Edition")

Base.metadata.create_all(bind=engine)
ALLOWED_MODELS = [m.strip() for m in os.getenv("ALLOWED_MODELS", "openai/gpt-4o-mini,openai/gpt-4o").split(",")]

# Lecture de la clé Rodium / OpenAI
_key_name = "RODIUM_API_KEY" if os.getenv("RODIUM_API_KEY") else "RODIUMAI_API_KEY"
API_KEY = (os.getenv("RODIUM_API_KEY") or os.getenv("RODIUMAI_API_KEY") or "").strip().strip('"').strip("'")
print(f"[config] variable utilisée : {_key_name}")
print(f"[config] clé chargée : {API_KEY[:6]}...{API_KEY[-4:]} (longueur {len(API_KEY)})")

def load_system_prompt() -> str:
    path = os.path.join("prompts", "system.md")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return "Tu es un agent IA personnel capable d'utiliser des outils pour aider l'utilisateur."

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
        if msg.role in ["user", "assistant", "system", "tool"]:
            llm_messages.append({"role": msg.role, "content": msg.content})
    return llm_messages


# ==========================================
# 🛠️ DÉFINITION DES OUTILS (TOOLS)
# ==========================================

def get_weather(city: str) -> str:
    """Récupère la météo actuelle via WeatherAPI."""
    api_key = os.getenv("WEATHER_API_KEY")
    if not api_key:
        return "Erreur : WEATHER_API_KEY non configurée."
    url = f"http://api.weatherapi.com/v1/current.json?key={api_key}&q={city}&lang=fr"
    try:
        res = requests.get(url).json()
        if "error" in res:
            return f"Impossible de trouver la météo pour {city}."
        temp = res["current"]["temp_c"]
        condition = res["current"]["condition"]["text"]
        return f"Météo actuelle à {city} : {temp}°C, {condition}."
    except Exception as e:
        return f"Erreur technique météo : {str(e)}"

def search_internet(query: str) -> str:
    """Effectue une recherche web via Tavily Search API."""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return "Erreur : TAVILY_API_KEY non configurée."
    url = "https://api.tavily.com/search"
    try:
        res = requests.post(url, json={"api_key": api_key, "query": query, "max_results": 3}).json()
        results = res.get("results", [])
        if not results:
            return "Aucun résultat trouvé sur Internet."
        output = []
        for r in results:
            output.append(f"- [{r.get('title')}]({r.get('url')}): {r.get('content')}")
        return "\n".join(output)
    except Exception as e:
        return f"Erreur technique recherche : {str(e)}"


AVAILABLE_TOOLS = {
    "get_weather": get_weather,
    "search_internet": search_internet,
    "send_email": send_email,  
}

tools_schema = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Récupère la météo actuelle pour une ville spécifique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "Nom de la ville (ex: Paris, Lomé, Dakar)"}
                },
                "required": ["city"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_internet",
            "description": "Recherche des informations actualisées ou des actualités sur Internet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Mots-clés de la recherche"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Envoie un email à un destinataire avec un objet et un contenu textuel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to_email": {
                        "type": "string",
                        "description": "Adresse email du destinataire (ex: test@example.com)"
                    },
                    "subject": {
                        "type": "string",
                        "description": "L'objet de l'email"
                    },
                    "body": {
                        "type": "string",
                        "description": "Le texte / contenu principal du message"
                    }
                },
                "required": ["to_email", "subject", "body"]
            }
        }
    }
]


# --- ROUTES DE BASE ---

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

# Initialisation du client OpenAI / Rodium
client = OpenAI(
    api_key=API_KEY,
    base_url="https://api.rodiumai.io/v1",
)

# --- ENDPOINT STREAMING AVEC TOOL CALLING ---

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
            # 1. Premier appel non-streaming pour vérifier si le LLM veut appeler un outil
            first_response = client.chat.completions.create(
                model=req.model,
                messages=llm_messages,
                tools=tools_schema,
                tool_choice="auto",
                stream=False
            )
            
            response_message = first_response.choices[0].message

            # 2. Si le LLM demande à appeler un ou plusieurs outils
            if response_message.tool_calls:
                # On ajoute la décision de l'outil à l'historique de messages
                llm_messages.append(response_message)

                for tool_call in response_message.tool_calls:
                    function_name = tool_call.function.name
                    function_args = json.loads(tool_call.function.arguments)

                    print(f"[Agent] Appel de l'outil : {function_name} avec les arguments : {function_args}")

                    # Exécution de la fonction Python correspondante
                    if function_name in AVAILABLE_TOOLS:
                        tool_output = AVAILABLE_TOOLS[function_name](**function_args)
                    else:
                        tool_output = f"Erreur : Outil {function_name} inconnu."

                    # On ajoute le résultat de l'outil dans les messages
                    llm_messages.append({
                        "tool_call_id": tool_call.id,
                        "role": "tool",
                        "name": function_name,
                        "content": str(tool_output)
                    })

                # 3. Deuxième appel (en streaming cette fois) pour générer la réponse finale avec le résultat de l'outil
                stream = client.chat.completions.create(
                    model=req.model,
                    messages=llm_messages,
                    stream=True
                )
            else:
                # Si aucun outil n'est nécessaire, on lance directement le stream classique
                stream = client.chat.completions.create(
                    model=req.model,
                    messages=llm_messages,
                    stream=True
                )

            # Lecture et envoi des chunks au frontend
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
            print(f"[erreur stream] {type(e).__name__}: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")