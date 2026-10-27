# job-alerts

Checks big tech and quant career sites every 10 minutes for new data science,
ML/AI, data engineering, research, and quant internships (skipping PhD-only and
Master's-only roles) and sends a push notification through [ntfy](https://ntfy.sh).

* `check_jobs.py`: the checker. Edit the company lists or keyword patterns at the top.
* `.github/workflows/jobs.yml`: the 10-minute schedule.
* `seen.json`: jobs already sent (saved automatically).
* `status.json`: last check time and whether each site worked.

Test locally without sending anything: `DRY_RUN=1 python3 check_jobs.py`
