"""
Oris Dental – Capacity & Demand Analyzer
=========================================
Henter alle services og timeslots per klinikk for de neste 8 ukene.
Genererer docs/capacity.json brukt av frontend-dashboardet.

Kjøres via GitHub Actions 1-2x daglig.
Rate limit: 60 kall/min → sleep(1) mellom hvert kall.
"""

import os
import json
import time
import calendar
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ── Konfigurasjon ──────────────────────────────────────────────────────────────

API_BASE   = "https://api.orisdental.no/api"
OSLO_TZ    = ZoneInfo("Europe/Oslo")
WEEKS_AHEAD = 8
OUTPUT_DIR  = os.path.join(os.path.dirname(__file__), "..", "docs")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "capacity.json")

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "nb-NO,nb;q=0.9,no;q=0.8,en-US;q=0.6",
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

# ── Auth (gjenbrukt fra scraper.py) ───────────────────────────────────────────

def get_bearer_token() -> str | None:
    env_token = os.environ.get("ORIS_BEARER_TOKEN")
    if env_token:
        print("✓ Bearer-token fra environment variable")
        return env_token

    if not PLAYWRIGHT_AVAILABLE:
        print("⚠ Playwright ikke installert – sett ORIS_BEARER_TOKEN som env var")
        return None

    print("→ Henter Bearer-token via Playwright...")
    token = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            page = browser.new_context(
                user_agent=HEADERS["User-Agent"], locale="nb-NO"
            ).new_page()

            def intercept(request):
                nonlocal token
                if token:
                    return
                auth = request.headers.get("authorization", "")
                if auth.startswith("Bearer ") and "api.orisdental.no" in request.url:
                    token = auth.replace("Bearer ", "").strip()
                    print(f"✓ Token fanget (lengde: {len(token)})")

            page.on("request", intercept)
            page.goto("https://booking.orisdental.no/", wait_until="networkidle", timeout=30000)

            if not token:
                page.wait_for_timeout(3000)
            if not token:
                try:
                    page.wait_for_selector("button", timeout=5000)
                    page.query_selector_all("button")[0].click()
                    page.wait_for_timeout(2000)
                except Exception:
                    pass

            browser.close()

    except Exception as e:
        print(f"⚠ Playwright-feil: {e}")

    return token


def build_session(token: str | None) -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    return session


# ── API-funksjoner ─────────────────────────────────────────────────────────────

def fetch_clinics(session: requests.Session) -> list[dict]:
    print("→ Henter klinikker...")
    try:
        resp = session.get(f"{API_BASE}/clinicsandregions", timeout=15)
        resp.raise_for_status()
        clinics = [c for c in resp.json().get("clinics", []) if c.get("published")]
        print(f"✓ {len(clinics)} publiserte klinikker")
        return clinics
    except Exception as e:
        print(f"✗ fetch_clinics: {e}")
        return []


def fetch_services(session: requests.Session, opus_id: str) -> list[dict]:
    """Henter alle tilgjengelige services for en klinikk."""
    today = datetime.now(OSLO_TZ)
    last_day = calendar.monthrange(today.year, today.month)[1]

    time.sleep(1)
    try:
        resp = session.get(
            f"{API_BASE}/services",
            params={
                "clinic_id": opus_id,
                "from_date": today.strftime("%Y-%m-%dT00:00:00Z"),
                "to_date":   today.strftime(f"%Y-%m-{last_day:02d}T23:59:59Z"),
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("services", [])
        return []
    except Exception as e:
        print(f"  ⚠ Services-feil ({opus_id}): {e}")
        return []


def fetch_timeslots_for_months(
    session: requests.Session,
    opus_id: str,
    service_id: str,
    duration: int,
    months: list[tuple[int, int]],
) -> list[dict]:
    """Henter timeslots for en gitt service over en liste av (year, month)-tupler."""
    all_slots = []
    for year, month in months:
        time.sleep(1)
        try:
            resp = session.get(
                f"{API_BASE}/timeslotmonth",
                params={
                    "clinic_id":  opus_id,
                    "service_id": service_id,
                    "duration":   duration,
                    "year":       year,
                    "month":      month,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            slots = data.get("timeslots", [])
            all_slots.extend(slots)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code not in (404, 400):
                print(f"  ⚠ HTTP {e.response.status_code} ({opus_id}, service {service_id}, {year}-{month:02d})")
        except Exception as e:
            print(f"  ⚠ Timeslots-feil: {e}")
    return all_slots


# ── Hjelpefunksjoner ───────────────────────────────────────────────────────────

def get_months_for_weeks(weeks: int) -> list[tuple[int, int]]:
    """Returnerer unike (year, month)-tupler som dekker de neste N ukene."""
    today = datetime.now(OSLO_TZ)
    end   = today + timedelta(weeks=weeks)
    months = set()
    current = today.replace(day=1)
    while current <= end:
        months.add((current.year, current.month))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return sorted(months)


def week_number_from_now(iso_str: str, now: datetime) -> int:
    """Returnerer hvilken uke fremover en slot er (0 = denne uken, 1 = neste uke, osv.)"""
    slot_time = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(OSLO_TZ)
    delta_days = (slot_time - now).days
    return max(0, delta_days // 7)


def categorize_service(name: str) -> str:
    """Grovkategoriserer en service basert på navn."""
    name_lower = name.lower()
    if any(k in name_lower for k in ["undersøk", "undersok", "rutine", "kontroll", "ny pas", "new pat", "examination"]):
        return "Undersøkelse"
    if any(k in name_lower for k in ["implantat", "implant"]):
        return "Implantat"
    if any(k in name_lower for k in ["reguler", "kjeveort", "ortodon", "braces", "invisalign"]):
        return "Tannregulering"
    if any(k in name_lower for k in ["bleking", "whitening", "estet", "composite", "veneer", "fasett"]):
        return "Estetikk"
    if any(k in name_lower for k in ["akutt", "smerte", "emergency"]):
        return "Akutt"
    if any(k in name_lower for k in ["kirurg", "trekk", "ekstraksjon", "visdom"]):
        return "Kirurgi"
    if any(k in name_lower for k in ["rens", "hygien", "periodont", "tannstein"]):
        return "Hygiene"
    if any(k in name_lower for k in ["rot", "endodon", "pulp"]):
        return "Rotfylling"
    return "Annet"


def compute_signal(slots_week_1_2: int, slots_week_5_8: int, total_slots: int) -> str:
    """
    Beregner budsjettanbefalingssignal.
    
    Logikk:
    - 🔴 BOOST: Høy total tilgjengelighet + lite booking pressure (mange slots langt frem)
    - 🟡 MONITOR: Middels
    - 🟢 PERFORMER: Lav tilgjengelighet / fyller raskt
    """
    if total_slots == 0:
        return "INGEN_DATA"

    # Booking pressure: andel slots som er i uke 1-2 vs. uke 5-8
    # Høy andel nær = fyller raskt = performer
    near_ratio = slots_week_1_2 / total_slots if total_slots > 0 else 0

    if total_slots >= 30 and near_ratio < 0.25:
        return "BOOST"       # 🔴 Mange ledige slots, spesielt langt frem
    if total_slots >= 15 and near_ratio < 0.40:
        return "MONITOR"     # 🟡
    if total_slots < 10 or near_ratio >= 0.50:
        return "PERFORMER"   # 🟢 Fyller raskt
    return "MONITOR"


# ── Hovedfunksjon ──────────────────────────────────────────────────────────────

def analyze():
    now    = datetime.now(OSLO_TZ)
    months = get_months_for_weeks(WEEKS_AHEAD)
    cutoff = now + timedelta(weeks=WEEKS_AHEAD)

    print(f"\n{'='*60}")
    print(f"  Oris Dental – Capacity Analyzer")
    print(f"  Kjøretid: {now.strftime('%Y-%m-%d %H:%M')} Oslo")
    print(f"  Analyserer: {WEEKS_AHEAD} uker fremover ({len(months)} måneder)")
    print(f"{'='*60}\n")

    token   = get_bearer_token()
    session = build_session(token)
    clinics = fetch_clinics(session)

    results = []
    total_api_calls = 0

    for i, clinic in enumerate(clinics):
        opus_id    = clinic.get("opus_id")
        clinic_name = clinic.get("name", "Ukjent")
        slug        = clinic.get("slug", "")
        region      = ""
        if clinic.get("category_slugs"):
            region = clinic["category_slugs"][0].replace("-", " ").title()

        print(f"\n[{i+1}/{len(clinics)}] {clinic_name}")

        if not opus_id:
            print(f"  ⏭ Ingen opus_id – hopper over")
            continue

        # Hent alle services
        services = fetch_services(session, opus_id)
        total_api_calls += 1

        if not services:
            print(f"  ⚠ Ingen services")
            continue

        print(f"  → {len(services)} services funnet")

        # Per-service slot-analyse
        service_data     = []
        all_clinic_slots = []

        for service in services:
            service_id   = service.get("id")
            service_name = service.get("name") or service.get("display_name", "Ukjent")
            duration     = service.get("duration", 40)
            category     = categorize_service(service_name)

            slots = fetch_timeslots_for_months(session, opus_id, service_id, duration, months)
            total_api_calls += len(months)

            # Filtrer til analysevindut
            valid_slots = [
                s for s in slots
                if datetime.fromisoformat(s["time_from"].replace("Z", "+00:00")).astimezone(OSLO_TZ) <= cutoff
                and datetime.fromisoformat(s["time_from"].replace("Z", "+00:00")).astimezone(OSLO_TZ) > now
            ]

            # Tell slots per uke-bucket
            week_buckets = {w: 0 for w in range(WEEKS_AHEAD)}
            for slot in valid_slots:
                w = week_number_from_now(slot["time_from"], now)
                if w < WEEKS_AHEAD:
                    week_buckets[w] = week_buckets.get(w, 0) + 1

            slots_w1_2 = sum(week_buckets.get(w, 0) for w in range(2))
            slots_w5_8 = sum(week_buckets.get(w, 0) for w in range(4, 8))

            service_data.append({
                "service_id":   str(service_id),
                "service_name": service_name,
                "category":     category,
                "duration_min": duration,
                "total_slots":  len(valid_slots),
                "slots_week_1_2": slots_w1_2,
                "slots_week_5_8": slots_w5_8,
                "week_buckets": week_buckets,
            })

            all_clinic_slots.extend(valid_slots)
            print(f"  ✓ {service_name}: {len(valid_slots)} ledige timer")

        # Aggregert per klinikk
        total_slots  = sum(s["total_slots"] for s in service_data)
        slots_w1_2   = sum(s["slots_week_1_2"] for s in service_data)
        slots_w5_8   = sum(s["slots_week_5_8"] for s in service_data)
        signal       = compute_signal(slots_w1_2, slots_w5_8, total_slots)

        # Mest underbookede behandlingstyper (høyest slot-andel langt frem)
        underboooked = sorted(
            [s for s in service_data if s["total_slots"] > 0],
            key=lambda s: s["slots_week_5_8"] / max(s["total_slots"], 1),
            reverse=True,
        )[:3]

        # Aggregert uke-trend på tvers av alle services
        aggregated_weeks = {}
        for s in service_data:
            for w, count in s["week_buckets"].items():
                aggregated_weeks[str(w)] = aggregated_weeks.get(str(w), 0) + count

        results.append({
            "clinic_id":       str(clinic.get("id", "")),
            "opus_id":         opus_id,
            "name":            clinic_name,
            "slug":            slug,
            "region":          region,
            "city":            clinic.get("city", ""),
            "total_slots":     total_slots,
            "slots_week_1_2":  slots_w1_2,
            "slots_week_5_8":  slots_w5_8,
            "signal":          signal,
            "week_trend":      aggregated_weeks,
            "services":        service_data,
            "top_underboooked": [
                {"name": s["service_name"], "category": s["category"], "slots": s["total_slots"]}
                for s in underboooked
            ],
        })

        print(f"  → Signal: {signal} | Totalt: {total_slots} ledige timer")

    # Sorter: BOOST øverst, deretter MONITOR, så PERFORMER
    signal_order = {"BOOST": 0, "MONITOR": 1, "PERFORMER": 2, "INGEN_DATA": 3}
    results.sort(key=lambda r: (signal_order.get(r["signal"], 3), -r["total_slots"]))

    output = {
        "generated_at":    now.isoformat(),
        "weeks_analyzed":  WEEKS_AHEAD,
        "total_clinics":   len(results),
        "api_calls_made":  total_api_calls,
        "clinics":         results,
        "summary": {
            "boost_count":     sum(1 for r in results if r["signal"] == "BOOST"),
            "monitor_count":   sum(1 for r in results if r["signal"] == "MONITOR"),
            "performer_count": sum(1 for r in results if r["signal"] == "PERFORMER"),
            "no_data_count":   sum(1 for r in results if r["signal"] == "INGEN_DATA"),
        },
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"  ✓ capacity.json skrevet: {len(results)} klinikker")
    print(f"  📊 BOOST: {output['summary']['boost_count']} | "
          f"MONITOR: {output['summary']['monitor_count']} | "
          f"PERFORMER: {output['summary']['performer_count']}")
    print(f"  🔌 API-kall totalt: {total_api_calls}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    analyze()
