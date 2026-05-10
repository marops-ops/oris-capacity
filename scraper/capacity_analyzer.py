"""
Oris Dental – Capacity Analyzer
=================================
Logikk:
- Velger beste proxy-service per klinikk (undersøkelse/ny pasient først)
- Henter timeslots for de neste 14 dagene
- Teller råantall ledige slots
- Signal basert på absolutt antall ledige slots (ikke beleggsprosent)

Kjøres via GitHub Actions 2x daglig.
"""

import os
import json
import time
import calendar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests

# ── Konfigurasjon ──────────────────────────────────────────────────────────────
API_BASE   = "https://api.orisdental.no/api"
OSLO_TZ    = ZoneInfo("Europe/Oslo")
DAYS_AHEAD = 14
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "docs")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "capacity.json")

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://booking.orisdental.no",
    "Referer": "https://booking.orisdental.no/",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "Sec-Ch-Ua-Platform": '"macOS"',
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

# Keywords for undersøkelse/ny pasient — prioriteres
EXAMINATION_KEYWORDS = [
    "undersøk", "undersok", "ny pas", "ny-pas", "new pat",
    "kontroll", "rutine", "examination", "førstegangs",
    "first", "konsultasjon", "sjekk", "basis", "standard",
    "pas ", " pas", "^pas$",
]

# Services som ekskluderes fra proxy-valg (spesialist/prosedyre)
SPECIALIST_KEYWORDS = [
    "implantat", "implant", "kirurg", "ortodon", "invisalign",
    "kjeveort", "sting", "ceph", "cbct", "røntgen", "xray",
    "odontofobi", "spesialist", "rotfylling", "endodon",
    "pulp", "biopsi", "narkose", "sedering", "henvisning",
    "bleking", "whitening", "veneer", "fasett", "estetisk",
    "snorking", "søvn", "tannregulering", "retainer",
    "fjerning", "ekstraksjon", "visdom",
]


def get_bearer_token() -> str | None:
    token = os.environ.get("ORIS_BEARER_TOKEN", "").strip()
    if not token:
        print("✗ Mangler ORIS_BEARER_TOKEN")
        return None
    return token


def build_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Authorization"] = f"Bearer {token}"
    return session


def get_months_for_days(days: int) -> list[tuple[int, int]]:
    today   = datetime.now(OSLO_TZ)
    end     = today + timedelta(days=days)
    months  = set()
    current = today.replace(day=1)
    while current <= end:
        months.add((current.year, current.month))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return sorted(months)


def is_specialist(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in SPECIALIST_KEYWORDS)


def is_examination(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in EXAMINATION_KEYWORDS)


def pick_best_service(services: list[dict]) -> dict | None:
    """
    Prioritet:
    1. Undersøkelse/ny pasient-type (uansett varighet)
    2. Korteste ikke-spesialist service
    3. Korteste service totalt
    """
    # Filtrer ut spesialist-only
    general = [s for s in services if not is_specialist(s.get("name", ""))]

    # Prøv å finne undersøkelse/ny pasient
    examination = [s for s in general if is_examination(s.get("name", ""))]
    if examination:
        # Velg korteste blant undersøkelser
        return min(examination, key=lambda s: s.get("duration", 999))

    # Ingen undersøkelse funnet — bruk korteste generelle service
    if general:
        return min(general, key=lambda s: s.get("duration", 999))

    # Fallback: korteste av alt
    if services:
        return min(services, key=lambda s: s.get("duration", 999))

    return None


def compute_signal(free_slots: int) -> str:
    """
    Signal basert på råantall ledige slots de neste 14 dagene.
    Terskler kalibrert for én proxy-service per klinikk:
    - LAVT:    30+ ledige slots  → klinikken trenger flere pasienter
    - MIDDELS: 10-29 ledige slots → følg med
    - HØYT:    under 10 slots    → godt belagt
    """
    if free_slots >= 30:
        return "LAVT"
    if free_slots >= 10:
        return "MIDDELS"
    return "HØYT"


def analyze():
    now    = datetime.now(OSLO_TZ)
    cutoff = now + timedelta(days=DAYS_AHEAD)
    months = get_months_for_days(DAYS_AHEAD)

    print(f"\n{'='*60}")
    print(f"  Oris Dental – Capacity Analyzer")
    print(f"  Kjøretid: {now.strftime('%Y-%m-%d %H:%M')} Oslo")
    print(f"  Analyserer: {DAYS_AHEAD} dager fremover")
    print(f"{'='*60}\n")

    token = get_bearer_token()
    if not token:
        return

    session = build_session(token)

    print("→ Henter klinikkliste...")
    try:
        resp    = session.get(f"{API_BASE}/clinicsandregions", timeout=20)
        resp.raise_for_status()
        clinics = [
            c for c in resp.json().get("clinics", [])
            if c.get("published") and c.get("opus_id")
        ]
        print(f"✓ {len(clinics)} klinikker med online booking\n")
    except Exception as e:
        print(f"✗ Kunne ikke hente klinikker: {e}")
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
            # 1. Hent services
            time.sleep(2.0)
            s_resp = session.get(
                f"{API_BASE}/services",
                params={
                    "clinic_id": opus_id,
                    "from_date": now.strftime("%Y-%m-%dT00:00:00Z"),
                    "to_date":   cutoff.strftime("%Y-%m-%dT23:59:59Z"),
                },
                timeout=15,
            )
            s_resp.raise_for_status()
            services = s_resp.json()
            if not isinstance(services, list):
                services = services.get("services", [])

            if not services:
                print(f"  ⚠ Ingen services")
                continue

            # 2. Velg beste proxy-service
            service = pick_best_service(services)
            if not service:
                print(f"  ⚠ Ingen passende service")
                continue

            print(f"  → '{service.get('name')}' ({service.get('duration')} min)")

            # 3. Hent timeslots
            all_slots = []
            for year, month in months:
                time.sleep(2.0)
                try:
                    t_resp = session.get(
                        f"{API_BASE}/timeslotmonth",
                        params={
                            "clinic_id":  opus_id,
                            "service_id": service["id"],
                            "duration":   service.get("duration", 30),
                            "year":       year,
                            "month":      month,
                        },
                        timeout=15,
                    )
                    t_resp.raise_for_status()
                    data = t_resp.json()
                    all_slots.extend(data.get("timeslots", []))
                except requests.exceptions.HTTPError as e:
                    if e.response.status_code not in (400, 404):
                        print(f"  ⚠ HTTP {e.response.status_code}")
                except Exception as e:
                    print(f"  ⚠ {e}")

            # 4. Filtrer til analysevinduet
            valid_slots = [
                s for s in all_slots
                if now < datetime.fromisoformat(
                    s["time_from"].replace("Z", "+00:00")
                ).astimezone(OSLO_TZ) <= cutoff
            ]

            free_slots  = len(valid_slots)
            duration    = service.get("duration", 30)
            free_hours  = round(free_slots * duration / 60, 1)
            signal      = compute_signal(free_slots)

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
            print(f"  ✗ Feil: {e}")
            continue

    # Sorter: LAVT øverst, deretter MIDDELS, så HØYT
    signal_order = {"LAVT": 0, "MIDDELS": 1, "HØYT": 2}
    results.sort(key=lambda r: (signal_order.get(r["signal"], 3), -r["free_slots"]))

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
    print(f"  ✓ {len(results)} klinikker lagret")
    print(f"  LAVT: {output['summary']['lavt_count']} | "
          f"MIDDELS: {output['summary']['middels_count']} | "
          f"HØYT: {output['summary']['hoyt_count']}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    analyze()
