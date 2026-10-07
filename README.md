# job-hunt-tools

`job_search_fetch.py` pulls job postings from public applicant-tracking-system endpoints (Workday, Oracle HCM, SmartRecruiters, Greenhouse, Lever, Ashby, Jibe, iCIMS, RSS, jobs.ac.uk, KCL), keeps London / UK-remote entry-level roles in clinical research, pharmacovigilance, medical writing and related fields, and drops anything already logged.

Stdlib only (needs `curl`).

```bash
curl -fsSL https://raw.githubusercontent.com/mijayz/job-hunt-tools/main/job_search_fetch.py -o jsf.py
python3 jsf.py --known known.json --out leads.json > survivors.txt 2> source_log.txt
```

`known.json` is a list of `{"url", "company", "role"}` for every posting already seen. `--only Medpace,IQVIA` runs a subset of sources.
