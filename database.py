import os
import sqlite3
from datetime import datetime


# Railway:
# Set DATABASE_PATH to a persistent path such as /data/bot_database.db
# when a Railway Volume is attached.
DB_NAME = os.getenv("DATABASE_PATH", "bot_database.db")

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

ORDER_STATUSES = {
    "waiting_payment",
    "waiting",
    "payment_review",
    "approved",
    "delivery_pending",
    "delivered",
    "rejected",
    "cancelled",
    "expired",
}


def now_text():
    return datetime.now().strftime(DATETIME_FORMAT)


def get_connection():
    conn = sqlite3.connect(
        DB_NAME,
        timeout=30,
        isolation_level=None,  # explicit transactions
    )
    conn.row_factory = sqlite3.Row

    # Better SQLite behavior for a bot running on Railway.
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")

    return conn


def _close_quietly(conn):
    try:
        conn.close()
    except Exception:
        pass


def _ensure_column(cur, table, column, definition):
    cur.execute(f"PRAGMA table_info({table})")
    columns = {row[1] for row in cur.fetchall()}

    if column not in columns:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_code TEXT UNIQUE,
                user_id INTEGER NOT NULL,
                service_name TEXT,
                volume TEXT,
                price INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'waiting_payment',
                receipt_file_id TEXT,
                config TEXT,
                purchase_date TEXT,
                expiry_date TEXT,
                service_start_date TEXT,
                service_key TEXT,
                discount_code TEXT,
                discount_amount INTEGER NOT NULL DEFAULT 0,
                final_price INTEGER,
                renewal_for_order_code TEXT,
                reminder_3_sent INTEGER NOT NULL DEFAULT 0,
                reminder_1_sent INTEGER NOT NULL DEFAULT 0,
                expired_notified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Persistent one-time notifications. These records survive bot restarts.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS notification_deliveries (
                notification_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (notification_id, user_id),
                FOREIGN KEY(notification_id) REFERENCES notifications(id) ON DELETE CASCADE
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_notification_deliveries_user
            ON notification_deliveries(user_id)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                admin_id INTEGER PRIMARY KEY,
                admin_name TEXT,
                added_by INTEGER,
                is_main INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS support_tickets (
                ticket_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                admin_id INTEGER,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                closed_at TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS suggestion_tickets (
                suggestion_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                admin_id INTEGER,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                answered_at TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS discount_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE,
                discount_type TEXT,
                discount_value INTEGER,
                max_uses INTEGER DEFAULT 0,
                used_count INTEGER DEFAULT 0,
                expires_at TEXT,
                active INTEGER DEFAULT 1,
                created_at TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS discount_usages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discount_code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                order_code TEXT,
                used_at TEXT NOT NULL,
                UNIQUE(discount_code, user_id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS services (
                service_key TEXT PRIMARY KEY,
                name TEXT,
                volume TEXT,
                price INTEGER,
                duration_days INTEGER DEFAULT 30,
                active INTEGER DEFAULT 1,
                category TEXT NOT NULL DEFAULT 'other',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cart_items (
                user_id INTEGER NOT NULL,
                service_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, service_key)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cart_items_user ON cart_items(user_id)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS receipt_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_code TEXT NOT NULL,
                reviewer_id INTEGER NOT NULL,
                reviewer_name TEXT NOT NULL,
                result TEXT NOT NULL,
                reviewed_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_permissions (
                admin_id INTEGER NOT NULL,
                permission TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(admin_id, permission)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                target TEXT,
                details TEXT,
                created_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                user_id INTEGER PRIMARY KEY,
                referrer_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                qualified_at TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS referral_rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_user_id INTEGER NOT NULL,
                reward_type TEXT NOT NULL,
                amount INTEGER NOT NULL,
                order_code TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(referrer_id, referred_user_id, reward_type, order_code)
            )
        """)

        _ensure_column(cur, "admins", "is_main", "INTEGER NOT NULL DEFAULT 0")

        # Safe migrations for existing databases.
        _ensure_column(cur, "orders", "service_key", "TEXT")
        _ensure_column(cur, "orders", "reminder_3_sent", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(cur, "orders", "reminder_1_sent", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(cur, "orders", "expired_notified", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(cur, "orders", "discount_code", "TEXT")
        _ensure_column(cur, "orders", "discount_amount", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(cur, "orders", "final_price", "INTEGER")
        _ensure_column(cur, "orders", "renewal_for_order_code", "TEXT")
        _ensure_column(cur, "orders", "service_start_date", "TEXT")
        _ensure_column(cur, "orders", "parent_order_code", "TEXT")
        _ensure_column(cur, "services", "created_at", "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP")
        _ensure_column(cur, "services", "category", "TEXT NOT NULL DEFAULT 'other'")
        _ensure_column(cur, "users", "user_number", "INTEGER")

        # Useful indexes for the queries used by the bot.
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_user_id
            ON orders(user_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_status
            ON orders(status)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_status_expiry
            ON orders(status, expiry_date)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_renewal
            ON orders(renewal_for_order_code)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_service_key
            ON orders(service_key)
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_parent_order ON orders(parent_order_code)")
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_discount_code
            ON discount_codes(code)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_discount_usages_user
            ON discount_usages(user_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_discount_usages_code
            ON discount_usages(discount_code)
        """)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_referral_rewards_referrer ON referral_rewards(referrer_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_referral_rewards_order ON referral_rewards(order_code)")

        # Assign stable internal user numbers to existing users once.
        next_number_row = cur.execute(
            "SELECT COALESCE(MAX(user_number), 1000) FROM users"
        ).fetchone()
        next_number = int(next_number_row[0] or 1000)
        missing_users = cur.execute(
            "SELECT user_id FROM users WHERE user_number IS NULL ORDER BY created_at ASC, user_id ASC"
        ).fetchall()
        for user_row in missing_users:
            next_number += 1
            cur.execute(
                "UPDATE users SET user_number=? WHERE user_id=?",
                (next_number, int(user_row[0])),
            )

        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_user_number
            ON users(user_number)
        """)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _close_quietly(conn)


# ---------------- ADMINS ----------------

def init_admins(admin_ids):
    """Seed the initial configured admins once, without re-adding removed admins."""
    ids = []
    for value in admin_ids or []:
        try:
            admin_id = int(value)
        except (TypeError, ValueError):
            continue
        if admin_id > 0 and admin_id not in ids:
            ids.append(admin_id)

    conn = get_connection()
    try:
        existing = conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
        if existing == 0:
            for index, admin_id in enumerate(ids):
                conn.execute(
                    "INSERT OR IGNORE INTO admins(admin_id, admin_name, added_by, is_main, created_at) VALUES(?,?,?,?,?)",
                    (admin_id, None, None, 1 if index == 0 else 0, now_text()),
                )
            conn.commit()
    finally:
        _close_quietly(conn)

def get_admins():
    conn = get_connection()
    try:
        return conn.execute("SELECT * FROM admins ORDER BY is_main DESC, created_at ASC, admin_id ASC").fetchall()
    finally:
        _close_quietly(conn)


def sync_admins_from_config(admin_ids):
    """Make the admins table mirror the hard-coded ADMIN_IDS in bot.py.

    The source of truth is now the code, not an admin-management UI.
    Existing permission rows for retained admins are preserved.
    """
    ids = []
    for value in admin_ids or []:
        try:
            admin_id = int(value)
        except (TypeError, ValueError):
            continue
        if admin_id > 0 and admin_id not in ids:
            ids.append(admin_id)

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        configured = set(ids)
        existing_rows = conn.execute("SELECT admin_id FROM admins").fetchall()
        for row in existing_rows:
            old_id = int(row["admin_id"])
            if old_id not in configured:
                conn.execute("DELETE FROM admin_permissions WHERE admin_id=?", (old_id,))
                conn.execute("DELETE FROM admins WHERE admin_id=?", (old_id,))

        for index, admin_id in enumerate(ids):
            conn.execute(
                "INSERT OR IGNORE INTO admins(admin_id, admin_name, added_by, is_main, created_at) VALUES(?,?,?,?,?)",
                (admin_id, None, None, 1 if index == 0 else 0, now_text()),
            )

        # Exactly one configured ID is the main admin: the first ID in bot.py.
        if ids:
            conn.execute("UPDATE admins SET is_main=0")
            conn.execute("UPDATE admins SET is_main=1 WHERE admin_id=?", (ids[0],))

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _close_quietly(conn)

def is_admin(admin_id):
    conn = get_connection()
    try:
        row = conn.execute("SELECT 1 FROM admins WHERE admin_id=? LIMIT 1", (int(admin_id),)).fetchone()
        return row is not None
    finally:
        _close_quietly(conn)

def add_admin(admin_id, admin_name=None, added_by=None):
    admin_id = int(admin_id)
    if admin_id <= 0:
        return False
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO admins(admin_id, admin_name, added_by, is_main, created_at) VALUES(?,?,?,?,?)",
            (admin_id, str(admin_name) if admin_name else None, int(added_by) if added_by else None, 0, now_text()),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)

def get_main_admin_id():
    conn = get_connection()
    try:
        row = conn.execute("SELECT admin_id FROM admins WHERE is_main=1 ORDER BY created_at ASC LIMIT 1").fetchone()
        if row:
            return int(row["admin_id"])
        row = conn.execute("SELECT admin_id FROM admins ORDER BY created_at ASC, admin_id ASC LIMIT 1").fetchone()
        return int(row["admin_id"]) if row else None
    finally:
        _close_quietly(conn)


def remove_admin(admin_id):
    """Remove an admin and its active permission overrides.

    Historical admin_logs are intentionally preserved for audit purposes,
    while admin_permissions are deleted so a future re-add starts clean.
    """
    admin_id = int(admin_id)
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("DELETE FROM admins WHERE admin_id=?", (admin_id,))
        if cur.rowcount == 1:
            conn.execute("DELETE FROM admin_permissions WHERE admin_id=?", (admin_id,))
        conn.commit()
        return cur.rowcount == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        _close_quietly(conn)

def update_admin_name(admin_id, admin_name):
    conn = get_connection()
    try:
        cur = conn.execute("UPDATE admins SET admin_name=? WHERE admin_id=?", (str(admin_name), int(admin_id)))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


# ---------------- USERS ----------------

def save_user(user_id, username=None, first_name=None):
    """Save a user and return (stable_user_number, is_new_user)."""
    user_id = int(user_id)
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT user_number FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()

        if existing is not None:
            user_number = existing["user_number"]
            if user_number is None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(user_number), 1000) + 1 AS next_number FROM users"
                ).fetchone()
                user_number = int(row["next_number"])
                conn.execute(
                    "UPDATE users SET user_number=? WHERE user_id=?",
                    (user_number, user_id),
                )

            conn.execute("""
                UPDATE users
                SET username=?, first_name=?
                WHERE user_id=?
            """, (username, first_name, user_id))
            conn.commit()
            return int(user_number), False

        row = conn.execute(
            "SELECT COALESCE(MAX(user_number), 1000) + 1 AS next_number FROM users"
        ).fetchone()
        user_number = int(row["next_number"])
        conn.execute("""
            INSERT INTO users (user_id, username, first_name, user_number, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, username, first_name, user_number, now_text()))
        conn.commit()
        return user_number, True
    except Exception:
        conn.rollback()
        raise
    finally:
        _close_quietly(conn)


def get_all_users():
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM users
            ORDER BY created_at DESC
        """).fetchall()
    finally:
        _close_quietly(conn)


def get_user_count():
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        _close_quietly(conn)


# ---------------- SHOPPING CART ----------------

def add_cart_item(user_id, service_key):
    conn = get_connection()
    try:
        cur = conn.execute("INSERT OR IGNORE INTO cart_items(user_id, service_key, created_at) VALUES(?,?,?)", (int(user_id), str(service_key), now_text()))
        conn.commit(); return cur.rowcount == 1
    finally: _close_quietly(conn)

def remove_cart_item(user_id, service_key):
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM cart_items WHERE user_id=? AND service_key=?", (int(user_id), str(service_key)))
        conn.commit(); return cur.rowcount == 1
    finally: _close_quietly(conn)

def get_cart_items(user_id):
    conn = get_connection()
    try:
        return conn.execute("SELECT service_key, created_at FROM cart_items WHERE user_id=? ORDER BY created_at ASC", (int(user_id),)).fetchall()
    finally: _close_quietly(conn)

def clear_cart(user_id):
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM cart_items WHERE user_id=?", (int(user_id),))
        conn.commit(); return cur.rowcount
    finally: _close_quietly(conn)


# ---------------- ORDERS ----------------

def create_order(
    order_code,
    user_id,
    service_name,
    volume,
    price,
    service_key=None,
    discount_code=None,
    discount_amount=0,
    final_price=None,
    renewal_for_order_code=None,
    parent_order_code=None,
):
    if final_price is None:
        final_price = max(0, int(price) - int(discount_amount))

    if int(price) < 0 or int(discount_amount) < 0 or int(final_price) < 0:
        raise ValueError("Invalid price/discount values.")

    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO orders (
                order_code,
                user_id,
                service_name,
                volume,
                price,
                status,
                service_key,
                discount_code,
                discount_amount,
                final_price,
                renewal_for_order_code,
                parent_order_code,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, 'waiting_payment', ?, ?, ?, ?, ?, ?, ?)
        """, (
            order_code,
            user_id,
            service_name,
            volume,
            int(price),
            service_key,
            discount_code.upper() if discount_code else None,
            int(discount_amount),
            int(final_price),
            renewal_for_order_code,
            parent_order_code,
            now_text(),
        ))
        conn.commit()
    finally:
        _close_quietly(conn)


def create_cart_child_orders(parent_order_code, user_id, children):
    """Create the individual service orders after a cart's single payment is approved."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        created = []
        for child in children:
            conn.execute("""
                INSERT INTO orders (order_code,user_id,service_name,volume,price,status,service_key,discount_amount,final_price,parent_order_code,created_at)
                VALUES (?,?,?,?,?,'approved',?,?,?, ?,?)
            """, (
                str(child["order_code"]), int(user_id), str(child["service_name"]), str(child["volume"]),
                int(child["price"]), str(child["service_key"]), 0, int(child["price"]), str(parent_order_code), now_text()
            ))
            created.append(str(child["order_code"]))

        # Parent and children must move together. If this compare-and-set fails,
        # the transaction is rolled back so we never leave orphan child orders.
        parent_update = conn.execute("""
            UPDATE orders
            SET status='delivery_pending'
            WHERE order_code=? AND status='approved'
        """, (str(parent_order_code),))
        if parent_update.rowcount != 1:
            conn.rollback()
            raise RuntimeError("Cart parent order is no longer in approved state.")

        conn.commit()
        return created
    except Exception:
        conn.rollback(); raise
    finally:
        _close_quietly(conn)

def get_cart_child_orders(parent_order_code):
    conn = get_connection()
    try:
        return conn.execute("SELECT * FROM orders WHERE parent_order_code=? ORDER BY created_at ASC", (str(parent_order_code),)).fetchall()
    finally:
        _close_quietly(conn)

def complete_cart_parent_if_ready(parent_order_code):
    conn = get_connection()
    try:
        row=conn.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN status='delivered' THEN 1 ELSE 0 END) AS done FROM orders WHERE parent_order_code=?", (str(parent_order_code),)).fetchone()
        if not row or int(row["total"] or 0)==0 or int(row["done"] or 0)!=int(row["total"]):
            return False
        cur=conn.execute("UPDATE orders SET status='delivered' WHERE order_code=? AND status='delivery_pending'", (str(parent_order_code),))
        conn.commit(); return cur.rowcount==1
    finally:
        _close_quietly(conn)


def get_order(order_code):
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM orders
            WHERE order_code = ?
        """, (order_code,)).fetchone()
    finally:
        _close_quietly(conn)


def update_order_status(order_code, status):
    if status not in ORDER_STATUSES:
        raise ValueError(f"Invalid order status: {status}")

    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE orders
            SET status = ?
            WHERE order_code = ?
        """, (status, order_code))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def set_order_status_if_current(order_code, new_status, current_status):
    """Atomic compare-and-set status update."""
    if new_status not in ORDER_STATUSES or current_status not in ORDER_STATUSES:
        raise ValueError("Invalid order status.")

    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE orders
            SET status = ?
            WHERE order_code = ?
              AND status = ?
        """, (new_status, order_code, current_status))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def save_receipt(order_code, receipt_file_id):
    """
    Attaches a receipt only to an order that is still waiting for payment.
    Returns True only when the status was changed successfully.
    """
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE orders
            SET receipt_file_id = ?,
                status = 'waiting'
            WHERE order_code = ?
              AND status IN ('waiting_payment', 'waiting')
        """, (receipt_file_id, order_code))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def save_config(order_code, config):
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE orders
            SET config = ?
            WHERE order_code = ?
        """, (config, order_code))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def set_dates(order_code, purchase_date, expiry_date, service_start_date=None):
    if service_start_date is None:
        service_start_date = purchase_date

    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE orders
            SET purchase_date = ?,
                expiry_date = ?,
                service_start_date = ?
            WHERE order_code = ?
        """, (purchase_date, expiry_date, service_start_date, order_code))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)



def claim_order_for_delivery(order_code):
    """Atomically reserve an approved order while its config is being sent.

    Returns True only for the admin/process that successfully changes the
    order from approved -> delivery_pending. This prevents two admins from
    delivering the same order at the same time.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("""
            UPDATE orders
            SET status = 'delivery_pending'
            WHERE order_code = ?
              AND status = 'approved'
        """, (order_code,))
        if cur.rowcount != 1:
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        _close_quietly(conn)


def release_order_delivery_claim(order_code):
    """Return an unsuccessfully delivered order to the approved state."""
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE orders
            SET status = 'approved'
            WHERE order_code = ?
              AND status = 'delivery_pending'
        """, (order_code,))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def finalize_order_delivery(
    order_code,
    config,
    purchase_date,
    expiry_date,
    service_start_date=None,
):
    """
    Atomically saves the config/dates and changes the order to delivered.

    Returns:
      True  -> finalization happened now
      False -> order was already delivered, or is not eligible
    """
    if service_start_date is None:
        service_start_date = purchase_date

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        order = conn.execute("""
            SELECT *
            FROM orders
            WHERE order_code = ?
        """, (order_code,)).fetchone()

        if order is None:
            conn.rollback()
            return False

        if order["status"] == "delivered":
            conn.commit()
            return False

        if order["status"] not in ("approved", "delivery_pending"):
            conn.rollback()
            return False

        cur = conn.execute("""
            UPDATE orders
            SET config = ?,
                purchase_date = ?,
                expiry_date = ?,
                service_start_date = ?,
                status = 'delivered'
            WHERE order_code = ?
              AND status IN ('approved', 'delivery_pending')
        """, (
            config,
            purchase_date,
            expiry_date,
            service_start_date,
            order_code,
        ))

        if cur.rowcount != 1:
            conn.rollback()
            return False

        # Discount consumption happens in the same transaction as delivery.
        # Each user can successfully consume a given code only once.
        # The UNIQUE(discount_code, user_id) constraint protects concurrent
        # delivery attempts as well.
        if order["discount_code"]:
            code = order["discount_code"].upper()
            discount = conn.execute("""
                SELECT max_uses, used_count
                FROM discount_codes
                WHERE code = ?
            """, (code,)).fetchone()

            if discount:
                already_used = conn.execute("""
                    SELECT 1
                    FROM discount_usages
                    WHERE discount_code = ? AND user_id = ?
                    LIMIT 1
                """, (code, int(order["user_id"]))).fetchone()

                if already_used:
                    conn.rollback()
                    return False

                if (
                    discount["max_uses"] > 0
                    and discount["used_count"] >= discount["max_uses"]
                ):
                    conn.rollback()
                    return False

                usage_inserted = conn.execute("""
                    INSERT INTO discount_usages (
                        discount_code,
                        user_id,
                        order_code,
                        used_at
                    )
                    VALUES (?, ?, ?, ?)
                """, (
                    code,
                    int(order["user_id"]),
                    order_code,
                    now_text(),
                ))

                if usage_inserted.rowcount != 1:
                    conn.rollback()
                    return False

                conn.execute("""
                    UPDATE discount_codes
                    SET used_count = used_count + 1
                    WHERE code = ?
                      AND (
                          max_uses = 0
                          OR used_count < max_uses
                      )
                """, (code,))

                # Automatically disable finite-capacity codes at the limit.
                conn.execute("""
                    UPDATE discount_codes
                    SET active = 0
                    WHERE code = ?
                      AND max_uses > 0
                      AND used_count >= max_uses
                """, (code,))

        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        _close_quietly(conn)


def get_user_orders(user_id):
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM orders
            WHERE user_id = ?
            ORDER BY created_at DESC
        """, (user_id,)).fetchall()
    finally:
        _close_quietly(conn)


def get_user_services(user_id):
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM orders
            WHERE user_id = ?
              AND status IN ('delivered', 'expired')
              AND config IS NOT NULL
            ORDER BY created_at DESC
        """, (user_id,)).fetchall()
    finally:
        _close_quietly(conn)


def get_all_orders():
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM orders
            ORDER BY created_at DESC
        """).fetchall()
    finally:
        _close_quietly(conn)


def get_pending_orders():
    # Payment review is the actual payment-review queue.
    # Approved orders are waiting for admin to deliver/configure.
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM orders
            WHERE status IN ('payment_review', 'approved')
            ORDER BY created_at ASC
        """).fetchall()
    finally:
        _close_quietly(conn)


def get_payment_pending_orders():
    """Return orders with a receipt waiting for admin payment review."""
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM orders
            WHERE status IN ('waiting', 'payment_review')
              AND receipt_file_id IS NOT NULL
              AND receipt_file_id != ''
            ORDER BY created_at ASC
        """).fetchall()
    finally:
        _close_quietly(conn)


def get_active_services():
    """Return currently active delivered services, excluding expired ones."""
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM orders
            WHERE status = 'delivered'
              AND (expiry_date IS NULL OR expiry_date > ?)
            ORDER BY expiry_date ASC
        """, (now_text(),)).fetchall()
    finally:
        _close_quietly(conn)


def get_order_count():
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    finally:
        _close_quietly(conn)


def get_order_count_by_status(status):
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT COUNT(*)
            FROM orders
            WHERE status = ?
        """, (status,)).fetchone()[0]
    finally:
        _close_quietly(conn)


def get_total_sales():
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT COALESCE(SUM(
                CASE
                    WHEN final_price IS NOT NULL THEN final_price
                    ELSE price
                END
            ), 0)
            FROM orders
            WHERE status IN ('delivered', 'expired')
              AND parent_order_code IS NULL
        """).fetchone()[0]
    finally:
        _close_quietly(conn)


def search_orders(query):
    conn = get_connection()
    try:
        q = f"%{query}%"
        return conn.execute("""
            SELECT *
            FROM orders
            WHERE order_code LIKE ?
               OR CAST(user_id AS TEXT) LIKE ?
               OR service_name LIKE ?
            ORDER BY created_at DESC
        """, (q, q, q)).fetchall()
    finally:
        _close_quietly(conn)


# ---------------- EXPIRY ----------------

def get_services_for_expiry_check():
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM orders
            WHERE status = 'delivered'
              AND expiry_date IS NOT NULL
        """).fetchall()
    finally:
        _close_quietly(conn)


def claim_reminder(order_code, reminder_number):
    """Atomically claim a reminder before sending it. Returns True only once."""
    if reminder_number == 3:
        column = "reminder_3_sent"
    elif reminder_number == 1:
        column = "reminder_1_sent"
    else:
        return False
    conn = get_connection()
    try:
        cur = conn.execute(f"""
            UPDATE orders
            SET {column} = 1
            WHERE order_code = ? AND {column} = 0
        """, (order_code,))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def reset_reminder_claim(order_code, reminder_number):
    """Release a reminder claim when Telegram delivery fails."""
    if reminder_number == 3:
        column = "reminder_3_sent"
    elif reminder_number == 1:
        column = "reminder_1_sent"
    else:
        return False
    conn = get_connection()
    try:
        cur = conn.execute(f"""
            UPDATE orders
            SET {column} = 0
            WHERE order_code = ? AND {column} = 1
        """, (order_code,))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def mark_reminder_sent(order_code, reminder_number):
    """Backward-compatible alias for claiming a reminder."""
    return claim_reminder(order_code, reminder_number)


def mark_expired(order_code):
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE orders
            SET status = 'expired'
            WHERE order_code = ?
              AND status = 'delivered'
        """, (order_code,))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def claim_expired_notification(order_code):
    """Atomically claim the expiry notification before sending it."""
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE orders
            SET expired_notified = 1
            WHERE order_code = ? AND expired_notified = 0
        """, (order_code,))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def reset_expired_notification_claim(order_code):
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE orders
            SET expired_notified = 0
            WHERE order_code = ? AND expired_notified = 1
        """, (order_code,))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def mark_expired_notified(order_code):
    """Backward-compatible alias for claiming an expiry notification."""
    return claim_expired_notification(order_code)


def has_scheduled_renewal(order_code, reference_date, now_text_value=None):
    if now_text_value is None:
        now_text_value = now_text()

    conn = get_connection()
    try:
        count = conn.execute("""
            SELECT COUNT(*)
            FROM orders
            WHERE renewal_for_order_code = ?
              AND status IN ('payment_review', 'approved', 'delivered')
              AND service_start_date >= ?
              AND (
                  expiry_date IS NULL
                  OR expiry_date > ?
              )
        """, (
            order_code,
            reference_date,
            now_text_value,
        )).fetchone()[0]
        return count > 0
    finally:
        _close_quietly(conn)


# ---------------- SETTINGS ----------------

def get_setting(key, default=None):
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT value
            FROM settings
            WHERE key = ?
        """, (key,)).fetchone()
        return default if row is None else row["value"]
    finally:
        _close_quietly(conn)


def set_setting(key, value):
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value
        """, (key, str(value)))
        conn.commit()
    finally:
        _close_quietly(conn)


def get_next_support_admin(admin_ids):
    if not admin_ids:
        return None

    normalized_ids = [int(x) for x in admin_ids]
    last = get_setting("last_support_admin")

    try:
        last_id = int(last) if last is not None else None
    except (TypeError, ValueError):
        last_id = None

    if last_id in normalized_ids:
        index = normalized_ids.index(last_id)
        selected = normalized_ids[(index + 1) % len(normalized_ids)]
    else:
        selected = normalized_ids[0]

    set_setting("last_support_admin", selected)
    return selected


# ---------------- SUPPORT TICKETS ----------------


def _new_support_ticket_id():
    """Create a short numeric ticket id that is unique in the database."""
    import secrets

    conn = get_connection()
    try:
        for _ in range(20):
            ticket_id = str(secrets.randbelow(900000) + 100000)
            exists = conn.execute(
                "SELECT 1 FROM support_tickets WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
            if exists is None:
                return ticket_id
    finally:
        _close_quietly(conn)

    return datetime.now().strftime("%d%H%M%S%f")[-12:]


def create_support_ticket(user_id, admin_id):
    """Create or reuse an open support ticket. Returns (ticket_id, admin_id)."""
    if admin_id is None:
        return None, None

    conn = get_connection()
    try:
        existing = conn.execute("""
            SELECT ticket_id, admin_id
            FROM support_tickets
            WHERE user_id = ? AND status = 'open'
            ORDER BY created_at DESC
            LIMIT 1
        """, (int(user_id),)).fetchone()

        if existing:
            assigned = existing["admin_id"] or int(admin_id)
            if existing["admin_id"] is None:
                conn.execute(
                    "UPDATE support_tickets SET admin_id = ? WHERE ticket_id = ?",
                    (assigned, existing["ticket_id"]),
                )
                conn.commit()
            return existing["ticket_id"], int(assigned)

        ticket_id = _new_support_ticket_id()
        conn.execute("""
            INSERT INTO support_tickets
                (ticket_id, user_id, admin_id, status, created_at)
            VALUES (?, ?, ?, 'open', ?)
        """, (ticket_id, int(user_id), int(admin_id), now_text()))
        conn.commit()
        return ticket_id, int(admin_id)
    finally:
        _close_quietly(conn)


def get_support_ticket(ticket_id):
    if not ticket_id:
        return None
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT user_id, admin_id
            FROM support_tickets
            WHERE ticket_id = ?
        """, (str(ticket_id),)).fetchone()
        if row is None:
            return None
        return int(row["user_id"]), (
            int(row["admin_id"]) if row["admin_id"] is not None else None
        )
    finally:
        _close_quietly(conn)


def get_user_open_support_ticket(user_id):
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT ticket_id
            FROM support_tickets
            WHERE user_id = ? AND status = 'open'
            ORDER BY created_at DESC
            LIMIT 1
        """, (int(user_id),)).fetchone()
        return row["ticket_id"] if row else None
    finally:
        _close_quietly(conn)


def support_ticket_is_open(ticket_id):
    if not ticket_id:
        return False
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT 1
            FROM support_tickets
            WHERE ticket_id = ? AND status = 'open'
            LIMIT 1
        """, (str(ticket_id),)).fetchone()
        return row is not None
    finally:
        _close_quietly(conn)


def close_support_ticket(ticket_id):
    if not ticket_id:
        return False
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE support_tickets
            SET status = 'closed', closed_at = ?
            WHERE ticket_id = ? AND status = 'open'
        """, (now_text(), str(ticket_id)))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


# ---------------- SUGGESTIONS ----------------

def _new_suggestion_id():
    """Create a short numeric suggestion id that is unique in the database."""
    import secrets

    conn = get_connection()
    try:
        for _ in range(20):
            suggestion_id = str(secrets.randbelow(900000) + 100000)
            exists = conn.execute(
                "SELECT 1 FROM suggestion_tickets WHERE suggestion_id = ?",
                (suggestion_id,),
            ).fetchone()
            if exists is None:
                return suggestion_id
    finally:
        _close_quietly(conn)

    return datetime.now().strftime("%d%H%M%S%f")[-12:]


def create_suggestion(user_id):
    """Create a new open suggestion ticket and return its id."""
    suggestion_id = _new_suggestion_id()
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO suggestion_tickets
                (suggestion_id, user_id, status, created_at)
            VALUES (?, ?, 'open', ?)
        """, (suggestion_id, int(user_id), now_text()))
        conn.commit()
        return suggestion_id
    finally:
        _close_quietly(conn)


def get_suggestion(suggestion_id):
    if not suggestion_id:
        return None
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT suggestion_id, user_id, admin_id, status, created_at, answered_at
            FROM suggestion_tickets
            WHERE suggestion_id = ?
        """, (str(suggestion_id),)).fetchone()
        return row
    finally:
        _close_quietly(conn)


def suggestion_is_open(suggestion_id):
    if not suggestion_id:
        return False
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT 1 FROM suggestion_tickets
            WHERE suggestion_id = ? AND status = 'open'
            LIMIT 1
        """, (str(suggestion_id),)).fetchone()
        return row is not None
    finally:
        _close_quietly(conn)


def claim_suggestion(suggestion_id, admin_id):
    """Atomically reserve an open suggestion for one admin while they type a reply."""
    if not suggestion_id or not admin_id:
        return False
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE suggestion_tickets
            SET status = 'answering', admin_id = ?
            WHERE suggestion_id = ? AND status = 'open'
        """, (int(admin_id), str(suggestion_id)))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def complete_suggestion(suggestion_id, admin_id):
    """Close a suggestion only for the admin who reserved it."""
    if not suggestion_id or not admin_id:
        return False
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE suggestion_tickets
            SET status = 'answered', answered_at = ?
            WHERE suggestion_id = ? AND status = 'answering' AND admin_id = ?
        """, (now_text(), str(suggestion_id), int(admin_id)))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def reopen_suggestion(suggestion_id, admin_id):
    """Return a failed reply reservation to the open state."""
    if not suggestion_id or not admin_id:
        return False
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE suggestion_tickets
            SET status = 'open', admin_id = NULL
            WHERE suggestion_id = ? AND status = 'answering' AND admin_id = ?
        """, (str(suggestion_id), int(admin_id)))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


# ---------------- DISCOUNTS ----------------

def create_discount_code(
    code,
    discount_type,
    discount_value,
    max_uses=0,
    expires_at=None,
):
    code = code.strip().upper()

    if discount_type not in {"percent", "fixed"}:
        raise ValueError("discount_type must be 'percent' or 'fixed'.")

    if int(discount_value) < 0:
        raise ValueError("discount_value cannot be negative.")

    if int(max_uses) < 0:
        raise ValueError("max_uses cannot be negative.")

    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO discount_codes (
                code,
                discount_type,
                discount_value,
                max_uses,
                used_count,
                expires_at,
                active,
                created_at
            )
            VALUES (?, ?, ?, ?, 0, ?, 1, ?)
        """, (
            code,
            discount_type,
            int(discount_value),
            int(max_uses),
            expires_at,
            now_text(),
        ))
        conn.commit()
    finally:
        _close_quietly(conn)



def get_all_discount_codes():
    """Backward-compatible alias used by older bot.py versions."""
    return get_discount_codes()


def set_discount_active(code, active):
    """Set discount code active state explicitly."""
    if not code:
        return False
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE discount_codes
            SET active = ?
            WHERE code = ?
        """, (1 if active else 0, code.strip().upper()))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)

def get_discount_codes():
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM discount_codes
            ORDER BY created_at DESC
        """).fetchall()
    finally:
        _close_quietly(conn)


def get_discount_code(code):
    if not code:
        return None

    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM discount_codes
            WHERE code = ?
        """, (code.strip().upper(),)).fetchone()
    finally:
        _close_quietly(conn)



def update_discount_code(
    code,
    discount_type=None,
    discount_value=None,
    max_uses=None,
    expires_at=None,
    active=None,
):
    """Update editable fields of an existing discount code."""
    if not code:
        return False

    code = str(code).strip().upper()
    fields = []
    values = []

    if discount_type is not None:
        if discount_type not in {"percent", "fixed"}:
            raise ValueError("discount_type must be 'percent' or 'fixed'.")
        fields.append("discount_type = ?")
        values.append(discount_type)

    if discount_value is not None:
        if int(discount_value) <= 0:
            raise ValueError("discount_value must be greater than zero.")
        fields.append("discount_value = ?")
        values.append(int(discount_value))

    if max_uses is not None:
        if int(max_uses) < 0:
            raise ValueError("max_uses cannot be negative.")
        fields.append("max_uses = ?")
        values.append(int(max_uses))

    if expires_at is not None:
        fields.append("expires_at = ?")
        values.append(expires_at)

    if active is not None:
        fields.append("active = ?")
        values.append(1 if active else 0)

    if not fields:
        return False

    values.append(code)
    conn = get_connection()
    try:
        cur = conn.execute(
            f"UPDATE discount_codes SET {', '.join(fields)} WHERE code = ?",
            values,
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def delete_discount_code(code):
    """Delete a discount code and its usage history."""
    if not code:
        return False

    code = str(code).strip().upper()
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        exists = conn.execute(
            "SELECT 1 FROM discount_codes WHERE code = ?",
            (code,),
        ).fetchone()
        if exists is None:
            conn.rollback()
            return False

        conn.execute("DELETE FROM discount_usages WHERE discount_code = ?", (code,))
        cur = conn.execute("DELETE FROM discount_codes WHERE code = ?", (code,))
        conn.commit()
        return cur.rowcount == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        _close_quietly(conn)


def get_discount_usage_stats(code):
    """Return aggregate usage statistics for a discount code."""
    if not code:
        return None

    code = str(code).strip().upper()
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT
                d.code,
                d.discount_type,
                d.discount_value,
                d.max_uses,
                d.used_count,
                d.expires_at,
                d.active,
                COUNT(u.id) AS usage_rows,
                COUNT(DISTINCT u.user_id) AS unique_users
            FROM discount_codes d
            LEFT JOIN discount_usages u ON u.discount_code = d.code
            WHERE d.code = ?
            GROUP BY d.id
        """, (code,)).fetchone()
        return row
    finally:
        _close_quietly(conn)

def has_user_used_discount(code, user_id):
    """Return True if this user has already successfully consumed the code."""
    if not code or user_id is None:
        return False

    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT 1
            FROM discount_usages
            WHERE discount_code = ? AND user_id = ?
            LIMIT 1
        """, (code.strip().upper(), int(user_id))).fetchone()
        return row is not None
    finally:
        _close_quietly(conn)


def calculate_discount(code, price, user_id=None):
    """
    Calculate a discount.

    user_id is optional for backward compatibility. When supplied, the
    discount is rejected if that user has already successfully used the code.
    """
    if not code:
        return 0, None

    price = int(price)
    if price < 0:
        return 0, "قیمت نامعتبر است."

    discount = get_discount_code(code)

    if not discount:
        return 0, "کد تخفیف پیدا نشد."

    if not discount["active"]:
        return 0, "این کد تخفیف غیرفعال است."

    if (
        discount["max_uses"] > 0
        and discount["used_count"] >= discount["max_uses"]
    ):
        return 0, "ظرفیت استفاده از این کد تخفیف تمام شده است."

    if user_id is not None and has_user_used_discount(code, user_id):
        return 0, "این کد تخفیف را قبلاً استفاده کرده‌اید."

    if discount["expires_at"]:
        try:
            expires = datetime.strptime(
                discount["expires_at"],
                DATETIME_FORMAT,
            )
            if datetime.now() > expires:
                return 0, "این کد تخفیف منقضی شده است."
        except ValueError:
            return 0, "تاریخ انقضای کد تخفیف نامعتبر است."

    if discount["discount_type"] == "percent":
        amount = int(price * discount["discount_value"] / 100)
    else:
        amount = int(discount["discount_value"])

    amount = max(0, min(amount, price))
    return amount, None


def increment_discount_usage(code):
    """
    Kept for compatibility with the existing bot.
    New delivery flow should use finalize_order_delivery(), which
    increments usage inside the same DB transaction.
    """
    if not code:
        return False

    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE discount_codes
            SET used_count = used_count + 1
            WHERE code = ?
              AND (
                  max_uses = 0
                  OR used_count < max_uses
              )
        """, (code.strip().upper(),))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def toggle_discount(code):
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE discount_codes
            SET active = CASE
                WHEN active = 1 THEN 0
                ELSE 1
            END
            WHERE code = ?
        """, (code.strip().upper(),))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


# ---------------- SERVICE CATEGORIES / RECEIPT REVIEWS ----------------

def set_service_category(service_key, category):
    """Store a service category in the main database."""
    category = str(category or "other")
    if category not in {"unlimited", "fixed_ip", "multi_location", "other", "gaming"}:
        raise ValueError("Invalid service category.")
    conn = get_connection()
    try:
        cur = conn.execute("UPDATE services SET category=? WHERE service_key=?", (category, str(service_key)))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def get_service_category(service_key):
    conn = get_connection()
    try:
        row = conn.execute("SELECT category FROM services WHERE service_key=?", (str(service_key),)).fetchone()
        return (row["category"] if row and row["category"] else "other")
    finally:
        _close_quietly(conn)


def save_receipt_review(order_code, reviewer_id, reviewer_name, result):
    """Save the receipt reviewer, result and review time in the main database."""
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO receipt_reviews(order_code, reviewer_id, reviewer_name, result, reviewed_at)
            VALUES(?,?,?,?,?)
        """, (str(order_code), int(reviewer_id), str(reviewer_name), str(result), now_text()))
        conn.commit()
        return True
    finally:
        _close_quietly(conn)


# ---------------- SERVICES ----------------

def get_all_services():
    """Return every configured service."""
    return get_services(active_only=False)


def get_active_service_catalog():
    """Return only active services for the customer catalog."""
    return get_services(active_only=True)


def create_service(service_key, name, volume, price, duration_days=30, active=True, category="other"):
    """Create a service record; return True on success."""
    if not service_key:
        raise ValueError("service_key is required.")
    if int(price) < 0:
        raise ValueError("price cannot be negative.")
    if int(duration_days) <= 0:
        raise ValueError("duration_days must be greater than zero.")
    category = str(category or "other")
    if category not in {"unlimited", "fixed_ip", "multi_location", "other", "gaming"}:
        raise ValueError("Invalid service category.")

    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO services (
                service_key, name, volume, price, duration_days, active, category, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(service_key).strip(),
            name,
            volume,
            int(price),
            int(duration_days),
            1 if active else 0,
            str(category or "other"),
            now_text(),
        ))
        conn.commit()
        return True
    finally:
        _close_quietly(conn)


def delete_service(service_key):
    """Delete a service by key; return True if a row was deleted."""
    if not service_key:
        return False
    conn = get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM services WHERE service_key = ?",
            (str(service_key).strip(),),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def set_service_active(service_key, active):
    """Set a service's active state explicitly."""
    return update_service(service_key, active=1 if active else 0)


def seed_services(services):
    """
    IMPORTANT:
    Existing service records are NOT overwritten on every restart.
    This prevents admin-edited prices/settings from being reset by Railway
    redeploys/restarts.

    Missing services are inserted with the default values.
    """
    conn = get_connection()
    try:
        for service in services:
            # bot.py uses the public name "service_key"; older versions
            # used "key". Accept both so the seeded catalog stays compatible.
            service_key = service.get("service_key", service.get("key"))
            if not service_key:
                raise ValueError("Each service must contain 'service_key' (or legacy 'key').")

            conn.execute("""
                INSERT INTO services (
                    service_key,
                    name,
                    volume,
                    price,
                    duration_days,
                    active,
                    category,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(service_key) DO NOTHING
            """, (
                service_key,
                service["name"],
                service["volume"],
                int(service["price"]),
                int(service.get("duration_days", 30)),
                int(service.get("active", 1)),
                str(service.get("category", "other") or "other"),
                now_text(),
            ))

        conn.commit()
    finally:
        _close_quietly(conn)


def get_services(active_only=False):
    conn = get_connection()
    try:
        if active_only:
            return conn.execute("""
                SELECT *
                FROM services
                WHERE active = 1
                ORDER BY price ASC
            """).fetchall()

        return conn.execute("""
            SELECT *
            FROM services
            ORDER BY price ASC
        """).fetchall()
    finally:
        _close_quietly(conn)


def get_service(service_key):
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM services
            WHERE service_key = ?
        """, (service_key,)).fetchone()
    finally:
        _close_quietly(conn)


def update_service(
    service_key,
    name=None,
    volume=None,
    price=None,
    duration_days=None,
    active=None,
    category=None,
):
    conn = get_connection()
    try:
        service = conn.execute("""
            SELECT *
            FROM services
            WHERE service_key = ?
        """, (service_key,)).fetchone()

        if not service:
            return False

        new_name = name if name is not None else service["name"]
        new_volume = volume if volume is not None else service["volume"]
        new_price = price if price is not None else service["price"]
        new_duration = (
            duration_days
            if duration_days is not None
            else service["duration_days"]
        )
        new_active = active if active is not None else service["active"]
        new_category = str(category if category is not None else (service["category"] or "other"))
        if new_category not in {"unlimited", "fixed_ip", "multi_location", "other"}:
            raise ValueError("Invalid service category.")

        if int(new_price) < 0:
            raise ValueError("price cannot be negative.")
        if int(new_duration) <= 0:
            raise ValueError("duration_days must be greater than zero.")
        if int(new_active) not in (0, 1):
            raise ValueError("active must be 0 or 1.")

        cur = conn.execute("""
            UPDATE services
            SET name = ?,
                volume = ?,
                price = ?,
                duration_days = ?,
                active = ?,
                category = ?
            WHERE service_key = ?
        """, (
            new_name,
            new_volume,
            int(new_price),
            int(new_duration),
            int(new_active),
            new_category,
            service_key,
        ))

        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


# ---------------- CONNECTED GROUPS ----------------

def init_connected_groups_table():
    """Create/migrate the persistent registry of groups where the bot is/was connected."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS connected_groups (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                username TEXT,
                chat_type TEXT NOT NULL DEFAULT 'group',
                active INTEGER NOT NULL DEFAULT 1,
                added_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_activity_at TEXT,
                member_count INTEGER,
                admin_count INTEGER
            )
        """)

        # Safe migrations for databases created by older bot versions.
        _ensure_column(cur, "connected_groups", "last_activity_at", "TEXT")
        _ensure_column(cur, "connected_groups", "member_count", "INTEGER")
        _ensure_column(cur, "connected_groups", "admin_count", "INTEGER")

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_connected_groups_active
            ON connected_groups(active)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_connected_groups_updated
            ON connected_groups(updated_at)
        """)
        conn.commit()
    finally:
        _close_quietly(conn)


def save_connected_group(chat_id, title=None, username=None, chat_type='group'):
    """Register/update a group chat when the bot is present in it.

    Existing history is preserved: re-registering a known group activates it
    again instead of creating a duplicate row.
    """
    init_connected_groups_table()
    chat_id = int(chat_id)
    timestamp = now_text()
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO connected_groups(
                chat_id, title, username, chat_type, active,
                added_at, updated_at, last_activity_at
            )
            VALUES(?,?,?,?,1,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title,
                username=excluded.username,
                chat_type=excluded.chat_type,
                active=1,
                updated_at=excluded.updated_at
        """, (
            chat_id,
            title,
            username,
            str(chat_type or 'group'),
            timestamp,
            timestamp,
            timestamp,
        ))
        conn.commit()
    finally:
        _close_quietly(conn)


def get_connected_group(chat_id):
    """Return one registered group by Telegram chat id."""
    init_connected_groups_table()
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM connected_groups WHERE chat_id=? LIMIT 1",
            (int(chat_id),),
        ).fetchone()
    finally:
        _close_quietly(conn)


def set_connected_group_active(chat_id, active):
    """Explicitly set a group's active state."""
    init_connected_groups_table()
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE connected_groups SET active=?, updated_at=? WHERE chat_id=?",
            (1 if active else 0, now_text(), int(chat_id)),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def deactivate_connected_group(chat_id):
    """Mark a group inactive when the bot leaves/is removed from it."""
    return set_connected_group_active(chat_id, False)


def reactivate_connected_group(chat_id):
    """Reactivate a previously registered group without losing its history."""
    return set_connected_group_active(chat_id, True)


def delete_connected_group(chat_id):
    """Permanently remove a group from the registry."""
    init_connected_groups_table()
    conn = get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM connected_groups WHERE chat_id=?",
            (int(chat_id),),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def record_connected_group_activity(chat_id, activity_at=None):
    """Update the last activity timestamp for a registered active group."""
    init_connected_groups_table()
    timestamp = activity_at or now_text()
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE connected_groups
            SET last_activity_at=?, updated_at=?
            WHERE chat_id=? AND active=1
        """, (timestamp, now_text(), int(chat_id)))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def update_connected_group_stats(chat_id, member_count=None, admin_count=None):
    """Persist the latest Telegram member/admin counts for a group."""
    init_connected_groups_table()
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE connected_groups
            SET member_count=?,
                admin_count=?,
                updated_at=?
            WHERE chat_id=?
        """, (
            None if member_count is None else int(member_count),
            None if admin_count is None else int(admin_count),
            now_text(),
            int(chat_id),
        ))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def get_connected_group_stats(chat_id):
    """Return persisted member/admin statistics for one group."""
    group = get_connected_group(chat_id)
    if group is None:
        return None
    return {
        "member_count": group["member_count"],
        "admin_count": group["admin_count"],
        "last_activity_at": group["last_activity_at"],
        "updated_at": group["updated_at"],
    }


def get_connected_groups(active_only=True):
    """Return registered groups sorted by status and title."""
    init_connected_groups_table()
    conn = get_connection()
    try:
        if active_only:
            return conn.execute("""
                SELECT * FROM connected_groups
                WHERE active=1
                ORDER BY title COLLATE NOCASE ASC, chat_id ASC
            """).fetchall()
        return conn.execute("""
            SELECT * FROM connected_groups
            ORDER BY active DESC, title COLLATE NOCASE ASC, chat_id ASC
        """).fetchall()
    finally:
        _close_quietly(conn)


# ---------------- PERSISTENT USER NOTIFICATIONS ----------------

def create_notification(kind, message):
    """Create a persistent notification campaign and return its numeric id."""
    kind = str(kind or "general").strip()
    message = str(message or "").strip()
    if not message:
        raise ValueError("Notification message cannot be empty.")
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO notifications(kind, message, created_at) VALUES(?,?,?)",
            (kind, message, now_text()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        _close_quietly(conn)


def notification_delivery_claimed(notification_id, user_id):
    """Atomically reserve a notification for a user; True means it was reserved now.

    Reserving before the Telegram API call guarantees that a successful restart
    cannot resend the same campaign. If Telegram rejects the send, the campaign
    is still considered attempted rather than being retried on every restart.
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO notification_deliveries(notification_id,user_id,sent_at) VALUES(?,?,?)",
            (int(notification_id), int(user_id), now_text()),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def notification_was_delivered(notification_id, user_id):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM notification_deliveries WHERE notification_id=? AND user_id=? LIMIT 1",
            (int(notification_id), int(user_id)),
        ).fetchone()
        return row is not None
    finally:
        _close_quietly(conn)



# ---------------- ADMIN PERMISSIONS / AUDIT ----------------

def get_admin_permissions(admin_id):
    conn=get_connection()
    try:
        rows=conn.execute("SELECT permission,enabled FROM admin_permissions WHERE admin_id=?",(int(admin_id),)).fetchall()
        return {str(r["permission"]): bool(r["enabled"]) for r in rows}
    finally: _close_quietly(conn)

def set_admin_permission(admin_id, permission, enabled):
    conn=get_connection()
    try:
        conn.execute("INSERT INTO admin_permissions(admin_id,permission,enabled) VALUES(?,?,?) ON CONFLICT(admin_id,permission) DO UPDATE SET enabled=excluded.enabled",(int(admin_id),str(permission),1 if enabled else 0))
        conn.commit()
    finally: _close_quietly(conn)

def has_admin_permission(admin_id, permission):
    row=get_connection()
    conn=row
    try:
        r=conn.execute("SELECT enabled FROM admin_permissions WHERE admin_id=? AND permission=?",(int(admin_id),str(permission))).fetchone()
        # Permissions are opt-in for secondary admins. A missing row means OFF.
        # The main admin bypasses this check in bot.py, so changing this default
        # does not remove the main admin's operational access.
        return False if r is None else bool(r[0])
    finally: _close_quietly(conn)

def log_admin_action(admin_id, action, target=None, details=None):
    conn=get_connection()
    try:
        conn.execute("INSERT INTO admin_logs(admin_id,action,target,details,created_at) VALUES(?,?,?,?,?)",(int(admin_id),str(action),None if target is None else str(target),None if details is None else str(details),now_text()))
        conn.commit()
    finally: _close_quietly(conn)

def get_admin_logs(limit=50):
    conn=get_connection()
    try: return conn.execute("SELECT * FROM admin_logs ORDER BY id DESC LIMIT ?",(int(limit),)).fetchall()
    finally: _close_quietly(conn)

# ---------------- REFERRALS ----------------

def get_referral_settings():
    conn=get_connection()
    try:
        rows=conn.execute("SELECT key,value FROM settings WHERE key IN ('referral_invite_reward','referral_purchase_reward','referral_enabled')").fetchall()
        data={r["key"]:r["value"] for r in rows}
        return {"invite_reward": int(data.get("referral_invite_reward","0") or 0), "purchase_reward": int(data.get("referral_purchase_reward","0") or 0), "enabled": data.get("referral_enabled","1")}
    finally: _close_quietly(conn)

def set_referral_setting(key,value):
    mapping={"invite_reward":"referral_invite_reward","purchase_reward":"referral_purchase_reward","enabled":"referral_enabled"}
    set_setting(mapping.get(key,key),value)

def register_referral(user_id, referrer_id):
    """Register a first-time referral safely, even if /start is retried concurrently."""
    user_id = int(user_id)
    referrer_id = int(referrer_id)
    if user_id <= 0 or referrer_id <= 0 or user_id == referrer_id:
        return False
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM users WHERE user_id=? LIMIT 1", (referrer_id,)).fetchone() is None:
            conn.rollback()
            return False
        if conn.execute("SELECT 1 FROM referrals WHERE user_id=? LIMIT 1", (user_id,)).fetchone() is not None:
            conn.commit()
            return False
        conn.execute("INSERT INTO referrals(user_id,referrer_id,created_at) VALUES(?,?,?)", (user_id, referrer_id, now_text()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    except Exception:
        conn.rollback()
        raise
    finally:
        _close_quietly(conn)

def get_referral_info(user_id):
    conn=get_connection()
    try: return conn.execute("SELECT * FROM referrals WHERE user_id=?",(int(user_id),)).fetchone()
    finally: _close_quietly(conn)

def _insert_referral_reward(referrer_id, referred_user_id, reward_type, amount, order_code=None):
    """Record a referral reward exactly once for the relevant event.

    Invite rewards have no order_code, so SQLite's UNIQUE constraint would
    otherwise allow duplicates because NULL values are not considered equal.
    Serialize the check+insert and explicitly reject a second invite reward.
    """
    referrer_id = int(referrer_id)
    referred_user_id = int(referred_user_id)
    reward_type = str(reward_type)
    amount = int(amount)
    if amount <= 0:
        return False

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        if reward_type == "invite":
            existing = conn.execute(
                "SELECT 1 FROM referral_rewards "
                "WHERE referrer_id=? AND referred_user_id=? AND reward_type='invite' "
                "LIMIT 1",
                (referrer_id, referred_user_id),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return False
        elif order_code is not None:
            existing = conn.execute(
                "SELECT 1 FROM referral_rewards "
                "WHERE referrer_id=? AND referred_user_id=? AND reward_type=? AND order_code=? "
                "LIMIT 1",
                (referrer_id, referred_user_id, reward_type, str(order_code)),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return False

        cur = conn.execute(
            "INSERT INTO referral_rewards("
            "referrer_id,referred_user_id,reward_type,amount,order_code,created_at"
            ") VALUES(?,?,?,?,?,?)",
            (referrer_id, referred_user_id, reward_type, amount, order_code, now_text()),
        )
        conn.commit()
        return cur.rowcount == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        _close_quietly(conn)

def reward_referral_invite(referred_user_id):
    settings=get_referral_settings(); info=get_referral_info(referred_user_id)
    if not info or settings["enabled"]=="0": return None
    amount=settings["invite_reward"]
    if not _insert_referral_reward(info["referrer_id"],referred_user_id,"invite",amount): return None
    conn=get_connection()
    try:
        conn.execute("UPDATE referrals SET qualified_at=COALESCE(qualified_at,?) WHERE user_id=?",(now_text(),int(referred_user_id))); conn.commit()
    finally: _close_quietly(conn)
    return int(info["referrer_id"]),amount

def reward_referral_purchase(order_code,user_id):
    settings=get_referral_settings(); info=get_referral_info(user_id)
    if not info or settings["enabled"]=="0": return None
    amount=settings["purchase_reward"]
    if not _insert_referral_reward(info["referrer_id"],user_id,"purchase",amount,order_code): return None
    return int(info["referrer_id"]),amount

def get_referral_stats(referrer_id):
    conn=get_connection()
    try:
        invited=conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?",(int(referrer_id),)).fetchone()[0]
        rewards=conn.execute("SELECT COALESCE(SUM(amount),0) FROM referral_rewards WHERE referrer_id=?",(int(referrer_id),)).fetchone()[0]
        purchases=conn.execute("SELECT COUNT(*) FROM referral_rewards WHERE referrer_id=? AND reward_type='purchase'",(int(referrer_id),)).fetchone()[0]
        return {"invited":int(invited),"rewards":int(rewards),"purchases":int(purchases)}
    finally: _close_quietly(conn)

# ---------------- DATA CLEANUP ----------------

def clear_data_section(section):
    """Delete only the records belonging to a selected admin cleanup section.
    Returns the number of deleted rows. Core settings/admin records are never removed.
    """
    actions = {
        "users": [("users", "DELETE FROM users")],
        "orders": [("orders", "DELETE FROM orders")],
        "support": [("support_tickets", "DELETE FROM support_tickets")],
        "suggestions": [("suggestion_tickets", "DELETE FROM suggestion_tickets")],
        "discounts": [
            ("discount_usages", "DELETE FROM discount_usages"),
            ("discount_codes", "DELETE FROM discount_codes"),
        ],
        "notifications": [
            ("notification_deliveries", "DELETE FROM notification_deliveries"),
            ("notifications", "DELETE FROM notifications"),
        ],
        "receipts": [("receipt_reviews", "DELETE FROM receipt_reviews")],
        "groups": [("connected_groups", "DELETE FROM connected_groups")],
        "services": [("services", "DELETE FROM services")],
    }
    if section not in actions:
        raise ValueError(f"Unknown cleanup section: {section}")

    conn = get_connection()
    total = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for _, sql in actions[section]:
            cur = conn.execute(sql)
            total += max(0, int(cur.rowcount))
        # Group-specific settings are disposable metadata and are removed only
        # when the connected-groups section is explicitly cleared.
        if section == "groups":
            cur = conn.execute(
                "DELETE FROM settings WHERE key LIKE 'connected_group:%'"
            )
            total += max(0, int(cur.rowcount))
        conn.commit()
        return total
    except Exception:
        conn.rollback()
        raise
    finally:
        _close_quietly(conn)


# ---------------- ADMIN DASHBOARD / DATABASE HEALTH ----------------

def get_admin_dashboard_stats():
    """Return a consistent snapshot for the admin dashboard."""
    conn = get_connection()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        users = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] or 0)
        orders = int(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] or 0)
        delivered = int(
            conn.execute(
                "SELECT COUNT(*) FROM orders WHERE status IN ('delivered','expired')"
            ).fetchone()[0] or 0
        )
        pending = int(
            conn.execute(
                "SELECT COUNT(*) FROM orders "
                "WHERE status IN ('waiting','waiting_payment','payment_review','approved','delivery_pending')"
            ).fetchone()[0] or 0
        )
        revenue = int(
            conn.execute(
                "SELECT COALESCE(SUM(COALESCE(final_price, price)),0) "
                "FROM orders WHERE status IN ('delivered','expired')"
            ).fetchone()[0] or 0
        )
        today_revenue = int(
            conn.execute(
                "SELECT COALESCE(SUM(COALESCE(final_price, price)),0) "
                "FROM orders WHERE status IN ('delivered','expired') "
                "AND substr(created_at,1,10)=?",
                (today,),
            ).fetchone()[0] or 0
        )
        active_services = int(
            conn.execute("SELECT COUNT(*) FROM services WHERE active=1").fetchone()[0] or 0
        )
        open_support = int(
            conn.execute(
                "SELECT COUNT(*) FROM support_tickets WHERE status='open'"
            ).fetchone()[0] or 0
        )
        open_suggestions = int(
            conn.execute(
                "SELECT COUNT(*) FROM suggestion_tickets WHERE status='open'"
            ).fetchone()[0] or 0
        )
        admins = int(conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0] or 0)
        referrals = int(conn.execute("SELECT COUNT(*) FROM referrals").fetchone()[0] or 0)
        return {
            "users": users,
            "orders": orders,
            "delivered": delivered,
            "pending": pending,
            "revenue": revenue,
            "today_revenue": today_revenue,
            "active_services": active_services,
            "open_support": open_support,
            "open_suggestions": open_suggestions,
            "admins": admins,
            "referrals": referrals,
        }
    finally:
        _close_quietly(conn)


def get_database_health():
    """Check SQLite integrity and basic file/journal information."""
    try:
        size_mb = os.path.getsize(DB_NAME) / (1024 * 1024)
    except OSError:
        size_mb = 0.0

    conn = get_connection()
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        return {
            "ok": str(integrity).lower() == "ok",
            "integrity": str(integrity),
            "journal_mode": str(journal_mode),
            "size_mb": float(size_mb),
        }
    finally:
        _close_quietly(conn)
