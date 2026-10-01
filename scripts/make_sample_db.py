"""Create data/sample.db — a small sales database to try DataMaster on."""

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

random.seed(42)
path = Path(__file__).resolve().parents[1] / "data" / "sample.db"
path.parent.mkdir(exist_ok=True)
path.unlink(missing_ok=True)

con = sqlite3.connect(path)
con.executescript("""
CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    country TEXT NOT NULL,
    segment TEXT NOT NULL,           -- Consumer / SMB / Enterprise
    signup_date DATE NOT NULL
);
CREATE TABLE products (
    product_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    unit_price REAL NOT NULL
);
CREATE TABLE orders (
    order_id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    order_date DATE NOT NULL,
    status TEXT NOT NULL             -- completed / cancelled / refunded
);
CREATE TABLE order_items (
    order_item_id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(order_id),
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    quantity INTEGER NOT NULL,
    unit_price REAL NOT NULL
);
""")

countries = ["Netherlands", "Belgium", "Germany", "France", "India", "United Kingdom"]
segments = ["Consumer", "SMB", "Enterprise"]
first = ["Anna", "Ravi", "Lotte", "Jan", "Priya", "Lucas", "Emma", "Arjun", "Sophie", "Noah"]
last = ["de Vries", "Sharma", "Janssen", "Müller", "Dubois", "Patel", "Smith", "Bakker"]
start = date(2025, 1, 1)

customers = [(i, f"{random.choice(first)} {random.choice(last)}", random.choice(countries),
              random.choices(segments, [6, 3, 1])[0], (start + timedelta(days=random.randint(0, 600))).isoformat())
             for i in range(1, 201)]
con.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", customers)

products = [
    (1, "Laptop Pro 14", "Electronics", 1499.0), (2, "Wireless Mouse", "Electronics", 29.0),
    (3, "4K Monitor", "Electronics", 399.0), (4, "Standing Desk", "Furniture", 549.0),
    (5, "Office Chair", "Furniture", 299.0), (6, "Notebook Pack", "Stationery", 12.5),
    (7, "Gel Pens (10)", "Stationery", 8.0), (8, "USB-C Hub", "Electronics", 59.0),
    (9, "Desk Lamp", "Furniture", 45.0), (10, "Noise-Cancelling Headphones", "Electronics", 249.0),
]
con.executemany("INSERT INTO products VALUES (?,?,?,?)", products)

oid = iid = 0
for c in customers:
    for _ in range(random.randint(0, 8)):
        oid += 1
        d = max(c[4], (start + timedelta(days=random.randint(0, 630))).isoformat())
        con.execute("INSERT INTO orders VALUES (?,?,?,?)",
                    (oid, c[0], d, random.choices(["completed", "cancelled", "refunded"], [88, 8, 4])[0]))
        for p in random.sample(products, random.randint(1, 3)):
            iid += 1
            con.execute("INSERT INTO order_items VALUES (?,?,?,?,?)",
                        (iid, oid, p[0], random.randint(1, 4), p[3]))

con.commit()
con.close()
print(f"Created {path} with {len(customers)} customers, {oid} orders, {iid} order lines.")
