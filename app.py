from flask import Flask, request, jsonify, render_template, redirect, url_for
import os, datetime, numpy as np, pickle
import mysql.connector
from speechbrain.pretrained import EncoderClassifier
from pydub import AudioSegment
from waitress import serve
import torchaudio

# Force torchaudio to use soundfile backend
torchaudio.set_audio_backend("soundfile")

app = Flask(__name__)
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ---- DATABASE CONFIG ----
DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "root",  # your MySQL password
    "database": "voice_attendance",
    "auth_plugin": "mysql_native_password"
}

# ---- Connect to Database ----
def db_connect():
    return mysql.connector.connect(**DB_CONFIG)

# ---- Load SpeechBrain Model ----
print("🔊 Loading SpeechBrain model (this takes a moment)...")
encoder = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb")
print("✅ Model loaded successfully.")

# ---- Load Existing Student Embeddings ----
student_embeddings = {}

def load_embeddings():
    student_embeddings.clear()
    print("📦 Loading student voice embeddings from database...")
    try:
        conn = db_connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, name, embedding FROM students")
        students = cursor.fetchall()
        conn.close()

        for s in students:
            student_embeddings[s["id"]] = {
                "name": s["name"],
                "embedding": pickle.loads(s["embedding"])
            }
        print(f"✅ Loaded {len(student_embeddings)} student(s).")
    except mysql.connector.Error as err:
        print(f"❌ Database Error during embedding load: {err}")

# ---- Cosine Similarity ----
def cosine_similarity(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

# ======================================================================
#                        HOME REDIRECT
# ======================================================================
@app.route("/")
def home():
    return redirect(url_for("enroll"))

# ======================================================================
#                        ENROLLMENT ROUTE
# ======================================================================
@app.route("/enroll", methods=["GET", "POST"])
def enroll():
    if request.method == "GET":
        return render_template("enroll.html")

    name = request.form.get('name')
    audio_files = request.files.getlist('audio_data')

    if not name or len(audio_files) < 3:
        return jsonify({"status": "error", "message": "Missing name or not enough audio files"}), 400

    embeddings = []
    for audio_file in audio_files:
        temp_path = os.path.join(UPLOAD_DIR, f"temp_{audio_file.filename}")
        audio_file.save(temp_path)

        # Standardize format
        sound = AudioSegment.from_file(temp_path)
        sound = sound.set_frame_rate(16000).set_channels(1)
        sound.export(temp_path, format="wav")

        emb = encoder.encode_batch(encoder.load_audio(temp_path))
        embeddings.append(emb.squeeze().detach().cpu().numpy())
        os.remove(temp_path)

    avg_embedding = np.mean(embeddings, axis=0)
    serialized_embedding = pickle.dumps(avg_embedding)

    try:
        conn = db_connect()
        cursor = conn.cursor()
        query = "INSERT INTO students (name, embedding) VALUES (%s, %s)"
        cursor.execute(query, (name, serialized_embedding))
        conn.commit()
        print(f"✅ Enrolled '{name}' successfully.")
    except mysql.connector.Error as err:
        print(f"❌ Database Error during enrollment: {err}")
        return jsonify({"status": "error", "message": "Database error"}), 500
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()

    load_embeddings()
    return redirect(url_for('enroll'))

# ======================================================================
#                        RECOGNITION PAGE
# ======================================================================
@app.route("/recognize", methods=["GET"])
def recognize_page():
    return render_template("recognize.html")

# ======================================================================
#                        AUDIO UPLOAD / RECOGNITION
# ======================================================================
@app.route("/upload", methods=["POST"])
def upload_audio():
    filename = os.path.join(UPLOAD_DIR, f"rec_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.wav")
    with open(filename, "wb") as f:
        f.write(request.data)

    # Convert WebM → WAV
    try:
        sound = AudioSegment.from_file(filename, format="webm")
        sound = sound.set_frame_rate(16000).set_channels(1)
        sound.export(filename, format="wav")
    except Exception as e:
        print(f"⚠️ Conversion failed: {e}")
        return jsonify({"status": "error", "message": "Audio conversion failed"}), 400

    # Process audio
    try:
        emb_new = encoder.encode_batch(encoder.load_audio(filename))
        emb_new = emb_new.squeeze().detach().cpu().numpy()
    except Exception as e:
        print(f"⚠️ Error processing uploaded audio: {e}")
        return jsonify({"status": "error", "message": "Could not process audio file"}), 400

    # Compare embeddings
    best_match_id = None
    best_match_name = None
    best_score = -1.0

    for sid, data in student_embeddings.items():
        score = cosine_similarity(emb_new, data["embedding"])
        if score > best_score:
            best_score = score
            best_match_id = sid
            best_match_name = data["name"]

    RECOGNITION_THRESHOLD = 0.45

    if best_match_id and best_score > RECOGNITION_THRESHOLD:
        log_attendance(best_match_id)
        print(f"✅ Recognized: {best_match_name} (score: {best_score:.2f})")
        return jsonify({"status": "success", "user": best_match_name, "score": float(best_score)})
    else:
        print(f"❌ Unrecognized voice (score: {best_score:.2f})")
        return jsonify({"status": "unrecognized", "user": None, "score": float(best_score)}), 401

# ======================================================================
#                        ATTENDANCE LOGGING
# ======================================================================
def log_attendance(student_id):
    try:
        conn = db_connect()
        cursor = conn.cursor()
        now = datetime.datetime.now()
        cursor.execute("INSERT INTO attendance (student_id, time) VALUES (%s, %s)", (student_id, now))
        conn.commit()
    except mysql.connector.Error as err:
        print(f"❌ Database Error during attendance logging: {err}")
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()

# ======================================================================
#                        ATTENDANCE DASHBOARD
# ======================================================================
@app.route("/dashboard")
def dashboard():
    try:
        conn = db_connect()
        cursor = conn.cursor(dictionary=True)
        query = """
            SELECT a.id, s.name, a.time
            FROM attendance a
            JOIN students s ON a.student_id = s.id
            ORDER BY a.time DESC
        """
        cursor.execute(query)
        records = cursor.fetchall()
        cursor.close()
        conn.close()
        return render_template("dashboard.html", records=records)
    except Exception as e:
        print(f"❌ Error loading dashboard: {e}")
        return "Error loading dashboard."

# ======================================================================
#                        APP START
# ======================================================================
if __name__ == "__main__":
    load_embeddings()
    print("🚀 Server running at: http://127.0.0.1:5000/enroll")
    serve(app, host="0.0.0.0", port=5000)
