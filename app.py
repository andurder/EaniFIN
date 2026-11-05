import os
import sqlite3
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, jsonify, g

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'finance.db')
ACCOUNT_NAMES = ['cash', 'bank1', 'bank2']
ACCOUNT_TYPES = ['cash', 'online app', 'bank']
TODAY = date.today().isoformat()


def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def _column_exists(table, column):
    db = get_db()
    rows = db.execute(f'PRAGMA table_info({table})').fetchall()
    return any(r['name'] == column for r in rows)


def init_db():
    with app.app_context():
        db = get_db()
        c = db.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'cash',
            balance REAL NOT NULL DEFAULT 0,
            initial_balance REAL NOT NULL DEFAULT 0
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS monthly_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            month TEXT NOT NULL,
            account_id INTEGER NOT NULL,
            initial_money REAL NOT NULL DEFAULT 0,
            UNIQUE(month, account_id)
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            type TEXT NOT NULL,
            description TEXT,
            date TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS spending_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_date TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            spent REAL NOT NULL DEFAULT 0,
            start_allowance REAL NOT NULL DEFAULT 0,
            saved_end REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )''')
        # migrations for existing DBs
        if not _column_exists('accounts', 'type'):
            c.execute("ALTER TABLE accounts ADD COLUMN type TEXT NOT NULL DEFAULT 'cash'")
        if not _column_exists('accounts', 'initial_balance'):
            c.execute("ALTER TABLE accounts ADD COLUMN initial_balance REAL NOT NULL DEFAULT 0")
        if not _column_exists('spending_events', 'spent'):
            c.execute("ALTER TABLE spending_events ADD COLUMN spent REAL NOT NULL DEFAULT 0")
        if not _column_exists('spending_events', 'start_allowance'):
            c.execute("ALTER TABLE spending_events ADD COLUMN start_allowance REAL NOT NULL DEFAULT 0")
        if not _column_exists('spending_events', 'saved_end'):
            c.execute("ALTER TABLE spending_events ADD COLUMN saved_end REAL NOT NULL DEFAULT 0")
        # seed accounts
        for i, name in enumerate(ACCOUNT_NAMES, 1):
            c.execute('INSERT OR IGNORE INTO accounts (id, name, type, balance, initial_balance) VALUES (?, ?, ?, 0, 0)',
                      (i, name, ACCOUNT_TYPES[min(i - 1, len(ACCOUNT_TYPES) - 1)]))
        db.commit()


init_db()


def current_month():
    return date.today().strftime('%Y-%m')


def validate_date(d):
    """Return None if valid (not future), else error message."""
    if not d:
        return 'date is required'
    try:
        d_obj = date.fromisoformat(d)
    except ValueError:
        return 'invalid date'
    if d_obj > date.today():
        return 'cannot log a date in the future'
    return None


def month_range(month_str):
    y, m = month_str.split('-')
    first = date(int(y), int(m), 1)
    if int(m) == 12:
        last = date(int(y) + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(int(y), int(m) + 1, 1) - timedelta(days=1)
    return first, last


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/accounts', methods=['GET'])
def list_accounts():
    db = get_db()
    rows = db.execute(
        'SELECT id, name, type, balance, initial_balance FROM accounts ORDER BY id'
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/accounts/<int:account_id>', methods=['POST'])
def update_account(account_id):
    data = request.get_json(force=True) or {}
    db = get_db()
    acct = db.execute('SELECT id FROM accounts WHERE id = ?', (account_id,)).fetchone()
    if acct is None:
        return jsonify({'error': 'account not found'}), 404

    fields = []
    params = []
    if 'name' in data:
        fields.append('name = ?'); params.append(data['name'])
    if 'type' in data:
        if data['type'] not in ACCOUNT_TYPES:
            return jsonify({'error': 'invalid type'}), 400
        fields.append('type = ?'); params.append(data['type'])
    if 'initial_balance' in data:
        fields.append('initial_balance = ?'); params.append(float(data['initial_balance']))
    if fields:
        params.append(account_id)
        db.execute(f'UPDATE accounts SET {", ".join(fields)} WHERE id = ?', params)
        db.commit()
    row = db.execute('SELECT id, name, type, balance, initial_balance FROM accounts WHERE id = ?', (account_id,)).fetchone()
    return jsonify(dict(row))


@app.route('/api/accounts/<int:account_id>/transaction', methods=['POST'])
def log_transaction(account_id):
    data = request.get_json(force=True) or {}
    amount = float(data.get('amount', 0))
    ttype = data.get('type', 'credit')
    description = data.get('description', '')
    log_date = data.get('date') or TODAY

    err = validate_date(log_date)
    if err:
        return jsonify({'error': err}), 400
    if ttype not in ('credit', 'debit'):
        return jsonify({'error': 'type must be credit or debit'}), 400

    db = get_db()
    acct = db.execute('SELECT balance FROM accounts WHERE id = ?', (account_id,)).fetchone()
    if acct is None:
        return jsonify({'error': 'account not found'}), 404

    signed = amount if ttype == 'credit' else -amount
    new_balance = acct['balance'] + signed

    db.execute('UPDATE accounts SET balance = ? WHERE id = ?', (new_balance, account_id))
    db.execute(
        'INSERT INTO transactions (account_id, amount, type, description, date) VALUES (?, ?, ?, ?, ?)',
        (account_id, signed, ttype, description, log_date)
    )
    db.commit()
    return jsonify({'id': account_id, 'balance': new_balance})


@app.route('/api/monthly', methods=['GET'])
def get_monthly():
    month = request.args.get('month') or current_month()
    db = get_db()
    rows = db.execute(
        '''SELECT a.id, a.name, a.type, a.balance,
                  COALESCE(m.initial_money, a.initial_balance) AS initial_money
           FROM accounts a
           LEFT JOIN monthly_settings m ON m.account_id = a.id AND m.month = ?
           ORDER BY a.id''', (month,)
    ).fetchall()
    result = [dict(r) for r in rows]
    total_initial = sum(r['initial_money'] for r in result)
    total_current = sum(r['balance'] for r in result)
    return jsonify({'month': month, 'accounts': result,
                    'total_initial': total_initial, 'total_current': total_current})


@app.route('/api/monthly', methods=['POST'])
def set_monthly():
    data = request.get_json(force=True) or {}
    account_id = int(data.get('account_id'))
    month = data.get('month') or current_month()
    initial = float(data.get('initial_money', 0))
    db = get_db()
    db.execute(
        'INSERT INTO monthly_settings (month, account_id, initial_money) VALUES (?, ?, ?) '
        'ON CONFLICT(month, account_id) DO UPDATE SET initial_money = excluded.initial_money',
        (month, account_id, initial)
    )
    # also keep accounts.initial_balance in sync for display fallback
    db.execute('UPDATE accounts SET initial_balance = ? WHERE id = ?', (initial, account_id))
    db.commit()
    return jsonify({'month': month, 'account_id': account_id, 'initial_money': initial})


@app.route('/api/events', methods=['GET'])
def list_events():
    db = get_db()
    rows = db.execute(
        'SELECT id, event_date, title, description, spent, start_allowance, saved_end, created_at '
        'FROM spending_events ORDER BY event_date DESC, id DESC'
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/events', methods=['POST'])
def add_event():
    data = request.get_json(force=True) or {}
    event_date = data.get('event_date') or TODAY
    err = validate_date(event_date)
    if err:
        return jsonify({'error': err}), 400
    title = data.get('title', '')
    description = data.get('description', '')
    start_allowance = float(data.get('start_allowance', 0))
    spent = float(data.get('spent', 0))
    saved_end = start_allowance - spent
    created_at = datetime.now().isoformat()

    db = get_db()
    cur = db.execute(
        'INSERT INTO spending_events (event_date, title, description, spent, start_allowance, saved_end, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        (event_date, title, description, spent, start_allowance, saved_end, created_at)
    )
    db.commit()
    return jsonify({'id': cur.lastrowid, 'saved_end': saved_end})


@app.route('/api/chart', methods=['GET'])
def chart_data():
    month = request.args.get('month') or current_month()
    first, last = month_range(month)
    db = get_db()
    rows = db.execute(
        'SELECT date, SUM(amount) AS daily_delta FROM transactions '
        'WHERE date >= ? AND date <= ? GROUP BY date ORDER BY date',
        (first.isoformat(), last.isoformat())
    ).fetchall()
    total_now = db.execute('SELECT SUM(balance) AS total FROM accounts').fetchone()['total'] or 0
    all_deltas = {r['date']: r['daily_delta'] for r in rows}
    running = total_now
    d = last
    while d >= first:
        iso = d.isoformat()
        if iso in all_deltas:
            running -= all_deltas[iso]
        d -= timedelta(days=1)
    labels = []
    values = []
    d = first
    while d <= last:
        iso = d.isoformat()
        if iso in all_deltas:
            running += all_deltas[iso]
        labels.append(d.strftime('%b %d'))
        values.append(round(running, 2))
        d += timedelta(days=1)
    return jsonify({'labels': labels, 'datasets': [{
        'label': 'Total Balance',
        'data': values,
        'borderColor': '#0d6efd',
        'backgroundColor': 'rgba(13, 110, 253, 0.1)',
        'fill': True,
        'tension': 0.3,
        'pointRadius': 3,
    }]})


if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5000)
