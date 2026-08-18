-- source_system: oracle-corpus
-- schema: CO (Customer Orders) — stored procedures
-- Self-authored. See oracle_dialect_corpus/README.md for attribution.
--
-- These procedures are deliberately dialect-heavy: NVL, DECODE, cursor
-- loops, and SYSDATE — the exact constructs the Risk agent should flag
-- and the Migration Planner should propose BigQuery-compatible
-- translations for (master doc §7.2, "Unsupported SQL" fault class).

CREATE OR REPLACE PROCEDURE CO.RECALC_ORDER_STATUS (
    p_order_id IN NUMBER
) AS
    v_item_count NUMBER := 0;
    v_status     CO.ORDERS.ORDER_STATUS%TYPE;
BEGIN
    SELECT COUNT(*) INTO v_item_count
    FROM CO.ORDER_ITEMS
    WHERE ORDER_ID = p_order_id;

    v_status := DECODE(SIGN(v_item_count), 0, 'EMPTY', 'HAS_ITEMS');

    UPDATE CO.ORDERS
    SET ORDER_STATUS = NVL(v_status, ORDER_STATUS),
        ORDER_DATE   = NVL(ORDER_DATE, SYSDATE)
    WHERE ORDER_ID = p_order_id;

    COMMIT;
END RECALC_ORDER_STATUS;
/

CREATE OR REPLACE PROCEDURE CO.APPLY_ACCOUNT_MANAGER_DEFAULTS AS
    CURSOR cust_cursor IS
        SELECT CUSTOMER_ID, ACCOUNT_MGR_ID FROM CO.CUSTOMERS;
    v_default_mgr NUMBER := 100;
BEGIN
    FOR cust_rec IN cust_cursor LOOP
        UPDATE CO.CUSTOMERS
        SET ACCOUNT_MGR_ID = NVL(cust_rec.ACCOUNT_MGR_ID, v_default_mgr)
        WHERE CUSTOMER_ID = cust_rec.CUSTOMER_ID;
    END LOOP;
    COMMIT;
END APPLY_ACCOUNT_MANAGER_DEFAULTS;
/
