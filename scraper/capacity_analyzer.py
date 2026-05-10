"""
Oris Dental – Capacity Analyzer
"""

import os
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests

API_BASE    = "https://api.orisdental.no/api"
OSLO_TZ     = ZoneInfo("Europe/Oslo")
DAYS_AHEAD  = 14
OUTPUT_DIR  = os.path.join(os.path.dirname(__file__), "..", "docs")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "capacity.json")

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://booking.orisdental.no",
    "Referer": "https://booking.orisdental.no/",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "Sec-Ch-Ua-Platform": '"macOS"',
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

EXCLUDE = [
    "implantat", "implant", "kirurg", "ortodon", "invisalign", "kjeveort",
    "sting", "ceph", "cbct", "røntgen", "xray", "odontofobi", "spesialist",
    "rotfylling", "endodon", "pulp", "biopsi", "narkose", "sedering",
    "henvisning", "bleking", "whitening", "veneer", "fasett", "estetisk",
    "snorking", "søvn", "tannregulering", "retainer", "ekstraksjon", "visdom",
]

def get_bearer_token():
    token = os.environ.get("ORIS_BEARER_TOKEN", "").strip()
    if not token:
        print("✗ Mangler ORIS_BEARER_TOKEN")
        return None
    return token

def build_session(token):
    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Authorization"] = f"Bearer {token}"
    return session

def get_months(days):
    today   = datetime.now(OSLO_TZ)
    end     = today + timedelta(days=days)
    months  = set()
    current = today.replace(day=1)
    while current <= end:
        months.add((current.year, current.month))
        current = current.replace(month=current.month+1) if current.month < 12 else current.replace(year=current.year+1, month=1)
    return sorted(months)

def is_excluded(name):
    n = name.lower()
    return any(k in n for k in EXCLUDE)

def pick_service(services):
    general = [s for s in services if not is_excluded(s.get("name", ""))]
    if general:
        return min(general, key=lambda s: s.get("duration", 999))
    return min(services, key=lambda s: s.get("duration", 999)) if services else None

def compute_signal(free_hours):
    """Signal basert på ledige timer — samme logikk som Gemini."""
    if free_hours >= 30:
        return "LAVT"
    if free_hours >= 10:
        return "MIDDELS"
    return "HØYT"

def analyze():
    now    = datetime.now(OSLO_TZ)
    cutoff = now + timedelta(days=DAYS_AHEAD)
    months = get_months(DAYS_AHEAD)

    print(f"\n{'='*60}")
    print(f"  Oris Dental – Capacity Analyzer")
    print(f"  {now.strftime('%Y-%m-%d %H:%M')} Oslo | {DAYS_AHEAD} dager fremover")
    print(f"{'='*60}\n")

    token = get_bearer_token()
    if not token:
        return

    session = build_session(token)

    print("→ Henter klinikkliste...")
    try:
        resp    = session.get(f"{API_BASE}/clinicsandregions", timeout=20)
        resp.raise_for_status()
        clinics = [c for c in resp.json().get("clinics", []) if c.get("published") and c.get("opus_id")]
        print(f"✓ {len(clinics)} klinikker\n")
    except Exception as e:
        print(f"✗ {e}")
        return

    results = []

    for i, clinic in enumerate(clinics):
        name    = clinic.get("name", "Ukjent")
        opus_id = clinic.get("opus_id")
        region  = "Andre"
        if clinic.get("category_slugs"):
            region = clinic["category_slugs"][0].replace("-", " ").title()

        print(f"[{i+1}/{len(clinics)}] {name}")

        try:
            time.sleep(2.0)
            s_resp = session.get(f"{API_BASE}/services", params={
                "clinic_id": opus_id,
                "from_date": now.strftime("%Y-%m-%dT00:00:00Z"),
                "to_date":   cutoff.strftime("%Y-%m-%dT23:59:59Z"),
            }, timeout=15)
            s_resp.raise_for_status()
            services = s_resp.json()
            if not isinstance(services, list):
                services = services.get("services", [])

            if not services:
                print(f"  ⚠ Ingen services")
                continue

            service = pick_service(services)
            if not service:
                continue

            print(f"  → '{service.get('name')}' ({service.get('duration')} min)")

            all_slots = []
            for year, month in months:
                time.sleep(2.0)
                try:
                    t_resp = session.get(f"{API_BASE}/timeslotmonth", params={
                        "clinic_id":  opus_id,
                        "service_id": service["id"],
                        "duration":   service.get("duration", 30),
                        "year":       year,
                        "month":      month,
                    }, timeout=15)
                    t_resp.raise_for_status()
                    all_slots.extend(t_resp.json().get("timeslots", []))
                except Exception:
                    pass

            valid_slots = [
                s for s in all_slots
                if now < datetime.fromisoformat(s["time_from"].replace("Z", "+00:00")).astimezone(OSLO_TZ) <= cutoff
            ]

            free_slots = len(valid_slots)
            free_hours = round(free_slots * service.get("duration", 30) / 60, 1)
            signal     = compute_signal(free_hours)

            results.append({
                "name":         name,
                "region":       region,
                "city":         clinic.get("city", ""),
                "slug":         clinic.get("slug", ""),
                "free_slots":   free_slots,
                "free_hours":   free_hours,
                "signal":       signal,
                "service_used": service.get("name", ""),
            })

            print(f"  ✓ {free_slots} slots | {free_hours}t | {signal}")

        except Exception as e:
            print(f"  ✗ {e}")
            continue

    signal_order = {"LAVT": 0, "MIDDELS": 1, "HØYT": 2}
    results.sort(key=lambda r: (signal_order.get(r["signal"], 3), -r["free_hours"]))

    output = {
        "generated_at":  now.isoformat(),
        "days_analyzed": DAYS_AHEAD,
        "total_clinics": len(results),
        "summary": {
            "lavt_count":    sum(1 for r in results if r["signal"] == "LAVT"),
            "middels_count": sum(1 for r in results if r["signal"] == "MIDDELS"),
            "hoyt_count":    sum(1 for r in results if r["signal"] == "HØYT"),
        },
        "clinics": results,
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"  ✓ {len(results)} klinikker | LAVT: {output['summary']['lavt_count']} | MIDDELS: {output['summary']['middels_count']} | HØYT: {output['summary']['hoyt_count']}")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    analyze()
