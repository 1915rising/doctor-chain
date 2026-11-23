# Doctor Chain report — avatar-preflight

- Input: `avatar.fbx`
- Chain verdict: **FAIL**
- Stopped at stage: **stl**
- Started: 2026-11-23 15:09:00 UTC · total 18.0s

| # | Stage | Status | Time | Verdict detail |
|---|---|---|---|---|
| 1 | ripdoctor | pass | 3.5s |  |
| 2 | cleanup | skipped | 0.0s | skipped in manifest / by --skip |
| 3 | viseme | pass | 8.4s | visemes_present=15, visemes_missing=0, key_count=57 |
| 4 | orient | pass | 2.7s | arrow_instances=15197 |
| 5 | stl | fail | 1.0s | stl-doctor verdict: FAIL (see report.json problems) |

## Stage 1 — ripdoctor (pass)
- in: `avatar.fbx`
- report (json): `01-ripdoctor/report.json`
- report (md): `01-ripdoctor/report.md`

## Stage 2 — cleanup (skipped)
- note: skipped in manifest / by --skip

## Stage 3 — viseme (pass)
- in: `avatar.fbx`
- out: `./03-viseme/viseme-fixed.fbx`
- report (json): `03-viseme/report.json`
- report (txt): `03-viseme/report.txt`

## Stage 4 — orient (pass)
- in: `./03-viseme/viseme-fixed.fbx`
- report (json): `04-orient/report.json`

## Stage 5 — stl (fail)
- in: `./03-viseme/viseme-fixed.fbx`
- report (stl): `05-stl/model.stl`
- report (json): `05-stl/report.json`
- note: stl-doctor verdict: FAIL (see report.json problems)

Reports per stage live beside this file; a failed stage's stdout/stderr and JSON are its evidence.
