"""
server.py — Horary Flask Backend (Zuhal Teyze Edition)
"""

from flask import Flask, request, jsonify, send_from_directory
import datetime
import os
import urllib.request
import urllib.parse
import json as _json
from horary_engine import (
    calc_chart, chart_to_dict, build_frawley_prompt,
    ask_claude, detect_question_type, get_house_ruler,
    PLANET_TR, SIGN_NAMES_TR, ESSENTIAL_DIGNITY_TABLE
)

app = Flask(__name__, static_folder=".")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FORMSPREE_URL = os.environ.get("FORMSPREE_URL", "")
print(f"[STARTUP] ANTHROPIC_API_KEY {'tanımlı (' + ANTHROPIC_API_KEY[:10] + '...)' if ANTHROPIC_API_KEY else 'TANIMLI DEĞİL'}")

_daily_cache = {}

DEFAULT_LAT = 42.17
DEFAULT_LON = 42.67


@app.route("/")
def index():
    return send_from_directory(".", "zuhal_teyze.html")


@app.route("/api/zuhal", methods=["POST"])
def api_zuhal():
    """
    Zuhal Teyze endpoint — sadece ilişki soruları.
    Body: { question, lat, lon, datetime }
    API key sunucuda, frontend'e gönderilmez.
    """
    data = request.json or {}

    question = data.get("question", "").strip()
    if not question:
        return jsonify({"error": "Soru boş olamaz"}), 400

    lat = float(data.get("lat", DEFAULT_LAT))
    lon = float(data.get("lon", DEFAULT_LON))

    dt_str = data.get("datetime")
    if dt_str:
        try:
            dt = datetime.datetime.fromisoformat(dt_str)
        except:
            dt = datetime.datetime.now()
    else:
        dt = datetime.datetime.now()

    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "Sunucu yapılandırma hatası"}), 500

    try:
        chart = calc_chart(question, dt, lat, lon)
        prompt = build_frawley_prompt(chart)  # ilişki sorusu ise build_iliski_prompt'a yönlenir
        interpretation = ask_claude(prompt, ANTHROPIC_API_KEY)

        return jsonify({
            "success": True,
            "interpretation": interpretation,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chart", methods=["POST"])
def api_chart():
    """
    Genel harita endpoint (mevcut) — korunuyor.
    """
    data = request.json or {}

    question = data.get("question", "").strip()
    if not question:
        return jsonify({"error": "Soru boş olamaz"}), 400

    lat = float(data.get("lat", DEFAULT_LAT))
    lon = float(data.get("lon", DEFAULT_LON))
    api_key = data.get("api_key") or ANTHROPIC_API_KEY

    dt_str = data.get("datetime")
    if dt_str:
        try:
            dt = datetime.datetime.fromisoformat(dt_str)
        except:
            dt = datetime.datetime.now()
    else:
        dt = datetime.datetime.now()

    try:
        chart = calc_chart(question, dt, lat, lon)
        chart_data = chart_to_dict(chart)
        prompt = build_frawley_prompt(chart)

        q_type = detect_question_type(question)
        lord1 = get_house_ruler(chart, 1)

        interpretation = None
        if api_key:
            interpretation = ask_claude(prompt, api_key)

        moon = chart_data["planets"]["moon"]
        vibe = get_moon_vibe(moon["sign"], chart_data["is_daytime"])

        return jsonify({
            "success": True,
            "chart": chart_data,
            "interpretation": interpretation,
            "question_type": q_type["desc"],
            "lord1": PLANET_TR.get(lord1, lord1),
            "moon_sign": moon["sign"],
            "vibe": vibe,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/contact", methods=["POST"])
def api_contact():
    if not FORMSPREE_URL:
        return jsonify({"error": "FORMSPREE_URL tanımlı değil"}), 500

    data = request.json or {}
    name = data.get("name", "").strip()
    email = data.get("email", "").strip()
    question = data.get("question", "").strip()
    chart_data = data.get("chart_data", "")

    if not name or not email or not question:
        return jsonify({"error": "Ad, e-posta ve soru zorunludur"}), 400

    payload = _json.dumps({
        "name": name, "email": email,
        "question": question, "chart_data": chart_data,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            FORMSPREE_URL, data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
        if status in (200, 201):
            return jsonify({"success": True})
        return jsonify({"error": f"Formspree {status} döndürdü"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/vibe", methods=["POST"])
def api_vibe():
    data = request.json or {}
    sign = data.get("sign", "Koç")
    today = datetime.date.today().isoformat()
    cache_key = f"{today}:{sign}"

    if cache_key in _daily_cache:
        return jsonify(_daily_cache[cache_key])

    if ANTHROPIC_API_KEY:
        try:
            vibe = generate_daily_vibe(sign, ANTHROPIC_API_KEY)
            _daily_cache[cache_key] = vibe
            return jsonify(vibe)
        except:
            pass

    return jsonify(get_moon_vibe(sign, True))


def generate_daily_vibe(sign: str, api_key: str) -> dict:
    from horary_engine import calc_chart, SIGN_NAMES_TR, PLANET_TR
    import json as _json

    now = datetime.datetime.now().replace(hour=12, minute=0)
    chart = calc_chart("Günlük enerji", now, 42.17, 42.67)

    planet_lines = []
    for pname, planet in chart.planets.items():
        retro = " ℞" if planet.retrograde else ""
        planet_lines.append(
            f"{PLANET_TR[pname]}: {int(planet.sign_degree)}° {SIGN_NAMES_TR[planet.sign_index]}{retro} (Ev {planet.house})"
        )
    planet_summary = "\n".join(planet_lines)

    prompt = f"""Bugünün gökyüzü:
{planet_summary}

Burç: {sign}
Tarih: {now.strftime("%d %B %Y")}

Bu gezegen pozisyonlarına dayanarak {sign} burcu için bugünün enerjisini yaz.

KURALLAR:
- İğneleyici, esprili, "zaten biliyordun" tonu
- Türkçe, 2-3 cümle, max 60 kelime
- Mood badge: 2-4 kelime, büyük harf
- 4 enerji barı: her biri 0-100

Sadece JSON döndür:
{{"text": "...", "mood": "...", "energy": {{"Etiket1": 80, "Etiket2": 45, "Etiket3": 70, "Etiket4": 30}}}}"""

    response = ask_claude(prompt, api_key)

    try:
        clean = response.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        result = _json.loads(clean.strip())
        if "text" in result and "mood" in result and "energy" in result:
            return result
    except:
        pass

    return get_moon_vibe(sign, True)


def get_moon_vibe(sign: str, is_daytime: bool) -> dict:
    VIBES = {
        "Koç":     {"text": "Mars bugün aceleciliğinizi körüklüyor. Evet, herkese önce girmek istiyorsunuz — ama trafik ışığı herkese aynı anda kırmızı yanar. Bir soluk alın.", "mood": "YANGIN MOD", "energy": {"Enerji": 90, "Sabır": 12, "Ego": 95, "Sezgi": 40}},
        "Boğa":    {"text": "Venüs size konfor vaat ediyor ama Satürn faturayı gönderiyor. Güzel şeyler istiyorsunuz, para mı var?", "mood": "KONFOR KOMA", "energy": {"Enerji": 45, "İnat": 99, "Keyif": 80, "Değişim": 5}},
        "İkizler": {"text": "Merkür beş farklı fikri aynı anda düşünmenizi sağlıyor. Bunların hepsini birden söyleyeceksiniz ve hiçbirini bitirmeyeceksiniz.", "mood": "KAOTİK ZİHİN", "energy": {"Enerji": 85, "Odak": 15, "Merak": 98, "Sonuç": 20}},
        "Yengeç":  {"text": "Ay bugün duygusal tüm kapıları açık bıraktı. Birileri sizi üzerse ağlarsınız, mutlu ederse de ağlarsınız.", "mood": "DUYGU OKYANUSU", "energy": {"Empati": 99, "Mesafe": 5, "Koruma": 85, "Bırakma": 10}},
        "Aslan":   {"text": "Güneş doğrudan üzerinize parlıyor ve siz bunu hak ettiğinizi düşünüyorsunuz. Belki de öyle.", "mood": "SAHNE BENİM", "energy": {"Özgüven": 99, "Alçakgönüllülük": 8, "Karizma": 95, "Dinleme": 20}},
        "Başak":   {"text": "Merkür her hatayı gösteriyor — başkalarının ve özellikle kendinizin. Eleştiri yeteneğiniz zirvedeyken eleştirilmekten nefret etmeniz ilginç.", "mood": "ANALİZ SIKIŞMASI", "energy": {"Dikkat": 99, "Öz-merhamet": 15, "Verimlilik": 85, "Mükemmelcilik": 100}},
        "Terazi":  {"text": "Venüs denge istiyor ama siz bugün karardan kaçıyorsunuz. Öğle yemeği seçmek bile varoluşsal kriz gibi geliyor.", "mood": "KARARSIZLIK DÖNEMİ", "energy": {"Uyum": 90, "Karar": 18, "Estetik": 95, "Net Tavır": 10}},
        "Akrep":   {"text": "Mars ve Plüton bugün sizi daha da yoğunlaştırıyor. Birisi sizi 'fazla ciddi' bulacak. Onlara aldırmayın.", "mood": "DERİN SULAR", "energy": {"Yoğunluk": 100, "Güven": 30, "Sezgi": 98, "Hafiflik": 5}},
        "Yay":     {"text": "Jüpiter bugün sizi biraz fazla iyimser yapıyor. Hayır, o proje bir haftada bitmez. Ama evet, enerjiniz harika.", "mood": "OPTİMİZM AŞIMI", "energy": {"Coşku": 98, "Gerçekçilik": 20, "Özgürlük": 90, "Taahhüt": 15}},
        "Oğlak":   {"text": "Satürn her zamanki gibi sorumluluğunuzu hatırlatıyor. Tatil planlarken bile 'ama şu iş nasıl olacak' diyorsunuz.", "mood": "SORUMLULUK TİRANI", "energy": {"Disiplin": 99, "Eğlence": 25, "Sabır": 90, "Spontanlık": 8}},
        "Kova":    {"text": "Hem sistemi değiştirmek hem de sisteme ait olmak istiyorsunuz. Bu çelişki sizi yoracak.", "mood": "DEVRİMCİ KAFA", "energy": {"Özgünlük": 95, "Bağlılık": 20, "Vizyon": 90, "Pratiklik": 25}},
        "Balık":   {"text": "Neptün bugün gerçekle hayali birbirine karıştırıyor. Ve evet, toplantınız öğleden sonra. Gerçek bir toplantı.", "mood": "SİS İÇİNDE", "energy": {"Sezgi": 98, "Sınır": 8, "Yaratıcılık": 95, "Zamanlama": 15}},
    }
    return VIBES.get(sign, VIBES["Koç"])


if __name__ == "__main__":
    print("=" * 50)
    print("🔮 Zuhal Teyze Server başlıyor...")
    print("📍 http://localhost:5000")
    print("=" * 50)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
