-- Small, deterministic seed. Enough rows for row-count, aggregate, null and
-- hash reconciliation to be meaningful; small enough to load instantly.

INSERT INTO retail.customers (customer_id, customer_name, email_address, phone_number, credit_limit) VALUES
  (1, 'Northwind Supplies',  'ap@northwind.example',   '+1-206-555-0101', 25000.00),
  (2, 'Contoso Retail',      'billing@contoso.example', NULL,             18000.50),
  (3, 'Fabrikam Wholesale',  NULL,                      '+1-425-555-0142', 42000.00),
  (4, 'Tailspin Traders',    'ar@tailspin.example',     '+1-312-555-0177',  9500.25),
  (5, 'Adventure Outfitters', NULL,                     NULL,              15750.75);

INSERT INTO retail.orders (order_id, customer_id, order_total, notes) VALUES
  (1001, 1, 1250.00, 'expedited'),
  (1002, 2,  875.50, NULL),
  (1003, 1, 2400.00, NULL),
  (1004, 4,  310.25, 'backordered'),
  (1005, 3, 5600.00, NULL);

INSERT INTO retail.order_items (order_id, line_no, sku, quantity) VALUES
  (1001, 1, 'SKU-100', 4), (1001, 2, 'SKU-221', 1),
  (1002, 1, 'SKU-100', 2),
  (1003, 1, 'SKU-410', 7), (1003, 2, 'SKU-221', 3),
  (1005, 1, 'SKU-999', 12);

INSERT INTO retail.tags (tag_id, label, note) VALUES
  (1, 'priority', 'ships first'),
  (2, 'fragile',  NULL),
  (3, 'bulk',     'palletised');
