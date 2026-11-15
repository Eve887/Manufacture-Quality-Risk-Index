# Methods note

**Wording rule:** results are described as based on public CDSCO regulatory records. Counts are raw failures, not rates.

## 1. Data
- NSQ and spurious alert tables downloaded from the CDSCO online NSQ portal (https://cdscoonline.gov.in/CDSCO/viewPublicNSQDrug), 6956 NSQ rows and 49 spurious rows.
- Reporting source spellings were standardised to two values: CDSCO lab and State lab.

## 2. Spurious alerts
- Spurious alerts are kept in a separate column and never added to a factory's NSQ count. The company named on a spurious product's label may not have made it. Most spurious rows have no manufacturer ("Under Investigation") and are not attached to any factory.

## 3. Splitting name and address, and matching spellings
- The text in the "manufactured by" field was split into company name and address with rules (company words such as Pharma, Labs, Ltd; PIN codes; address words). Rows the rules could not split are in manufacturer_needs_review.csv (95 rows), placeholders ("Under Investigation", "Not applicable") in rows_no_manufacturer.csv (68 rows).
- Spelling variants were matched within the same PIN-code area using name similarity (rapidfuzz token-sort ratio of at least 90 and the same first word). A company id groups factories of the same company (similarity of at least 94). 97 of 100 merged name pairs were checked by hand and judged to be the same factory (97% precision).
- Factories without a location are matched only by name inside their own group and are not merged with located factories.

## 4. Analyses
- Failure categories come from keywords in the test-result text; assay, sterility and identification are treated as serious.
- Time until the next failure: Kaplan-Meier curves; the event is a second NSQ alert in a different reporting month; factories without one are censored at the end of the data.
- Risk score (0-100): percentile ranks of number of incidents (40%), recency-weighted incidents (35%, half-life 2 years) and spread over years and reporting places (25%). Weights were chosen by comparing variants in time-split backtests (cdsco_score_check.py), so the reported lift is probably a little optimistic. Backtest: up to 2023 -> 2024 to 2026.
- Count model: zero-truncated Negative Binomial on incidents per factory (only factories with at least one failure are in the data). It uses PIN area, share of injectable / oral solid / oral liquid products and whether a company has several factories. Uncertainty is from the model or, if that fails, from a bootstrap.
- Hotspots: PIN code -> district with the offline package indiapins; Getis-Ord Gi* on repeat-failure factories (3+ incidents), 8 nearest districts, 999 permutations.

## 5. Limits
- No denominator: the number of samples tested per factory is not published, so large and heavily sampled factories look worse. RTIs for this were planned.
- Alert volume rose sharply from 2025. This coincides with a large rise in state-laboratory reporting, so it is most likely a reporting change, not a change in quality.
- The first alert seen in the data may not be a factory's first ever alert.
- Disputed results (Section 25 of the Drugs and Cosmetics Act, re-testing at CDL) are not tracked.
- Factories without a PIN code cannot be placed on the map; location-unknown factories have few details and look better than they may be.
- Government blacklists, licence data and factory size are not included yet.
- Missed merges (the same factory left as two separate factories) were not measured; the precision check covers wrong merges only.
- The "company has multiple factories" effect in the count model may partly come from matching: the same factory written once with a PIN code and once with only a city can be split into two factory ids under one company.
