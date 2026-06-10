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
import random
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
# FREE QUESTION (ilk soru, auth yok)
# ─────────────────────────────────────────

@app.route("/api/zuhal/free", methods=["POST"])
def api_zuhal_free():
    """İlk ücretsiz soru — auth gerekmez."""
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "Sunucu yapılandırma hatası."}), 500
    data = request.json or {}
    question = data.get("question", "").strip()
    lat = float(data.get("lat", DEFAULT_LAT))
    lon = float(data.get("lon", DEFAULT_LON))
    if not question:
        return jsonify({"error": "Soru boş olamaz."}), 400
    try:
        dt = datetime.datetime.now()
        chart = calc_chart(question, dt, lat, lon)
        prompt = build_frawley_prompt(chart)
        interpretation = ask_claude(prompt, ANTHROPIC_API_KEY)
        return jsonify({"success": True, "interpretation": interpretation})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────
# CODE AUTH (6 haneli kod ile giriş)
# ─────────────────────────────────────────

@app.route("/api/auth/code/send", methods=["POST"])
def auth_code_send():
    """Email'e 6 haneli doğrulama kodu gönder."""
    data = request.json or {}
    email = data.get("email", "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "Geçerli bir email adresi girin."}), 400

    code = str(random.randint(100000, 999999))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "DELETE FROM magic_tokens WHERE email = ?", (email,)
    )
    conn.execute(
        "INSERT INTO magic_tokens (email, token) VALUES (?, ?)",
        (email, f"CODE:{code}")
    )
    conn.commit()
    conn.close()

    try:
        resend.Emails.send({
            "from": "Zuhal Teyze <noreply@zuhalteyze.live>",
            "to": email,
            "subject": "Zuhal Teyze — Doğrulama Kodun",
            "html": f"""
            <div style="font-family:Georgia,serif;max-width:480px;margin:0 auto;padding:2rem;background:#f5f0e8;">
                <h2 style="font-family:'Cinzel',serif;color:#2e1f6e;text-align:center;letter-spacing:.1em;">ZUHAL TEYZE</h2>
                <p style="color:#4a3a2a;font-size:17px;line-height:1.7;font-style:italic;text-align:center;">
                    Doğrulama kodun:
                </p>
                <div style="text-align:center;margin:2rem 0;">
                    <span style="font-size:40px;font-family:'Cinzel',serif;color:#2e1f6e;letter-spacing:.35em;font-weight:bold;">{code}</span>
                </div>
                <p style="color:#9e8c6a;font-size:12px;text-align:center;">Bu kod 1 saat geçerlidir.</p>
            </div>
            """
        })
        return jsonify({"success": True})
    except Exception as e:
        print(f"[CODE EMAIL ERROR] {e}")
        return jsonify({"error": "Email gönderilemedi. Lütfen tekrar deneyin."}), 500


@app.route("/api/auth/code/verify", methods=["POST"])
def auth_code_verify():
    """6 haneli kodu doğrula, oturum oluştur."""
    data = request.json or {}
    email = data.get("email", "").strip().lower()
    code  = data.get("code", "").strip()
    if not email or not code:
        return jsonify({"error": "Email ve kod gerekli."}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT email FROM magic_tokens
        WHERE email = ? AND token = ? AND used = 0
          AND created_at > datetime('now', '-1 hour')
    """, (email, f"CODE:{code}")).fetchone()

    if not row:
        conn.close()
        return jsonify({"error": "Kod geçersiz veya süresi dolmuş."}), 401

    conn.execute(
        "UPDATE magic_tokens SET used = 1 WHERE email = ? AND token = ?",
        (email, f"CODE:{code}")
    )
    conn.commit()
    conn.close()

    user = get_or_create_user(email)
    session_token = create_session(user["id"])
    resp = jsonify({"success": True, "email": user["email"], "credits": user["credits"]})
    resp.set_cookie("zt_session", session_token, max_age=2592000, path="/", samesite="Lax")
    return resp




# ─────────────────────────────────────────
# PADDLE ENTEGRASYONU
# ─────────────────────────────────────────

PADDLE_WEBHOOK_SECRET = os.environ.get("PADDLE_WEBHOOK_SECRET", "")
PADDLE_PRICE_ID       = os.environ.get("PADDLE_PRICE_ID", "pri_01ktryb7je2xte6q7p8v3r2wpp")
PADDLE_CLIENT_TOKEN   = os.environ.get("PADDLE_CLIENT_TOKEN", "")
PADDLE_ENV            = os.environ.get("PADDLE_ENV", "production")  # sandbox | production
CREDITS_PER_PURCHASE  = int(os.environ.get("CREDITS_PER_PURCHASE", "10"))


@app.route("/api/paddle/webhook", methods=["POST"])
def paddle_webhook():
    """Paddle ödeme bildirimi — kredi ekle."""
    import hmac, hashlib

    raw_body = request.get_data()
    sig_header = request.headers.get("Paddle-Signature", "")

    # İmza doğrulama
    if PADDLE_WEBHOOK_SECRET:
        try:
            parts = dict(p.split("=", 1) for p in sig_header.split(";"))
            ts = parts.get("ts", "")
            h1 = parts.get("h1", "")
            signed = f"{ts}:{raw_body.decode()}"
            expected = hmac.new(
                PADDLE_WEBHOOK_SECRET.encode(),
                signed.encode(),
                hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, h1):
                return jsonify({"error": "Invalid signature"}), 401
        except Exception as e:
            print(f"[PADDLE WEBHOOK] Signature error: {e}")
            return jsonify({"error": "Signature error"}), 401

    data = request.json or {}
    event_type = data.get("event_type", "")
    print(f"[PADDLE WEBHOOK] event: {event_type}")

    if event_type == "transaction.completed":
        txn = data.get("data", {})
        customer_email = None

        # Email'i address objesinden al
        address = txn.get("address", {})
        if not customer_email:
            customer_email = txn.get("customer", {}).get("email")
        if not customer_email:
            # custom_data varsa dene
            custom = txn.get("custom_data") or {}
            customer_email = custom.get("email")

        if customer_email:
            user = get_or_create_user(customer_email.lower().strip())
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "UPDATE users SET credits = credits + ? WHERE id = ?",
                (CREDITS_PER_PURCHASE, user["id"])
            )
            conn.commit()
            conn.close()
            print(f"[PADDLE] +{CREDITS_PER_PURCHASE} kredi → {customer_email}")
        else:
            print(f"[PADDLE WEBHOOK] Email bulunamadı: {_json.dumps(txn)[:200]}")

    return jsonify({"ok": True})


@app.route("/api/paddle/config")
def paddle_config():
    """Frontend'e Paddle config döndür."""
    return jsonify({
        "client_token": PADDLE_CLIENT_TOKEN,
        "price_id": PADDLE_PRICE_ID,
        "env": PADDLE_ENV
    })


# ─────────────────────────────────────────
# LEGAL PAGES
# ─────────────────────────────────────────

LEGAL_STYLE = """
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Georgia,serif;background:#f5f0e8;color:#2e1f2e;padding:3rem 1rem;max-width:720px;margin:0 auto;line-height:1.8}
h1{font-family:'Cinzel',serif;color:#2e1f6e;font-size:1.6rem;margin-bottom:2rem;letter-spacing:.05em}
h2{color:#2e1f6e;font-size:1.1rem;margin:1.5rem 0 .5rem}
p{margin-bottom:1rem;color:#4a3a2a}
a{color:#2e1f6e}
.back{display:inline-block;margin-bottom:2rem;font-size:.9rem;color:#9e8c6a;text-decoration:none}
</style>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400&display=swap" rel="stylesheet">
"""

@app.route("/terms")
def terms():
    return f"""<!DOCTYPE html><html lang="tr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kullanım Koşulları — Zuhal Teyze</title>{LEGAL_STYLE}</head><body>
<a href="/" class="back">← Zuhal Teyze'ye dön</a>
<h1>Kullanım Koşulları</h1>
<p>Son güncelleme: Haziran 2025</p>
<h2>1. Hizmet Hakkında</h2>
<p>Zuhal Teyze (zuhalteyze.live), geleneksel horary astroloji tekniklerine dayanan yapay zeka destekli yorum servisidir. Sunulan içerikler tamamen eğlence amaçlıdır; tıbbi, hukuki veya finansal tavsiye niteliği taşımaz.</p>
<h2>2. Sorumluluk Sınırlaması</h2>
<p>Bu platform üzerinden sağlanan yorumlar kehanet veya kesin gerçek olarak değerlendirilemez. Kullanıcı, aldığı yorumları kendi takdir ve sorumluluğunda değerlendirir. Platform, yorumların doğruluğu veya sonuçları konusunda herhangi bir garanti vermez.</p>
<h2>3. Kullanıcı Yükümlülükleri</h2>
<p>Kullanıcılar platformu yasalara uygun şekilde, başkalarına zarar vermeyecek biçimde kullanmayı kabul eder. Sistemi kötüye kullanmak, aşırı yük oluşturmak veya izinsiz erişim sağlamak yasaktır.</p>
<h2>4. Fikri Mülkiyet</h2>
<p>Platform içeriği, tasarımı ve yazılımı Zuhal Teyze'ye aittir. İzinsiz çoğaltılamaz veya dağıtılamaz.</p>
<h2>5. Değişiklikler</h2>
<p>Bu koşullar önceden bildirim yapılmaksızın güncellenebilir. Platformu kullanmaya devam etmek güncel koşulları kabul etmek anlamına gelir.</p>
<h2>İletişim</h2>
<p>Sorularınız için: <a href="mailto:noreply@zuhalteyze.live">noreply@zuhalteyze.live</a></p>
</body></html>"""


@app.route("/privacy")
def privacy():
    return f"""<!DOCTYPE html><html lang="tr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gizlilik Politikası — Zuhal Teyze</title>{LEGAL_STYLE}</head><body>
<a href="/" class="back">← Zuhal Teyze'ye dön</a>
<h1>Gizlilik Politikası</h1>
<p>Son güncelleme: Haziran 2025</p>
<h2>Topladığımız Veriler</h2>
<p>Platform yalnızca şu verileri toplar: e-posta adresi (giriş için), sorulan astroloji soruları ve soru zamanı. Ödeme işlemleri Paddle tarafından yürütülür; kart bilgileri Zuhal Teyze'ye iletilmez.</p>
<h2>Verilerin Kullanımı</h2>
<p>E-posta adresiniz yalnızca kimlik doğrulama ve hizmet bildirimleri için kullanılır. Sorularınız hizmet kalitesini geliştirmek amacıyla anonim olarak analiz edilebilir. Verileriniz üçüncü taraflarla paylaşılmaz veya satılmaz.</p>
<h2>Çerezler ve Oturumlar</h2>
<p>Platform, oturum yönetimi için çerez kullanır. Tarayıcı ayarlarınızdan çerezleri devre dışı bırakabilirsiniz; ancak bu durumda giriş yapamazsınız.</p>
<h2>Veri Saklama</h2>
<p>Hesabınızı silmek veya verilerinizin kaldırılmasını talep etmek için bizimle iletişime geçebilirsiniz.</p>
<h2>KVKK / GDPR</h2>
<p>Türkiye'de yerleşik kullanıcılar KVKK kapsamındaki haklarını, AB'de yerleşik kullanıcılar GDPR kapsamındaki haklarını kullanabilir. Talepler için: <a href="mailto:noreply@zuhalteyze.live">noreply@zuhalteyze.live</a></p>
</body></html>"""


@app.route("/refund")
def refund():
    return f"""<!DOCTYPE html><html lang="tr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>İade Politikası — Zuhal Teyze</title>{LEGAL_STYLE}</head><body>
<a href="/" class="back">← Zuhal Teyze'ye dön</a>
<h1>İade Politikası</h1>
<p>Son güncelleme: Haziran 2025</p>
<h2>Genel İlke</h2>
<p>Zuhal Teyze, dijital içerik satmaktadır. Satın alınan krediler kullanıldıktan sonra iade edilemez.</p>
<h2>İade Koşulları</h2>
<p>Satın alma tarihinden itibaren 14 gün içinde, hiç kullanılmamış kredi paketleri için tam iade talebinde bulunabilirsiniz. İade talebi, ödeme yapılan e-posta adresinden <a href="mailto:noreply@zuhalteyze.live">noreply@zuhalteyze.live</a> adresine gönderilmelidir.</p>
<h2>Teknik Sorunlar</h2>
<p>Platform kaynaklı teknik bir hata nedeniyle kredi harcandıysa, kanıtlayıcı bilgilerle başvurmanız halinde kredi iadesi yapılır.</p>
<h2>İşlem Süresi</h2>
<p>Onaylanan iadeler 5-10 iş günü içinde orijinal ödeme yöntemiyle gerçekleştirilir.</p>
<h2>İletişim</h2>
<p>İade talepleriniz için: <a href="mailto:noreply@zuhalteyze.live">noreply@zuhalteyze.live</a></p>
</body></html>"""


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
