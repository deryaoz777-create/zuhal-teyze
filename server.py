"""
server.py — Zuhal Teyze Backend (Magic Link + Credits)
"""

from flask import Flask, request, jsonify, send_from_directory
import datetime
import os
import json as _json
import sqlite3
import secrets
import urllib.request
import resend
from database import (
    init_db, get_or_create_user, create_magic_token,
    verify_magic_token, create_session, get_user_by_session,
    use_credit, log_question
)
from horary_engine import (
    calc_chart, build_frawley_prompt, ask_claude,
    PLANET_TR, SIGN_NAMES_TR, ESSENTIAL_DIGNITY_TABLE
)

app = Flask(__name__, static_folder=".")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
BASE_URL = os.environ.get("BASE_URL", "https://zuhal-teyze-production.up.railway.app")

resend.api_key = RESEND_API_KEY

DEFAULT_LAT = 42.17
DEFAULT_LON = 42.67

LAB_PASSWORD = os.environ.get("LAB_PASSWORD", "zuhal2024lab")
DB_PATH      = os.environ.get("DB_PATH", "zuhal_teyze.db")

print(f"[STARTUP] ANTHROPIC_API_KEY {'tanımlı' if ANTHROPIC_API_KEY else 'YOK'}")
print(f"[STARTUP] RESEND_API_KEY {'tanımlı' if RESEND_API_KEY else 'YOK'}")

init_db()



def _lab_session_valid(token):
    if not token:
        return False
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT token FROM lab_sessions WHERE token = ?", (token,)
    ).fetchone()
    conn.close()
    return row is not None


def _create_lab_session():
    token = secrets.token_hex(32)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO lab_sessions VALUES (?, ?)",
        (token, datetime.datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
    return token


def _lab_authed():
    return _lab_session_valid(request.cookies.get("zt_lab", ""))


def _call_claude_raw(system_prompt, user_message):
    """Anthropic API'yi doğrudan çağır (lab için)."""
    payload = _json.dumps({
        "model": "claude-opus-4-5-20251101",
        "max_tokens": 1024,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}]
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = _json.loads(resp.read())
    return "".join(b.get("text", "") for b in data.get("content", []))


# ─────────────────────────────────────────
# STATIC
# ─────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(".", "zuhal_teyze.html")


# ─────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────

@app.route("/api/auth/request", methods=["POST"])
def auth_request():
    """Email al, magic link gönder."""
    data = request.json or {}
    email = data.get("email", "").strip().lower()

    if not email or "@" not in email:
        return jsonify({"error": "Geçerli bir email adresi girin."}), 400

    token = create_magic_token(email)
    magic_link = f"{BASE_URL}/api/auth/verify?token={token}"

    try:
        resend.Emails.send({
            "from": "Zuhal Teyze <noreply@zuhalteyze.live>",
            "to": email,
            "subject": "Zuhal Teyze — Giriş Linkiniz",
            "html": f"""
            <div style="font-family: Georgia, serif; max-width: 480px; margin: 0 auto; padding: 2rem; background: #f5f0e8;">
                <h2 style="font-family: 'Cinzel', serif; color: #2e1f6e; text-align: center;">ZUHAL TEYZE</h2>
                <p style="color: #4a3a2a; font-size: 17px; line-height: 1.7; font-style: italic;">
                    Gözüm, linke tıkla da içeri gir. 1 saat geçerliliği var, geç kalma.
                </p>
                <div style="text-align: center; margin: 2rem 0;">
                    <a href="{magic_link}"
                       style="background: #2e1f6e; color: #f5e8b8; padding: 14px 32px;
                              text-decoration: none; font-family: sans-serif;
                              font-size: 14px; letter-spacing: 2px; border-radius: 4px;">
                        GİRİŞ YAP
                    </a>
                </div>
                <p style="color: #9e8c6a; font-size: 12px; text-align: center;">
                    Bu emaili siz istemediyseniz görmezden gelin.
                </p>
            </div>
            """
        })
        return jsonify({"success": True, "message": "Link emailinize gönderildi."})
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return jsonify({"error": "Email gönderilemedi. Lütfen tekrar deneyin."}), 500


@app.route("/api/auth/verify")
def auth_verify():
    """Magic link doğrula, session oluştur, ana sayfaya yönlendir."""
    token = request.args.get("token", "")
    email = verify_magic_token(token)

    if not email:
        return """
        <html><body style="font-family:serif;text-align:center;padding:3rem;background:#f5f0e8;">
        <h2 style="color:#2e1f6e">Link geçersiz veya süresi dolmuş.</h2>
        <p><a href="/" style="color:#9e8c6a">Ana sayfaya dön</a></p>
        </body></html>
        """, 400

    user = get_or_create_user(email)
    session_token = create_session(user["id"])

    response = app.make_response(f"""
    <html>
    <head>
    <script>
        document.cookie = "zt_session={session_token}; path=/; max-age=2592000; SameSite=Lax";
        window.location.href = "/";
    </script>
    </head>
    <body style="font-family:serif;text-align:center;padding:3rem;background:#f5f0e8;">
    <p style="color:#2e1f6e">Giriş yapılıyor...</p>
    </body>
    </html>
    """)
    return response


@app.route("/api/auth/me")
def auth_me():
    """Oturum bilgisi döndür."""
    session_token = request.cookies.get("zt_session", "")
    if not session_token:
        return jsonify({"logged_in": False})
    user = get_user_by_session(session_token)
    if not user:
        return jsonify({"logged_in": False})
    return jsonify({
        "logged_in": True,
        "email": user["email"],
        "credits": user["credits"]
    })


# ─────────────────────────────────────────
# ZUHAL TEYZE
# ─────────────────────────────────────────

@app.route("/api/zuhal", methods=["POST"])
def api_zuhal():
    """Soru al, chart hesapla, yorum döndür."""
    # Oturum kontrolü
    session_token = request.cookies.get("zt_session", "")
    if not session_token:
        return jsonify({"error": "Lütfen önce giriş yapın.", "auth_required": True}), 401

    user = get_user_by_session(session_token)
    if not user:
        return jsonify({"error": "Oturum süresi dolmuş. Lütfen tekrar giriş yapın.", "auth_required": True}), 401

    # Credit kontrolü
    if user["credits"] <= 0:
        return jsonify({"error": "Krediniz kalmadı. Yeni paket alın.", "no_credits": True}), 402

    data = request.json or {}
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"error": "Soru boş olamaz."}), 400

    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "Sunucu yapılandırma hatası."}), 500

    try:
        dt = datetime.datetime.now()
        chart = calc_chart(question, dt, DEFAULT_LAT, DEFAULT_LON)
        prompt = build_frawley_prompt(chart)
        interpretation = ask_claude(prompt, ANTHROPIC_API_KEY)

        # Credit kullan ve logla
        use_credit(user["id"])
        log_question(user["id"], question)

        # Güncel credit sayısını al
        updated_user = get_or_create_user(user["email"])

        return jsonify({
            "success": True,
            "interpretation": interpretation,
            "credits_remaining": updated_user["credits"]
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────
# LAB
# ─────────────────────────────────────────

@app.route("/lab")
def lab():
    return send_from_directory(".", "lab.html")


@app.route("/api/lab/auth", methods=["POST"])
def lab_auth():
    data = request.json or {}
    if data.get("password", "") != LAB_PASSWORD:
        return jsonify({"error": "Yanlış şifre."}), 401
    token = _create_lab_session()
    resp = jsonify({"success": True})
    resp.set_cookie("zt_lab", token, max_age=86400 * 30, path="/", samesite="Lax")
    return resp


@app.route("/api/lab/reading", methods=["POST"])
def lab_reading():
    if not _lab_authed():
        return jsonify({"error": "Yetkisiz erişim."}), 401
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY tanımlı değil."}), 500

    data = request.json or {}
    question     = data.get("question", "").strip()
    chart_data   = data.get("chart_data", "").strip()
    system_prompt = data.get("system_prompt", "").strip()

    if not question:
        return jsonify({"error": "Soru boş olamaz."}), 400

    user_msg = f"Soru: {question}"
    if chart_data:
        user_msg += f"\n\nHarita verisi:\n{chart_data}"

    try:
        output = _call_claude_raw(system_prompt, user_msg)
        return jsonify({"success": True, "output": output})
    except Exception as e:
        print(f"[LAB READING ERROR] {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/lab/feedback", methods=["POST"])
def lab_feedback():
    if not _lab_authed():
        return jsonify({"error": "Yetkisiz erişim."}), 401
    data = request.json or {}
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO lab_feedback
            (created_at, question, chart_data, system_prompt, output, rating, tags, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.datetime.now().isoformat(),
        data.get("question", ""),
        data.get("chart_data", ""),
        data.get("system_prompt", ""),
        data.get("output", ""),
        data.get("rating", 0),
        _json.dumps(data.get("tags", []), ensure_ascii=False),
        data.get("note", "")
    ))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/lab/history", methods=["GET"])
def lab_history():
    if not _lab_authed():
        return jsonify({"error": "Yetkisiz erişim."}), 401
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM lab_feedback ORDER BY id DESC LIMIT 200"
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        result.append({
            "id":      r["id"],
            "date":    r["created_at"][:10],
            "question": r["question"],
            "output":   r["output"],
            "rating":   r["rating"],
            "tags":     _json.loads(r["tags"] or "[]"),
            "note":     r["note"]
        })
    return jsonify(result)



# ─────────────────────────────────────────
# REVIEW (Kullanıcıdan astrologa gönder)
# ─────────────────────────────────────────

@app.route("/api/review/submit", methods=["POST"])
def review_submit():
    """Kullanıcı yorumu astrologa gönderir."""
    session_token = request.cookies.get("zt_session", "")
    user = get_user_by_session(session_token) if session_token else None
    user_id = user["id"] if user else None

    data = request.json or {}
    question = data.get("question", "").strip()
    output   = data.get("output", "").strip()
    chart    = data.get("chart_data", "")

    if not question or not output:
        return jsonify({"error": "Soru ve yorum zorunlu."}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO review_requests
            (submitted_at, user_id, question, output, chart_data, status)
        VALUES (?, ?, ?, ?, ?, 'pending')
    """, (datetime.datetime.now().isoformat(), user_id, question, output, chart))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/lab/reviews", methods=["GET"])
def lab_reviews():
    """Lab: bekleyen ve geçmiş review'ları döndür."""
    if not _lab_authed():
        return jsonify({"error": "Yetkisiz erişim."}), 401
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM review_requests ORDER BY id DESC LIMIT 200"
    ).fetchall()
    conn.close()
    return jsonify([{
        "id":              r["id"],
        "date":            r["submitted_at"][:10] if r["submitted_at"] else "",
        "question":        r["question"],
        "output":          r["output"],
        "status":          r["status"],
        "astrologer_note": r["astrologer_note"]
    } for r in rows])


@app.route("/api/lab/reviews/<int:review_id>", methods=["POST"])
def lab_review_update(review_id):
    """Lab: review'u güncelle (not ekle, status değiştir)."""
    if not _lab_authed():
        return jsonify({"error": "Yetkisiz erişim."}), 401
    data = request.json or {}
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE review_requests
        SET status = ?, astrologer_note = ?
        WHERE id = ?
    """, (data.get("status", "reviewed"), data.get("note", ""), review_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


if __name__ == "__main__":
    print("=" * 50)
    print("🔮 Zuhal Teyze Server başlıyor...")
    print("📍 http://localhost:5000")
    print("=" * 50)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
