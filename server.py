"""Annuaire PBM: public directory + admin API (Python standard library only).

Environment:
  PORT             port to listen on (Render sets it)
  DATA_DIR         folder for the SQLite database (a Render persistent disk, e.g. /var/data)
  MASTER_EMAIL     e-mail of the principal administrator (the only one who manages accounts)
  MASTER_PASSWORD  initial password of that account (12+ characters), used only when the account is created
  RESET_MASTER_PASSWORD=1  re-applies MASTER_PASSWORD at start-up (forgotten password); remove it afterwards
"""
import base64, csv, hashlib, hmac, io, json, os, re, secrets, sqlite3, threading, time, unicodedata
import urllib.parse, urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from equipment import merge
from xlsx import read as read_xlsx

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / 'public'
DATA_DIR = Path(os.environ.get('DATA_DIR', ROOT / 'data'))
DB_PATH = DATA_DIR / 'annuaire.db'
SESSION_HOURS = 12
MAX_BODY = 4_000_000
MIN_PASSWORD = 12
MASTER, ADMIN = 'principal', 'secondaire'

db_lock = threading.Lock()
norm = lambda s: re.sub(r'\s+', ' ', unicodedata.normalize('NFD', str(s or '')).encode('ascii', 'ignore').decode().lower()).strip()
now = lambda: datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')


class Refused(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------- storage ----------

def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript('''
      CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, data TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS admins (email TEXT PRIMARY KEY, hash TEXT NOT NULL, role TEXT NOT NULL, created TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, email TEXT NOT NULL, csrf TEXT NOT NULL, expires REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, email TEXT, action TEXT, record_id TEXT);
    ''')
    return con

con = connect()

def q(sql, args=()):
    with db_lock:
        cur = con.execute(sql, args)
        con.commit()
        return cur.fetchall()

def audit(email, action, record_id=''):
    q('INSERT INTO audit(date,email,action,record_id) VALUES(?,?,?,?)', (now(), email, action, record_id))

def all_records():
    return [json.loads(r['data']) for r in q('SELECT data FROM records')]

def get_record(rid):
    rows = q('SELECT data FROM records WHERE id=?', (rid,))
    return json.loads(rows[0]['data']) if rows else None

def put_record(rec):
    q('INSERT OR REPLACE INTO records(id,data) VALUES(?,?)', (rec['id'], json.dumps(rec, ensure_ascii=False)))

def hash_password(pw):
    salt = secrets.token_bytes(16)
    return 'scrypt$' + salt.hex() + '$' + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()

def check_password(pw, stored):
    _, salt, digest = stored.split('$')
    return hmac.compare_digest(hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex(), digest)

DUMMY_HASH = hash_password(secrets.token_hex(8))

def setup():
    if not q('SELECT 1 FROM records LIMIT 1') and (ROOT / 'donnees.json').exists():
        for rec in json.loads((ROOT / 'donnees.json').read_text(encoding='utf-8')):
            put_record(rec)
        print('Données initiales chargées.')
    email = norm(os.environ.get('MASTER_EMAIL', ''))
    if not email:
        print('ATTENTION : MASTER_EMAIL non défini, aucun administrateur principal.')
        return
    pw = os.environ.get('MASTER_PASSWORD', '')
    existing = q('SELECT email FROM admins WHERE email=?', (email,))
    if not existing or os.environ.get('RESET_MASTER_PASSWORD') == '1':
        if len(pw) < MIN_PASSWORD:
            print(f'ATTENTION : MASTER_PASSWORD doit faire au moins {MIN_PASSWORD} caractères.')
            return
        q('INSERT OR REPLACE INTO admins(email,hash,role,created) VALUES(?,?,?,?)', (email, hash_password(pw), MASTER, now()))
        q('DELETE FROM sessions WHERE email=?', (email,))
        print('Mot de passe de l’administrateur principal défini.')
    # only the configured address holds the principal role
    q('UPDATE admins SET role=? WHERE email!=?', (ADMIN, email))
    q('UPDATE admins SET role=? WHERE email=?', (MASTER, email))


# ---------- records ----------

def clean_text(v, limit=500):
    return re.sub(r'\s+', ' ', str(v or '')).strip()[:limit]

def coord(v, lo, hi):
    if v in (None, ''):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise Refused('Coordonnée invalide.')
    if not lo <= f <= hi:
        raise Refused('Coordonnée hors limites.')
    return f

def clean_address(a):
    out = {k: clean_text(a.get(k)) for k in ('street', 'postalCode', 'city', 'country', 'locationSource')}
    out['locationNote'] = clean_text(a.get('locationNote'), 1000)
    out['latitude'] = coord(a.get('latitude'), -90, 90)
    out['longitude'] = coord(a.get('longitude'), -180, 180)
    if (out['latitude'] is None) != (out['longitude'] is None):
        raise Refused('Renseignez la latitude et la longitude ensemble.')
    if not out['country']:
        raise Refused('Chaque lieu doit au moins indiquer un pays (sans adresse précise, il apparaît au centre du pays).')
    return out

def clean_record(data):
    rec = {k: clean_text(data.get(k)) for k in ('lastName', 'firstName', 'profession', 'structure')}
    if not (rec['lastName'] and rec['firstName']):
        raise Refused('Le nom et le prénom sont obligatoires.')
    items = [x.strip() for x in str(data.get('equipment') or '').replace('\n', ',').split(',') if x.strip()]
    rec['equipment'] = ', '.join(dict.fromkeys(items))[:2000]
    addresses = data.get('addresses')
    if not isinstance(addresses, list) or not addresses or len(addresses) > 12:
        raise Refused('Indiquez entre 1 et 12 lieux d’exercice.')
    rec['addresses'] = [clean_address(a if isinstance(a, dict) else {}) for a in addresses]
    rec['visible'] = bool(data.get('visible'))
    rec['verified'] = bool(data.get('verified'))
    return rec

def label(rec):
    return f"{rec.get('firstName', '')} {rec.get('lastName', '')}".strip()


# ---------- geocoding ----------

FRANCE = {'france', 'la reunion', 'reunion', 'guadeloupe', 'martinique', 'guyane', 'mayotte', 'nouvelle-caledonie', 'polynesie francaise'}
nominatim_lock = threading.Lock()
nominatim_last = [0.0]

def fetch_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'annuaire-pbm/1.0', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())

def geocode(a):
    country, cp, city, street = clean_text(a.get('country')), clean_text(a.get('postalCode')), clean_text(a.get('city')), clean_text(a.get('street'))
    try:
        if norm(country) in FRANCE:
            feats = fetch_json('https://api-adresse.data.gouv.fr/search/?' + urllib.parse.urlencode({'q': f'{street} {cp} {city}', 'limit': 1}))['features']
            if not feats:
                return {'latitude': None, 'longitude': None, 'locationNote': 'Adresse non trouvée par l’IGN.', 'locationSource': ''}
            p, (lon, lat) = feats[0]['properties'], feats[0]['geometry']['coordinates']
            if p['score'] < .6:
                return {'latitude': None, 'longitude': None, 'locationSource': '',
                        'locationNote': f"Correspondance IGN incertaine : {p['label']} (score {p['score']:.2f}). Coordonnées non retenues."}
            return {'latitude': lat, 'longitude': lon, 'locationSource': 'IGN / BAN',
                    'locationNote': f"Adresse retrouvée par l’IGN : {p['label']}. Contrôle visuel recommandé."}
        with nominatim_lock:  # OpenStreetMap usage policy: at most one request per second
            time.sleep(max(0, 1.1 - (time.time() - nominatim_last[0])))
            hits = fetch_json('https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode(
                {'q': f'{street}, {cp} {city}, {country}', 'format': 'json', 'limit': 1}))
            nominatim_last[0] = time.time()
        if not hits:
            return {'latitude': None, 'longitude': None, 'locationNote': 'Adresse non trouvée par OpenStreetMap.', 'locationSource': ''}
        return {'latitude': float(hits[0]['lat']), 'longitude': float(hits[0]['lon']), 'locationSource': 'OpenStreetMap / Nominatim',
                'locationNote': f"Adresse retrouvée par OpenStreetMap : {hits[0].get('display_name', '')}. Contrôle visuel recommandé."}
    except Exception:
        return {'latitude': None, 'longitude': None, 'locationNote': 'Service de localisation indisponible, réessayez plus tard.', 'locationSource': ''}


# ---------- import (Yapla export, Excel or CSV) ----------

COLUMNS = {
    'lastName': ['nom', 'nom de famille'], 'firstName': ['prenom'], 'profession': ['profession', 'metier'],
    'postalCode': ['code postal', 'cp'], 'street': ['adresse', 'adresse professionnelle', 'rue'], 'city': ['ville'],
    'country': ['pays'], 'structure': ['structure / centre', 'structure', 'centre', 'etablissement'],
    'pbm': ['equipements pbm', 'equipement pbm', 'equipements', 'equipement'], 'other': ['autres appareils', 'autre appareil', 'autres equipements'],
    'cp2': ['adresse 2 - cp'], 'street2': ['adresse 2 - rue'], 'city2': ['adresse 2 - ville'],
    'sec2': ['adresse secondaire 2'], 'sec3': ['adresse secondaire 3'], 'sec4': ['adresse secondaire 4'],
}

def table_from_upload(filename, raw):
    if filename.lower().endswith('.xlsx'):
        sheets = read_xlsx(io.BytesIO(raw))
        return max(sheets.values(), key=len)
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = raw.decode('latin-1')
    dialect = csv.Sniffer().sniff(text[:4000], delimiters=';,\t')
    return list(csv.reader(io.StringIO(text), dialect))

def city_case(s):
    s = clean_text(s)
    return re.sub(r"[^\s\-']+", lambda m: m.group().capitalize(), s.lower()) if s.isupper() else s

def postal(v, country):
    v = clean_text(v)
    v = v[:-2] if re.fullmatch(r'\d+\.0', v) else v
    return v.zfill(5) if norm(country) in FRANCE and re.fullmatch(r'\d{4}', v) else v

def parse_import(filename, raw):
    rows = table_from_upload(filename, raw)
    if not rows:
        raise Refused('Fichier vide.')
    head = [norm(h) for h in rows[0]]
    col = {k: next((head.index(a) for a in aliases if a in head), None) for k, aliases in COLUMNS.items()}
    if col['lastName'] is None or col['firstName'] is None:
        raise Refused('Colonnes « Nom » et « Prénom » introuvables dans le fichier.')
    records, errors, excluded = [], [], 0
    for n, row in enumerate(rows[1:], start=2):
        raw_cell = lambda k: str(row[col[k]] or '') if col[k] is not None and col[k] < len(row) else ''
        get = lambda k: clean_text(raw_cell(k))
        if not any(clean_text(c) for c in row):
            continue
        equipment = merge(raw_cell('pbm'), raw_cell('other'))  # line breaks separate devices
        if not equipment:
            excluded += 1
            continue
        if not (get('lastName') and get('firstName')):
            errors.append(f'Ligne {n} : nom ou prénom manquant.')
            continue
        country = get('country') or 'France'
        addresses = [{'street': get('street'), 'postalCode': postal(get('postalCode'), country), 'city': city_case(get('city')), 'country': country}]
        if get('street2'):
            addresses.append({'street': get('street2'), 'postalCode': postal(get('cp2'), country), 'city': city_case(get('city2')), 'country': country})
        for k in ('sec2', 'sec3', 'sec4'):
            lines = [l.strip() for l in raw_cell(k).splitlines() if l.strip()]
            if len(lines) == 2 and re.match(r'\d{4,5}\s', lines[1]):
                cp, city = lines[1].split(' ', 1)
                addresses.append({'street': lines[0], 'postalCode': cp, 'city': city_case(city), 'country': country})
        if not (addresses[0]['street'] and addresses[0]['city']):
            errors.append(f"Ligne {n} ({get('firstName')} {get('lastName')}) : adresse ou ville manquante.")
            continue
        records.append({'lastName': get('lastName').upper(), 'firstName': get('firstName'), 'profession': get('profession'),
                        'structure': get('structure'), 'equipment': ', '.join(equipment), 'addresses': addresses})
    return records, errors, excluded

def same_place(a, b):
    return norm(a['street']) == norm(b['street']) and norm(a['city']) == norm(b['city'])

def find_person(index, rec):
    # registrations sometimes swap first and last name
    last, first = norm(rec['lastName']), norm(rec['firstName'])
    return index.get((last, first)) or index.get((first, last))

def apply_import(items, email, update_existing):
    index = {(norm(r['lastName']), norm(r['firstName'])): r for r in all_records()}
    added = updated = 0
    review = []
    for item in items[:1000]:
        if not isinstance(item, dict):
            continue
        new = clean_record({**item, 'visible': True, 'verified': False,
                            'addresses': [{**a, 'latitude': None, 'longitude': None} for a in item.get('addresses') or []]})
        old = find_person(index, new)
        if old is None:
            rec = {'id': secrets.token_hex(6), **new, 'addresses': [{**a, **geocode(a)} for a in new['addresses']]}
            put_record(rec)
            index[(norm(rec['lastName']), norm(rec['firstName']))] = rec
            added += 1
            continue
        if update_existing and any(new[k] and new[k] != old.get(k) for k in ('profession', 'structure', 'equipment')):
            for k in ('profession', 'structure', 'equipment'):
                old[k] = new[k] or old.get(k, '')
            put_record(old)
            updated += 1
        # addresses of existing fiches are never changed by an import: they often carry manual corrections
        for a in new['addresses']:
            if not any(same_place(a, b) for b in old['addresses']):
                review.append(f"{label(old)} : {a['street']}, {a['postalCode']} {a['city']}")
    audit(email, f'Import : {added} ajout(s), {updated} mise(s) à jour')
    return {'added': added, 'updated': updated, 'review': review}


# ---------- HTTP ----------

failed_logins = {}

def export_csv():
    out = io.StringIO()
    w = csv.writer(out, delimiter=';')
    w.writerow(['Nom', 'Prénom', 'Profession', 'Structure', 'Équipements PBM', 'Lieu', 'Adresse', 'Code postal', 'Ville', 'Pays',
                'Latitude', 'Longitude', 'Publiée', 'Vérifiée'])
    for r in sorted(all_records(), key=lambda r: (norm(r['lastName']), norm(r['firstName']))):
        for i, a in enumerate(r.get('addresses') or [{}], start=1):
            w.writerow([r['lastName'], r['firstName'], r.get('profession', ''), r.get('structure', ''), r.get('equipment', ''), i,
                        a.get('street', ''), a.get('postalCode', ''), a.get('city', ''), a.get('country', ''),
                        a.get('latitude') if a.get('latitude') is not None else '', a.get('longitude') if a.get('longitude') is not None else '',
                        'oui' if r.get('visible') else 'non', 'oui' if r.get('verified') else 'non'])
    return ('﻿' + out.getvalue()).encode('utf-8')

TYPES = {'.html': 'text/html; charset=utf-8', '.json': 'application/json', '.geojson': 'application/json',
         '.jpg': 'image/jpeg', '.png': 'image/png', '.svg': 'image/svg+xml', '.css': 'text/css', '.js': 'text/javascript'}


class Handler(BaseHTTPRequestHandler):
    server_version = 'AnnuairePBM'

    def log_message(self, fmt, *args):
        print('%s %s' % (self.command, self.path.split('?')[0]), args[1] if len(args) > 1 else '')

    def client_ip(self):
        return (self.headers.get('X-Forwarded-For') or self.client_address[0]).split(',')[0].strip()

    def send(self, status, body, ctype='application/json; charset=utf-8', headers=()):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'same-origin')
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def json(self, data, status=200, headers=()):
        self.send(status, json.dumps(data, ensure_ascii=False).encode(), headers=headers)

    def session(self):
        cookie = self.headers.get('Cookie', '')
        m = re.search(r'(?:^|;\s*)pbm_session=([A-Za-z0-9_\-]+)', cookie)
        if not m:
            return None
        rows = q('SELECT s.email, s.csrf, s.expires, a.role FROM sessions s JOIN admins a ON a.email=s.email WHERE s.token=?', (m.group(1),))
        if not rows or rows[0]['expires'] < time.time():
            return None
        return {'email': rows[0]['email'], 'csrf': rows[0]['csrf'], 'role': rows[0]['role'], 'token': m.group(1)}

    def cookie(self, value, max_age):
        secure = '; Secure' if self.headers.get('X-Forwarded-Proto') == 'https' else ''
        return ('Set-Cookie', f'pbm_session={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}')

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path.startswith('/api/'):
                return self.api_get(path, self.session())
            return self.static(path)
        except Refused as e:
            self.json({'error': str(e)}, e.status)

    def static(self, path):
        if path in ('/', '/admin', '/index.html'):
            path = '/index.html'
        target = (PUBLIC / path.lstrip('/')).resolve()
        if PUBLIC not in target.parents or not target.is_file():
            return self.send(404, 'Page introuvable'.encode(), 'text/plain; charset=utf-8')
        cache = 'no-cache' if target.suffix == '.html' else 'public, max-age=86400'
        self.send(200, target.read_bytes(), TYPES.get(target.suffix, 'application/octet-stream'), [('Cache-Control', cache)])

    def api_get(self, path, s):
        if path == '/api/me':
            return self.json({'email': s['email'], 'csrf': s['csrf'], 'role': s['role']} if s else {'email': None, 'csrf': None, 'role': None},
                             headers=[('Cache-Control', 'no-store')])
        if path == '/api/records':
            recs = all_records()
            return self.json(recs if s else [r for r in recs if r.get('visible')], headers=[('Cache-Control', 'no-store')])
        if not s:
            raise Refused('Connexion requise.', 401)
        if path == '/api/audit':
            return self.json([dict(r) for r in q('SELECT date,email,action,record_id FROM audit ORDER BY id DESC LIMIT 100')])
        if path == '/api/export':
            name = 'annuaire-pbm-' + datetime.now().strftime('%Y-%m-%d') + '.csv'
            return self.send(200, export_csv(), 'text/csv; charset=utf-8', [('Content-Disposition', f'attachment; filename="{name}"')])
        if path == '/api/admins':
            self.need_master(s)
            return self.json([dict(r) for r in q('SELECT email, role, created FROM admins ORDER BY role=? DESC, email', (MASTER,))])
        raise Refused('Adresse inconnue.', 404)

    def need_master(self, s):
        if s['role'] != MASTER:
            raise Refused('Seul l’administrateur principal peut gérer les accès.', 403)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            size = int(self.headers.get('Content-Length') or 0)
            if size > MAX_BODY:
                raise Refused('Envoi trop volumineux.', 413)
            try:
                data = json.loads(self.rfile.read(size) or b'{}')
            except ValueError:
                raise Refused('Requête invalide.')
            if not isinstance(data, dict):
                raise Refused('Requête invalide.')
            if path == '/api/login':
                return self.login(data)
            s = self.session()
            if not s:
                raise Refused('Session expirée : reconnectez-vous.', 401)
            if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), s['csrf']):
                raise Refused('Session expirée : rechargez la page.', 403)
            self.api_post(path, data, s)
        except Refused as e:
            self.json({'error': str(e)}, e.status)

    def login(self, data):
        ip, t = self.client_ip(), time.time()
        recent = [x for x in failed_logins.get(ip, []) if t - x < 900]
        if len(recent) >= 8:
            raise Refused('Trop de tentatives : réessayez dans 15 minutes.', 429)
        email, pw = norm(data.get('email')), str(data.get('password') or '')
        rows = q('SELECT hash, role FROM admins WHERE email=?', (email,))
        ok = check_password(pw, rows[0]['hash'] if rows else DUMMY_HASH) and bool(rows)
        if not ok:
            failed_logins[ip] = recent + [t]
            raise Refused('E-mail ou mot de passe incorrect.', 401)
        failed_logins.pop(ip, None)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        q('DELETE FROM sessions WHERE expires<?', (t,))
        q('INSERT INTO sessions VALUES(?,?,?,?)', (token, email, csrf, t + SESSION_HOURS * 3600))
        audit(email, 'Connexion')
        self.json({'email': email, 'csrf': csrf, 'role': rows[0]['role']}, headers=[self.cookie(token, SESSION_HOURS * 3600)])

    def api_post(self, path, data, s):
        email = s['email']
        if path == '/api/logout':
            q('DELETE FROM sessions WHERE token=?', (s['token'],))
            return self.json({'ok': True}, headers=[self.cookie('', 0)])
        if path == '/api/records/save':
            rec = clean_record(data)
            rid = str(data.get('id') or '')
            if rid and not get_record(rid):
                raise Refused('Fiche introuvable (supprimée entre-temps ?).', 404)
            rec['id'] = rid or secrets.token_hex(6)
            put_record(rec)
            audit(email, ('Fiche modifiée : ' if rid else 'Fiche créée : ') + label(rec), rec['id'])
            return self.json(rec)
        if path == '/api/records/delete':
            rec = get_record(str(data.get('id') or ''))
            if not rec:
                raise Refused('Fiche introuvable.', 404)
            q('DELETE FROM records WHERE id=?', (rec['id'],))
            audit(email, 'Fiche supprimée : ' + label(rec), rec['id'])
            return self.json({'ok': True})
        if path == '/api/geocode':
            if data.get('consent') is not True:
                raise Refused('Accord requis pour envoyer l’adresse au service de localisation.')
            return self.json(geocode(data))
        if path == '/api/import/preview':
            try:
                raw = base64.b64decode(str(data.get('content') or ''), validate=True)
            except ValueError:
                raise Refused('Fichier illisible.')
            try:
                records, errors, excluded = parse_import(str(data.get('filename') or ''), raw)
            except Refused:
                raise
            except Exception:
                raise Refused('Fichier illisible : utilisez l’export Excel (.xlsx) ou CSV de Yapla.')
            index = {(norm(r['lastName']), norm(r['firstName'])): r for r in all_records()}
            news = sum(find_person(index, r) is None for r in records)
            return self.json({'records': records, 'errors': errors, 'excluded': excluded, 'new': news, 'known': len(records) - news})
        if path == '/api/import/apply':
            items = data.get('records')
            if not isinstance(items, list):
                raise Refused('Aucune fiche à importer.')
            return self.json(apply_import(items, email, data.get('updateExisting') is True))
        if path == '/api/password':
            row = q('SELECT hash FROM admins WHERE email=?', (email,))[0]
            if not check_password(str(data.get('currentPassword') or ''), row['hash']):
                raise Refused('Mot de passe actuel incorrect.', 403)
            new = str(data.get('newPassword') or '')
            if len(new) < MIN_PASSWORD:
                raise Refused(f'Le nouveau mot de passe doit faire au moins {MIN_PASSWORD} caractères.')
            q('UPDATE admins SET hash=? WHERE email=?', (hash_password(new), email))
            q('DELETE FROM sessions WHERE email=? AND token!=?', (email, s['token']))
            audit(email, 'Mot de passe modifié')
            return self.json({'ok': True})
        if path == '/api/admins/add':
            self.need_master(s)
            new_email, pw = norm(data.get('email')), str(data.get('password') or '')
            if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', new_email):
                raise Refused('Adresse e-mail invalide.')
            if len(pw) < MIN_PASSWORD:
                raise Refused(f'Le mot de passe initial doit faire au moins {MIN_PASSWORD} caractères.')
            if q('SELECT 1 FROM admins WHERE email=?', (new_email,)):
                raise Refused('Cette personne a déjà un accès.')
            q('INSERT INTO admins VALUES(?,?,?,?)', (new_email, hash_password(pw), ADMIN, now()))
            audit(email, 'Administrateur ajouté : ' + new_email)
            return self.json({'ok': True})
        if path == '/api/admins/delete':
            self.need_master(s)
            target = norm(data.get('email'))
            rows = q('SELECT role FROM admins WHERE email=?', (target,))
            if not rows:
                raise Refused('Accès introuvable.', 404)
            if rows[0]['role'] == MASTER:
                raise Refused('L’accès de l’administrateur principal ne peut pas être retiré.', 403)
            q('DELETE FROM admins WHERE email=?', (target,))
            q('DELETE FROM sessions WHERE email=?', (target,))
            audit(email, 'Administrateur retiré : ' + target)
            return self.json({'ok': True})
        raise Refused('Adresse inconnue.', 404)


if __name__ == '__main__':
    setup()
    port = int(os.environ.get('PORT', 8000))
    print(f'Annuaire PBM sur le port {port}')
    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()
