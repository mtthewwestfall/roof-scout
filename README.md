# Roof Scout — roof leads, verified from above

Live at **https://getveridatenow.com**.

Type in a zip code — get back a ranked list of residential roofs that need work.

**How it works**
1. Zip centroid lookup + jittered-grid reverse-geocoding (OpenStreetMap/Nominatim) finds real addresses in the zip.
2. High-res aerial tiles (Esri World Imagery, then USGS NAIP) are pulled for each rooftop. With no aerial view, a street-level photo is used instead: Mapillary (`MAPILLARY_ACCESS_TOKEN`), then Google Street View (`GOOGLE_MAPS_API_KEY`). Each is skipped if its key is unset.
3. Gemini vision grades every roof on the **Roof Tru Scale (0–5)**:
   - **5** Solid · **4** Healthy · **3** Aging (watch) · **2** Worn (repair soon) · **1** Failing (replace now) · **0** Can't tell from the imagery
4. Results come back worst-first, each with the address, public location info, visual evidence, aerial photo, and Google Maps / Street View links.

**Run**
```
pip install -r requirements.txt
GEMINI_API_KEY=... python server.py
```

**Deploy (Railway)**: `Procfile` runs gunicorn. Service variables:
- `GEMINI_API_KEY` (required) — roof grading
- `DB_PATH` — SQLite path on the mounted volume (defaults to `/data/roofscout.db` when `/data` exists)
- `ADMIN_PASSWORD` (optional) — password gate for `/admin`; the owner account can always reach it after logging in
- `RESEND_API_KEY` (required for signup) — sends the email-verification link via Resend
- `MAIL_FROM` — sender shown on verification emails, e.g. `Roof Scout <support@lockeddoor.ai>`
- `PUBLIC_APP_URL` — public base URL used in verification links, e.g. `https://getveridatenow.com`

**Domain**: `getveridatenow.com` is a Railway custom domain on the `roof-scout` service. DNS lives at Porkbun:
- `ALIAS @ → <railway target>.up.railway.app` (Porkbun has no apex CNAME; use ALIAS)
- `TXT _railway-verify → railway-verify=…` (ownership token shown in Railway → Settings → Networking)

**Notes**
- Grades are AI estimates from aerial imagery — verify on site before quoting work.
- Address sampling respects Nominatim's usage policy (≤1 req/s).
- Imagery © Esri World Imagery, USGS, Mapillary contributors (CC BY-SA), Google · Addresses © OpenStreetMap contributors.
- Google Street View photos are never written to the scan cache; cached leads keep only the Street View link.
