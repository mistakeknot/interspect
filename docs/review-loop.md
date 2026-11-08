# Review loop (weekly job)

Samples the BLOCK verdicts of one finished ISO week, has a judge on a different
lab label each finding, and reports bounds on the share of BLOCK receipts that
carried a confirmed warranted defect, plus backlog proposals per mechanism.
It files nothing and edits no policy: its output is a report that a person turns
into issues and pull requests.

## Steps

Each step reads and writes JSON files so a week replays byte for byte from its snapshot.

    scripts/review_loop/run.sh freeze --receipts DIR --week 2026-W40 --run-at 2026-10-25T00:00:00Z --out snap.json
    scripts/review_loop/run.sh sample --snapshot snap.json --out sample.json
    (judge each sampled PR: see review_loop/judge.py for the prompt and reply format)
    scripts/review_loop/run.sh aggregate --snapshot snap.json --sample sample.json --judgments j.json --report report.md --digest digest.txt

`freeze` refuses to run until 14 days after the week closes.

## Design

- A PR belongs to the week of its first review of any verdict. The frame is the
  PRs with at least one BLOCK inside the 14-day horizon. A PR is the sampling and
  analysis unit; its stratum is its first BLOCK's reviewer and producer.
- Budget 30 PRs; the whole frame is taken when it has 30 or fewer.
- Lower bound L = weighted confirmed / weighted sampled receipts (Hajek ratio).
  Upper bound U adds weighted unresolved receipts without a confirmed defect over
  the same denominator, so 0 <= L <= U <= 100%.
- The interval is a Rao-Wu rescaled bootstrap with finite-population correction,
  recomputing both bounds in each replicate. Under 8 sampled PRs only counts and
  bounds are printed.
- A backlog proposal needs 3 defects across 2 PRs for one mechanism, or one
  high-severity defect. A proposal whose change would relax a check is rejected.
- The report does not rank reviewer or author models.

## Tests

    pytest tests/review_loop
