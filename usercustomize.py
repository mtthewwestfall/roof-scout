"""Load RoofScout's strict Grade 1 quality-control wrapper after sitecustomize."""
try:
    import grade1_qc
except Exception as _grade1_qc_error:
    print(f"Roof Scout Grade 1 QC skipped: {_grade1_qc_error}", flush=True)
