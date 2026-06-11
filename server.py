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

@app.route("/en")
def index_en():
    return send_from_directory(".", "en.html")


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
            "reply_to": "deryaoz777@gmail.com",
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
            "reply_to": "deryaoz777@gmail.com",
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

    # Email doğrulamasında minimum 3 kredi garantile
    if user["credits"] < 3:
        conn3 = sqlite3.connect(DB_PATH)
        conn3.execute("UPDATE users SET credits = 3 WHERE id = ?", (user["id"],))
        conn3.commit()
        conn3.close()
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



@app.route("/faq")
def faq():
    return f"""<!DOCTYPE html><html lang="tr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SSS — Zuhal Teyze</title>
<meta name="description" content="Zuhal Teyze nedir, nasıl çalışır, ne kadar güvenilir? Dürüst cevaplar.">
{LEGAL_STYLE}
<style>
.lab-box{{background:#ede8d8;border-left:3px solid #2e1f6e;padding:1.1rem 1.4rem;border-radius:0 6px 6px 0;margin:1.5rem 0}}
.lab-box p{{margin:0;color:#2e1f2e;font-size:14px;line-height:1.75}}
.lab-box strong{{color:#2e1f6e}}
</style>
</head><body>
<a href="/" class="back">← Zuhal Teyze'ye dön</a>
<h1>Sıkça Sorulan Sorular</h1>

<div class="lab-box">
  <p><strong>Önce şunu söyleyelim:</strong> Bu platform bir deney laboratuvarıdır. Klasik horary astroloji yöntemleri üzerine kurulmuş, gerçek kullanıcı geri bildirimleriyle sürekli geliştirilen, açık uçlu bir uğraştır. Asla gerçek bir astroloğun yerini tutmaz — bunu iddia etmek de doğru olmaz.</p>
</div>

<h2>Bu ne, tam olarak?</h2>
<p>Zuhal Teyze; pyswisseph ile gerçek zamanlı gökyüzü hesabı yapan, Regiomontanus ev sistemi ve Frawley/Lilly geleneğine dayanan kurallarla haritayı analiz eden, ardından bu teknik veriyi yapay zekaya yorumlatan bir uygulamadır. Hesaplama kısmı gerçek horary metodolojisine dayanır. Yorum kısmı ise — dürüst olmak gerekirse — hâlâ geliştirilmektedir. Bazen çok iyi okur. Bazen hata yapar. Bu yüzden lab.</p>

<h2>Horary astroloji nedir?</h2>
<p>Bir sorunun sorulduğu tam ana ait gökyüzü haritasını yorumlayan kadim bir disiplindir. Doğum haritasına ihtiyaç duymaz — sorunun kendisi, sorulduğu an ve yer yeterlidir. William Lilly ve diğer klasik astrologlar tarafından sistematize edilmiş bu yöntem; ev lordları, aspect'ler, reception, combust ve void of course gibi teknik kurallara dayanır. Doğru uygulandığında şaşırtıcı kesinlikte sonuçlar verebilir.</p>

<h2>Ne kadar güvenilir?</h2>
<p>Dürüst cevap: değişken. Sistem teknik veriyi doğru hesaplar — gezegen dereceleri, ev cusps'ları, dignity tablosu gerçektir. Ancak bu veriyi yorumlamak başka bir iştir. Gerçek bir horary astroloğu on yıllık pratikle ve sezgisel bir okumayla yorumlar; bu uygulama kurallara dayalı bir yapay zeka yorumudur. Bazı okumalar neredeyse mükemmel çıkar. Bazılarında teknik bir hata veya gözden kaçan bir nüans olabilir. Bu yüzden her yorumu kör bir güvenle değil, merakla okuyun.</p>

<h2>Önemli bir kararım var — bunu kullanabilir miyim?</h2>
<p>Fikir edinmek, haritayı görmek, teknik durumu anlamak için evet. Ama kariyer değişikliği, ilişki kararı, sağlık meselesi gibi hayat değiştirici konularda lütfen gerçek bir klasik astrologla çalışın. Her önüne gelen horary okuyamaz — Frawley geleneğinde uzmanlaşmış, gerçek pratikle pişmiş biri gerekir. Türkçe için <a href="https://t.me/zuhalteyze" target="_blank" rel="noopener">Telegram</a>'dan ulaşabilirsiniz. İngilizce detaylı okuma için <a href="https://www.fiverr.com/s/LdwmRpA" target="_blank" rel="noopener">Fiverr profilim</a>e bakabilirsiniz.</p>

<h2>Soru nasıl sorulmalı?</h2>
<p>Spesifik, samimi, o an gerçekten merak edilen bir şey olmalı. "Hayatım nasıl gidecek?" değil — "Bu işi kabul etsem mi?" veya "O kişi geri döner mi?" gibi tek konuya odaklı sorular. Soruyu sormadan önce gerçekten o şeyi merak ediyor olmanız gerekir; test amaçlı veya eğlence için sorulan sorular genellikle net yanıt vermez.</p>

<h2>Aynı soruyu tekrar sorabilir miyim?</h2>
<p>Klasik gelenekte önerilmez. Cevabı beğenmediğiniz için değil, gerçekten bir şeyler değiştiğinde yeniden sorulabilir. Aynı soruyu arka arkaya sormak tutarsız veya yanıltıcı haritalar üretir.</p>

<h2>Verilerimi nasıl kullanıyorsunuz?</h2>
<p>E-posta adresiniz yalnızca giriş için kullanılır. Detay için <a href="/privacy">Gizlilik Politikası</a>na bakabilirsiniz.</p>

</body></html>"""


@app.route("/contact")
def contact():
    return f"""<!DOCTYPE html><html lang="tr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>İletişim — Zuhal Teyze</title>
<meta name="description" content="Zuhal Teyze ile iletişime geçin. Destek, profesyonel horary okuma ve iş birlikleri için.">
{LEGAL_STYLE}
<style>
.contact-card{{background:#efe9d8;border-radius:8px;padding:1.5rem;margin:1rem 0}}
.contact-card h3{{color:#2e1f6e;font-size:1rem;margin-bottom:.5rem}}
.contact-card p{{font-size:14px;margin:0}}
.contact-card a{{color:#2e1f6e;font-weight:500}}
.tag{{display:inline-block;font-size:11px;background:#2e1f6e;color:#f5e8b8;padding:2px 8px;border-radius:3px;margin-bottom:.5rem;letter-spacing:.05em}}
</style></head><body>
<a href="/" class="back">← Zuhal Teyze'ye dön</a>
<h1>İletişim</h1>

<div class="contact-card">
  <span class="tag">DESTEK</span>
  <h3>Teknik sorun veya ödeme ile ilgili</h3>
  <p>Email: <a href="mailto:noreply@zuhalteyze.live">noreply@zuhalteyze.live</a></p>
</div>

<div class="contact-card">
  <span class="tag">TELEGRAM</span>
  <h3>Hızlı ulaşım</h3>
  <p><a href="https://t.me/zuhalteyze" target="_blank" rel="noopener">@zuhalteyze</a> — sorular, destek, duyurular</p>
</div>

<div class="contact-card">
  <span class="tag">PROFESYONEL YORUM</span>
  <h3>Detaylı horary okuma (İngilizce)</h3>
  <p>Kişiselleştirilmiş, derinlemesine horary yorumu için:<br>
  <a href="https://www.fiverr.com/s/LdwmRpA" target="_blank" rel="noopener">Fiverr — Horary Derya</a></p>
</div>

<div class="contact-card">
  <span class="tag">İŞ BİRLİĞİ</span>
  <h3>Ortaklık ve iş birliği teklifleri</h3>
  <p>Email: <a href="mailto:noreply@zuhalteyze.live">noreply@zuhalteyze.live</a></p>
</div>

<p style="margin-top:2rem;font-size:13px;color:#9e8c6a;">Yanıt süresi genellikle 24-48 saattir.</p>
</body></html>"""


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
<p>Son güncelleme: Haziran 2026</p>
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
<p>Son güncelleme: Haziran 2026</p>
<h2>Topladığımız Veriler</h2>
<p>Platform yalnızca şu verileri toplar: e-posta adresi (giriş için) ve soru zamanı. Bu uygulama şu an deneme aşamasındadır ve tamamen ücretsizdir; herhangi bir ödeme sistemi aktif değildir.</p>
<h2>Verilerin Kullanımı</h2>
<p>E-posta adresiniz yalnızca kimlik doğrulama ve hizmet bildirimleri için kullanılır. Verileriniz üçüncü taraflarla paylaşılmaz veya satılmaz.</p>
<h2>Çerezler ve Oturumlar</h2>
<p>Platform, oturum yönetimi için çerez kullanır. Tarayıcı ayarlarınızdan çerezleri devre dışı bırakabilirsiniz; ancak bu durumda giriş yapamazsınız.</p>
<h2>Veri Saklama</h2>
<p>Hesabınızı silmek veya verilerinizin kaldırılmasını talep etmek için bizimle iletişime geçebilirsiniz.</p>
<h2>KVKK / GDPR</h2>
<p>Türkiye'de yerleşik kullanıcılar KVKK kapsamındaki haklarını, AB'de yerleşik kullanıcılar GDPR kapsamındaki haklarını kullanabilir. Talepler için: <a href="mailto:noreply@zuhalteyze.live">noreply@zuhalteyze.live</a></p>
</body></html>"""


@app.route("/refund")
def refund():
    from flask import redirect
    return redirect("/faq")


# ─────────────────────────────────────────
# LAB
# ─────────────────────────────────────────

@app.route("/tablo")
def tablo():
    return """<!DOCTYPE html><html lang="tr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Onurlar Tablosu — Zuhal Teyze</title>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#1a0e18;color:#e6d3ae;font-family:Georgia,serif;padding:1.5rem 1rem;min-height:100vh}
h1{font-family:'Cinzel',serif;font-size:1.2rem;letter-spacing:.15em;color:#c9a876;text-align:center;margin-bottom:.3rem}
.sub{text-align:center;font-size:11px;color:rgba(201,168,118,.45);letter-spacing:.1em;margin-bottom:1.5rem}
.back{display:inline-block;margin-bottom:1.25rem;font-size:11px;color:rgba(201,168,118,.4);text-decoration:none;letter-spacing:.05em}
.back:hover{color:rgba(201,168,118,.75)}
.tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:12px;min-width:640px}
thead th{background:rgba(201,168,118,.1);color:#c9a876;font-family:'Cinzel',serif;font-size:9px;letter-spacing:.12em;padding:8px 6px;border-bottom:1px solid rgba(201,168,118,.25);text-align:center;white-space:nowrap}
tbody tr{border-bottom:1px solid rgba(201,168,118,.08)}
tbody tr:hover{background:rgba(201,168,118,.05)}
td{padding:7px 5px;text-align:center;color:rgba(230,211,174,.82);vertical-align:middle}
td.sign{font-size:16px;text-align:left;padding-left:8px}
td.pl{font-size:14px}
td.terms{font-size:11px;color:rgba(201,168,118,.65);white-space:nowrap;letter-spacing:.01em}
td.deg{font-size:10px;color:rgba(201,168,118,.5)}
.sect{font-size:9px;color:rgba(201,168,118,.4);display:block;margin-top:1px}
.none{color:rgba(201,168,118,.25);font-size:11px}
.note{margin-top:1.5rem;font-size:11px;color:rgba(201,168,118,.35);text-align:center;line-height:1.7;letter-spacing:.03em}
</style>
</head><body>
<a href="/" class="back">← Zuhal Teyze'ye dön</a>
<h1>ONURLAR TABLOSU</h1>
<div class="sub">BATLAMYUS · PTOLEMY · FRAWLEY/LİLLY GELENEĞİ</div>
<div class="tbl-wrap">
<table>
<thead>
<tr>
  <th>BURÇ</th>
  <th>TAHT</th>
  <th>YÜCELME</th>
  <th>ÜÇLÜLÜK<br>G / G</th>
  <th colspan="5">HADLER (PTOLEMAEUS)</th>
  <th colspan="3">YÜZLER</th>
  <th>ZARAR</th>
  <th>DÜŞÜŞ</th>
</tr>
</thead>
<tbody>
<tr>
  <td class="sign">♈ Koç</td>
  <td class="pl">♂</td>
  <td>☉ <span class="deg">19°</span></td>
  <td class="terms">☉ / ♃</td>
  <td class="terms">♃ 6</td><td class="terms">♀ 14</td><td class="terms">☿ 21</td><td class="terms">♂ 26</td><td class="terms">♄ 30</td>
  <td class="terms">♂ 10</td><td class="terms">☉ 20</td><td class="terms">♀ 30</td>
  <td class="pl">♀</td><td class="pl">♄</td>
</tr>
<tr>
  <td class="sign">♉ Boğa</td>
  <td class="pl">♀</td>
  <td>☽ <span class="deg">3°</span></td>
  <td class="terms">♀ / ☽</td>
  <td class="terms">♀ 8</td><td class="terms">☿ 15</td><td class="terms">♃ 22</td><td class="terms">♄ 26</td><td class="terms">♂ 30</td>
  <td class="terms">☿ 10</td><td class="terms">☽ 20</td><td class="terms">♄ 30</td>
  <td class="pl">♂</td><td class="none">—</td>
</tr>
<tr>
  <td class="sign">♊ İkizler</td>
  <td class="pl">☿</td>
  <td>☊ <span class="deg">3°</span></td>
  <td class="terms">♄ / ☿</td>
  <td class="terms">☿ 7</td><td class="terms">♃ 14</td><td class="terms">♀ 21</td><td class="terms">♄ 25</td><td class="terms">♂ 30</td>
  <td class="terms">♃ 10</td><td class="terms">♂ 20</td><td class="terms">☉ 30</td>
  <td class="pl">♃</td><td class="none">—</td>
</tr>
<tr>
  <td class="sign">♋ Yengeç</td>
  <td class="pl">☽</td>
  <td>♃ <span class="deg">15°</span></td>
  <td class="terms">♂ / ♂</td>
  <td class="terms">♂ 6</td><td class="terms">♃ 13</td><td class="terms">☿ 20</td><td class="terms">♀ 27</td><td class="terms">♄ 30</td>
  <td class="terms">♀ 10</td><td class="terms">☿ 20</td><td class="terms">☽ 30</td>
  <td class="pl">♄</td><td class="pl">♂</td>
</tr>
<tr>
  <td class="sign">♌ Aslan</td>
  <td class="pl">☉</td>
  <td class="none">—</td>
  <td class="terms">☉ / ♃</td>
  <td class="terms">♄ 6</td><td class="terms">☿ 13</td><td class="terms">♀ 19</td><td class="terms">♃ 25</td><td class="terms">♂ 30</td>
  <td class="terms">♄ 10</td><td class="terms">♃ 20</td><td class="terms">♂ 30</td>
  <td class="pl">♄</td><td class="none">—</td>
</tr>
<tr>
  <td class="sign">♍ Başak</td>
  <td class="pl">☿</td>
  <td>☿ <span class="deg">15°</span></td>
  <td class="terms">♀ / ☽</td>
  <td class="terms">☿ 7</td><td class="terms">♀ 13</td><td class="terms">♃ 18</td><td class="terms">♄ 24</td><td class="terms">♂ 30</td>
  <td class="terms">☉ 10</td><td class="terms">♀ 20</td><td class="terms">☿ 30</td>
  <td class="pl">♃</td><td class="pl">♀</td>
</tr>
<tr>
  <td class="sign">♎ Terazi</td>
  <td class="pl">♀</td>
  <td>♄ <span class="deg">21°</span></td>
  <td class="terms">♄ / ☿</td>
  <td class="terms">♄ 6</td><td class="terms">♀ 11</td><td class="terms">♃ 19</td><td class="terms">☿ 24</td><td class="terms">♂ 30</td>
  <td class="terms">☽ 10</td><td class="terms">♄ 20</td><td class="terms">♃ 30</td>
  <td class="pl">♂</td><td class="pl">☉</td>
</tr>
<tr>
  <td class="sign">♏ Akrep</td>
  <td class="pl">♂</td>
  <td class="none">—</td>
  <td class="terms">♂ / ♂</td>
  <td class="terms">♂ 6</td><td class="terms">♃ 14</td><td class="terms">♀ 21</td><td class="terms">☿ 27</td><td class="terms">♄ 30</td>
  <td class="terms">♂ 10</td><td class="terms">☉ 20</td><td class="terms">♀ 30</td>
  <td class="pl">♀</td><td class="pl">☽</td>
</tr>
<tr>
  <td class="sign">♐ Yay</td>
  <td class="pl">♃</td>
  <td>☊ <span class="deg">3°</span></td>
  <td class="terms">☉ / ♃</td>
  <td class="terms">♃ 8</td><td class="terms">♀ 14</td><td class="terms">☿ 19</td><td class="terms">♄ 25</td><td class="terms">♂ 30</td>
  <td class="terms">☿ 10</td><td class="terms">☽ 20</td><td class="terms">♄ 30</td>
  <td class="pl">☿</td><td class="none">—</td>
</tr>
<tr>
  <td class="sign">♑ Oğlak</td>
  <td class="pl">♄</td>
  <td>♂ <span class="deg">28°</span></td>
  <td class="terms">♀ / ☽</td>
  <td class="terms">♀ 6</td><td class="terms">☿ 12</td><td class="terms">♃ 19</td><td class="terms">♂ 25</td><td class="terms">♄ 30</td>
  <td class="terms">♃ 10</td><td class="terms">♂ 20</td><td class="terms">☉ 30</td>
  <td class="pl">☽</td><td class="pl">♃</td>
</tr>
<tr>
  <td class="sign">♒ Kova</td>
  <td class="pl">♄</td>
  <td class="none">—</td>
  <td class="terms">♄ / ☿</td>
  <td class="terms">♄ 6</td><td class="terms">☿ 12</td><td class="terms">♀ 20</td><td class="terms">♃ 25</td><td class="terms">♂ 30</td>
  <td class="terms">♀ 10</td><td class="terms">☿ 20</td><td class="terms">☽ 30</td>
  <td class="pl">☉</td><td class="none">—</td>
</tr>
<tr>
  <td class="sign">♓ Balık</td>
  <td class="pl">♃</td>
  <td>♀ <span class="deg">27°</span></td>
  <td class="terms">♂ / ♂</td>
  <td class="terms">♀ 8</td><td class="terms">♃ 14</td><td class="terms">☿ 20</td><td class="terms">♂ 26</td><td class="terms">♄ 30</td>
  <td class="terms">♄ 10</td><td class="terms">♃ 20</td><td class="terms">♂ 30</td>
  <td class="pl">☿</td><td class="pl">☿</td>
</tr>
</tbody>
</table>
</div>
<p class="note">Üçlülük: G = Gündüz, G = Gece &nbsp;·&nbsp; Hadler kümülatif derece &nbsp;·&nbsp; Yüzler Keldani sırası (her 10°)<br>
☊ = Baş Ejder (Kuzey Düğüm) &nbsp;·&nbsp; Kaynak: Ptolemy / Lilly / Frawley</p>
</body></html>"""


@app.route("/lab")
def lab():
    return send_from_directory(".", "lab.html")


@app.route("/api/lab/debug")
def lab_debug():
    """DB path ve tablo bilgisi — sadece lab auth ile."""
    if not _lab_authed():
        return jsonify({"error": "Yetkisiz"}), 401
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM lab_feedback").fetchone()[0]
    conn.close()
    return jsonify({"db_path": DB_PATH, "lab_feedback_count": count})


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
    question      = data.get("question", "").strip()
    chart_data    = data.get("chart_data", "").strip()
    system_prompt = data.get("system_prompt", "").strip()
    lat = float(data.get("lat", DEFAULT_LAT))
    lon = float(data.get("lon", DEFAULT_LON))

    if not question:
        return jsonify({"error": "Soru boş olamaz."}), 400

    try:
        dt = datetime.datetime.now()

        if not system_prompt:
            # Tam horary engine — haritayı hesapla, build_frawley_prompt kullan
            chart = calc_chart(question, dt, lat, lon)
            prompt = build_frawley_prompt(chart)
            output = ask_claude(prompt, ANTHROPIC_API_KEY)
        else:
            # Custom prompt modu — sistem promptunu kullan
            if chart_data:
                user_msg = f"Soru: {question}\n\nHarita verisi:\n{chart_data}"
            else:
                # Haritayı hesapla, veri bölümünü ekle
                chart = calc_chart(question, dt, lat, lon)
                auto_prompt = build_frawley_prompt(chart)
                # Prompt yapısı: [sistem talimatı] --- [harita verisi] --- [kapanış]
                # İkinci bölüm (index 1) harita verisidir
                parts = auto_prompt.split("---")
                data_section = parts[1].strip() if len(parts) >= 3 else auto_prompt
                user_msg = f"{data_section}"
            output = _call_claude_raw(system_prompt, user_msg)

        # Otomatik kaydet (rating/tags sonra eklenebilir)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO lab_feedback
                (created_at, question, chart_data, system_prompt, output, rating, tags, note)
            VALUES (?, ?, ?, ?, ?, 0, '[]', '')
        """, (dt.isoformat(), question, chart_data, system_prompt, output))
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()

        return jsonify({"success": True, "output": output, "id": new_id})
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


@app.route("/api/lab/feedback/<int:entry_id>", methods=["POST"])
def lab_feedback_update(entry_id):
    """Mevcut feedback kaydını güncelle (rating/tags/note)."""
    if not _lab_authed():
        return jsonify({"error": "Yetkisiz erişim."}), 401
    data = request.json or {}
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE lab_feedback SET rating=?, tags=?, note=? WHERE id=?
    """, (
        data.get("rating", 0),
        _json.dumps(data.get("tags", []), ensure_ascii=False),
        data.get("note", ""),
        entry_id
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
