from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import sqlite3
import pymupdf
import os
import json
from datetime import datetime, date, timedelta
from functools import wraps
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from google import genai
from google.genai.errors import ServerError

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "ai-study-companion-secure-key-2026")

UPLOAD_FOLDER = "uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Configurable Gemini Model
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

# Initialize Gemini Client
client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY")
)

# Helper function to get database connection
def get_db():
    conn = sqlite3.connect("study_companion.db")
    conn.row_factory = sqlite3.Row
    return conn

# Database Initialization
def init_db():
    conn = get_db()
    cursor = conn.cursor()

    # 1. Users Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Ensure created_at exists if upgrading an existing users table
    cursor.execute("PRAGMA table_info(users)")
    columns = [col["name"] for col in cursor.fetchall()]
    if "created_at" not in columns:
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        except sqlite3.OperationalError:
            pass

    # 2. Uploaded Documents Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            filename TEXT NOT NULL,
            extracted_text TEXT,
            summary TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # 3. Quiz Attempts Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quiz_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            score INTEGER,
            total INTEGER,
            weak_topics TEXT,
            test_type TEXT DEFAULT 'Quiz',
            percentage REAL DEFAULT 0.0,
            strong_topics TEXT DEFAULT '[]',
            time_taken_seconds INTEGER DEFAULT 0,
            details_json TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # Ensure new columns exist for quiz_attempts if upgrading
    cursor.execute("PRAGMA table_info(quiz_attempts)")
    qa_cols = [col["name"] for col in cursor.fetchall()]
    for col_name, col_type, default_val in [
        ("test_type", "TEXT", "'Quiz'"),
        ("percentage", "REAL", "0.0"),
        ("strong_topics", "TEXT", "'[]'"),
        ("time_taken_seconds", "INTEGER", "0"),
        ("details_json", "TEXT", "'[]'")
    ]:
        if col_name not in qa_cols:
            try:
                cursor.execute(f"ALTER TABLE quiz_attempts ADD COLUMN {col_name} {col_type} DEFAULT {default_val}")
            except sqlite3.OperationalError:
                pass

    # 4. Study Sessions Table (Timer Tracking)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS study_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            duration_seconds INTEGER,
            session_date DATE DEFAULT (DATE('now')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # 5. Study Plans Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS study_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            subject TEXT NOT NULL,
            topic TEXT NOT NULL,
            target_date DATE,
            status TEXT DEFAULT 'pending',
            priority TEXT DEFAULT 'Medium',
            estimated_hours INTEGER DEFAULT 2,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # Ensure priority and estimated_hours exist if upgrading
    cursor.execute("PRAGMA table_info(study_plans)")
    plan_cols = [col["name"] for col in cursor.fetchall()]
    if "priority" not in plan_cols:
        try:
            cursor.execute("ALTER TABLE study_plans ADD COLUMN priority TEXT DEFAULT 'Medium'")
        except sqlite3.OperationalError:
            pass
    if "estimated_hours" not in plan_cols:
        try:
            cursor.execute("ALTER TABLE study_plans ADD COLUMN estimated_hours INTEGER DEFAULT 2")
        except sqlite3.OperationalError:
            pass

    # 6. Doubts Q&A Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS doubts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # 7. Revisions Table (Spaced Repetition)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            topic TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            interval_days INTEGER DEFAULT 1,
            repetition_count INTEGER DEFAULT 0,
            next_review DATE DEFAULT (DATE('now', '+1 day')),
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    conn.commit()
    conn.close()

init_db()

# Login Required Decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to access this page.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

# Memory fallback for active session
last_extracted_text = ""

# --- Routes ---

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name")
        email = request.form.get("email")
        password = request.form.get("password")

        if not name or not email or not password:
            flash("All fields are required.", "danger")
            return render_template("register.html")

        hashed_password = generate_password_hash(password)

        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO users (name, email, password) VALUES (?, ?, ?)",
                (name, email, hashed_password)
            )
            conn.commit()
            flash("Registration successful! Please login.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Email is already registered. Please login.", "danger")
            return render_template("register.html")
        finally:
            conn.close()

    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
        user = cursor.fetchone()
        conn.close()

        if user:
            password_match = False
            stored_pwd = user["password"]
            
            # Check hashed or legacy plaintext password
            if stored_pwd.startswith("scrypt:") or stored_pwd.startswith("pbkdf2:"):
                password_match = check_password_hash(stored_pwd, password)
            else:
                if stored_pwd == password:
                    password_match = True
                    # Upgrade legacy plaintext to secure hash
                    new_hash = generate_password_hash(password)
                    up_conn = get_db()
                    up_conn.cursor().execute("UPDATE users SET password = ? WHERE id = ?", (new_hash, user["id"]))
                    up_conn.commit()
                    up_conn.close()

            if password_match:
                session["user_id"] = user["id"]
                session["user_name"] = user["name"]
                session["user_email"] = user["email"]
                flash(f"Welcome back, {user['name']}!", "success")
                return redirect(url_for("dashboard"))

        flash("Invalid email or password.", "danger")
        return render_template("login.html")

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out successfully.", "info")
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    user_id = session["user_id"]
    today_str = date.today().isoformat()
    conn = get_db()
    cursor = conn.cursor()

    # 1. Total Study Time & Sessions from SQLite
    cursor.execute("""
        SELECT SUM(duration_seconds) as total_sec, COUNT(*) as session_count 
        FROM study_sessions 
        WHERE user_id = ?
    """, (user_id,))
    session_row = cursor.fetchone()
    total_seconds = session_row["total_sec"] or 0
    study_sessions_count = session_row["session_count"] or 0

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    total_time_str = f"{hours:02d}h {minutes:02d}m"

    # 2. Daily Goal Progress (Target: 60 mins)
    cursor.execute("""
        SELECT SUM(duration_seconds) as today_sec 
        FROM study_sessions 
        WHERE user_id = ? AND session_date = ?
    """, (user_id, today_str))
    today_sec = cursor.fetchone()["today_sec"] or 0
    today_mins = today_sec // 60
    daily_goal_mins = 60
    daily_goal_pct = min(int((today_mins / daily_goal_mins) * 100), 100)

    # 3. Weekly Progress (Last 7 Days)
    weekly_labels = []
    weekly_mins = []
    for i in range(6, -1, -1):
        day_date = date.today() - timedelta(days=i)
        day_iso = day_date.isoformat()
        day_name = day_date.strftime("%a")
        weekly_labels.append(day_name)

        cursor.execute("""
            SELECT SUM(duration_seconds) as d_sec 
            FROM study_sessions 
            WHERE user_id = ? AND session_date = ?
        """, (user_id, day_iso))
        d_sec = cursor.fetchone()["d_sec"] or 0
        weekly_mins.append(round(d_sec / 60, 1))

    # 4. Study Streak Calculation
    cursor.execute("""
        SELECT DISTINCT session_date 
        FROM study_sessions 
        WHERE user_id = ? 
        ORDER BY session_date DESC
    """, (user_id,))
    active_dates = [r["session_date"] for r in cursor.fetchall()]
    
    streak = 0
    check_day = date.today()
    if active_dates:
        if active_dates[0] == check_day.isoformat():
            curr = check_day
            for d in active_dates:
                if d == curr.isoformat():
                    streak += 1
                    curr -= timedelta(days=1)
                else:
                    break
        elif active_dates[0] == (check_day - timedelta(days=1)).isoformat():
            curr = check_day - timedelta(days=1)
            for d in active_dates:
                if d == curr.isoformat():
                    streak += 1
                    curr -= timedelta(days=1)
                else:
                    break
    if streak == 0 and total_seconds > 0:
        streak = 1

    # 5. Quiz Performance & Topic Tracking
    cursor.execute("""
        SELECT * FROM quiz_attempts 
        WHERE user_id = ? 
        ORDER BY id DESC
    """, (user_id,))
    all_attempts = cursor.fetchall()
    total_tests = len(all_attempts)
    avg_score = round(sum(a["percentage"] for a in all_attempts) / total_tests, 1) if total_tests > 0 else 0.0
    latest_attempt = all_attempts[0] if total_tests > 0 else None

    weak_topic_counts = {}
    strong_topic_set = set()
    for a in all_attempts:
        if a["weak_topics"]:
            try:
                wt = json.loads(a["weak_topics"])
                for t in wt:
                    weak_topic_counts[t] = weak_topic_counts.get(t, 0) + 1
            except Exception:
                pass
        if a["strong_topics"]:
            try:
                st = json.loads(a["strong_topics"])
                for t in st:
                    strong_topic_set.add(t)
            except Exception:
                pass

    sorted_weak_topics = sorted(weak_topic_counts.items(), key=lambda x: x[1], reverse=True)

    # 6. Pending Revisions Due Today
    cursor.execute("""
        SELECT COUNT(*) as count FROM revisions 
        WHERE user_id = ? AND (next_review <= ? OR next_review IS NULL)
    """, (user_id, today_str))
    due_revisions_count = cursor.fetchone()["count"]

    # 7. Study Plans Summary
    cursor.execute("SELECT * FROM study_plans WHERE user_id = ?", (user_id,))
    all_plans = cursor.fetchall()
    total_plans = len(all_plans)
    completed_plans = sum(1 for p in all_plans if p["status"] == "completed")
    syllabus_pct = int((completed_plans / total_plans) * 100) if total_plans > 0 else 0

    cursor.execute("""
        SELECT * FROM study_plans 
        WHERE user_id = ? AND status = 'pending' 
        ORDER BY 
            CASE priority WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END, 
            target_date ASC 
        LIMIT 1
    """, (user_id,))
    next_topic = cursor.fetchone()

    cursor.execute("SELECT id, filename, created_at FROM documents WHERE user_id = ? ORDER BY id DESC LIMIT 3", (user_id,))
    recent_docs = cursor.fetchall()

    conn.close()

    return render_template(
        "dashboard.html",
        user_name=session.get("user_name", "Student"),
        total_time_str=total_time_str,
        total_seconds=total_seconds,
        study_sessions_count=study_sessions_count,
        today_mins=today_mins,
        daily_goal_mins=daily_goal_mins,
        daily_goal_pct=daily_goal_pct,
        streak=streak,
        total_tests=total_tests,
        avg_score=avg_score,
        latest_attempt=latest_attempt,
        strong_topics=list(strong_topic_set)[:5],
        weak_topics=sorted_weak_topics[:5],
        due_revisions_count=due_revisions_count,
        total_plans=total_plans,
        completed_plans=completed_plans,
        syllabus_pct=syllabus_pct,
        next_topic=next_topic,
        recent_docs=recent_docs,
        weekly_labels=weekly_labels,
        weekly_mins=weekly_mins
    )

@app.route("/log_study_session", methods=["POST"])
@login_required
def log_study_session():
    data = request.get_json() if request.is_json else request.form
    try:
        duration_seconds = int(data.get("duration_seconds", 0))
    except (ValueError, TypeError):
        duration_seconds = 0

    if duration_seconds > 0:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO study_sessions (user_id, duration_seconds, session_date)
            VALUES (?, ?, DATE('now'))
        """, (session["user_id"], duration_seconds))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Invalid duration"}), 400

@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    global last_extracted_text
    extracted_text = ""
    summary = ""

    if request.method == "POST":
        file = request.files.get("pdf")

        if not file or file.filename == "":
            flash("Please select a PDF file.", "warning")
            return render_template("upload.html")

        if not file.filename.lower().endswith(".pdf"):
            flash("Only PDF files are allowed.", "danger")
            return render_template("upload.html")

        filepath = os.path.join(app.config["UPLOAD_FOLDER"], file.filename)
        file.save(filepath)

        try:
            document = pymupdf.open(filepath)
            for page in document:
                extracted_text += page.get_text()
            document.close()
        except Exception as e:
            flash(f"Error reading PDF: {str(e)}", "danger")
            return render_template("upload.html")

        last_extracted_text = extracted_text

        # Generate summary using Gemini
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=(
                    "Summarize this engineering study material in simple language "
                    "using headings and bullet points:\n\n"
                    + extracted_text[:12000]
                )
            )
            summary = response.text

            # Save document to database
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO documents (user_id, filename, extracted_text, summary) VALUES (?, ?, ?, ?)",
                (session["user_id"], file.filename, extracted_text, summary)
            )
            conn.commit()
            conn.close()

        except ServerError:
            summary = "Gemini is currently busy. Please wait a little and try again."
        except Exception as e:
            summary = f"Summary generation failed: {str(e)}"

    return render_template(
        "upload.html",
        extracted_text=extracted_text,
        summary=summary
    )

@app.route("/generate_quiz", methods=["POST"])
@login_required
def generate_quiz():
    if not last_extracted_text:
        flash("Please upload a PDF first.", "warning")
        return redirect(url_for("upload"))

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=f"""
Create exactly 5 multiple-choice questions from the following engineering study notes.

For every question, provide:
- Question
- Option A, Option B, Option C, Option D
- Correct answer (A, B, C, or D)
- Short explanation
- Topic: Specific engineering topic

Return ONLY valid JSON in this exact structure without markdown fences:
[
  {{
    "topic": "Specific topic",
    "question": "Question text",
    "options": {{
      "A": "Option A",
      "B": "Option B",
      "C": "Option C",
      "D": "Option D"
    }},
    "answer": "A",
    "explanation": "Short explanation"
  }}
]

Study notes:
{last_extracted_text[:12000]}
"""
        )

        clean_text = response.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        quiz_data = json.loads(clean_text)

        for question in quiz_data:
            if not question.get("topic"):
                question["topic"] = "General"

        return render_template("quiz.html", quiz=quiz_data)
    except Exception as e:
        flash(f"Failed to generate quiz: {str(e)}", "danger")
        return redirect(url_for("upload"))

@app.route("/generate_flashcards", methods=["POST"])
@login_required
def generate_flashcards():
    if not last_extracted_text:
        flash("Please upload a PDF first.", "warning")
        return redirect(url_for("upload"))

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=f"""
Create exactly 5 study flashcards from the following engineering study notes.

Return ONLY valid JSON without markdown fences:
[
  {{
    "question": "Question or term",
    "answer": "Short and clear answer"
  }}
]

Study notes:
{last_extracted_text[:12000]}
"""
        )

        clean_text = response.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        flashcards = json.loads(clean_text)
        return render_template("flashcards.html", flashcards=flashcards)
    except Exception as e:
        flash(f"Failed to generate flashcards: {str(e)}", "danger")
        return redirect(url_for("upload"))

@app.route("/mindmap", methods=["GET"])
@login_required
def mindmap_view():
    global last_extracted_text
    active_map = session.get("active_mindmap")
    
    # If not in session, check if notes exist in DB to suggest auto-generation
    doc_name = None
    if not last_extracted_text:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT filename, extracted_text FROM documents WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (session["user_id"],))
        doc = cursor.fetchone()
        conn.close()
        if doc and doc["extracted_text"]:
            last_extracted_text = doc["extracted_text"]
            doc_name = doc["filename"]

    return render_template(
        "mindmap.html",
        mindmap=active_map,
        has_notes=bool(last_extracted_text),
        doc_name=doc_name
    )

@app.route("/generate_mindmap", methods=["GET", "POST"])
@login_required
def generate_mindmap():
    global last_extracted_text
    custom_topic = None

    if request.method == "POST":
        if request.is_json:
            data = request.get_json() or {}
            custom_topic = data.get("topic", "").strip()
        else:
            custom_topic = request.form.get("topic", "").strip()

    # Hydrate document from SQLite if needed
    if not last_extracted_text and not custom_topic:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT filename, extracted_text FROM documents WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (session["user_id"],))
        doc = cursor.fetchone()
        conn.close()
        if doc and doc["extracted_text"]:
            last_extracted_text = doc["extracted_text"]

    if not last_extracted_text and not custom_topic:
        flash("Please upload a PDF or enter an engineering topic first.", "warning")
        return redirect(url_for("upload"))

    subject_scope = f"Focus Topic: '{custom_topic}'" if custom_topic else "Extracted Study Notes"
    notes_context = last_extracted_text[:12000] if last_extracted_text else ""

    prompt = f"""You are an expert engineering academic visualizer.
Create a rich, structured, hierarchical conceptual mind map for: {subject_scope}.

Study Notes Context:
{notes_context}

Return ONLY a valid JSON object with NO markdown formatting or ```json code fences in this exact structure:
{{
    "main_topic": "Central Engineering Domain / Unit Title",
    "summary": "A concise 1-sentence overview of this engineering concept.",
    "topics": [
        {{
            "name": "Module or Principle 1",
            "icon": "⚙️",
            "description": "Short summary of this branch",
            "subtopics": [
                {{
                    "name": "Subtopic A",
                    "detail": "Key technical explanation, formula, or law"
                }},
                {{
                    "name": "Subtopic B",
                    "detail": "Key technical explanation, formula, or law"
                }}
            ]
        }}
    ]
}}

Generate 4 to 6 main topics, each with 2 to 4 detailed subtopics.
"""

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )

        clean_text = response.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        mindmap_data = json.loads(clean_text)

        # Normalize subtopics so both string and dict subtopics work seamlessly
        for t in mindmap_data.get("topics", []):
            normalized_sub = []
            for st in t.get("subtopics", []):
                if isinstance(st, str):
                    normalized_sub.append({"name": st, "detail": "Core technical concept."})
                elif isinstance(st, dict):
                    normalized_sub.append({
                        "name": st.get("name", "Concept"),
                        "detail": st.get("detail", "Technical concept detail.")
                    })
            t["subtopics"] = normalized_sub

        # Save to session for fast rendering on reload
        session["active_mindmap"] = mindmap_data

        if request.is_json:
            return jsonify({"success": True, "mindmap": mindmap_data})

        return render_template("mindmap.html", mindmap=mindmap_data, has_notes=bool(last_extracted_text))

    except Exception as e:
        if request.is_json:
            return jsonify({"success": False, "error": str(e)}), 500
        flash(f"Failed to generate mind map: {str(e)}", "danger")
        return redirect(url_for("upload"))

@app.route("/generate_questions", methods=["POST"])
@login_required
def generate_questions():
    if not last_extracted_text:
        flash("Please upload a PDF first.", "warning")
        return redirect(url_for("upload"))

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=f"""
Create VTU/GATE-style practice questions from the following engineering study notes.

Return ONLY valid JSON without markdown fences:
[
    {{
        "type": "VTU 2-Mark",
        "question": "Question text"
    }},
    {{
        "type": "GATE MCQ",
        "question": "Question text",
        "options": {{
            "A": "Option A",
            "B": "Option B",
            "C": "Option C",
            "D": "Option D"
        }},
        "answer": "A"
    }}
]

Generate exactly 8 questions:
- 2 VTU 2-mark questions
- 2 VTU 5-mark questions
- 2 VTU 10-mark questions
- 2 GATE-style MCQs

Study notes:
{last_extracted_text[:12000]}
"""
        )

        clean_text = response.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        questions = json.loads(clean_text)
        return render_template("questions.html", questions=questions)
    except Exception as e:
        flash(f"Failed to generate questions: {str(e)}", "danger")
        return redirect(url_for("upload"))

@app.route("/study_timer")
@login_required
def study_timer():
    return render_template("study_timer.html")

@app.route("/doubts")
@login_required
def doubts():
    global last_extracted_text
    conn = get_db()
    cursor = conn.cursor()

    current_doc_name = None
    if not last_extracted_text:
        cursor.execute("SELECT filename, extracted_text FROM documents WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (session["user_id"],))
        doc = cursor.fetchone()
        if doc and doc["extracted_text"]:
            last_extracted_text = doc["extracted_text"]
            current_doc_name = doc["filename"]
    else:
        cursor.execute("SELECT filename FROM documents WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (session["user_id"],))
        doc = cursor.fetchone()
        if doc:
            current_doc_name = doc["filename"]

    cursor.execute("SELECT * FROM doubts WHERE user_id = ? ORDER BY id ASC", (session["user_id"],))
    doubt_history = cursor.fetchall()
    conn.close()
    return render_template("doubts.html", doubts=doubt_history, has_notes=bool(last_extracted_text), doc_name=current_doc_name)

@app.route("/ask_doubt", methods=["POST"])
@login_required
def ask_doubt():
    global last_extracted_text
    
    if request.is_json:
        data = request.get_json()
        question = data.get("question", "").strip() if data else ""
    else:
        question = request.form.get("question", "").strip()

    if not question:
        if request.is_json:
            return jsonify({"success": False, "error": "Question cannot be empty."}), 400
        flash("Please enter a question.", "warning")
        return redirect(url_for("doubts"))

    conn = get_db()
    cursor = conn.cursor()

    # Check if we need to hydrate document from database
    if not last_extracted_text:
        cursor.execute("SELECT extracted_text FROM documents WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (session["user_id"],))
        doc = cursor.fetchone()
        if doc and doc["extracted_text"]:
            last_extracted_text = doc["extracted_text"]

    # Retrieve last 3 doubts for follow-up conversational context
    cursor.execute("SELECT question, answer FROM doubts WHERE user_id = ? ORDER BY id DESC LIMIT 3", (session["user_id"],))
    recent_doubts = cursor.fetchall()
    recent_doubts = list(reversed(recent_doubts))

    history_context = ""
    if recent_doubts:
        history_context = "\nRecent Conversation History:\n"
        for rd in recent_doubts:
            history_context += f"Student: {rd['question']}\nTutor: {rd['answer']}\n"

    study_context = last_extracted_text[:14000] if last_extracted_text else "No uploaded document. Answer as a general engineering expert."

    prompt = f"""You are a friendly, encouraging AI Study Companion and Engineering Tutor for undergraduate B.Tech engineering students.
A student is asking for help with an engineering doubt.

---
Study Notes Context:
{study_context}
---
{history_context}
---
Current Student Question:
{question}

Instructions:
1. If the question relates to the study notes, prioritize the facts and concepts from the notes.
2. Explain the core engineering concept in clear, simple language that is easy to understand.
3. Use bullet points, bold key terms, and simple examples or intuitive analogies where helpful.
4. Support follow-up questions smoothly if the student refers to an earlier answer.
5. If mathematical or algorithmic steps are involved, break them down step-by-step.
6. Keep the response concise, structured, and educational without unnecessary fluff.
"""

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )
        answer = response.text.strip()
    except Exception as e:
        answer = f"I encountered an issue connecting to Gemini AI: {str(e)}. Please check your internet connection or try again."

    # Save to doubts table
    cursor.execute(
        "INSERT INTO doubts (user_id, question, answer) VALUES (?, ?, ?)",
        (session["user_id"], question, answer)
    )
    conn.commit()
    doubt_id = cursor.lastrowid
    conn.close()

    if request.is_json:
        return jsonify({
            "success": True,
            "id": doubt_id,
            "question": question,
            "answer": answer
        })

    return redirect(url_for("doubts"))

@app.route("/clear_doubts", methods=["POST"])
@login_required
def clear_doubts():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM doubts WHERE user_id = ?", (session["user_id"],))
    conn.commit()
    conn.close()
    flash("Doubt conversation history cleared.", "info")
    return redirect(url_for("doubts"))

@app.route("/planner")
@login_required
def planner():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM study_plans 
        WHERE user_id = ? 
        ORDER BY 
            CASE status WHEN 'pending' THEN 1 ELSE 2 END,
            target_date ASC,
            id DESC
    """, (session["user_id"],))
    plans = cursor.fetchall()

    total_plans = len(plans)
    completed_plans = sum(1 for p in plans if p["status"] == "completed")
    pending_plans = total_plans - completed_plans
    progress_pct = int((completed_plans / total_plans) * 100) if total_plans > 0 else 0

    cursor.execute("""
        SELECT * FROM study_plans 
        WHERE user_id = ? AND status = 'pending' 
        ORDER BY 
            CASE priority WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END,
            target_date ASC 
        LIMIT 1
    """, (session["user_id"],))
    next_topic = cursor.fetchone()

    cursor.execute("SELECT weak_topics FROM quiz_attempts WHERE user_id = ? ORDER BY id DESC LIMIT 3", (session["user_id"],))
    quiz_rows = cursor.fetchall()
    conn.close()

    weak_areas = []
    for row in quiz_rows:
        if row["weak_topics"]:
            try:
                wt_list = json.loads(row["weak_topics"])
                weak_areas.extend(wt_list)
            except Exception:
                pass

    today_str = date.today().isoformat()
    return render_template(
        "planner.html",
        plans=plans,
        total_plans=total_plans,
        completed_plans=completed_plans,
        pending_plans=pending_plans,
        progress_pct=progress_pct,
        next_topic=next_topic,
        weak_areas=list(set(weak_areas)),
        today_str=today_str,
        has_notes=bool(last_extracted_text)
    )

@app.route("/add_plan", methods=["POST"])
@login_required
def add_plan():
    subject = request.form.get("subject", "").strip()
    topic = request.form.get("topic", "").strip()
    target_date = request.form.get("target_date", "").strip()
    priority = request.form.get("priority", "Medium").strip()
    try:
        estimated_hours = int(request.form.get("estimated_hours", 2))
    except (ValueError, TypeError):
        estimated_hours = 2

    if not subject or not topic:
        flash("Subject and Topic are required.", "warning")
        return redirect(url_for("planner"))

    if not target_date:
        target_date = (date.today() + timedelta(days=3)).isoformat()

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO study_plans (user_id, subject, topic, target_date, status, priority, estimated_hours)
        VALUES (?, ?, ?, ?, 'pending', ?, ?)
    """, (session["user_id"], subject, topic, target_date, priority, estimated_hours))
    conn.commit()
    conn.close()

    flash(f"Goal for '{topic}' added to your study plan!", "success")
    return redirect(url_for("planner"))

@app.route("/toggle_plan_status/<int:plan_id>", methods=["POST"])
@login_required
def toggle_plan_status(plan_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM study_plans WHERE id = ? AND user_id = ?", (plan_id, session["user_id"]))
    plan = cursor.fetchone()

    if not plan:
        conn.close()
        if request.is_json:
            return jsonify({"success": False, "error": "Plan not found"}), 404
        flash("Goal not found.", "warning")
        return redirect(url_for("planner"))

    new_status = "completed" if plan["status"] == "pending" else "pending"
    cursor.execute("UPDATE study_plans SET status = ? WHERE id = ? AND user_id = ?", (new_status, plan_id, session["user_id"]))
    conn.commit()
    conn.close()

    if request.is_json:
        return jsonify({"success": True, "new_status": new_status})

    flash(f"Study goal marked as {new_status}!", "success")
    return redirect(url_for("planner"))

@app.route("/delete_plan/<int:plan_id>", methods=["POST"])
@login_required
def delete_plan(plan_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM study_plans WHERE id = ? AND user_id = ?", (plan_id, session["user_id"]))
    conn.commit()
    conn.close()

    flash("Study goal removed.", "info")
    return redirect(url_for("planner"))

@app.route("/generate_ai_schedule", methods=["POST"])
@login_required
def generate_ai_schedule():
    global last_extracted_text
    data = request.get_json() if request.is_json else request.form

    subject = data.get("subject", "").strip() or "Engineering Core"
    try:
        days = int(data.get("days", 7))
    except (ValueError, TypeError):
        days = 7
    try:
        daily_hours = int(data.get("daily_hours", 2))
    except (ValueError, TypeError):
        daily_hours = 2

    notes_snippet = last_extracted_text[:6000] if last_extracted_text else ""
    prompt = f"""You are an expert engineering academic advisor for B.Tech students.
Generate a structured, progressive {days}-day study plan for the subject '{subject}'.
The student can dedicate {daily_hours} hour(s) per day.

Study Notes Context (if available):
{notes_snippet}

Return ONLY a valid JSON array of {days} items (one for each day), with NO markdown formatting or ```json code fences:
[
  {{
    "day": 1,
    "topic": "Specific Topic Name",
    "day_offset": 1,
    "priority": "High",
    "estimated_hours": {daily_hours},
    "tip": "VTU/GATE exam focus tip or core concept to master"
  }}
]
"""

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )
        clean_text = response.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        schedule_data = json.loads(clean_text)
        return jsonify({"success": True, "schedule": schedule_data})
    except Exception as e:
        return jsonify({"success": False, "error": f"Failed to generate schedule: {str(e)}"}), 500

@app.route("/bulk_add_plans", methods=["POST"])
@login_required
def bulk_add_plans():
    data = request.get_json()
    if not data or "plans" not in data:
        return jsonify({"success": False, "error": "No plans data provided."}), 400

    subject = data.get("subject", "Engineering Subject").strip()
    plans_list = data.get("plans", [])

    conn = get_db()
    cursor = conn.cursor()
    count = 0
    today = date.today()

    for item in plans_list:
        topic = item.get("topic", "").strip()
        if not topic:
            continue
        try:
            day_offset = int(item.get("day_offset", count + 1))
        except (ValueError, TypeError):
            day_offset = count + 1
        target_date = (today + timedelta(days=day_offset)).isoformat()
        priority = item.get("priority", "Medium")
        try:
            hours = int(item.get("estimated_hours", 2))
        except (ValueError, TypeError):
            hours = 2

        cursor.execute("""
            INSERT INTO study_plans (user_id, subject, topic, target_date, status, priority, estimated_hours)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """, (session["user_id"], subject, topic, target_date, priority, hours))
        count += 1

    conn.commit()
    conn.close()
    return jsonify({"success": True, "added_count": count})

# --- Feature E: Advanced Mock Test System ---

@app.route("/mock_test")
@login_required
def mock_test():
    global last_extracted_text
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, filename FROM documents WHERE user_id = ? ORDER BY created_at DESC LIMIT 5", (session["user_id"],))
    recent_docs = cursor.fetchall()

    cursor.execute("""
        SELECT * FROM quiz_attempts 
        WHERE user_id = ? 
        ORDER BY id DESC 
        LIMIT 5
    """, (session["user_id"],))
    past_attempts = cursor.fetchall()
    conn.close()

    return render_template(
        "mock_test.html",
        has_notes=bool(last_extracted_text),
        recent_docs=recent_docs,
        past_attempts=past_attempts
    )

@app.route("/start_mock_test", methods=["POST"])
@login_required
def start_mock_test():
    global last_extracted_text
    test_format = request.form.get("test_format", "GATE MCQ Challenge")
    subject = request.form.get("subject", "").strip() or "Engineering Examination"
    try:
        duration_minutes = int(request.form.get("duration_minutes", 10))
    except (ValueError, TypeError):
        duration_minutes = 10

    if not last_extracted_text:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT extracted_text FROM documents WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (session["user_id"],))
        doc = cursor.fetchone()
        conn.close()
        if doc and doc["extracted_text"]:
            last_extracted_text = doc["extracted_text"]

    notes_context = last_extracted_text[:12000] if last_extracted_text else ""

    if "GATE" in test_format:
        num_questions = 6
        prompt_instruction = f"""Generate exactly {num_questions} GATE-style Multiple Choice Questions for engineering students covering '{subject}'.
Focus on analytical problem-solving and conceptual accuracy.

Return ONLY a valid JSON array of objects with NO markdown formatting or ```json code fences:
[
  {{
    "id": 1,
    "type": "GATE MCQ",
    "topic": "Specific Topic Name",
    "marks": 2,
    "question": "Question statement...",
    "options": {{
      "A": "Option A text",
      "B": "Option B text",
      "C": "Option C text",
      "D": "Option D text"
    }},
    "correct_answer": "B",
    "explanation": "Concise step-by-step reasoning."
  }}
]
"""
    elif "VTU" in test_format:
        prompt_instruction = f"""Generate a balanced VTU Engineering Exam Paper covering '{subject}' with exactly 5 questions:
- 2 VTU 2-Mark Questions (definitions/principles)
- 2 VTU 5-Mark Questions (explanations/comparisons/algorithms)
- 1 GATE MCQ (analytical)

Return ONLY a valid JSON array of objects with NO markdown formatting or ```json code fences:
[
  {{
    "id": 1,
    "type": "VTU 2-Mark",
    "topic": "Specific Topic Name",
    "marks": 2,
    "question": "Question statement...",
    "sample_answer": "Key points expected in student answer.",
    "keywords": ["keyword1", "keyword2"],
    "explanation": "Model answer explanation."
  }},
  {{
    "id": 2,
    "type": "GATE MCQ",
    "topic": "Specific Topic Name",
    "marks": 2,
    "question": "MCQ Question statement...",
    "options": {{
      "A": "Option A",
      "B": "Option B",
      "C": "Option C",
      "D": "Option D"
    }},
    "correct_answer": "A",
    "explanation": "Explanation."
  }}
]
"""
    else:
        num_questions = 5
        prompt_instruction = f"""Generate exactly {num_questions} Multiple Choice Questions for '{subject}'.
Return ONLY a valid JSON array of objects with NO markdown formatting or ```json code fences:
[
  {{
    "id": 1,
    "type": "Quiz MCQ",
    "topic": "Topic Name",
    "marks": 1,
    "question": "Question text...",
    "options": {{
      "A": "Option A",
      "B": "Option B",
      "C": "Option C",
      "D": "Option D"
    }},
    "correct_answer": "C",
    "explanation": "Explanation."
  }}
]
"""

    full_prompt = f"""{prompt_instruction}

Study Notes Context:
{notes_context}
"""

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=full_prompt
        )
        clean_text = response.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        questions_list = json.loads(clean_text)

        session["active_test"] = {
            "test_format": test_format,
            "subject": subject,
            "duration_minutes": duration_minutes,
            "duration_seconds": duration_minutes * 60,
            "questions": questions_list,
            "start_time": datetime.now().isoformat()
        }
        return redirect(url_for("take_mock_test"))
    except Exception as e:
        flash(f"Error generating mock test: {str(e)}", "danger")
        return redirect(url_for("mock_test"))

@app.route("/take_mock_test")
@login_required
def take_mock_test():
    active_test = session.get("active_test")
    if not active_test:
        flash("No active mock test. Please start a new test.", "warning")
        return redirect(url_for("mock_test"))

    return render_template("mock_test_active.html", test=active_test)

@app.route("/submit_mock_test", methods=["POST"])
@login_required
def submit_mock_test():
    active_test = session.get("active_test")
    if not active_test:
        flash("Session expired. Please start a new test.", "warning")
        return redirect(url_for("mock_test"))

    questions = active_test.get("questions", [])
    try:
        time_taken_seconds = int(request.form.get("time_taken_seconds", 0))
    except (ValueError, TypeError):
        time_taken_seconds = 0

    total_marks = 0
    earned_marks = 0
    strong_topics = []
    weak_topics = []
    evaluated_details = []

    for q in questions:
        q_id = str(q.get("id"))
        q_type = q.get("type", "MCQ")
        q_topic = q.get("topic", "General")
        max_marks = int(q.get("marks", 2))
        total_marks += max_marks

        student_answer = request.form.get(f"q_{q_id}", "").strip()
        marks_awarded = 0
        is_correct = False
        feedback = ""

        if "MCQ" in q_type:
            correct_ans = q.get("correct_answer", "A").upper()
            if student_answer.upper() == correct_ans:
                marks_awarded = max_marks
                is_correct = True
                strong_topics.append(q_topic)
                feedback = "Correct! " + q.get("explanation", "")
            else:
                marks_awarded = 0
                weak_topics.append(q_topic)
                feedback = f"Incorrect. Correct answer was ({correct_ans}). " + q.get("explanation", "")
        else:
            keywords = q.get("keywords", [])
            matches = sum(1 for kw in keywords if kw.lower() in student_answer.lower())
            
            if len(student_answer) < 5:
                marks_awarded = 0
                weak_topics.append(q_topic)
                feedback = "Question was not attempted or answer too brief."
            elif matches >= len(keywords) and len(keywords) > 0:
                marks_awarded = max_marks
                is_correct = True
                strong_topics.append(q_topic)
                feedback = "Excellent! You covered all key technical principles."
            elif matches > 0:
                marks_awarded = max(1, max_marks // 2)
                weak_topics.append(q_topic)
                feedback = "Partially correct. Included some key points but missed others."
            else:
                if len(student_answer) > 30:
                    marks_awarded = 1
                    weak_topics.append(q_topic)
                    feedback = "Attempted with relevant context; review model answer for full marks."
                else:
                    marks_awarded = 0
                    weak_topics.append(q_topic)
                    feedback = "Incomplete answer. Model answer: " + q.get("sample_answer", "")

        earned_marks += marks_awarded
        evaluated_details.append({
            "id": q_id,
            "type": q_type,
            "topic": q_topic,
            "question": q.get("question"),
            "options": q.get("options"),
            "student_answer": student_answer or "Not Answered",
            "correct_answer": q.get("correct_answer") or q.get("sample_answer"),
            "marks_awarded": marks_awarded,
            "max_marks": max_marks,
            "is_correct": is_correct,
            "feedback": feedback
        })

    percentage = round((earned_marks / total_marks * 100), 1) if total_marks > 0 else 0.0

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO quiz_attempts (
            user_id, score, total, weak_topics, test_type, percentage, 
            strong_topics, time_taken_seconds, details_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        session["user_id"],
        earned_marks,
        total_marks,
        json.dumps(list(set(weak_topics))),
        active_test.get("test_format", "Mock Test"),
        percentage,
        json.dumps(list(set(strong_topics))),
        time_taken_seconds,
        json.dumps(evaluated_details)
    ))
    conn.commit()
    attempt_id = cursor.lastrowid
    conn.close()

    session.pop("active_test", None)
    return redirect(url_for("mock_test_result", attempt_id=attempt_id))

@app.route("/mock_test_result/<int:attempt_id>")
@login_required
def mock_test_result(attempt_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM quiz_attempts WHERE id = ? AND user_id = ?", (attempt_id, session["user_id"]))
    attempt = cursor.fetchone()
    conn.close()

    if not attempt:
        flash("Attempt record not found.", "warning")
        return redirect(url_for("mock_test"))

    details = []
    if attempt["details_json"]:
        try:
            details = json.loads(attempt["details_json"])
        except Exception:
            details = []

    weak_topics_list = []
    if attempt["weak_topics"]:
        try:
            weak_topics_list = json.loads(attempt["weak_topics"])
        except Exception:
            weak_topics_list = []

    strong_topics_list = []
    if attempt["strong_topics"]:
        try:
            strong_topics_list = json.loads(attempt["strong_topics"])
        except Exception:
            strong_topics_list = []

    return render_template(
        "mock_test_result.html",
        attempt=attempt,
        details=details,
        weak_topics=weak_topics_list,
        strong_topics=strong_topics_list
    )

@app.route("/save_quiz_score", methods=["POST"])
@login_required
def save_quiz_score():
    data = request.get_json() if request.is_json else request.form
    if not data:
        return jsonify({"success": False, "error": "No data"}), 400

    try:
        score = int(data.get("score", 0))
        total = int(data.get("total", 5))
    except (ValueError, TypeError):
        score, total = 0, 5

    percentage = round((score / total * 100), 1) if total > 0 else 0.0
    weak_topics = data.get("weak_topics", [])
    if isinstance(weak_topics, str):
        try:
            weak_topics = json.loads(weak_topics)
        except Exception:
            weak_topics = [weak_topics]

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO quiz_attempts (user_id, score, total, weak_topics, test_type, percentage)
        VALUES (?, ?, ?, ?, 'Quick Quiz', ?)
    """, (session["user_id"], score, total, json.dumps(weak_topics), percentage))
    conn.commit()
    conn.close()

    return jsonify({"success": True})

# --- Feature C: AI Performance Analysis ---

@app.route("/analytics")
@login_required
def analytics():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM quiz_attempts 
        WHERE user_id = ? 
        ORDER BY created_at ASC
    """, (session["user_id"],))
    attempts = cursor.fetchall()
    conn.close()

    total_tests = len(attempts)
    if total_tests == 0:
        return render_template(
            "analytics.html",
            total_tests=0,
            avg_score=0,
            best_score=0,
            attempts=[],
            chart_dates="[]",
            chart_scores="[]",
            topic_names="[]",
            topic_counts="[]",
            recommendation="No mock tests or quizzes taken yet. Complete your first test to see detailed performance graphs!"
        )

    scores = [a["percentage"] for a in attempts]
    avg_score = round(sum(scores) / total_tests, 1)
    best_score = round(max(scores), 1)

    chart_dates = []
    chart_scores = []
    weak_topic_freq = {}
    strong_topic_freq = {}

    for idx, a in enumerate(attempts):
        date_label = a["created_at"].split(" ")[0] if a["created_at"] else f"Test {idx + 1}"
        chart_dates.append(f"#{idx + 1} ({date_label})")
        chart_scores.append(a["percentage"])

        if a["weak_topics"]:
            try:
                wt = json.loads(a["weak_topics"])
                for t in wt:
                    weak_topic_freq[t] = weak_topic_freq.get(t, 0) + 1
            except Exception:
                pass

        if a["strong_topics"]:
            try:
                st = json.loads(a["strong_topics"])
                for t in st:
                    strong_topic_freq[t] = strong_topic_freq.get(t, 0) + 1
            except Exception:
                pass

    sorted_weak = sorted(weak_topic_freq.items(), key=lambda x: x[1], reverse=True)
    topic_names = [item[0] for item in sorted_weak[:6]]
    topic_counts = [item[1] for item in sorted_weak[:6]]

    if sorted_weak:
        top_weak = ", ".join([f"'{item[0]}'" for item in sorted_weak[:3]])
        recommendation = f"Based on your {total_tests} test attempts, your priority revision focus should be {top_weak}. Practice flashcards and solve targeted VTU/GATE questions on these topics to raise your average from {avg_score}%."
    else:
        recommendation = f"Outstanding performance! You are maintaining an average score of {avg_score}% with strong accuracy across all tested modules."

    recent_attempts = list(reversed(attempts))

    return render_template(
        "analytics.html",
        total_tests=total_tests,
        avg_score=avg_score,
        best_score=best_score,
        attempts=recent_attempts,
        chart_dates=json.dumps(chart_dates),
        chart_scores=json.dumps(chart_scores),
        topic_names=json.dumps(topic_names),
        topic_counts=json.dumps(topic_counts),
        recommendation=recommendation
    )

# --- Feature D: Smart Revision System (Spaced Repetition) ---

@app.route("/revisions")
@login_required
def revisions():
    today_str = date.today().isoformat()
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM revisions 
        WHERE user_id = ? AND (next_review <= ? OR next_review IS NULL)
        ORDER BY next_review ASC, id ASC
    """, (session["user_id"], today_str))
    due_today_cards = cursor.fetchall()

    cursor.execute("""
        SELECT * FROM revisions 
        WHERE user_id = ? AND next_review > ?
        ORDER BY next_review ASC
    """, (session["user_id"], today_str))
    upcoming_cards = cursor.fetchall()

    cursor.execute("""
        SELECT COUNT(*) as count FROM revisions 
        WHERE user_id = ? AND repetition_count >= 3
    """, (session["user_id"],))
    mastered_count = cursor.fetchone()["count"]

    cursor.execute("SELECT COUNT(*) as count FROM revisions WHERE user_id = ?", (session["user_id"],))
    total_cards = cursor.fetchone()["count"]

    cursor.execute("SELECT weak_topics FROM quiz_attempts WHERE user_id = ? ORDER BY id DESC LIMIT 5", (session["user_id"],))
    quiz_rows = cursor.fetchall()
    conn.close()

    weak_topics_set = set()
    for row in quiz_rows:
        if row["weak_topics"]:
            try:
                wt = json.loads(row["weak_topics"])
                for t in wt:
                    weak_topics_set.add(t)
            except Exception:
                pass

    return render_template(
        "revisions.html",
        due_cards=due_today_cards,
        upcoming_cards=upcoming_cards,
        total_cards=total_cards,
        due_count=len(due_today_cards),
        upcoming_count=len(upcoming_cards),
        mastered_count=mastered_count,
        recommended_topics=list(weak_topics_set)[:4],
        today_str=today_str,
        has_notes=bool(last_extracted_text)
    )

@app.route("/generate_revision_cards", methods=["POST"])
@login_required
def generate_revision_cards():
    global last_extracted_text
    data = request.get_json() if request.is_json else request.form
    topic = data.get("topic", "").strip() or "Core Engineering Concepts"

    if not last_extracted_text:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT extracted_text FROM documents WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (session["user_id"],))
        doc = cursor.fetchone()
        conn.close()
        if doc and doc["extracted_text"]:
            last_extracted_text = doc["extracted_text"]

    context_snippet = last_extracted_text[:10000] if last_extracted_text else ""

    prompt = f"""You are an expert engineering educator specializing in high-yield spaced repetition flashcards.
Generate exactly 5 memory-retention revision flashcards focusing on the topic '{topic}'.

Study Material Context (if available):
{context_snippet}

Requirements for each flashcard:
1. 'question': Clear concept question, formula check, law, or definition.
2. 'answer': Concise, high-impact answer that makes technical concepts easy to recall.
3. 'topic': Specific subtopic name.

Return ONLY a valid JSON array of objects with NO markdown formatting or ```json code fences:
[
  {{
    "topic": "{topic}",
    "question": "Question or principle to recall",
    "answer": "Clear, concise technical answer"
  }}
]
"""

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )
        clean_text = response.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        cards_data = json.loads(clean_text)

        conn = get_db()
        cursor = conn.cursor()
        today_str = date.today().isoformat()
        added_count = 0

        for c in cards_data:
            q = c.get("question", "").strip()
            a = c.get("answer", "").strip()
            t = c.get("topic", topic).strip()
            if q and a:
                cursor.execute("""
                    INSERT INTO revisions (user_id, topic, question, answer, interval_days, repetition_count, next_review, status)
                    VALUES (?, ?, ?, ?, 1, 0, ?, 'pending')
                """, (session["user_id"], t, q, a, today_str))
                added_count += 1

        conn.commit()
        conn.close()

        if request.is_json:
            return jsonify({"success": True, "count": added_count})
        
        flash(f"Successfully generated and scheduled {added_count} smart revision flashcards!", "success")
        return redirect(url_for("revisions"))

    except Exception as e:
        if request.is_json:
            return jsonify({"success": False, "error": str(e)}), 500
        flash(f"Error generating revision flashcards: {str(e)}", "danger")
        return redirect(url_for("revisions"))

@app.route("/review_card/<int:card_id>", methods=["POST"])
@login_required
def review_card(card_id):
    data = request.get_json() if request.is_json else request.form
    rating = data.get("rating", "good").lower()

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM revisions WHERE id = ? AND user_id = ?", (card_id, session["user_id"]))
    card = cursor.fetchone()

    if not card:
        conn.close()
        return jsonify({"success": False, "error": "Card not found"}), 404

    current_interval = card["interval_days"] or 1
    current_reps = card["repetition_count"] or 0

    if rating == "hard":
        new_interval = 1
        new_reps = 0
    elif rating == "easy":
        new_interval = max(3, current_interval * 3)
        new_reps = current_reps + 1
    else:
        new_interval = max(2, current_interval * 2)
        new_reps = current_reps + 1

    next_review_date = (date.today() + timedelta(days=new_interval)).isoformat()

    cursor.execute("""
        UPDATE revisions 
        SET interval_days = ?, repetition_count = ?, next_review = ?, status = 'pending'
        WHERE id = ? AND user_id = ?
    """, (new_interval, new_reps, next_review_date, card_id, session["user_id"]))
    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "next_review": next_review_date,
        "interval_days": new_interval,
        "reps": new_reps
    })

@app.route("/save_to_revisions", methods=["POST"])
@login_required
def save_to_revisions():
    data = request.get_json()
    if not data or "cards" not in data:
        return jsonify({"success": False, "error": "No cards data"}), 400

    cards = data.get("cards", [])
    topic = data.get("topic", "Study Notes Flashcards")
    today_str = date.today().isoformat()

    conn = get_db()
    cursor = conn.cursor()
    count = 0

    for c in cards:
        q = c.get("question", "").strip()
        a = c.get("answer", "").strip()
        if q and a:
            cursor.execute("""
                INSERT INTO revisions (user_id, topic, question, answer, interval_days, repetition_count, next_review, status)
                VALUES (?, ?, ?, ?, 1, 0, ?, 'pending')
            """, (session["user_id"], topic, q, a, today_str))
            count += 1

    conn.commit()
    conn.close()
    return jsonify({"success": True, "count": count})

@app.route("/delete_revision_card/<int:card_id>", methods=["POST"])
@login_required
def delete_revision_card(card_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM revisions WHERE id = ? AND user_id = ?", (card_id, session["user_id"]))
    conn.commit()
    conn.close()
    flash("Card removed from revision queue.", "info")
    return redirect(url_for("revisions"))

if __name__ == "__main__":
    app.run(debug=True)
