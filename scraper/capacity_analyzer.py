import os
import json
import time
import calendar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests

# ── Konfigurasjon ──────────────────────────────────────────────────────────────
API_BASE    = "https://api.orisdental.no/api"
OSLO_TZ     = ZoneInfo("Europe/Oslo")
WEEKS_AHEAD = 2  # Vi ser på de neste 2 ukene for markedsføringstrykk
OUTPUT_DIR  = os.path.join(os.path.dirname(__file__), "..", "docs")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "capacity.json")

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://booking.orisdental.no",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
}

# ── Auth & API ─────────────────────────────────────────────────────────────────
def get_bearer_token():
    token = os.environ.get("ORIS_BEARER_TOKEN")
    if token and not token.startswith("Bearer "):
        token = f"Bearer {token}"
    return token

def build_session(token):
    session = requests.Session()
    session.headers.update(HEADERS)
    if token:
        session.headers["Authorization"] = token
    return session

def fetch_clinics(session):
    print("→ Henter klinikker...")
    try:
        resp = session.get(f"{API_BASE}/clinicsandregions", timeout=15)
        resp.raise_for_status()
        return [c for c in resp.json().get("clinics", []) if c.get("published")]
    except:
        return [{"opus_id": "641-004488bd-8e85-4639-85ba-bc0dccde0864", "name": "Aker Brygge", "slug": "oslo-aker-brygge"}]

def fetch_services(session, opus_id):
    today = datetime.now(OSLO_TZ)
    try:
        resp = session.get(f"{API_BASE}/services", params={"clinic_id": opus_id, "from_date": today.strftime("%Y-%m-%dT00:00:00Z"), "to_date": today.strftime("%Y-%m-28T23:59:59Z")}, timeout=15)
        return resp.json() if isinstance(resp.json(), list) else resp.json().get("services", [])
    except: return []

def fetch_timeslots(session, opus_id, service_id, duration, year, month):
    try:
        resp = session.get(f"{API_BASE}/timeslotmonth", params={"clinic_id": opus_id, "service_id": service_id, "duration": duration, "year": year, "month": month}, timeout=15)
        return resp.json().get("timeslots", [])
    except: return []

# ── Business Logic ─────────────────────────────────────────────────────────────
def analyze():
    now = datetime.now(OSLO_TZ)
    cutoff = now + timedelta(weeks=WEEKS_AHEAD)
    
    token = get_bearer_token()
    session = build_session(token)
    clinics = fetch_clinics(session)
    
    print(f"\nAnalyserer {len(clinics)} klinikker for ledig kapasitet (man-hours)...\n")
    results = []

    for clinic in clinics:
        opus_id = clinic.get("opus_id")
        if not opus_id: continue
        
        # Finn 'Undersøkelse' som vår målestokk for kapasitet
        services = fetch_services(session, opus_id)
        s = next((s for s in services if "undersøkelse" in s.get("name", "").lower() or "ny pas" in s.get("name", "").lower()), None)
        if not s: continue

        # Hent slots for inneværende og neste måned
        all_slots = []
        for m_offset in [0, 1]:
            check_date = now + timedelta(days=30 * m_offset)
            all_slots.extend(fetch_timeslots(session, opus_id, s['id'], 40, check_date.year, check_date.month))
            time.sleep(0.3)

        # Map: Hvor mange tannleger er ledige per 30-min blokk?
        time_slots = {}
        for sl in all_slots:
            dt = datetime.fromisoformat(sl["time_from"].replace("Z", "+00:00")).astimezone(OSLO_TZ)
            if now < dt <= cutoff:
                # Vi runder til 30 min bolker
                key = dt.strftime("%Y-%m-%d %H:%M")
                if key not in time_slots: time_slots[key] = set()
                time_slots[key].add(sl.get("clinician_id"))

        # Beregn totalt antall ledige timer (hver behandler-slot er 0.5 time)
        total_free_hours = sum(len(c_ids) for c_ids in time_slots.values()) * 0.5
        
        # Beregn trend (timer per dag)
        daily = {}
        for k, v in time_slots.items():
            day = k.split(" ")[0]
            daily[day] = daily.get(day, 0) + (len(v) * 0.5)

        # Signal-logikk
        signal = "PERFORMER"
        if total_free_hours > 40: signal = "BOOST"
        elif total_free_hours > 20: signal = "MONITOR"

        print(f"✓ {clinic['name']}: {total_free_hours:.1f} timer ledig")

        results.append({
            "name": clinic['name'],
            "slug": clinic.get("slug"),
            "free_hours": round(total_free_hours, 1),
            "signal": signal,
            "daily_trend": daily
        })

    # Sorter etter mest ledig kapasitet (hvor vi trenger marketing)
    results.sort(key=lambda x: x["free_hours"], reverse=True)
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump({"generated_at": now.isoformat(), "clinics": results}, f, indent=2)
    print(f"\n✓ Ferdig! JSON lagret i docs/capacity.json")

if __name__ == "__main__":
    analyze()