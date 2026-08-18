-- source_system: oracle-corpus
-- cross-schema reporting views (CO + SH + HR)
-- Self-authored. See oracle_dialect_corpus/README.md for attribution.
--
-- Purpose: gives the Lineage agent real view -> table dependency edges to
-- parse (read/write relationships across CO, SH, and HR), and gives the
-- Risk agent a "critical downstream dependency" scenario (master doc
-- §7.2, "Broken dependency" fault class — the account-manager join here
-- is intentionally the kind of edge that breaks if HR.EMPLOYEES changes).

CREATE OR REPLACE VIEW CO.V_CUSTOMER_ACCOUNT_SUMMARY AS
SELECT
    c.CUSTOMER_ID,
    c.CUSTOMER_NAME,
    NVL(e.LAST_NAME, 'UNASSIGNED') AS ACCOUNT_MANAGER,
    COUNT(o.ORDER_ID) AS TOTAL_ORDERS,
    SYSDATE AS REPORT_GENERATED_AT
FROM CO.CUSTOMERS c
LEFT JOIN HR.EMPLOYEES e ON e.EMPLOYEE_ID = c.ACCOUNT_MGR_ID
LEFT JOIN CO.ORDERS o ON o.CUSTOMER_ID = c.CUSTOMER_ID
GROUP BY c.CUSTOMER_ID, c.CUSTOMER_NAME, NVL(e.LAST_NAME, 'UNASSIGNED');

-- Downstream finance-report-style view feeding the "Finance Reporting
-- Impact Agent" cross-department discovery scenario (master doc §20.3).
CREATE OR REPLACE VIEW SH.V_QUARTERLY_REVENUE_BY_CHANNEL AS
SELECT
    t.FISCAL_YEAR,
    t.FISCAL_QTR,
    DECODE(ch.CHANNEL_DESC, 'Direct Sales', 'D', 'Partners', 'P', 'O') AS CHANNEL_CODE,
    SUM(s.AMOUNT_SOLD) AS TOTAL_REVENUE
FROM SH.SALES s
JOIN SH.TIMES t ON t.TIME_ID = s.TIME_ID
JOIN SH.CHANNELS ch ON ch.CHANNEL_ID = s.CHANNEL_ID
GROUP BY t.FISCAL_YEAR, t.FISCAL_QTR,
         DECODE(ch.CHANNEL_DESC, 'Direct Sales', 'D', 'Partners', 'P', 'O');
