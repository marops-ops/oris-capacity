import os
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests

# ── Konfigurasjon ──────────────────────────────────────────────────────────────
API_BASE    = "https://api.orisdental.no/api"
OSLO_TZ     = ZoneInfo("Europe/Oslo")
WEEKS_AHEAD = 2 
OUTPUT_DIR  = os.path.join(os.path.dirname(__file__), "..", "docs")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "capacity.json")

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://booking.orisdental.no",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
}

def analyze():
    now = datetime.now(OSLO_TZ)
    cutoff = now + timedelta(weeks=WEEKS_AHEAD)
    token = os.environ.get("ORIS_BEARER_TOKEN")
    if token and not token.startswith("Bearer "): token = f"Bearer {token}"

    session = requests.Session()
    session.headers.update(HEADERS)
    if token: session.headers["Authorization"] = token

    print("→ Henter klinikkliste...")
    try:
        resp = session.get(f"{API_BASE}/clinicsandregions", timeout=20)
        # Vi fjerner 'published'-sjekken for å se om vi henter flere enn 19
        clinics = resp.json().get("clinics", [])
        print(f" debug: API returnerte {len(clinics)} klinikker.")
    except Exception as e:
        print(f"✗ Feil: {e}")
        return

    results = []
    for i, clinic in enumerate(clinics):
        opus_id = clinic.get("opus_id")
        name = clinic.get("name", "Ukjent")
        if not opus_id: continue

        try:
            time.sleep(0.3)
            s_resp = session.get(f"{API_BASE}/services", params={"clinic_id": opus_id, "from_date": now.strftime("%Y-%m-%dT00:00:00Z"), "to_date": now.strftime("%Y-%m-28T23:59:59Z")}, timeout=10)
            services = s_resp.json()
            if not isinstance(services, list): services = services.get("services", [])
            
            keywords = ["undersøk", "ny pas", "sjekk", "kontroll", "rutine"]
            s = next((s for s in services if any(k in s.get("name", "").lower() for k in keywords)), None)
            if not s and services: s = services[0]
            if not s: continue

            t_resp = session.get(f"{API_BASE}/timeslotmonth", params={"clinic_id": opus_id, "service_id": s['id'], "duration": s.get('duration', 40), "year": now.year, "month": now.month}, timeout=10)
            slots = t_resp.json().get("timeslots", [])

            time_slots = {}
            for sl in slots:
                dt = datetime.fromisoformat(sl["time_from"].replace("Z", "+00:00")).astimezone(OSLO_TZ)
                if now < dt <= cutoff:
                    key = dt.strftime("%Y-%m-%d %H:%M")
                    if key not in time_slots: time_slots[key] = set()
                    time_slots[key].add(sl.get("clinician_id"))

            free_hours = sum(len(c_ids) for c_ids in time_slots.values()) * 0.5
            signal = "BOOST" if free_hours >= 30 else ("MONITOR" if free_hours >= 15 else "PERFORMER")
            
            region = "Andre"
            if clinic.get("category_slugs"):
                region = clinic["category_slugs"][0].replace("-", " ").title()

            results.append({
                "name": name,
                "region": region,
                "free_hours": round(free_hours, 1),
                "signal": signal,
                "total_slots": int(free_hours)
            })
            print(f"✓ [{i+1}/{len(clinics)}] {name}: {free_hours}t")
        except: continue

    output = {
        "generated_at": now.isoformat(),
        "total_clinics": len(results),
        "summary": {
            "boost_count": sum(1 for r in results if r["signal"] == "BOOST"),
            "monitor_count": sum(1 for r in results if r["signal"] == "MONITOR"),
            "performer_count": sum(1 for r in results if r["signal"] == "PERFORMER")
        },
        "clinics": results
    }
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    analyze()