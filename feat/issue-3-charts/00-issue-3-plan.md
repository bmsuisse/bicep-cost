Issue #3 — Cost breakdown charts

Add a dependency-free horizontal bar chart to text and Markdown estimate reports, sorted descending by monthly resource cost. Reuse report grouping for type/module text reports. Scale bars to largest amount, show monthly amount/currency and share of known total, retain table/details and unpriced-resource notices, handle zero totals without division. Machine-readable formats unchanged. Budget failure writes the chart before returning nonzero. Test ordering, scale, totals, zero/empty reports, grouping and budget-failure output. Capture before/after report previews for desktop and narrow widths; ask human to verify while continuing later issues.

Plan review (bms-plan-review, Python guidelines, ponytail): go. Presentation belongs in report.py; stdlib covers chart rendering, no chart library or web application required. Distinguish estimated known costs from incomplete pricing through existing notices.
