-- LogSentinel testbed schema + seed data.
CREATE TABLE products (sku text PRIMARY KEY, name text NOT NULL, price numeric(10,2) NOT NULL);
INSERT INTO products SELECT 'sku-' || g, 'product ' || g, (g % 97) + 0.99 FROM generate_series(1, 1000) g;

CREATE TABLE stock (sku text PRIMARY KEY REFERENCES products(sku), qty bigint NOT NULL);
INSERT INTO stock SELECT sku, 1000000000 FROM products;

-- customer_ref is deliberately NOT indexed: scenario S5 (inefficient query) filters on it.
CREATE TABLE orders (
  id bigserial PRIMARY KEY,
  customer_ref text NOT NULL,
  sku text NOT NULL,
  qty int NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO orders (customer_ref, sku, qty)
SELECT 'c' || g, 'sku-' || (1 + g % 1000), 1 + g % 5 FROM generate_series(1, 200000) g;
ANALYZE;
