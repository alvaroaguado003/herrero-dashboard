import os, time, sqlite3, json, csv, io
from dotenv import load_dotenv
load_dotenv()
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
import requests

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'herrero-dev-secret-change-in-prod')

GHL_API           = 'https://services.leadconnectorhq.com'
GHL_TOKEN         = os.environ.get('GHL_TOKEN', 'pit-2233c8f8-145b-4646-933a-540690968625')
GHL_LOC           = os.environ.get('GHL_LOCATION_ID', 'YXM7CtkzqZwuvW7XBrFf')
PIPELINE_ID       = os.environ.get('PIPELINE_ID', 'sy43mg1hzwROLvmbnKaM')
CLIENTES_PIPELINE = os.environ.get('CLIENTES_PIPELINE_ID', 'zeJs5CrqWj7vNZWdAPo4')
DB                = os.environ.get('DB_PATH', 'dashboard.db')
CACHE_TTL         = 300

IG_TOKEN      = os.environ.get('IG_TOKEN', '')
IG_ACCOUNT_ID = os.environ.get('IG_ACCOUNT_ID', '')
IG_API        = 'https://graph.facebook.com/v25.0'

SHEETS = {
    'disparos':       os.environ.get('SHEET_DISPAROS',       ''),
    'conversaciones': os.environ.get('SHEET_CONVERSACIONES', ''),
    'agendas':        os.environ.get('SHEET_AGENDAS',        ''),
}

# ── Pipeline de Ventas stages ─────────────────────────────────────────────────
STAGES_META = [
    {'id': 'd6a4d9d2-a2a4-4b41-8a30-b3dc1058adb7', 'name': 'Llamada Agendada',        'group': 'agenda',  'color': '#2563EB'},
    {'id': '6197e41a-c477-4177-8900-d639bae53181', 'name': 'No Se Presenta',          'group': 'nsp',     'color': '#78716C'},
    {'id': 'fd7b5783-7c9a-445c-9afa-c25769001ef2', 'name': 'Seguimiento',             'group': 'gestion', 'color': '#F59E0B'},
    {'id': '2381edeb-835b-43b6-a902-e199b2e65afc', 'name': 'Seguimiento con Recurso', 'group': 'gestion', 'color': '#EA580C'},
    {'id': '1e5ff1fc-0f72-4100-a842-c2b5f52e37d3', 'name': 'Cancelado',              'group': 'lost',    'color': '#374151'},
    {'id': 'b9be8d54-6f74-4ccd-b773-f76317705240', 'name': 'Venta Cerrada',           'group': 'won',     'color': '#16A34A'},
    {'id': '9d75f1e3-c539-4751-bc20-34a49f2bb190', 'name': 'Venta Perdida',           'group': 'lost',    'color': '#DC2626'},
]
STAGE_GROUP = {s['id']: s['group'] for s in STAGES_META}
STAGE_NAME  = {s['id']: s['name']  for s in STAGES_META}
STAGE_COLOR = {s['id']: s['color'] for s in STAGES_META}

# ── Progreso de Clientes stages ───────────────────────────────────────────────
CLIENTES_STAGES = [
    {'id': 'fbb84842-bafb-4b41-8bac-70357e484d75', 'name': 'Onboarding',   'color': '#3B82F6', 'group': 'active'},
    {'id': 'cc5428c4-8478-4f50-9e06-4921ad21b8fc', 'name': 'En Programa',  'color': '#F59E0B', 'group': 'active'},
    {'id': '8d4dcb94-7409-4898-a1d5-3e111a8ee3d2', 'name': 'Offboarding',  'color': '#EF4444', 'group': 'done'},
]
CLIENTES_STAGE_NAME  = {s['id']: s['name']  for s in CLIENTES_STAGES}
CLIENTES_STAGE_COLOR = {s['id']: s['color'] for s in CLIENTES_STAGES}
CLIENTES_STAGE_GROUP = {s['id']: s['group'] for s in CLIENTES_STAGES}
CLIENTES_STAGE_POS   = {s['id']: i for i, s in enumerate(CLIENTES_STAGES)}

_cache = {}

# ── DB ────────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT,
        password_hash TEXT NOT NULL,
        role TEXT DEFAULT 'viewer',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()

    # Auto-seed admin from env vars on every startup (safe on Render restarts)
    admin_user = os.environ.get('ADMIN_USER', 'admin')
    admin_pass = os.environ.get('ADMIN_PASS', '')
    if admin_pass:
        try:
            conn.execute(
                'INSERT INTO users (username, password_hash, role) VALUES (?,?,?)',
                (admin_user, generate_password_hash(admin_pass, method='pbkdf2:sha256'), 'admin')
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass  # user already exists

    conn.close()

# ── Auth ──────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            return jsonify({'error': 'Acceso denegado'}), 403
        return f(*args, **kwargs)
    return decorated

# ── GHL ───────────────────────────────────────────────────────────────────────
def ghl_headers():
    return {'Authorization': f'Bearer {GHL_TOKEN}', 'Version': '2021-07-28'}

def fetch_user_map():
    cache_key = 'user_map'
    now = time.time()
    if cache_key in _cache and now - _cache[cache_key]['ts'] < CACHE_TTL:
        return _cache[cache_key]['data']
    try:
        r = requests.get(f'{GHL_API}/users/', headers=ghl_headers(),
                         params={'locationId': GHL_LOC}, timeout=10)
        r.raise_for_status()
        user_map = {u['id']: u.get('name') or u.get('firstName', '—')
                    for u in r.json().get('users', [])}
    except Exception as e:
        print(f'GHL users error: {e}'); user_map = {}
    _cache[cache_key] = {'data': user_map, 'ts': now}
    return user_map

def fetch_all_opportunities(pipeline_id):
    cache_key = f'opps_{pipeline_id}'
    now = time.time()
    if cache_key in _cache and now - _cache[cache_key]['ts'] < CACHE_TTL:
        return _cache[cache_key]['data']
    all_opps, params = [], {'location_id': GHL_LOC, 'pipeline_id': pipeline_id, 'limit': 100}
    while True:
        try:
            r = requests.get(f'{GHL_API}/opportunities/search',
                             headers=ghl_headers(), params=params, timeout=15)
            r.raise_for_status()
        except Exception as e:
            print(f'GHL error: {e}'); break
        data = r.json()
        all_opps.extend(data.get('opportunities', []))
        meta = data.get('meta', {})
        if not meta.get('nextPage'): break
        params['startAfter']   = meta['startAfter']
        params['startAfterId'] = meta['startAfterId']
        time.sleep(0.15)
    _cache[cache_key] = {'data': all_opps, 'ts': now}
    return all_opps

def slim_pipeline(o):
    sid         = o.get('pipelineStageId', '')
    c           = o.get('contact', {})
    user        = o.get('user') or {}
    assigned_id = o.get('assignedTo') or ''
    user_map    = fetch_user_map()
    closer      = user.get('name') or user.get('firstName') or user_map.get(assigned_id) or '—'
    attrs       = o.get('attributions', [])
    first       = next((a for a in attrs if a.get('isFirst')), attrs[0] if attrs else {})
    return {
        'name':          o.get('name') or c.get('name', '—'),
        'email':         c.get('email', '—'),
        'stage_id':      sid,
        'stage':         STAGE_NAME.get(sid, '—'),
        'group':         STAGE_GROUP.get(sid, ''),
        'color':         STAGE_COLOR.get(sid, '#64748b'),
        'value':         o.get('monetaryValue') or 0,
        'created':       o.get('createdAt', '')[:10],
        'stage_changed': (o.get('lastStageChangeAt') or o.get('createdAt', ''))[:10],
        'src':           first.get('utmSource') or first.get('utmSessionSource') or 'Directo',
        'campaign':      first.get('utmCampaign') or '—',
        'content':       first.get('utmContent') or '—',
        'medium':        first.get('utmMedium') or '—',
        'closer':        closer,
    }

def slim_cliente(o):
    sid = o.get('pipelineStageId', '')
    c   = o.get('contact', {})
    return {
        'name':          o.get('name') or c.get('name', '—'),
        'email':         c.get('email', '—'),
        'stage_id':      sid,
        'stage':         CLIENTES_STAGE_NAME.get(sid, '—'),
        'group':         CLIENTES_STAGE_GROUP.get(sid, ''),
        'color':         CLIENTES_STAGE_COLOR.get(sid, '#64748b'),
        'pos':           CLIENTES_STAGE_POS.get(sid, 0),
        'value':         o.get('monetaryValue') or 0,
        'created':       o.get('createdAt', '')[:10],
        'stage_changed': (o.get('lastStageChangeAt') or o.get('createdAt', ''))[:10],
    }

# ── Google Sheets CSV ─────────────────────────────────────────────────────────
def fetch_sheet(url, name):
    if not url:
        return []
    cache_key = f'sheet_{name}'
    now = time.time()
    if cache_key in _cache and now - _cache[cache_key]['ts'] < CACHE_TTL:
        return _cache[cache_key]['data']
    try:
        r = requests.get(url, timeout=10, allow_redirects=True)
        text = r.text.strip()
        if not r.ok or text.startswith('<!DOCTYPE') or text.startswith('<html'):
            _cache[cache_key] = {'data': [], 'ts': now}
            return []
        reader = csv.DictReader(io.StringIO(text))
        rows = [dict(row) for row in reader]
        _cache[cache_key] = {'data': rows, 'ts': now}
        return rows
    except Exception as e:
        print(f'Sheet {name} error: {e}')
        _cache[cache_key] = {'data': [], 'ts': now}
        return []

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password_hash'], password):
            session.update({'user_id': user['id'], 'username': user['username'], 'role': user['role']})
            return redirect(url_for('dashboard'))
        error = 'Usuario o contraseña incorrectos'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    p_raw = fetch_all_opportunities(PIPELINE_ID)
    c_raw = fetch_all_opportunities(CLIENTES_PIPELINE)
    p_slim = [slim_pipeline(o) for o in p_raw]
    c_slim = [slim_cliente(o)  for o in c_raw]

    sheets = {
        'disparos':       fetch_sheet(SHEETS['disparos'],       'disparos'),
        'conversaciones': fetch_sheet(SHEETS['conversaciones'], 'conversaciones'),
        'agendas':        fetch_sheet(SHEETS['agendas'],        'agendas'),
    }

    unique_src = sorted({o['src'] for o in p_slim if o['src'] not in ('—', '')})
    unique_cam = sorted({o['campaign'] for o in p_slim if o['campaign'] not in ('—', '')})[:40]

    return render_template('dashboard.html',
        pipeline_json=p_slim,
        clientes_json=c_slim,
        stages_meta=STAGES_META,
        clientes_stages=CLIENTES_STAGES,
        sheets_json=sheets,
        unique_src=unique_src,
        unique_cam=unique_cam,
        username=session.get('username'),
        role=session.get('role'),
        ig_configured=bool(IG_TOKEN and IG_ACCOUNT_ID))

@app.route('/api/refresh')
@login_required
def api_refresh():
    for k in list(_cache.keys()):
        if k.startswith('opps_') or k.startswith('sheet_') or k == 'user_map':
            _cache.pop(k, None)
    return jsonify({'ok': True})

@app.route('/admin/users', methods=['GET'])
@admin_required
def list_users():
    conn = get_db()
    users = conn.execute('SELECT id,username,email,role,created_at FROM users').fetchall()
    conn.close()
    return jsonify([dict(u) for u in users])

@app.route('/admin/users', methods=['POST'])
@admin_required
def create_user():
    d = request.get_json()
    u, pw, em, role = d.get('username','').strip(), d.get('password',''), d.get('email',''), d.get('role','viewer')
    if not u or not pw:
        return jsonify({'error': 'username y password requeridos'}), 400
    try:
        conn = get_db()
        conn.execute('INSERT INTO users (username,email,password_hash,role) VALUES (?,?,?,?)',
                     (u, em, generate_password_hash(pw, method='pbkdf2:sha256'), role))
        conn.commit(); conn.close()
        return jsonify({'ok': True, 'username': u})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'El usuario ya existe'}), 409

@app.route('/admin/users/<int:uid>', methods=['DELETE'])
@admin_required
def delete_user(uid):
    if uid == session['user_id']:
        return jsonify({'error': 'No puedes eliminarte a ti mismo'}), 400
    conn = get_db()
    conn.execute('DELETE FROM users WHERE id=?', (uid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/instagram')
@login_required
def api_instagram():
    if not IG_TOKEN or not IG_ACCOUNT_ID:
        return jsonify({'error': 'Instagram no configurado. Añade IG_TOKEN e IG_ACCOUNT_ID en el archivo .env'}), 503
    cache_key = 'ig_data'
    now = time.time()
    if cache_key in _cache and now - _cache[cache_key]['ts'] < CACHE_TTL:
        return jsonify(_cache[cache_key]['data'])
    try:
        profile = requests.get(f'{IG_API}/{IG_ACCOUNT_ID}',
            params={'fields': 'username,followers_count,follows_count,media_count,biography',
                    'access_token': IG_TOKEN}, timeout=10).json()
        media = requests.get(f'{IG_API}/{IG_ACCOUNT_ID}/media',
            params={'fields': 'id,caption,media_type,media_url,thumbnail_url,timestamp,like_count,comments_count,permalink,video_views',
                    'limit': 50, 'access_token': IG_TOKEN}, timeout=10).json()
        result = {'profile': profile, 'media': media.get('data', [])}
        _cache[cache_key] = {'data': result, 'ts': now}
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 8081))
    app.run(debug=True, port=port)
