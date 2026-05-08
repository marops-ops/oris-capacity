# Oris Dental — Kapasitetsanalyseverktøy

Automatisk kapasitets- og etterspørselsanalyse for Oris Dentals 90+ klinikker.
Kjøres 2x daglig via GitHub Actions og eksponeres som et statisk dashboard på Vercel.

## Arkitektur

```
GitHub Actions (06:00 + 14:00 Oslo)
    → scraper/capacity_analyzer.py
    → docs/capacity.json  (committes til repo)
    
Vercel (statisk hosting)
    → frontend/index.html  (leser capacity.json direkte)
```

## Oppsett

### 1. Klone og opprett repo

```bash
git clone <this-repo> oris-capacity
cd oris-capacity
git remote set-url origin https://github.com/DITT_BRUKERNAVN/oris-capacity.git
git push -u origin main
```

### 2. Legg til GitHub Secret

I GitHub repo → Settings → Secrets → Actions:

```
ORIS_BEARER_TOKEN = <token fra booking.orisdental.no>
```

Token hentes manuelt første gang via Playwright (kjør `capacity_analyzer.py` lokalt).
GitHub Actions bruker Playwright automatisk for token-refresh.

### 3. Deploy til Vercel

```bash
npm i -g vercel
vercel --prod
```

Eller koble GitHub-repo direkte i Vercel-dashboardet (anbefalt — auto-deploy ved push).

### 4. Tillatte e-postdomener

Rediger `ALLOWED_DOMAINS` i `frontend/index.html` (linje ~220):

```js
const ALLOWED_DOMAINS = ['amidays.no', 'orisdental.no', 'oris.no'];
```

Legg til eller fjern domener etter behov.

## Manuell kjøring

```bash
cd scraper
pip install -r requirements.txt
playwright install chromium
python capacity_analyzer.py
```

Genererer `docs/capacity.json`. Åpne `frontend/index.html` direkte i nettleseren for å teste.

## Filstruktur

```
oris-capacity/
├── scraper/
│   ├── capacity_analyzer.py   ← Hoved-scraper (kjøres av GitHub Actions)
│   └── requirements.txt
├── docs/
│   └── capacity.json          ← Auto-generert data (commit av Actions)
├── frontend/
│   └── index.html             ← Dashboard (statisk, ingen build-steg)
├── .github/workflows/
│   └── fetch.yml              ← GitHub Actions workflow
├── vercel.json                ← Vercel routing-konfig
└── README.md
```

## Signallogikk

| Signal | Kriterier | Anbefaling |
|--------|-----------|------------|
| 🔴 BOOST | ≥30 ledige timer + <25% er i uke 1–2 | Øk betalt mediespend |
| 🟡 MONITOR | Middels tilgjengelighet | Følg med |
| 🟢 PERFORMER | <10 ledige timer ELLER ≥50% er i uke 1–2 | Medietrykk ikke nødvendig |
