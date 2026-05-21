"""
server.py — Zuhal Teyze Backend (Magic Link + Credits)
"""

from flask import Flask, request, jsonify, send_from_directory
import datetime
import os
import json as _json
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

print(f"[STARTUP] ANTHROPIC_API_KEY {'tanımlı' if ANTHROPIC_API_KEY else 'YOK'}")
print(f"[STARTUP] RESEND_API_KEY {'tanımlı' if RESEND_API_KEY else 'YOK'}")

init_db()


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


if __name__ == "__main__":
    print("=" * 50)
    print("🔮 Zuhal Teyze Server başlıyor...")
    print("📍 http://localhost:5000")
    print("=" * 50)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
