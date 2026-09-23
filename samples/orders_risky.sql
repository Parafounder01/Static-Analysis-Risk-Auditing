/* SAMPLE: intentionally risky SQL for the auditor to flag.
   Do NOT deploy. See orders_refactored.sql for the fixed version. */

-- Risky report query: SELECT *, non-sargable YEAR(), IN-subquery, OR chain.
SELECT *
FROM dbo.orders AS o
WHERE YEAR(o.order_date) = 2024
  AND o.order_id IN (SELECT order_id FROM dbo.order_items WHERE qty > 10)
  AND (o.status = 'NEW' OR o.status = 'OPEN' OR o.priority = 'HIGH');

-- Correlated subquery in SELECT + function on JOIN column.
SELECT
    c.customer_id,
    (SELECT COUNT(*) FROM dbo.orders AS o WHERE o.customer_id = c.customer_id) AS order_count
FROM dbo.customers AS c
JOIN dbo.orders AS o2 ON LOWER(o2.region) = LOWER(c.region);

-- Risky DDL: no PK/FK, INT money, missing lineage columns.
CREATE TABLE dbo.fact_sales (
    order_id INT,
    amount INT,
    note VARCHAR(50)
);
