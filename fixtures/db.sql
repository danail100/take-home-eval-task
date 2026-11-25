-- Hermetic seed data for the fake run_sql tool (loaded into in-memory SQLite).
CREATE TABLE customers (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    email   TEXT NOT NULL,
    region  TEXT NOT NULL
);

CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    region      TEXT NOT NULL,
    amount      REAL NOT NULL,
    order_date  TEXT NOT NULL  -- ISO date
);

INSERT INTO customers VALUES
    ('C-001', 'Acme Corp',         'ops@acme.example',        'AMER'),
    ('C-003', 'Globex',            'finance@globex.example',  'EMEA'),
    ('C-007', 'Northwind Traders', 'it@northwind.example',    'EMEA'),
    ('C-012', 'Initech',           'billing@initech.example', 'APAC');

INSERT INTO orders VALUES
    (1, 'C-007', 'EMEA', 1200.00, '2026-07-14'),
    (2, 'C-003', 'EMEA', 3450.50, '2026-08-02'),
    (3, 'C-007', 'EMEA',  980.00, '2026-09-21'),
    (4, 'C-003', 'EMEA', 2210.25, '2026-09-30'),
    (5, 'C-007', 'EMEA', 1500.00, '2025-08-10'),
    (6, 'C-007', 'EMEA',  640.00, '2026-05-03'),
    (7, 'C-001', 'AMER', 5000.00, '2026-07-20'),
    (8, 'C-012', 'APAC', 2750.00, '2026-08-15'),
    (9, 'C-001', 'AMER', 1325.00, '2026-10-02');
