# Multimodal documentation fixtures (master doc §22)

Self-authored, generated (not hand-drawn or scanned) — `generate_fixtures.py`
produces both files from the tables below. Every discrepancy from the
real, introspected schema is itemized here deliberately, the same
honesty discipline as `DATA_SOURCES.md` and
`simulator/failure_injector/`: nothing here is an accidental bug
disguised as a feature.

```bash
python simulator/documentation/generate_fixtures.py
```

## `erd_sales_customers.png` — documents `Sales.Customers` (SQL Server / WWI)

| Documented | Real (introspected) | Drift type (§22.2) |
|---|---|---|
| `EmailAddress NVARCHAR(100)` | *(column does not exist)* | `MISSING_IN_ACTUAL` — stale documentation |
| `PhoneNumber CHAR(10)`, sensitivity `PUBLIC` | `PhoneNumber NVARCHAR`, Risk-classified `PII` | `TYPE_DIVERGENCE` **and** `CLASSIFICATION_GAP` |
| `CreditLimit FLOAT` | `CreditLimit DECIMAL` | `TYPE_DIVERGENCE` |
| `CustomerID INT`, `CustomerName NVARCHAR(100)` | matches | *(no drift — a true positive so the diff isn't 100% noise)* |
| *(undocumented)* | `IsOnCreditHold`, `DeliveryAddressLine1`, and ~25 more real columns | `MISSING_IN_DOCUMENTED` — shadow assets |

## `data_dictionary_co_customers.pdf` — documents `CO.CUSTOMERS` (Oracle corpus)

| Documented | Real (introspected) | Drift type (§22.2) |
|---|---|---|
| `EMAIL_ADDRESS VARCHAR2(150)`, sensitivity `PUBLIC` | Risk-classified `PII` | `CLASSIFICATION_GAP` |
| `ACCOUNT_MGR_ID VARCHAR2(20)` | `ACCOUNT_MGR_ID NUMBER` | `TYPE_DIVERGENCE` |
| *(undocumented)* | `NATIONAL_ID` — a real column, Risk-classified `PII` | `MISSING_IN_DOCUMENTED` — the highest-risk case: an undocumented, unclassified PII column |
| `CUSTOMER_ID`, `CUSTOMER_NAME` | match | *(true positives)* |

`tools/multimodal_discovery.py` extracts each documented schema (Gemini
vision for the image, a Gemini file-input call for the PDF — both with a
deterministic fallback to the tables above if the model call isn't
available) and diffs it against the same run's real catalog to produce
`RiskFinding` records with `finding_type` in `MISSING_IN_ACTUAL`,
`MISSING_IN_DOCUMENTED`, `TYPE_DIVERGENCE`, `CLASSIFICATION_GAP`.
