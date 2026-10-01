# GetVeridataNow — roof leads, verified from above

Type in a zip code — get back a ranked list of residential roofs that need work.

**How it works**
1. Zip centroid lookup + jittered-grid reverse-geocoding (OpenStreetMap/Nominatim) finds real addresses in the zip.
2. High-res aerial tiles (Esri World Imagery) are pulled for each rooftop.
3. Gemini vision grades every roof on the **Roof Tru Scale (0–5)**:
   - **5** Solid · **4** Healthy · **3** Aging (watch) · **2** Worn (repair soon) · **1** Failing (replace now) · **0** Can't tell from the imagery
4. Results come back worst-first, each with the address, public location info, visual evidence, aerial photo, and Google Maps / Street View links.

**Run**
```
pip install -r requirements.txt
GEMINI_API_KEY=... python server.py
```

**Deploy (Railway)**: `Procfile` runs gunicorn. Set `GEMINI_API_KEY` in the service variables.

**Notes**
- Grades are AI estimates from aerial imagery — verify on site before quoting work.
- Address sampling respects Nominatim's usage policy (≤1 req/s).
- Imagery © Esri World Imagery · Addresses © OpenStreetMap contributors.
