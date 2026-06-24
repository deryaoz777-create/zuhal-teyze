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
import urllib.parse
import random
import hmac
import hashlib
import resend
from database import (
    init_db, get_or_create_user, create_magic_token,
    verify_magic_token, create_session, get_user_by_session,
    use_credit, log_question,
    add_credits, is_payment_processed, mark_payment_processed
)
from horary_engine import (
    calc_chart, build_frawley_prompt, ask_claude, chart_to_dict,
    PLANET_TR, SIGN_NAMES_TR, ESSENTIAL_DIGNITY_TABLE
)

app = Flask(__name__, static_folder=".")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
BASE_URL = os.environ.get("BASE_URL", "https://zuhal-teyze-production.up.railway.app")

LS_WEBHOOK_SECRET = os.environ.get("LS_WEBHOOK_SECRET", "")
VARIANT_CREDITS = {
    int(os.environ.get("LS_VARIANT_3Q",  "0")): 3,
    int(os.environ.get("LS_VARIANT_5Q",  "0")): 5,
    int(os.environ.get("LS_VARIANT_10Q", "0")): 10,
}
LS_CHECKOUT_URLS = {
    "3q":  os.environ.get("LS_CHECKOUT_3Q",  ""),
    "5q":  os.environ.get("LS_CHECKOUT_5Q",  ""),
    "10q": os.environ.get("LS_CHECKOUT_10Q", ""),
}

resend.api_key = RESEND_API_KEY

DEFAULT_LAT = 42.17
DEFAULT_LON = 42.67

LAB_PASSWORD = os.environ.get("LAB_PASSWORD", "zuhal2024lab")
DB_PATH      = os.environ.get("DB_PATH", "zuhal_teyze.db")

print(f"[STARTUP] ANTHROPIC_API_KEY {'tanımlı' if ANTHROPIC_API_KEY else 'YOK'}")
print(f"[STARTUP] RESEND_API_KEY {'tanımlı' if RESEND_API_KEY else 'YOK'}")

init_db()

# Rate limiting ayarları — Railway'de env variable ile override edilebilir
FREE_DAILY_PER_IP  = int(os.environ.get("FREE_DAILY_PER_IP", "1"))   # IP başına günlük max ücretsiz soru
FREE_DAILY_GLOBAL  = int(os.environ.get("FREE_DAILY_GLOBAL", "80"))  # Tüm platformda günlük max ücretsiz soru

# Rate limit tablosunu oluştur (database.py'de yoksa burada halledelim)
def _ensure_rate_table():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS free_rate (
            ip       TEXT NOT NULL,
            day      TEXT NOT NULL,
            count    INTEGER DEFAULT 1,
            PRIMARY KEY (ip, day)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lab_sessions (
            token      TEXT PRIMARY KEY,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

_ensure_rate_table()

def _check_free_rate(ip: str) -> tuple[bool, str]:
    """True = izin ver. False = blokla, mesajla birlikte."""
    today = datetime.date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # IP kontrolü
    row = conn.execute(
        "SELECT count FROM free_rate WHERE ip=? AND day=?", (ip, today)
    ).fetchone()
    ip_count = row["count"] if row else 0
    if ip_count >= FREE_DAILY_PER_IP:
        conn.close()
        return False, "Bugünlük ücretsiz sorunuzu kullandınız. Devam etmek için giriş yapın."

    # Global günlük cap
    total = conn.execute(
        "SELECT SUM(count) as t FROM free_rate WHERE day=?", (today,)
    ).fetchone()["t"] or 0
    if total >= FREE_DAILY_GLOBAL:
        conn.close()
        return False, "Bugünlük kapasite doldu. Lütfen giriş yaparak devam edin."

    # Sayacı artır
    conn.execute("""
        INSERT INTO free_rate (ip, day, count) VALUES (?, ?, 1)
        ON CONFLICT(ip, day) DO UPDATE SET count = count + 1
    """, (ip, today))
    conn.commit()
    conn.close()
    return True, ""




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
        "model": "claude-sonnet-4-6",
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
    lang = data.get("lang", "tr")
    if lang not in ("tr", "en"):
        lang = "tr"
    if not question:
        return jsonify({"error": "Soru boş olamaz."}), 400

    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "Sunucu yapılandırma hatası."}), 500

    try:
        dt = datetime.datetime.now()
        chart = calc_chart(question, dt, DEFAULT_LAT, DEFAULT_LON)
        prompt = build_frawley_prompt(chart, lang=lang)
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

    # IP rate limiting
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    allowed, msg = _check_free_rate(ip)
    if not allowed:
        return jsonify({"error": msg, "auth_required": True}), 429

    data = request.json or {}
    question = data.get("question", "").strip()
    lat = float(data.get("lat", DEFAULT_LAT))
    lon = float(data.get("lon", DEFAULT_LON))
    lang = data.get("lang", "tr")
    if lang not in ("tr", "en"):
        lang = "tr"
    if not question:
        return jsonify({"error": "Soru boş olamaz."}), 400
    try:
        dt = datetime.datetime.now()
        chart = calc_chart(question, dt, lat, lon)
        prompt = build_frawley_prompt(chart, lang=lang)
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
# ENGLISH PAGES
# ─────────────────────────────────────────

EN_LEGAL_STYLE = """
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Georgia,serif;background:#f5f0e8;color:#2e1f2e;padding:3rem 1rem;max-width:720px;margin:0 auto;line-height:1.8}
h1{font-family:'Cinzel',serif;color:#2e1f6e;font-size:1.6rem;margin-bottom:2rem;letter-spacing:.05em}
h2{color:#2e1f6e;font-size:1.1rem;margin:1.5rem 0 .5rem}
p{margin-bottom:1rem;color:#4a3a2a}
a{color:#2e1f6e}
.back{display:inline-block;margin-bottom:2rem;font-size:.9rem;color:#9e8c6a;text-decoration:none}
.lab-box{background:#ede8d8;border-left:3px solid #2e1f6e;padding:1.1rem 1.4rem;border-radius:0 6px 6px 0;margin:1.5rem 0}
.lab-box p{margin:0;color:#2e1f2e;font-size:14px;line-height:1.75}
.lab-box strong{color:#2e1f6e}
.contact-card{background:#efe9d8;border-radius:8px;padding:1.5rem;margin:1rem 0}
.contact-card h3{color:#2e1f6e;font-size:1rem;margin-bottom:.5rem}
.contact-card p{font-size:14px;margin:0}
.contact-card a{color:#2e1f6e;font-weight:500}
.tag{display:inline-block;font-size:11px;background:#2e1f6e;color:#f5e8b8;padding:2px 8px;border-radius:3px;margin-bottom:.5rem;letter-spacing:.05em}
</style>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400&display=swap" rel="stylesheet">
"""

@app.route("/ogren")
def ogren():
    return f"""<!DOCTYPE html><html lang="tr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Horary Astroloji Nedir? Sade Bir Rehber — Zuhal Teyze</title>
<meta name="description" content="Horary astroloji nasıl çalışır, soru nasıl sorulur, harita ne söyler — doğum haritası gerekmez. Zuhal Teyze ile ücretsiz deneyin.">
<link rel="canonical" href="https://zuhalteyze.live/ogren">
<meta property="og:title" content="Horary Astroloji Nedir? — Zuhal Teyze">
<meta property="og:description" content="Horary astroloji nasıl çalışır, soru nasıl sorulur, harita ne söyler. Sade, dürüst bir rehber.">
<meta property="og:url" content="https://zuhalteyze.live/ogren">
<meta name="google-site-verification" content="hhgkCOajijszpcMpIxFkPTokjYTPS7CzDhsG1L1aqJA">
{LEGAL_STYLE}
<style>
body{{max-width:760px}}
.cta-box{{background:#2e1f2e;color:#e6d3ae;border-radius:8px;padding:1.5rem 1.8rem;margin:2.5rem 0;text-align:center}}
.cta-box p{{color:#c9a876;margin-bottom:1rem;font-size:15px;font-style:italic}}
.cta-btn{{display:inline-block;background:transparent;border:1px solid rgba(201,168,118,.6);color:#c9a876;font-family:'Cinzel',serif;font-size:11px;letter-spacing:.15em;padding:12px 28px;text-decoration:none;border-radius:4px}}
.toc{{background:#f0ead8;border-radius:6px;padding:1.2rem 1.6rem;margin:1.5rem 0;font-size:14px}}
.toc h3{{color:#2e1f6e;font-size:.85rem;letter-spacing:.1em;margin-bottom:.75rem}}
.toc ol{{padding-left:1.2rem;color:#4a3a2a;line-height:2}}
.toc a{{color:#2e1f6e;text-decoration:none}}
blockquote{{border-left:3px solid #2e1f6e;margin:1.5rem 0;padding:.75rem 1.2rem;background:#f0ead8;color:#4a3a2a;font-style:italic;font-size:15px}}
</style>
</head><body>
<a href="/" class="back">← Zuhal Teyze'ye dön</a>
<h1>Horary Astroloji Nedir?</h1>
<p style="color:#9e8c6a;font-size:13px;margin-bottom:1.5rem">Sade bir rehber — önceden astroloji bilgisi gerekmez</p>

<div class="toc">
  <h3>BU REHBERDE</h3>
  <ol>
    <li><a href="#ne">Horary astroloji nedir</a></li>
    <li><a href="#fark">Modern astrolojiden farkı</a></li>
    <li><a href="#harita">Haritada ne var</a></li>
    <li><a href="#kurallar">Temel kurallar</a></li>
    <li><a href="#soru">İyi soru nasıl sorulur</a></li>
    <li><a href="#sinir">Horary'nin yapamadıkları</a></li>
    <li><a href="#dene">Ücretsiz deneyin</a></li>
  </ol>
</div>

<h2 id="ne">Horary astroloji nedir</h2>
<p>Horary astroloji, belirli bir soruyu — sorunun samimiyetle sorulduğu tam ana ait gökyüzü haritasını okuyarak — yanıtlar. <em>Horary</em> kelimesi Latince <em>hora</em>'dan gelir: saat. Bu, ömürlerin değil, anların astrolojisidir.</p>
<p>Doğum haritanıza ihtiyacınız yok. Yükselen burcu bilmenize gerek yok. Harita sorunun kendisine aittir: sorma anı, sorma yeri ve arkasındaki samimilik. Bu yeterlidir.</p>
<p>Gelenek eskidir. Ortaçağ'da İslam dünyasının büyük astrologları bu yöntemi sistematize etti. İngilizce'de en kapsamlı eser 1647'de <strong>William Lilly</strong> tarafından yazılan <em>Christian Astrology</em>'dir — bugün hâlâ temel başvuru kaynağıdır. Yirminci yüzyılın sonunda <strong>John Frawley</strong> bu geleneği çağdaş pratisyenler için yeniden canlandırdı ve netleştirdi. Zuhal Teyze bu çizgiyi takip eder.</p>

<h2 id="fark">Modern astrolojiden farkı</h2>
<p>Modern astroloji — gazete köşelerinde ve çoğu uygulamada gördüğünüz tür — ağırlıklı olarak natal astrolojidir. Doğum haritası üzerinden karakter ve yaşam temalarını inceler. Psikolojik dil kullanır: arketipler, örüntüler, iç yolculuklar.</p>
<p>Horary neredeyse her açıdan farklıdır:</p>
<p><strong>Kişiliği değil, soruları yanıtlar.</strong> "Bu ilişki devam eder mi?" sorusunun horary'de evet ya da hayır cevabı vardır. Bağlanma stilinize dair bir meditasyon değil.</p>
<p><strong>Yalnızca yedi klasik gezegen kullanılır.</strong> Güneş, Ay, Merkür, Venüs, Mars, Jüpiter, Satürn. Uranüs, Neptün ve Plüton — gelenek kurulduktan sonra keşfedildiğinden — kullanılmaz. Atanmış yöneticilik ilişkileri, test edilmiş anlamları yoktur. Zuhal Teyze bunları kullanmaz.</p>
<p><strong>Regiomontanus ev sistemi kullanılır.</strong> Placidus değil, tam burç değil. Regiomontanus sistemi yüzyıllarca horary için standart olmuş ve kurallar bu sistem üzerine inşa edilmiştir.</p>
<p><strong>Teknik önce, sezgi sonra gelir.</strong> Bir horary haritası belirli kurallarla okunur. Astrolog haritayı sadece "hissetmez." Ev lordlarını tespit eder, kondisyonlarını kontrol eder, uygulayan bir açı yapıp yapmadıklarını inceler ve o açının her iki gezegen de burcunu değiştirmeden tamamlanıp tamamlanmadığını belirler. Bu süreç bir hüküm üretir.</p>

<h2 id="harita">Haritada ne var</h2>
<p>Bir horary haritası gökyüzünü on iki eve böler; her ev yaşamın bir alanını yönetir. <strong>Birinci ev</strong> soru soran kişiyi temsil eder. Sorulan konunun evi — ilişkiler ve açık düşmanlar için <strong>yedinci ev</strong>, kariyer için <strong>onuncu ev</strong>, ev ve mülkiyet için <strong>dördüncü ev</strong> vb. — sorulan şeyi temsil eder.</p>
<p>Her evin bir <strong>lordu</strong> vardır: o evin cuspundaki burcu yöneten gezegen. Birinci evin lordu haritadaki "siz"sinizdir. Yedinci evin lordu karşı taraf ya da sorulan konudur. Harita bu anlamlandırıcılar arasındaki ilişkiyi inceleyerek okunur.</p>
<p><strong>Ay</strong> özel bir ağırlık taşır. Sorucuya ortak anlamlandırıcı olur, son yaşananları gösterir ve mevcut burcunu terk etmeden önceki son açısı çoğunlukla sonucu tarif eder. Boş seyreden Ay — mevcut burçta başka bir uygulayan açı yapmayan Ay — geleneksel olarak "bu konudan bir şey çıkmaz" anlamına gelir. Bu her zaman olumsuz değildir: bazen hiçbir şeyin olmaması tam olarak aradığınız cevaptır.</p>

<h2 id="kurallar">Temel kurallar</h2>
<p><strong>Combustion (yanma).</strong> Güneş'e yaklaşık 8 derece içinde olan bir gezegen combust sayılır — zayıflamış, gizlenmiş, etkin biçimde hareket edemiyor. 17 yay dakikası içinde ise gezegen <em>cazimi</em>'dedir, Güneş'in kalbindedir; bu olağanüstü güç konumudur. Fark önemlidir.</p>
<p><strong>Temel onurlar (essential dignity).</strong> Her gezegen bulunduğu burca göre güçlü ya da zayıftır. Kendi burcunda (taht) veya yücelmesinde olan bir gezegen güven ve etkinlikle hareket eder. Zararında ya da düşüşünde olan bir gezegen zayıftır ve temsil ettiği kişiye kolayca yardım edemez. <a href="/tablo">Batlamyus onurlar tablosu</a> tüm bu konumları gösterir.</p>
<p><strong>Reception (kabul).</strong> İki gezegen birbirinin burcunda olabilir — karşılıklı kabul — ya da biri diğerinin onura sahip olduğu bir burçta bulunabilir. Kabul, bir açının anlamını önemli ölçüde değiştirir. Karşılıklı kabulde iki gezegen arasındaki zorlu bir açı, kabul olmaksızın aynı açıya kıyasla çok daha az ciddidir.</p>
<p><strong>Açı ve tamamlanma.</strong> Bir sonucun gerçekleşmesi için anlamlandırıcıların, her iki gezegen de burcunu değiştirmeden önce tamamlanan bir uygulayan açı yapması gerekir. Ayrılan bir açı olanı tarif eder. Uygulayan açı gelecekte olanı tarif eder. Açının gerçekten tamamlanıp tamamlanmadığı — yoksa bir burç değişimi ya da araya giren bir gezegen tarafından engellenip engellenmediği — cevabı belirler.</p>

<blockquote>"Harita yalan söylemez. Yalnızca bir düzeyde zaten bildiğinizi söyler." — Zuhal Teyze'nin oldukça ikna olduğu bir horary ilkesi.</blockquote>

<h2 id="soru">İyi soru nasıl sorulur</h2>
<p>Sorunun kalitesi doğrudan haritanın kalitesini etkiler. Bir horary sorusu şöyle olmalıdır:</p>
<p><strong>Spesifik.</strong> "Hayatım nasıl gidecek?" değil, "bu işe gireceğim mi?" değil. "Bu iş teklifini kabul etsem mi?" veya "o geri döner mi?" Tek konu, tek soru.</p>
<p><strong>Samimi.</strong> Gerçekten bilmek istemelisiniz. Sistemi test etmek ya da boş meraktan sorulan sorular belirsiz veya okunamaz haritalar üretir. Gelenek, haritanın sorucunun zihnini yansıttığını öne sürer — zihin gerçekten meşgul değilse, harita da öyle.</p>
<p><strong>Şimdiki zaman.</strong> "Berlin'e taşınmalı mıyım?" "Hiç yurt dışında yaşar mıyım?"dan iyidir. Soru, şu an karşı karşıya olduğunuz gerçek bir kararı veya gerçek bir belirsizliği tarif etmelidir.</p>
<p>Varsa bağlam ekleyin: soruyu sorduğunuz tarih ve saat, konumunuz ve duruma ilişkin ilgili ayrıntılar. Harita ne kadar kesin dökülebilirse, o kadar kesin okunabilir.</p>

<h2 id="sinir">Horary'nin yapamadıkları</h2>
<p>Horary yanılmaz değildir. Doğru teknik temeller üzerine dikkatle inşa edilmiş bile olsa otomatik bir sistem, deneyimli bir insan astroloğun yerini tutamaz. Gerçek horary hükmü yıllarca süren pratik, bağlam duygusu ve haritadaki çelişen tanıklıkları tartabilme becerisini gerektirir. Zuhal Teyze bir laboratuvardır: faydalı, eğitici, çoğunlukla şaşırtıcı derecede isabetli ve ne olduğu konusunda her zaman dürüst.</p>
<p>Hayatı değiştiren kararlar için — önemli sağlık, hukuk veya finans meseleleri — lütfen gerçek bir klasik astrologla çalışın. Türkçe sorularınız ve detaylı okuma için <a href="https://t.me/zuhalteyze" target="_blank" rel="noopener">Telegram</a>'dan ulaşabilirsiniz. İngilizce derinlemesine okuma için <a href="https://www.fiverr.com/s/LdwmRpA" target="_blank" rel="noopener">Fiverr — Horary Derya</a>.</p>

<h2 id="dene">Ücretsiz deneyin</h2>

<div class="cta-box">
  <p>Yeterince okudunuz. Şimdi gerçekten merak ettiğiniz bir şeyi sorun.</p>
  <a href="/" class="cta-btn">ZUHAL TEYZE'YE SOR — ÜCRETSİZ</a>
</div>

<p style="font-size:12px;color:#9e8c6a;margin-top:2rem">
  İleri okuma: William Lilly, <em>Christian Astrology</em> (1647) — internette ücretsiz mevcut. John Frawley, <em>The Horary Textbook</em> (2005). Ayrıca her okumada kullanılan <a href="/tablo">klasik onurlar tablosuna</a> bakabilirsiniz.
</p>

</body></html>"""


@app.route("/en/learn")
def en_learn():
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>What Is Horary Astrology? A Plain Guide — Auntie Zuhal</title>
<meta name="description" content="A plain, honest guide to horary astrology — how it works, how to ask a question, what the chart tells you, and why it is not like modern astrology. Free horary readings at zuhalteyze.live/en.">
<link rel="canonical" href="https://zuhalteyze.live/en/learn">
<link rel="alternate" hreflang="en" href="https://zuhalteyze.live/en/learn">
<meta property="og:title" content="What Is Horary Astrology? A Plain Guide — Auntie Zuhal">
<meta property="og:description" content="How horary astrology works, in plain language. William Lilly, house lords, void of course Moon, and how to ask a question the stars can actually answer.">
<meta property="og:url" content="https://zuhalteyze.live/en/learn">
<meta name="google-site-verification" content="hhgkCOajijszpcMpIxFkPTokjYTPS7CzDhsG1L1aqJA">
{EN_LEGAL_STYLE}
<style>
body{{max-width:760px}}
.cta-box{{background:#2e1f2e;color:#e6d3ae;border-radius:8px;padding:1.5rem 1.8rem;margin:2.5rem 0;text-align:center}}
.cta-box p{{color:#c9a876;margin-bottom:1rem;font-size:15px;font-style:italic}}
.cta-btn{{display:inline-block;background:transparent;border:1px solid rgba(201,168,118,.6);color:#c9a876;font-family:'Cinzel',serif;font-size:11px;letter-spacing:.15em;padding:12px 28px;text-decoration:none;border-radius:4px;transition:all .2s}}
.cta-btn:hover{{background:rgba(201,168,118,.12);color:#e6d3ae}}
.toc{{background:#f0ead8;border-radius:6px;padding:1.2rem 1.6rem;margin:1.5rem 0;font-size:14px}}
.toc h3{{color:#2e1f6e;font-size:.85rem;letter-spacing:.1em;margin-bottom:.75rem}}
.toc ol{{padding-left:1.2rem;color:#4a3a2a;line-height:2}}
.toc a{{color:#2e1f6e;text-decoration:none}}
.toc a:hover{{text-decoration:underline}}
blockquote{{border-left:3px solid #2e1f6e;margin:1.5rem 0;padding:.75rem 1.2rem;background:#f0ead8;color:#4a3a2a;font-style:italic;font-size:15px}}
</style>
</head><body>
<a href="/en" class="back">← Try Auntie Zuhal free</a>
<h1>What Is Horary Astrology?</h1>
<p style="color:#9e8c6a;font-size:13px;margin-bottom:1.5rem">A plain guide — no prior knowledge of astrology required</p>

<div class="toc">
  <h3>IN THIS GUIDE</h3>
  <ol>
    <li><a href="#what">What horary astrology is</a></li>
    <li><a href="#differ">How it differs from modern astrology</a></li>
    <li><a href="#chart">What the chart contains</a></li>
    <li><a href="#rules">The core rules</a></li>
    <li><a href="#question">How to ask a good question</a></li>
    <li><a href="#limits">What horary cannot do</a></li>
    <li><a href="#try">Try a free reading</a></li>
  </ol>
</div>

<h2 id="what">What horary astrology is</h2>
<p>Horary astrology answers a specific question by reading the sky chart cast for the exact moment the question is sincerely asked. The word <em>horary</em> comes from the Latin <em>hora</em> — hour. This is an astrology of moments, not of lifetimes.</p>
<p>You do not need a birth chart. You do not need to know your rising sign. The chart belongs to the question itself: the moment of asking, the place of asking, and the sincerity behind it. That is enough.</p>
<p>The tradition is old. Persian astrologers practised it in the medieval period. It was systematised in English by <strong>William Lilly</strong>, whose 1647 masterwork <em>Christian Astrology</em> remains the primary reference. In the late twentieth century, <strong>John Frawley</strong> revived and clarified the tradition for contemporary practitioners. Auntie Zuhal follows this line.</p>

<h2 id="differ">How it differs from modern astrology</h2>
<p>Modern astrology — the kind found in newspaper columns and most apps — is primarily natal astrology. It studies character and life themes through the birth chart. It tends toward psychological language: archetypes, patterns, inner journeys.</p>
<p>Horary is different in almost every respect:</p>
<p><strong>It answers questions, not describes personalities.</strong> "Will this relationship last?" has a yes or no answer in horary. Not a meditation on your attachment style.</p>
<p><strong>It uses only the seven classical planets.</strong> Sun, Moon, Mercury, Venus, Mars, Jupiter, Saturn. Uranus, Neptune, and Pluto — discovered after the tradition was formed — are not used. They have no assigned rulerships, no tested meanings. Auntie Zuhal does not use them.</p>
<p><strong>It uses Regiomontanus houses.</strong> Not Placidus, not whole sign. The Regiomontanus system was standard for horary for centuries and is what the rules were built for.</p>
<p><strong>It is technical before it is intuitive.</strong> A horary chart is read through a set of specific rules. The astrologer does not simply "feel" the chart. They identify house lords, check their condition, examine whether they form an applying aspect, and determine whether that aspect perfects before either planet changes sign. This produces a judgment.</p>

<h2 id="chart">What the chart contains</h2>
<p>A horary chart divides the sky into twelve houses, each governing an area of life. The <strong>first house</strong> represents the person asking. The house of the matter in question — the <strong>seventh</strong> for relationships and open enemies, the <strong>tenth</strong> for career, the <strong>fourth</strong> for home and property, and so on — represents what is being asked about.</p>
<p>Each house has a <strong>lord</strong>: the planet that rules the sign on its cusp. The lord of the first house is "you" in the chart. The lord of the seventh house is the other person, or the matter being asked about. The chart is read by examining the relationship between these significators.</p>
<p>The <strong>Moon</strong> carries special weight. She co-significates the querent, shows recent events, and her last aspect before leaving her current sign often describes the outcome. A void of course Moon — one that makes no further applying aspects before changing sign — traditionally indicates "nothing will come of the matter." This is not always negative: sometimes nothing happening is exactly the answer.</p>

<h2 id="rules">The core rules</h2>
<p><strong>Combustion.</strong> A planet within approximately 8 degrees of the Sun is said to be combust — weakened, obscured, unable to act effectively. Within 17 minutes of arc, the planet is <em>cazimi</em>, in the heart of the Sun, which is a position of exceptional strength. The difference matters.</p>

<p><strong>Essential dignity.</strong> Each planet is stronger or weaker depending on which sign it occupies. A planet in its own sign (domicile) or exaltation acts with confidence and effectiveness. A planet in its detriment or fall is weakened and cannot easily help the person it signifies. The <a href="/tablo">Ptolemy dignities table</a> shows these placements in full.</p>

<p><strong>Reception.</strong> Two planets may be in each other's signs — mutual reception — or one may be in a sign where the other has dignity. Reception modifies the meaning of an aspect considerably. A difficult aspect between two planets in mutual reception is far less severe than the same aspect without it.</p>

<p><strong>Aspect and perfection.</strong> For an outcome to occur, the significators must form an applying aspect that perfects — completes — before either planet changes sign. A separating aspect describes what has already happened. An applying aspect describes what is coming. Whether the aspect actually perfects, or is frustrated by a change of sign or an intervening planet, determines the answer.</p>

<blockquote>"The chart does not lie. It only tells you what you already, at some level, know." — a horary principle Auntie Zuhal is quite convinced of.</blockquote>

<h2 id="question">How to ask a good question</h2>
<p>The quality of the question directly affects the quality of the chart. A horary question should be:</p>
<p><strong>Specific.</strong> Not "how will my life go?" but "will I get this particular job?" Not "what about my relationship?" but "will he come back?" One subject, one question.</p>
<p><strong>Sincere.</strong> You must genuinely want to know. A question asked to test the system, or out of idle curiosity, tends to produce an ambiguous or unreadable chart. The tradition holds that the chart reflects the mind of the querent — if the mind is not truly engaged, neither is the chart.</p>
<p><strong>Present tense.</strong> "Should I move to Berlin?" is better than "will I ever live abroad?" The question must describe a real decision or genuine uncertainty you face right now.</p>
<p>Include context if you have it: the date and time you are asking, your location, and any relevant details about the situation. The more precisely the chart can be cast, the more precisely it can be read.</p>

<h2 id="limits">What horary cannot do</h2>
<p>Horary is not infallible. An automated system — even one built carefully on correct technical foundations — is not a substitute for an experienced human astrologer. Real horary judgment requires years of practice, a feel for context, and the ability to weigh competing testimonies in a chart. Auntie Zuhal is a laboratory: useful, educational, often surprisingly accurate, and always honest about what it is.</p>
<p>For life-changing decisions — significant medical, legal, or financial matters — please consult a real classical astrologer. For English-language in-depth readings, <a href="https://www.fiverr.com/s/LdwmRpA" target="_blank" rel="noopener">Horary Derya on Fiverr</a> offers detailed personal interpretations in the Frawley tradition.</p>

<h2 id="try">Try a free reading</h2>

<div class="cta-box">
  <p>You have read enough. Now ask something you genuinely want to know.</p>
  <a href="/en" class="cta-btn">ASK AUNTIE ZUHAL — FREE</a>
</div>

<p style="font-size:12px;color:#9e8c6a;margin-top:2rem">
  Further reading: William Lilly, <em>Christian Astrology</em> (1647) — available free online. John Frawley, <em>The Horary Textbook</em> (2005). See also the <a href="/tablo">classical dignities table</a> used in every reading.
</p>

</body></html>"""



def en_faq():
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FAQ — Auntie Zuhal</title>
<meta name="description" content="What is Auntie Zuhal? How does horary astrology work? Honest answers.">
{EN_LEGAL_STYLE}</head><body>
<a href="/en" class="back">← Back to Auntie Zuhal</a>
<h1>Frequently Asked Questions</h1>

<div class="lab-box">
  <p><strong>First, let us be clear:</strong> This platform is an experiment. It is a labour of love built on classical horary astrology, continuously improved through real user feedback. It will never replace a real astrologer — and it would be dishonest to claim otherwise.</p>
</div>

<h2>What is this, exactly?</h2>
<p>Auntie Zuhal calculates a real-time sky chart using pyswisseph, analyses it with Regiomontanus houses and rules from the Frawley/Lilly tradition, and then asks an AI to interpret the result. The calculation is genuine horary methodology. The interpretation is — honestly — still being refined. Sometimes it reads very well. Sometimes it makes mistakes. That is why it is a lab.</p>

<h2>What is horary astrology?</h2>
<p>Horary astrology interprets the sky chart cast for the exact moment a question is asked. No birth chart is needed — the question itself, the time it was asked, and the place are sufficient. Systematised by William Lilly and other classical astrologers centuries ago, it relies on specific technical rules: house lords, aspects, reception, combustion, and void of course Moon. When applied correctly, it can produce remarkably precise answers.</p>

<h2>How reliable is it?</h2>
<p>Honestly: variable. The technical data is real — planetary degrees, house cusps, dignity scores are all calculated correctly. But interpreting that data is a different matter. A real horary astrologer reads with years of practice and intuition; this application produces a rule-based AI interpretation. Some readings come out nearly perfect. Others contain a technical error or a missed nuance. Read every answer with curiosity, not blind trust.</p>

<h2>I have an important decision to make — should I use this?</h2>
<p>For gaining perspective, seeing the chart, understanding the technical situation — yes. But for life-changing matters such as career changes, relationship decisions, or health concerns, please work with a real classical astrologer. Not everyone can read horary — you need someone trained in the Frawley tradition with real practice. For English readings: <a href="https://www.fiverr.com/s/LdwmRpA" target="_blank" rel="noopener">Fiverr — Horary Derya</a>. For Turkish: <a href="https://t.me/zuhalteyze" target="_blank" rel="noopener">Telegram</a>.</p>

<h2>How should I phrase my question?</h2>
<p>Specific, sincere, and about something you genuinely want to know right now. Not "how will my life go?" — but "should I accept this job offer?" or "will this person come back?" One subject, asked honestly. Questions asked to test the system, or out of idle curiosity, tend not to produce clear answers.</p>

<h2>Can I ask the same question again?</h2>
<p>Classical tradition advises against it. Not because you disliked the answer — but only when something has genuinely changed. Repeating the same question tends to produce inconsistent or misleading charts.</p>

<h2>What data do you collect?</h2>
<p>Email address (for login only) and question time. This application is currently in beta and is completely free. No payment system is active. Your data is not shared with or sold to third parties.</p>

</body></html>"""


@app.route("/en/contact")
def en_contact():
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Contact — Auntie Zuhal</title>
<meta name="description" content="Contact Auntie Zuhal. Support, professional horary readings, and collaborations.">
{EN_LEGAL_STYLE}</head><body>
<a href="/en" class="back">← Back to Auntie Zuhal</a>
<h1>Contact</h1>

<div class="contact-card">
  <span class="tag">SUPPORT</span>
  <h3>Technical issues</h3>
  <p>Email: <a href="mailto:noreply@zuhalteyze.live">noreply@zuhalteyze.live</a></p>
</div>

<div class="contact-card">
  <span class="tag">TELEGRAM</span>
  <h3>Quick contact</h3>
  <p><a href="https://t.me/zuhalteyze" target="_blank" rel="noopener">@zuhalteyze</a> — questions, support, announcements</p>
</div>

<div class="contact-card">
  <span class="tag">PROFESSIONAL READING</span>
  <h3>In-depth horary reading (English)</h3>
  <p>For a personalised, detailed horary interpretation:<br>
  <a href="https://www.fiverr.com/s/LdwmRpA" target="_blank" rel="noopener">Fiverr — Horary Derya</a></p>
</div>

<div class="contact-card">
  <span class="tag">COLLABORATION</span>
  <h3>Partnership and business enquiries</h3>
  <p>Email: <a href="mailto:noreply@zuhalteyze.live">noreply@zuhalteyze.live</a></p>
</div>

<p style="margin-top:2rem;font-size:13px;color:#9e8c6a;">Response time is usually 24–48 hours.</p>
</body></html>"""


@app.route("/en/terms")
def en_terms():
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Terms of Use — Auntie Zuhal</title>{EN_LEGAL_STYLE}</head><body>
<a href="/en" class="back">← Back to Auntie Zuhal</a>
<h1>Terms of Use</h1>
<p>Last updated: June 2026</p>
<h2>1. About the Service</h2>
<p>Auntie Zuhal (zuhalteyze.live/en) is an AI-assisted interpretation service based on traditional horary astrology techniques. All content provided is strictly for entertainment purposes and does not constitute medical, legal, or financial advice.</p>
<h2>2. Limitation of Liability</h2>
<p>Interpretations provided through this platform cannot be treated as prophecy or certain fact. Users evaluate any interpretation at their own discretion and responsibility. The platform makes no guarantees regarding the accuracy or outcomes of its interpretations.</p>
<h2>3. User Obligations</h2>
<p>Users agree to use the platform lawfully and in a manner that does not harm others. Misuse, overloading the system, or unauthorised access is prohibited.</p>
<h2>4. Intellectual Property</h2>
<p>The platform's content, design, and software belong to Auntie Zuhal / zuhalteyze.live. Reproduction or distribution without permission is not permitted.</p>
<h2>5. Changes</h2>
<p>These terms may be updated without prior notice. Continued use of the platform constitutes acceptance of the current terms.</p>
<h2>Contact</h2>
<p>Questions: <a href="mailto:noreply@zuhalteyze.live">noreply@zuhalteyze.live</a></p>
</body></html>"""


@app.route("/en/privacy")
def en_privacy():
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Privacy Policy — Auntie Zuhal</title>{EN_LEGAL_STYLE}</head><body>
<a href="/en" class="back">← Back to Auntie Zuhal</a>
<h1>Privacy Policy</h1>
<p>Last updated: June 2026</p>
<h2>Data We Collect</h2>
<p>The platform collects only the following: email address (for login) and question time. This application is currently in beta and is completely free; no payment system is active.</p>
<h2>How Data Is Used</h2>
<p>Your email address is used only for authentication and service notifications. Your data is not shared with or sold to third parties.</p>
<h2>Cookies and Sessions</h2>
<p>The platform uses a cookie for session management. You may disable cookies in your browser settings; however, you will not be able to log in if you do so.</p>
<h2>Data Retention</h2>
<p>To request deletion of your account or data, please contact us.</p>
<h2>GDPR</h2>
<p>Users resident in the EU may exercise their rights under GDPR. Requests: <a href="mailto:noreply@zuhalteyze.live">noreply@zuhalteyze.live</a></p>
</body></html>"""




# ─────────────────────────────────────────
# VAKALAR (TR)
# ─────────────────────────────────────────

@app.route("/vakalar")
def vakalar_index():
    return """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Horary Vaka İncelemeleri — Gerçek Sorular, Gerçek Haritalar | Zuhal Teyze</title>
<meta name="description" content="Gerçek horary astroloji vakları, gerçek haritalar. Frawley/Lilly geleneği. Kariyer, ilişki, sağlık soruları — anonimleştirilmiş gerçek okumalar.">
<link rel="canonical" href="https://zuhalteyze.live/vakalar">
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,500;0,600;1,400&family=Outfit:wght@300;400;500&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#F5F0E8;color:#1A1218;font-family:'Outfit',sans-serif;font-weight:300}
.hdr{background:#2D1B2E;padding:1rem 2rem;display:flex;align-items:center;justify-content:space-between}
.logo{font-family:'Cormorant Garamond',serif;font-size:1.4rem;font-weight:500;color:#C9A84C;text-decoration:none}
.nav a{font-size:.8rem;color:rgba(245,240,232,.6);text-decoration:none;margin-left:1.5rem;letter-spacing:.08em;text-transform:uppercase}
.nav a:hover{color:#C9A84C}
.hero{background:#2D1B2E;padding:3.5rem 2rem 3rem;text-align:center}
.hero h1{font-family:'Cormorant Garamond',serif;font-size:clamp(1.8rem,4vw,2.8rem);font-weight:500;color:#F5F0E8;margin-bottom:.75rem}
.hero p{color:rgba(245,240,232,.55);font-size:.9rem;max-width:520px;margin:0 auto}
.container{max-width:840px;margin:0 auto;padding:3rem 2rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:1.5rem}
.card{background:white;border:1px solid rgba(201,168,76,.2);border-radius:10px;overflow:hidden;text-decoration:none;color:inherit;display:block;transition:box-shadow .2s}
.card:hover{box-shadow:0 8px 32px rgba(45,27,46,.12)}
.card-top{padding:1.5rem 1.75rem 1.25rem;border-bottom:1px solid rgba(201,168,76,.12)}
.card-cat{font-size:.68rem;letter-spacing:.13em;text-transform:uppercase;color:#C9A84C;margin-bottom:.6rem}
.card-title{font-family:'Cormorant Garamond',serif;font-size:1.4rem;font-weight:500;color:#2D1B2E;line-height:1.3;margin-bottom:.5rem}
.card-desc{font-size:.85rem;color:#7A6D72;line-height:1.6}
.card-bottom{padding:1rem 1.75rem;display:flex;align-items:center;justify-content:space-between}
.card-tags{display:flex;gap:.5rem;flex-wrap:wrap}
.tag{font-size:.7rem;background:rgba(201,168,76,.1);color:#7B5EA7;padding:3px 8px;border-radius:3px;letter-spacing:.04em}
.card-arrow{color:#C9A84C;font-size:1.1rem}
footer{background:#1a1018;padding:1.5rem 2rem;text-align:center;font-size:.78rem;color:rgba(245,240,232,.25);margin-top:3rem}
@media(max-width:600px){.nav{display:none}.grid{grid-template-columns:1fr}.container{padding:2rem 1.25rem}}
</style>
</head>
<body>
<header class="hdr">
  <a href="https://zuhalteyze.live" class="logo">Zuhal Teyze</a>
  <nav class="nav">
    <a href="/">Ana Sayfa</a>
    <a href="/ogren">Öğren</a>
    <a href="/faq">SSS</a>
    <a href="/contact">İletişim</a>
  </nav>
</header>
<div class="hero">
  <h1>Horary Vaka İncelemeleri</h1>
  <p>Gerçek sorular. Gerçek haritalar. Frawley / Lilly geleneğiyle anonimleştirilmiş okumalar.</p>
</div>
<div class="container">
  <div class="grid">
    <a class="card" href="/vakalar/meslegimi-birakayim-mi">
      <div class="card-top">
        <div class="card-cat">Kariyer &amp; Meslek</div>
        <div class="card-title">Meslekten Vazgeçmeli miyim?</div>
        <div class="card-desc">Lord 1 Mars sabit yıldız Pollux'ta — bir bilgisayar mühendisi mesleki yolundan vazgeçip geçmeyeceğini soruyor. Savaşçı ve âlim.</div>
      </div>
      <div class="card-bottom">
        <div class="card-tags"><span class="tag">Kariyer</span><span class="tag">Sabit Yıldız</span><span class="tag">Lord 10</span></div>
        <span class="card-arrow">&#8599;</span>
      </div>
    </a>
    <a class="card" href="/vakalar/kocami-bulacak-miyim">
      <div class="card-top">
        <div class="card-cat">İlişki &amp; Evlilik</div>
        <div class="card-title">Kocamla Tanışacak mıyım?</div>
        <div class="card-desc">Jüpiter zararda ve geri harekette, üç kısır burç, Bellatrix kavuşumu. Harita açıkça konuşuyor — ama sessizce bir umut da saklıyor.</div>
      </div>
      <div class="card-bottom">
        <div class="card-tags"><span class="tag">Evlilik</span><span class="tag">Geri Hareket</span><span class="tag">Kısır Burçlar</span></div>
        <span class="card-arrow">&#8599;</span>
      </div>
    </a>
  </div>
  <p style="margin-top:3rem;text-align:center;font-size:.85rem;color:#7A6D72;">
    Yeni vakalar düzenli ekleniyor. Kendi sorunuz mu var?
    <a href="https://www.fiverr.com/s/LdwmRpA" target="_blank" style="color:#7B5EA7">Fiverr&#39;da Horary Derya &#8599;</a>
  </p>
</div>
<footer>&copy; 2025 Zuhal Teyze &middot; zuhalteyze.live &middot; Klasik Horary Astroloji</footer>
</body>
</html>"""


@app.route("/vakalar/meslegimi-birakayim-mi")
def vaka_meslek():
    return send_from_directory("cases", "meslek.html")


@app.route("/vakalar/kocami-bulacak-miyim")
def vaka_esim():
    return send_from_directory("cases", "esim.html")


# ─────────────────────────────────────────
# CASE STUDIES
# ─────────────────────────────────────────

@app.route("/cases")
def cases_index():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Horary Case Studies — Real Questions, Real Charts | Zuhal Teyze</title>
<meta name="description" content="Real horary astrology case studies with full charts. Classical Frawley/Lilly tradition. Career, relationships, medical questions — anonymised real readings.">
<link rel="canonical" href="https://zuhalteyze.live/cases">
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,500;0,600;1,400&family=Outfit:wght@300;400;500&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#F5F0E8;color:#1A1218;font-family:'Outfit',sans-serif;font-weight:300}
.hdr{background:#2D1B2E;padding:1rem 2rem;display:flex;align-items:center;justify-content:space-between}
.logo{font-family:'Cormorant Garamond',serif;font-size:1.4rem;font-weight:500;color:#C9A84C;text-decoration:none}
.nav a{font-size:.8rem;color:rgba(245,240,232,.6);text-decoration:none;margin-left:1.5rem;letter-spacing:.08em;text-transform:uppercase}
.nav a:hover{color:#C9A84C}
.hero{background:#2D1B2E;padding:3.5rem 2rem 3rem;text-align:center}
.hero h1{font-family:'Cormorant Garamond',serif;font-size:clamp(1.8rem,4vw,2.8rem);font-weight:500;color:#F5F0E8;margin-bottom:.75rem}
.hero p{color:rgba(245,240,232,.55);font-size:.9rem;max-width:520px;margin:0 auto}
.container{max-width:840px;margin:0 auto;padding:3rem 2rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:1.5rem}
.card{background:white;border:1px solid rgba(201,168,76,.2);border-radius:10px;overflow:hidden;text-decoration:none;color:inherit;display:block;transition:box-shadow .2s}
.card:hover{box-shadow:0 8px 32px rgba(45,27,46,.12)}
.card-top{padding:1.5rem 1.75rem 1.25rem;border-bottom:1px solid rgba(201,168,76,.12)}
.card-cat{font-size:.68rem;letter-spacing:.13em;text-transform:uppercase;color:#C9A84C;margin-bottom:.6rem}
.card-title{font-family:'Cormorant Garamond',serif;font-size:1.4rem;font-weight:500;color:#2D1B2E;line-height:1.3;margin-bottom:.5rem}
.card-desc{font-size:.85rem;color:#7A6D72;line-height:1.6}
.card-bottom{padding:1rem 1.75rem;display:flex;align-items:center;justify-content:space-between}
.card-tags{display:flex;gap:.5rem;flex-wrap:wrap}
.tag{font-size:.7rem;background:rgba(201,168,76,.1);color:#7B5EA7;padding:3px 8px;border-radius:3px;letter-spacing:.04em}
.card-arrow{color:#C9A84C;font-size:1.1rem}
footer{background:#1a1018;padding:1.5rem 2rem;text-align:center;font-size:.78rem;color:rgba(245,240,232,.25);margin-top:3rem}
@media(max-width:600px){.nav{display:none}.grid{grid-template-columns:1fr}.container{padding:2rem 1.25rem}}
</style>
</head>
<body>
<header class="hdr">
  <a href="https://zuhalteyze.live" class="logo">Zuhal Teyze</a>
  <nav class="nav">
    <a href="/en">Home</a>
    <a href="/en/learn">Learn</a>
    <a href="/en/faq">FAQ</a>
    <a href="/en/contact">Contact</a>
  </nav>
</header>
<div class="hero">
  <h1>Horary Case Studies</h1>
  <p>Real questions. Real charts. Anonymised readings in the classical Frawley / Lilly tradition.</p>
</div>
<div class="container">
  <div class="grid">
    <a class="card" href="/cases/should-i-give-up-profession">
      <div class="card-top">
        <div class="card-cat">Career &amp; Profession</div>
        <div class="card-title">Should I Give Up On This Profession?</div>
        <div class="card-desc">Lord 1 Mars conjunct Pollux — a Computer Science professional asks whether to abandon their path. The warrior and the scholar.</div>
      </div>
      <div class="card-bottom">
        <div class="card-tags"><span class="tag">Career</span><span class="tag">Fixed Star</span><span class="tag">Lord 10</span></div>
        <span class="card-arrow">&#8599;</span>
      </div>
    </a>
    <a class="card" href="/cases/will-i-meet-my-husband">
      <div class="card-top">
        <div class="card-cat">Relationships &amp; Marriage</div>
        <div class="card-title">Will I Meet My Future Husband?</div>
        <div class="card-desc">Jupiter retrograde in detriment, three barren signs, Bellatrix rising. The chart speaks clearly — but also hides a quiet hope.</div>
      </div>
      <div class="card-bottom">
        <div class="card-tags"><span class="tag">Marriage</span><span class="tag">Retrograde</span><span class="tag">Barren Signs</span></div>
        <span class="card-arrow">&#8599;</span>
      </div>
    </a>
  </div>
  <p style="margin-top:3rem;text-align:center;font-size:.85rem;color:#7A6D72;">
    More cases added regularly. Have a question of your own?
    <a href="https://www.fiverr.com/s/LdwmRpA" target="_blank" style="color:#7B5EA7">Ask Horary Derya on Fiverr &#8599;</a>
  </p>
</div>
<footer>&copy; 2025 Zuhal Teyze &middot; zuhalteyze.live &middot; Classical Horary Astrology</footer>
</body>
</html>"""


@app.route("/cases/should-i-give-up-profession")
def case_profession():
    return send_from_directory("cases", "profession.html")


@app.route("/cases/will-i-meet-my-husband")
def case_husband():
    return send_from_directory("cases", "husband.html")


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


@app.route("/sitemap.xml")
def sitemap():
    from flask import Response
    base = "https://zuhalteyze.live"
    urls = [
        ("", "1.0",  "daily"),
        ("/ogren", "0.9", "monthly"),
        ("/en", "1.0",  "daily"),
        ("/en/learn", "0.9", "monthly"),
        ("/faq", "0.8", "weekly"),
        ("/en/faq", "0.8", "weekly"),
        ("/tablo", "0.7", "monthly"),
        ("/vakalar", "0.8", "monthly"),
        ("/vakalar/meslegimi-birakayim-mi", "0.8", "monthly"),
        ("/vakalar/kocami-bulacak-miyim", "0.8", "monthly"),
        ("/cases", "0.8", "monthly"),
        ("/cases/should-i-give-up-profession", "0.8", "monthly"),
        ("/cases/will-i-meet-my-husband", "0.8", "monthly"),
        ("/contact", "0.5", "monthly"),
        ("/en/contact", "0.5", "monthly"),
        ("/terms", "0.3", "monthly"),
        ("/en/terms", "0.3", "monthly"),
        ("/privacy", "0.3", "monthly"),
        ("/en/privacy", "0.3", "monthly"),
    ]
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"\n'
    xml += '        xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
    for path, priority, freq in urls:
        xml += f"""  <url>
    <loc>{base}{path}</loc>
    <changefreq>{freq}</changefreq>
    <priority>{priority}</priority>
  </url>\n"""
    xml += '</urlset>'
    return Response(xml, mimetype="application/xml")


@app.route("/robots.txt")
def robots():
    from flask import Response
    txt = """User-agent: *
Allow: /
Allow: /en
Allow: /faq
Allow: /en/faq
Allow: /tablo
Allow: /contact
Allow: /en/contact
Allow: /terms
Allow: /en/terms
Allow: /privacy
Allow: /en/privacy
Allow: /sitemap.xml
Disallow: /lab
Disallow: /api/

Sitemap: https://zuhalteyze.live/sitemap.xml
"""
    return Response(txt, mimetype="text/plain")


# ─────────────────────────────────────────
# LEMON SQUEEZY WEBHOOK
# ─────────────────────────────────────────

@app.route("/api/webhook/lemonsqueezy", methods=["POST"])
def webhook_lemonsqueezy():
    """Lemon Squeezy ödeme bildirimi — başarılı her siparişte credit yükler."""

    payload_bytes = request.get_data()

    # İmza doğrula
    signature = request.headers.get("X-Signature", "")
    if not LS_WEBHOOK_SECRET:
        print("[LS WEBHOOK] LS_WEBHOOK_SECRET tanımlı değil!")
        return jsonify({"error": "Webhook secret eksik"}), 500

    expected_sig = hmac.new(
        LS_WEBHOOK_SECRET.encode("utf-8"),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_sig, signature):
        print(f"[LS WEBHOOK] İmza geçersiz.")
        return jsonify({"error": "Geçersiz imza"}), 401

    try:
        payload = _json.loads(payload_bytes)
    except Exception:
        return jsonify({"error": "JSON parse hatası"}), 400

    # Sadece order_created işle
    event_name = payload.get("meta", {}).get("event_name", "")
    if event_name != "order_created":
        print(f"[LS WEBHOOK] Atlandı: {event_name}")
        return jsonify({"ok": True, "skipped": event_name}), 200

    data_attrs = payload.get("data", {}).get("attributes", {})
    order_id   = str(payload.get("data", {}).get("id", ""))
    status     = data_attrs.get("status", "")
    email      = (data_attrs.get("user_email") or "").strip().lower()

    # custom_data'da email varsa onu tercih et
    custom_email = (
        payload.get("meta", {}).get("custom_data", {}).get("user_email", "")
    ).strip().lower()
    if custom_email:
        email = custom_email

    variant_id = data_attrs.get("first_order_item", {}).get("variant_id")

    print(f"[LS WEBHOOK] order={order_id} status={status} email={email} variant={variant_id}")

    if status != "paid":
        return jsonify({"ok": True, "skipped": f"status={status}"}), 200

    if not email or "@" not in email:
        return jsonify({"error": "Email yok"}), 400

    # Idempotency
    if is_payment_processed(order_id):
        print(f"[LS WEBHOOK] Duplicate: {order_id}")
        return jsonify({"ok": True, "duplicate": True}), 200

    # Credit miktarı
    credits_to_add = VARIANT_CREDITS.get(variant_id, 0)
    if credits_to_add == 0:
        print(f"[LS WEBHOOK] ⚠️  Bilinmeyen variant_id={variant_id}, 3 credit verildi.")
        credits_to_add = 3

    new_total = add_credits(email, credits_to_add)
    mark_payment_processed(order_id, email, credits_to_add, variant_id)
    print(f"[LS WEBHOOK] ✓ {email} → +{credits_to_add} credit (toplam: {new_total})")

    # Bildirim emaili
    try:
        resend.Emails.send({
            "from": "Zuhal Teyze <noreply@zuhalteyze.live>",
            "to": email,
            "reply_to": "deryaoz777@gmail.com",
            "subject": "Zuhal Teyze — Krediniz yüklendi ✨",
            "html": f"""
            <div style="font-family:Georgia,serif;max-width:480px;margin:0 auto;
                        padding:2rem;background:#f5f0e8;">
                <h2 style="font-family:'Cinzel',serif;color:#2e1f6e;
                            text-align:center;letter-spacing:.1em;">ZUHAL TEYZE</h2>
                <p style="color:#4a3a2a;font-size:17px;line-height:1.7;font-style:italic;">
                    Gözüm, ödemen geldi. Hesabına <strong>{credits_to_add} soru hakkı</strong>
                    yüklendi. Toplam kredin: <strong>{new_total}</strong>.
                </p>
                <div style="text-align:center;margin:2rem 0;">
                    <a href="https://zuhalteyze.live"
                       style="background:#2e1f6e;color:#f5e8b8;padding:14px 32px;
                              text-decoration:none;font-family:sans-serif;
                              font-size:14px;letter-spacing:2px;border-radius:4px;">
                        SORUNU SOR
                    </a>
                </div>
            </div>
            """
        })
    except Exception as e:
        print(f"[LS WEBHOOK] Bildirim emaili gönderilemedi: {e}")

    return jsonify({"ok": True, "credits_added": credits_to_add, "new_total": new_total}), 200


# ─────────────────────────────────────────
# CHECKOUT URL
# ─────────────────────────────────────────

@app.route("/api/checkout/url", methods=["GET"])
def checkout_url():
    """
    Frontend'e Lemon Squeezy checkout URL'i döndürür.
    Kullanım: /api/checkout/url?variant=3q | 5q | 10q
    """
    session_token = request.cookies.get("zt_session", "")
    user = get_user_by_session(session_token) if session_token else None

    variant = request.args.get("variant", "").lower()
    base_url = LS_CHECKOUT_URLS.get(variant)

    if not base_url:
        return jsonify({"error": f"Bilinmeyen paket: {variant}"}), 400

    if user:
        sep = "&" if "?" in base_url else "?"
        email_enc = urllib.parse.quote(user["email"])
        checkout_full = (
            f"{base_url}{sep}"
            f"checkout[email]={email_enc}"
            f"&checkout[custom][user_email]={email_enc}"
        )
    else:
        checkout_full = base_url

    return jsonify({"url": checkout_full})


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
    lang = data.get("lang", "tr")
    if lang not in ("tr", "en"):
        lang = "tr"

    if not question:
        return jsonify({"error": "Soru boş olamaz."}), 400

    try:
        dt = datetime.datetime.now()

        if not system_prompt:
            # Tam horary engine — haritayı hesapla, build_frawley_prompt kullan
            chart = calc_chart(question, dt, lat, lon)
            prompt = build_frawley_prompt(chart, lang=lang)
            output = ask_claude(prompt, ANTHROPIC_API_KEY)
            chart_json = chart_to_dict(chart)
        else:
            # Custom prompt modu — sistem promptunu kullan
            if chart_data:
                user_msg = f"Soru: {question}\n\nHarita verisi:\n{chart_data}"
                chart_json = {}
            else:
                # Haritayı hesapla, veri bölümünü ekle
                chart = calc_chart(question, dt, lat, lon)
                auto_prompt = build_frawley_prompt(chart, lang=lang)
                chart_json = chart_to_dict(chart)
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

        return jsonify({"success": True, "output": output, "id": new_id, "chart_json": chart_json})
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
