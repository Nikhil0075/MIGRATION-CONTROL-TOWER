-- source_system: oracle-corpus
-- schema: SH (Sales History)
-- Self-authored, modeled on Oracle's public Sales History sample schema shape.
-- Intended as the "large fact table" fixture for scale tests (master doc §2.2).
-- See oracle_dialect_corpus/README.md for attribution.

CREATE TABLE SH.PRODUCTS (
    PROD_ID       NUMBER(6)      NOT NULL,
    PROD_NAME     VARCHAR2(50)   NOT NULL,
    PROD_CATEGORY VARCHAR2(30),
    PROD_LIST_PRICE NUMBER(10,2),
    CONSTRAINT PK_PRODUCTS PRIMARY KEY (PROD_ID)
);

CREATE TABLE SH.TIMES (
    TIME_ID     DATE          NOT NULL,
    FISCAL_YEAR NUMBER(4),
    FISCAL_QTR  NUMBER(1),
    CONSTRAINT PK_TIMES PRIMARY KEY (TIME_ID)
);

CREATE TABLE SH.CHANNELS (
    CHANNEL_ID   NUMBER(2)     NOT NULL,
    CHANNEL_DESC VARCHAR2(30),
    CONSTRAINT PK_CHANNELS PRIMARY KEY (CHANNEL_ID)
);

-- Large fact table: source of the row-count/hash reconciliation checks
-- and the volume/scale test layer (master doc §6.1, TPC-DI/TPC-DS note).
CREATE TABLE SH.SALES (
    PROD_ID     NUMBER(6)      NOT NULL,
    TIME_ID     DATE           NOT NULL,
    CHANNEL_ID  NUMBER(2)      NOT NULL,
    CUST_ID     NUMBER(10)     NOT NULL,
    QUANTITY_SOLD NUMBER(10,2) NOT NULL,
    AMOUNT_SOLD   NUMBER(12,2) NOT NULL,             -- fact measure used in Revenue-sum reconciliation demo (§11.3)
    CONSTRAINT FK_SALES_PROD FOREIGN KEY (PROD_ID) REFERENCES SH.PRODUCTS(PROD_ID),
    CONSTRAINT FK_SALES_TIME FOREIGN KEY (TIME_ID) REFERENCES SH.TIMES(TIME_ID),
    CONSTRAINT FK_SALES_CHANNEL FOREIGN KEY (CHANNEL_ID) REFERENCES SH.CHANNELS(CHANNEL_ID)
);

-- Oracle-dialect construct: DECODE-based channel classification,
-- used by the Risk agent as a dialect-incompatibility finding.
SELECT
    s.PROD_ID,
    s.TIME_ID,
    DECODE(c.CHANNEL_DESC, 'Direct Sales', 'D', 'Partners', 'P', 'O') AS CHANNEL_CODE,
    SUM(s.AMOUNT_SOLD) AS TOTAL_REVENUE
FROM SH.SALES s
JOIN SH.CHANNELS c ON c.CHANNEL_ID = s.CHANNEL_ID
GROUP BY s.PROD_ID, s.TIME_ID, DECODE(c.CHANNEL_DESC, 'Direct Sales', 'D', 'Partners', 'P', 'O');
