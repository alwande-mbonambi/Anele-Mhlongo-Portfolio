from flask import Flask, request, jsonify, render_template
from functools import wraps
import firebase_admin
from firebase_admin import credentials, firestore, auth
import os
from dotenv import load_dotenv
import cloudinary
import cloudinary.uploader
import json
from pypdf import PdfReader
import io
from datetime import datetime
import base64
from time import time
import requests

load_dotenv()

app = Flask(__name__)

# Firebase key handling
firebase_key_b64 = os.getenv('FIREBASE_KEY_BASE64')
if firebase_key_b64:
    with open('serviceAccountKey.json', 'wb') as f:
        f.write(base64.b64decode(firebase_key_b64))

firebase_key_path = '/etc/secrets/serviceAccountKey.json'
if not os.path.exists(firebase_key_path):
    firebase_key_path = 'serviceAccountKey.json'

if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_key_path)
    firebase_admin.initialize_app(cred)
    print(f"Firebase initialized from {firebase_key_path}")

db = firestore.client()

# Cloudinary
cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET'),
    secure=True
)

# API Keys for free providers
GROQ_API_KEY = os.getenv('GROQ_API_KEY')
COHERE_API_KEY = os.getenv('COHERE_API_KEY')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')

# Cache
_portfolio_cache = {"data": None, "timestamp": 0}
CACHE_TTL = 5

def get_portfolio_context():
    """Fetch portfolio data from Firestore and flatten skillCats into a simple skills list."""
    global _portfolio_cache
    now = time()
    if _portfolio_cache["data"] is not None and (now - _portfolio_cache["timestamp"]) < CACHE_TTL:
        return _portfolio_cache["data"]
    
    doc = db.collection('portfolio').document('structured_data').get()
    data = doc.to_dict() if doc.exists else {}
    
    # Default structure
    defaults = {
        "name": "",
        "job": "",
        "desc": "",
        "skills": [],
        "projects": [],
        "certificates": [],
        "quals": [],
        "exps": [],
        "contactInfo": {},
        "extraKnowledge": "",
        "skillCats": []   # Frontend stores skills in this nested structure
    }
    for k, v in defaults.items():
        if k not in data:
            data[k] = v
    
    # Convert skillCats into a flat skills list for the AI prompt
    if data.get("skillCats") and isinstance(data["skillCats"], list):
        flat_skills = []
        for category in data["skillCats"]:
            if isinstance(category, dict) and "items" in category:
                for item in category["items"]:
                    if isinstance(item, dict) and "n" in item:
                        flat_skills.append(item["n"])
        if flat_skills:
            data["skills"] = flat_skills   # Override empty skills list
    
    _portfolio_cache["data"] = data
    _portfolio_cache["timestamp"] = now
    return data

def invalidate_portfolio_cache():
    global _portfolio_cache
    _portfolio_cache["data"] = None
    _portfolio_cache["timestamp"] = 0

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({"error": "Unauthorized"}), 401
        id_token = auth_header.split(' ')[1]
        try:
            decoded = auth.verify_id_token(id_token, clock_skew_seconds=10)
            uid = decoded['uid']
            if not db.collection('admins').document(uid).get().exists:
                return jsonify({"error": "Unauthorized"}), 401
            return f(*args, **kwargs)
        except Exception as e:
            print(f"Token verification failed: {e}")
            return jsonify({"error": "Unauthorized"}), 401
    return decorated

@app.route('/')
def index():
    return render_template('index.html',
                           firebase_api_key=os.getenv('FIREBASE_API_KEY'),
                           firebase_auth_domain=os.getenv('FIREBASE_AUTH_DOMAIN'))

@app.route('/api/verify-token', methods=['POST'])
def verify_token():
    data = request.json
    id_token = data.get('idToken')
    if not id_token:
        return jsonify({"isAdmin": False, "error": "No token provided"}), 401
    try:
        decoded = auth.verify_id_token(id_token, clock_skew_seconds=10)
        uid = decoded['uid']
        admin_doc = db.collection('admins').document(uid).get()
        is_admin = admin_doc.exists
        return jsonify({"isAdmin": is_admin, "uid": uid})
    except Exception as e:
        print(f"Token verification error: {e}")
        return jsonify({"isAdmin": False, "error": "Authentication failed"}), 401

@app.route('/api/get-data', methods=['GET'])
def get_data():
    try:
        doc = db.collection('portfolio').document('structured_data').get()
        return jsonify(doc.to_dict() if doc.exists else {}), 200
    except Exception as e:
        return jsonify({"error": "Failed to load data"}), 500

@app.route('/api/update-data', methods=['POST'])
@admin_required
def update_data():
    try:
        data = request.json
        db.collection('portfolio').document('structured_data').set(data)
        invalidate_portfolio_cache()
        return jsonify({"message": "Data synced!"}), 200
    except Exception as e:
        return jsonify({"error": "Update failed"}), 500

@app.route('/api/upload', methods=['POST'])
@admin_required
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error": "No file"}), 400
    file = request.files['file']
    try:
        result = cloudinary.uploader.upload(file, resource_type="auto")
        return jsonify({"url": result.get('secure_url')}), 200
    except Exception as e:
        return jsonify({"error": "Upload failed"}), 500

@app.route('/api/upload-knowledge', methods=['POST'])
@admin_required
def upload_knowledge():
    if 'file' not in request.files:
        return jsonify({"error": "No file"}), 400
    file = request.files['file']
    filename = file.filename.lower()
    extracted_text = ""
    try:
        if filename.endswith('.txt'):
            extracted_text = file.read().decode('utf-8')
        elif filename.endswith('.pdf'):
            pdf_reader = PdfReader(io.BytesIO(file.read()))
            for page in pdf_reader.pages:
                extracted_text += page.extract_text() + "\n"
        else:
            return jsonify({"error": "Only .txt or .pdf files are supported"}), 400
        
        doc_ref = db.collection('portfolio').document('structured_data')
        doc = doc_ref.get()
        data = doc.to_dict() if doc.exists else {}
        current_extra = data.get('extraKnowledge', '')
        new_extra = current_extra + "\n\n" + extracted_text if current_extra else extracted_text
        doc_ref.set({**data, 'extraKnowledge': new_extra})
        invalidate_portfolio_cache()
        return jsonify({"message": "Knowledge added successfully!", "extracted_length": len(extracted_text)}), 200
    except Exception as e:
        return jsonify({"error": "Upload failed"}), 500

# ---------- AI Provider Functions ----------
def call_groq_updated(prompt):
    if not GROQ_API_KEY:
        return None
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 500
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        else:
            print(f"Groq error {resp.status_code}: {resp.text}")
            return None
    except Exception as e:
        print(f"Groq exception: {e}")
        return None

def call_cohere_updated(prompt):
    if not COHERE_API_KEY:
        return None
    url = "https://api.cohere.ai/v1/chat"
    headers = {"Authorization": f"Bearer {COHERE_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "command",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 500
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            return resp.json()["text"].strip()
        else:
            print(f"Cohere error {resp.status_code}: {resp.text}")
            return None
    except Exception as e:
        print(f"Cohere exception: {e}")
        return None

def call_openrouter_updated(prompt):
    if not OPENROUTER_API_KEY:
        return None
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "nvidia/nemotron-3-nano-30b-a3b:free",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 500
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        else:
            print(f"OpenRouter error {resp.status_code}: {resp.text}")
            return None
    except Exception as e:
        print(f"OpenRouter exception: {e}")
        return None

@app.route('/api/chat', methods=['POST'])
def chat():
    user_question = request.json.get('message')
    if not user_question:
        return jsonify({"error": "No message"}), 400

    context = get_portfolio_context()
    birth_year = 1997
    current_year = datetime.now().year
    age = current_year - birth_year
    current_date = datetime.now().strftime("%B %d, %Y")
    
    prompt = f"""
You are the Anele, even when answering questions use terms like "I","Me"(first person terms) but when asked state that you official AI duplicate version of Anele Mhlongo. Your primary goal is to provide accurate, helpful information about Anele's professional experience, education, overall career, non-sensitive personal information and projects to recruiters, clients, and visitors.

**Tone & Style**
- Persona: Professional, calm, informative, and approachable.
- Humor: You may use very mild, occasional humor, but prioritize clarity and professionalism but dont seem like a report.
- Emojis: Use emojis exceedingly sparingly (maximum of one per conversation turn, and only if it naturally fits). Never use exaggerated or overly joyful emojis (like 🤩, 🎉, or 🤪).

**Knowledge Boundaries & Accuracy**
- Zero Hallucination: You must be 100% certain of the information you provide. Only use the data explicitly provided below.
- Skills Enforcement: If asked about Anele's skills, only pull from the "Skills" section below. Do not infer, guess, or list external skills. You may additionally touch on whatever else is regarded as a skill based on what's asked from the information you've been given – nothing else.
- Allowed Expansion: If you are absolutely certain about a fact from the portfolio, you may expand on it slightly to highlight Anele's capabilities, but keep it concise and relevant.
- Fallback Protocol: If you do not have the exact answer in your provided context, do not guess. Instead say exactly: "I don't have the details on that, but I'd recommend reaching out to the original Anele directly to find out. You can contact him from the contact section below."

**Privacy & Security (Strictly Enforced)**
- Under no circumstances reveal sensitive personal information (ID number, student number, physical address, financial details, personal family information, etc.).
- If asked for such information, refuse with: "For privacy and security reasons, I cannot share that information."

You are allowed to expand and talk as if you are a human and use general expansion only on accurate information

**Alwande's Background (from the portfolio database)**
- Name: Anele Mhlongo
- Age: {age} years old (born {birth_year})
- Occupation/Profile: {context.get('job', '')}
- Short bio: {context.get('desc', '')}
- Skills: {json.dumps(context.get('skills', []))}
- Projects: {json.dumps(context.get('projects', []))}
- Certificates: {json.dumps(context.get('certificates', []))}
- Qualifications: {json.dumps(context.get('quals', []))}
- Work Experience: {json.dumps(context.get('exps', []))}
- Contact info: {json.dumps(context.get('contactInfo', {}))}
- Extra knowledge (hobbies, background, etc.): {context.get('extraKnowledge', '')}

Current date is {current_date}.

Now answer the following user question as Anele . Follow all the rules above strictly.
User question: {user_question}
"""

    # Try providers in order
    answer = call_groq_updated(prompt)
    if answer:
        return jsonify({"answer": answer}), 200

    answer = call_cohere_updated(prompt)
    if answer:
        return jsonify({"answer": answer}), 200

    answer = call_openrouter_updated(prompt)
    if answer:
        return jsonify({"answer": answer}), 200

    return jsonify({"answer": "Im sorry, im currently busy right now and can't really talk, can we try again tomorrow"}), 200

@app.route('/ping')
def ping():
    return "OK", 200

if __name__ == '__main__':
    app.run(debug=True)