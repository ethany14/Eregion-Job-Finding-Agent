# Resume generation regression checks

The ordinary Python suite includes tests for joined graduation dates, grouped legacy
education evidence, preferred names, metadata-only education, source ownership,
provider failures, bounded revision attempts, repeated requests, and progress-only
agent loops. It uses synthetic data and isolated temporary databases.

```sh
python -m pytest tests/test_resume_education_recovery.py tests/test_application_pack.py tests/test_content_quality.py tests/test_resume_source_scope.py tests/test_task_loop.py tests/test_career_flow.py tests/test_material_routes.py -q
```

## Real model test

Configure the normal `LLM_*` settings in `.env`, then explicitly enable the live test:

```sh
JOB_AGENT_LIVE_RESUME_GENERATION=1 python -m pytest tests/test_resume_generation_live.py -q -s
```

This sends synthetic resumes to the configured model provider and can incur API
charges. It does not open the user's application database. The three cases cover
joined graduation dates, an already-imported grouped education section, and date
ranges. Each runs the production writer and verifier, checks preserved education
metadata, renders and extracts the PDF, and confirms a repeated generation request
creates no new version. The normal one-revision limit applies; failed provider calls
fail the test instead of being retried indefinitely.

A live run on November 4, 2026 with `muse-spark-1.3-contributor` passed all three cases:

| Case | Seconds | Revisions | Result |
| --- | ---: | ---: | --- |
| Joined dates | 71.43 | 1 | Verified, PDF exported |
| Legacy grouped education | 43.84 | 0 | Verified, PDF exported |
| Date ranges | 41.33 | 0 | Verified, PDF exported |

These results validate that run, not provider availability or every possible model
response. Offline tests also check that writer timeouts and verifier connection
errors terminate visibly, preserve existing drafts, and never mark failures ready.
