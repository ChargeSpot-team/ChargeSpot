import os
import secrets
import hmac
import io
import qrcode
from flask import send_file, abort
import sqlite3
import math
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from functools import wraps
from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get('DATABASE_PATH', os.path.join(BASE, 'chargespot.db'))
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))


def riyadh_now():
    return datetime.now(ZoneInfo("Asia/Riyadh")).replace(tzinfo=None)

def connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def init_db():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'driver');
        CREATE TABLE IF NOT EXISTS vehicles(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, plate TEXT NOT NULL, connector TEXT NOT NULL, capacity REAL NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS stations(id INTEGER PRIMARY KEY, name TEXT NOT NULL, location TEXT NOT NULL, price REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS chargers(id INTEGER PRIMARY KEY, station_id INTEGER NOT NULL, connector TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Available', FOREIGN KEY(station_id) REFERENCES stations(id));
        CREATE TABLE IF NOT EXISTS reservations(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, charger_id INTEGER NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL, status TEXT NOT NULL, amount REAL NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(charger_id) REFERENCES chargers(id));
        CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, reservation_id INTEGER NOT NULL, amount REAL NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(reservation_id) REFERENCES reservations(id));
        CREATE TABLE IF NOT EXISTS charging_sessions(id INTEGER PRIMARY KEY, reservation_id INTEGER UNIQUE NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, energy_kwh REAL, total_cost REAL, FOREIGN KEY(reservation_id) REFERENCES reservations(id));
        ''')
        for table,col,typ in [('users','payment_method',"TEXT DEFAULT 'Card'"),('reservations','vehicle_id','INTEGER'),('reservations','rate','REAL'),('stations','archived','INTEGER DEFAULT 0'),('chargers','archived','INTEGER DEFAULT 0'),('users','disabled','INTEGER DEFAULT 0')]:
            if col not in [r['name'] for r in db.execute('PRAGMA table_info('+table+')')]: db.execute('ALTER TABLE '+table+' ADD COLUMN '+col+' '+typ)
        db.execute('CREATE INDEX IF NOT EXISTS booking_slot ON reservations(charger_id,status,start_at,end_at)')
        db.execute('CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY,user_id INTEGER,message TEXT,created_at TEXT)')
        if not db.execute("SELECT 1 FROM users WHERE email='admin@chargespot.local'").fetchone():
            db.execute('INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)', ('System Admin','admin@chargespot.local',generate_password_hash(os.environ.get('ADMIN_PASSWORD') or secrets.token_urlsafe(24)),'admin'))
        if not db.execute('SELECT 1 FROM stations').fetchone():
            for name, location, price, connectors in [
                ('Riyadh Central Charging','Olaya, Riyadh',1.25,['CCS2','Type 2']),
                ('East Riyadh EV Hub','Al Khaleej, Riyadh',1.10,['CCS2','CHAdeMO']),
                ('North Riyadh ChargeSpot','Al Malqa, Riyadh',1.40,['Type 2','CCS2'])]:
                cur=db.execute('INSERT INTO stations(name,location,price) VALUES(?,?,?)',(name,location,price))
                for c in connectors: db.execute('INSERT INTO chargers(station_id,connector) VALUES(?,?)',(cur.lastrowid,c))

def finish_expired_sessions():
    now=riyadh_now().isoformat(timespec='seconds')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE reservations SET status='EXPIRED' WHERE status='CONFIRMED' AND (end_at<=? OR datetime(start_at,'+15 minutes')<=datetime(?))",(now,now))
        db.execute("UPDATE payments SET status='REFUNDED' WHERE reservation_id IN (SELECT id FROM reservations WHERE status='EXPIRED') AND status='SIMULATED_APPROVED'")
        rows=db.execute("""SELECT r.id,r.charger_id,r.end_at,cs.started_at,COALESCE(r.rate,s.price) AS price FROM reservations r JOIN charging_sessions cs ON cs.reservation_id=r.id JOIN chargers c ON c.id=r.charger_id JOIN stations s ON s.id=c.station_id WHERE r.status='ACTIVE' AND r.end_at<=?""",(now,)).fetchall()
        for r in rows:
            hours=max(0,(datetime.fromisoformat(r['end_at'])-datetime.fromisoformat(r['started_at'])).total_seconds()/3600)
            energy=round(hours*7,4)
            db.execute('UPDATE charging_sessions SET ended_at=?,energy_kwh=?,total_cost=? WHERE reservation_id=?',(r['end_at'],energy,round(energy*r['price'],2),r['id']))
            db.execute("UPDATE reservations SET status='COMPLETED' WHERE id=?",(r['id'],))
            db.execute('UPDATE payments SET amount=? WHERE reservation_id=?',(round(energy*r['price'],2),r['id']))
            db.execute("UPDATE chargers SET status='Available' WHERE id=?",(r['charger_id'],))

def expiry_worker():
    while True:
        try:
            finish_expired_sessions()
        except Exception:
            app.logger.exception('Automatic checkout failed; will retry')
        time.sleep(5)

@app.before_request
def reconcile_expired():
    finish_expired_sessions()

@app.before_request
def load_user():
    g.user = None
    if session.get('uid'):
        with connect() as db:
            g.user=db.execute('SELECT id,name,email,role FROM users WHERE id=? AND disabled=0',(session['uid'],)).fetchone()

def login_required(fn):
    @wraps(fn)
    def wrapper(*a,**kw):
        if not g.user: flash('Please log in first.','warning'); return redirect(url_for('login'))
        return fn(*a,**kw)
    return wrapper

def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*a,**kw):
        if g.user['role']!='admin': flash('Admin access required.','danger'); return redirect(url_for('index'))
        return fn(*a,**kw)
    return wrapper

def driver_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*a,**kw):
        if g.user['role']!='driver': flash('Driver account required.','danger'); return redirect(url_for('admin'))
        return fn(*a,**kw)
    return wrapper

@app.context_processor
def helpers():
    return {'now':riyadh_now(), 'user':g.get('user')}

@app.route('/version')
def version():
    return {'release': 'CS-20261126-R2'}

@app.route('/')
def index():
    search=request.args.get('q','').strip()
    minimum=request.args.get('min_price','').strip()
    maximum=request.args.get('max_price','').strip()
    connector=request.args.get('connector','').strip()
    availability=request.args.get('availability','')
    clauses=["s.archived=0","(?='' OR s.name LIKE ? OR s.location LIKE ?)"]
    params=[search,f'%{search}%',f'%{search}%']
    try:
        lo=float(minimum) if minimum else None
        hi=float(maximum) if maximum else None
        if any(not math.isfinite(v) or v<0 for v in (lo,hi) if v is not None) or (lo is not None and hi is not None and lo>hi):
            raise ValueError()
        for value, op in ((lo,'>='),(hi,'<=')):
            if value is not None:
                clauses.append('s.price '+op+' ?');params.append(value)
    except ValueError:
        flash('Enter a valid price range.','warning');clauses.append('0')
    if connector or availability:
        sub='EXISTS (SELECT 1 FROM chargers f WHERE f.station_id=s.id AND f.archived=0'
        if connector: sub+=' AND f.connector=?';params.append(connector)
        if availability in ('Available','Occupied','Out of Service'): sub+=' AND f.status=?';params.append(availability)
        clauses.append(sub+')')
    with connect() as db:
        connectors=db.execute('SELECT DISTINCT connector FROM chargers ORDER BY connector').fetchall()
        q="SELECT s.*, COUNT(c.id) AS total, SUM(CASE WHEN c.status='Available' THEN 1 ELSE 0 END) AS available FROM stations s LEFT JOIN chargers c ON c.station_id=s.id AND c.archived=0 WHERE "+' AND '.join(clauses)+' GROUP BY s.id ORDER BY s.id'
        stations=db.execute(q,params).fetchall()
    return render_template('index.html',stations=stations,search=search,minimum=minimum,maximum=maximum,connector=connector,connectors=connectors,availability=availability)

@app.route('/register',methods=['GET','POST'])
def register():
    if request.method=='POST':
        name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); pw=request.form.get('password','')
        if not name or '@' not in email or len(pw)<8: flash('Enter a name, valid email and password of at least 8 characters.','danger')
        else:
            try:
                with connect() as db:
                    cur=db.execute('INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)',(name,email,generate_password_hash(pw),'driver'))
                    session.clear();session['uid']=cur.lastrowid
                flash('Account created. Add your vehicle to reserve.','success');return redirect(url_for('profile'))
            except sqlite3.IntegrityError: flash('Email already registered.','danger')
    return render_template('auth.html',mode='register')

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        with connect() as db: u=db.execute('SELECT * FROM users WHERE email=?',(request.form.get('email','').lower().strip(),)).fetchone()
        if u and not u['disabled'] and check_password_hash(u['password'],request.form.get('password','')):
            session.clear();session['uid']=u['id'];return redirect(url_for('admin' if u['role']=='admin' else 'index'))
        flash('Incorrect email or password.','danger')
    return render_template('auth.html',mode='login')

@app.route('/logout')
def logout(): session.clear();return redirect(url_for('index'))

@app.route('/profile',methods=['GET','POST'])
@driver_required
def profile():
    with connect() as db:
        if request.method=='POST':
            plate=request.form.get('plate','').strip(); connector=request.form.get('connector','');capacity=request.form.get('capacity','')
            try:
                cap=float(capacity)
                if not math.isfinite(cap) or not plate or connector not in ('CCS2','Type 2','CHAdeMO') or cap<=0: raise ValueError()
                db.execute('INSERT INTO vehicles(user_id,plate,connector,capacity) VALUES(?,?,?,?)',(g.user['id'],plate,connector,cap));flash('Vehicle saved.','success');return redirect(url_for('profile'))
            except ValueError:flash('Enter valid vehicle information.','danger')
        vehicles=db.execute('SELECT * FROM vehicles WHERE user_id=?',(g.user['id'],)).fetchall()
    return render_template('profile.html',vehicles=vehicles)

@app.route('/station/<int:station_id>')
def station(station_id):
    with connect() as db:
        s=db.execute('SELECT * FROM stations WHERE id=? AND archived=0',(station_id,)).fetchone()
        if not s: return 'Station not found',404
        chargers=db.execute('SELECT * FROM chargers WHERE station_id=? AND archived=0',(station_id,)).fetchall()
        vehicles=db.execute('SELECT * FROM vehicles WHERE user_id=?',(g.user['id'],)).fetchall() if g.user and g.user['role']=='driver' else []
    return render_template('station.html',station=s,chargers=chargers,vehicles=vehicles)

@app.route('/reserve/<int:charger_id>',methods=['POST'])
@driver_required
def reserve(charger_id):
    try:
        start=datetime.fromisoformat(request.form['start_at']);end=datetime.fromisoformat(request.form['end_at'])
        if start.tzinfo or end.tzinfo or start<riyadh_now()-timedelta(minutes=1) or end<=start or end-start>timedelta(hours=8): raise ValueError()
        vehicle_id=int(request.form['vehicle_id'])
    except (ValueError,KeyError): flash('Select a future time range (up to 8 hours) and a vehicle.','danger');return redirect(url_for('index'))
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        c=db.execute('SELECT c.*,s.price FROM chargers c JOIN stations s ON s.id=c.station_id WHERE c.id=?',(charger_id,)).fetchone()
        v=db.execute('SELECT * FROM vehicles WHERE id=? AND user_id=?',(vehicle_id,g.user['id'])).fetchone()
        if not c or c['archived'] or c['status']=='Out of Service' or not v or v['connector']!=c['connector']:
            flash('Charger unavailable or incompatible with your vehicle.','danger');return redirect(url_for('index'))
        overlap=db.execute('''SELECT 1 FROM reservations WHERE charger_id=? AND status IN ('CONFIRMED','ACTIVE') AND start_at<? AND end_at>?''',(charger_id,end.isoformat(timespec='minutes'),start.isoformat(timespec='minutes'))).fetchone()
        if overlap:flash('This charger is already booked for that time.','danger');return redirect(url_for('station',station_id=c['station_id']))
        # Demonstration payment is simulated; no card information is collected.
        if request.form.get('payment_method')=='Decline (test)':
            flash('Demo payment declined. No slot held; choose another method.','danger');return redirect(url_for('station',station_id=c['station_id']))
        amount=round((end-start).total_seconds()/3600*c['price']*7,2)
        cur=db.execute('INSERT INTO reservations(user_id,charger_id,start_at,end_at,status,amount,created_at) VALUES(?,?,?,?,?,?,?)',(g.user['id'],charger_id,start.isoformat(timespec='minutes'),end.isoformat(timespec='minutes'),'CONFIRMED',amount,riyadh_now().isoformat(timespec='seconds')))
        db.execute('UPDATE reservations SET vehicle_id=?,rate=? WHERE id=?',(vehicle_id,c['price'],cur.lastrowid))
        notify(db,g.user['id'],f'Booking CS-{cur.lastrowid:05d} confirmed; demo payment SAR {amount:.2f}.')
        db.execute('INSERT INTO payments(reservation_id,amount,status,created_at) VALUES(?,?,?,?)',(cur.lastrowid,amount,'SIMULATED_APPROVED',riyadh_now().isoformat(timespec='seconds')))
    flash('Reservation confirmed. Demo payment approved.','success');return redirect(url_for('reservations'))

@app.route('/reservations')
@driver_required
def reservations():
    with connect() as db:
        rows=db.execute('''SELECT r.*,s.name AS station_name,c.connector,cs.id AS session_id,cs.total_cost,cs.energy_kwh,cs.started_at,cs.ended_at FROM reservations r JOIN chargers c ON r.charger_id=c.id JOIN stations s ON c.station_id=s.id LEFT JOIN charging_sessions cs ON cs.reservation_id=r.id WHERE r.user_id=? ORDER BY r.id DESC''',(g.user['id'],)).fetchall()
    return render_template('reservations.html',reservations=rows)

@app.route('/reservation/<int:rid>/cancel',methods=['POST'])
@driver_required
def cancel(rid):
    with connect() as db:
        cur=db.execute("UPDATE reservations SET status='CANCELLED' WHERE id=? AND user_id=? AND status='CONFIRMED'",(rid,g.user['id']))
        if cur.rowcount: db.execute("UPDATE payments SET status='REFUNDED' WHERE reservation_id=?",(rid,))
    flash('Reservation cancelled.' if cur.rowcount else 'Cannot cancel this reservation.','success' if cur.rowcount else 'danger');return redirect(url_for('reservations'))

@app.route('/reservation/<int:rid>/checkin',methods=['POST'])
@driver_required
def checkin(rid):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        r=db.execute('''SELECT r.*,c.status AS charger_status FROM reservations r JOIN chargers c ON c.id=r.charger_id WHERE r.id=? AND r.user_id=?''',(rid,g.user['id'])).fetchone()
        now = datetime.now(ZoneInfo("Asia/Riyadh")).replace(tzinfo=None)
        if not r or r['status'] != 'CONFIRMED' or r['charger_status'] != 'Available' or not (datetime.fromisoformat(r['start_at']) <= now < min(datetime.fromisoformat(r['end_at']),datetime.fromisoformat(r['start_at'])+timedelta(minutes=15))):
            flash('Check-in requires a confirmed reservation within its time window and an available charger.','danger')
        else:
            db.execute("UPDATE reservations SET status='ACTIVE' WHERE id=?",(rid,))
            db.execute("UPDATE chargers SET status='Occupied' WHERE id=?",(r['charger_id'],))
            db.execute('INSERT INTO charging_sessions(reservation_id,started_at) VALUES(?,?)',(rid,now.isoformat(timespec='seconds')))
            flash('Charging session started.','success')
    return redirect(url_for('reservations'))

@app.route('/reservation/<int:rid>/checkout',methods=['POST'])
@driver_required
def checkout(rid):
    try:
        energy=float(request.form.get('energy',''))
        if not 0<energy<=300:raise ValueError()
    except ValueError:flash('Enter energy delivered between 0 and 300 kWh.','danger');return redirect(url_for('reservations'))
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        r=db.execute('''SELECT r.*,COALESCE(r.rate,s.price) AS price FROM reservations r JOIN chargers c ON c.id=r.charger_id JOIN stations s ON s.id=c.station_id WHERE r.id=? AND r.user_id=?''',(rid,g.user['id'])).fetchone()
        if not r or r['status']!='ACTIVE':flash('No active charging session.','danger')
        else:
            cost=round(energy*r['price'],2)
            db.execute('UPDATE charging_sessions SET ended_at=?,energy_kwh=?,total_cost=? WHERE reservation_id=?',(riyadh_now().isoformat(timespec='seconds'),energy,cost,rid))
            db.execute("UPDATE reservations SET status='COMPLETED' WHERE id=?",(rid,))
            db.execute("UPDATE chargers SET status='Available' WHERE id=?",(r['charger_id'],))
            db.execute('UPDATE payments SET amount=? WHERE reservation_id=?',(cost,rid))
            flash(f'Session completed. Final energy cost: SAR {cost:.2f}.','success')
    return redirect(url_for('reservations'))

@app.route('/admin')
@admin_required
def admin():
    with connect() as db:
        stations=db.execute('SELECT * FROM stations WHERE archived=0 ORDER BY id').fetchall()
        chargers=db.execute('SELECT c.*,s.name AS station_name FROM chargers c JOIN stations s ON s.id=c.station_id WHERE c.archived=0 AND s.archived=0 ORDER BY c.id').fetchall()
        users=db.execute("SELECT id,name,email,disabled FROM users WHERE role='driver'").fetchall()
        stats={'stations':len(stations),'chargers':len(chargers),'drivers':len(users),'reservations':db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0], 'revenue':db.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='SIMULATED_APPROVED'").fetchone()[0]}
        recent=db.execute('''SELECT r.id,u.name,s.name AS station_name,r.status,r.amount FROM reservations r JOIN users u ON u.id=r.user_id JOIN chargers c ON c.id=r.charger_id JOIN stations s ON s.id=c.station_id ORDER BY r.id DESC LIMIT 12''').fetchall()
        reports=report_rows(db)
    return render_template('admin.html',stations=stations,chargers=chargers,users=users,stats=stats,recent=recent,reports=reports)

@app.route('/admin/station',methods=['POST'])
@admin_required
def add_station():
    try:
        name=request.form['name'].strip();location=request.form['location'].strip();price=float(request.form['price'])
        if not name or not location or not math.isfinite(price) or price<=0:raise ValueError()
        with connect() as db:db.execute('INSERT INTO stations(name,location,price) VALUES(?,?,?)',(name,location,price))
        flash('Station added.','success')
    except (ValueError,KeyError):flash('Invalid station details.','danger')
    return redirect(url_for('admin'))

@app.route('/admin/station/<int:sid>/delete',methods=['POST'])
@admin_required
def delete_station(sid):
    with connect() as db:
        used=db.execute('''SELECT 1 FROM reservations r JOIN chargers c ON r.charger_id=c.id WHERE c.station_id=?''',(sid,)).fetchone()
        if used:
            if db.execute("SELECT 1 FROM reservations r JOIN chargers c ON c.id=r.charger_id WHERE c.station_id=? AND r.status IN ('CONFIRMED','ACTIVE')",(sid,)).fetchone(): flash('Cancel or finish current bookings first.','danger')
            else: db.execute('UPDATE stations SET archived=1 WHERE id=?',(sid,));db.execute('UPDATE chargers SET archived=1 WHERE station_id=?',(sid,));flash('Station archived; history retained.','success')
        else:
            db.execute('DELETE FROM chargers WHERE station_id=?',(sid,));db.execute('DELETE FROM stations WHERE id=?',(sid,));flash('Station removed.','success')
    return redirect(url_for('admin'))

@app.route('/admin/charger',methods=['POST'])
@admin_required
def add_charger():
    try:
        sid=int(request.form['station_id']);connector=request.form['connector']
        if connector not in ('CCS2','Type 2','CHAdeMO'):raise ValueError()
        with connect() as db:db.execute('INSERT INTO chargers(station_id,connector) VALUES(?,?)',(sid,connector))
        flash('Charger added.','success')
    except (ValueError,KeyError,sqlite3.IntegrityError):flash('Invalid charger details.','danger')
    return redirect(url_for('admin'))

@app.route('/admin/charger/<int:cid>/status',methods=['POST'])
@admin_required
def charger_status(cid):
    status=request.form.get('status')
    if status not in ('Available','Occupied','Out of Service'):flash('Invalid status.','danger');return redirect(url_for('admin'))
    with connect() as db:
        active=db.execute("SELECT 1 FROM reservations WHERE charger_id=? AND status='ACTIVE'",(cid,)).fetchone()
        if active:flash('Finish the active charging session before changing status.','danger')
        else:
            apply_status(db,cid,status);flash('Charger status updated.','success')
    return redirect(url_for('admin'))

@app.route('/admin/user/<int:uid>/delete',methods=['POST'])
@admin_required
def delete_user(uid):
    with connect() as db:
        used=db.execute('SELECT 1 FROM reservations WHERE user_id=?',(uid,)).fetchone()
        if used:flash('Cannot delete a driver with reservation history.','danger')
        else:
            db.execute('DELETE FROM vehicles WHERE user_id=?',(uid,));db.execute("DELETE FROM users WHERE id=? AND role='driver'",(uid,));flash('Driver account deleted.','success')
    return redirect(url_for('admin'))

def notify(db,uid,message):
    db.execute('INSERT INTO notifications(user_id,message,created_at) VALUES(?,?,?)',(uid,message,riyadh_now().isoformat()))

def apply_status(db,cid,status):
    db.execute('UPDATE chargers SET status=? WHERE id=?',(status,cid))
    if status=='Out of Service':
        for r in db.execute("SELECT * FROM reservations WHERE charger_id=? AND status='CONFIRMED'",(cid,)).fetchall():
            db.execute("UPDATE reservations SET status='CANCELLED' WHERE id=?",(r['id'],))
            db.execute("UPDATE payments SET status='REFUNDED' WHERE reservation_id=?",(r['id'],))
            notify(db,r['user_id'],f"Booking #{r['id']} cancelled: charger out of service. Please browse stations to rebook.")

@app.before_request
def csrf_guard():
    if 'csrf' not in session: session['csrf']=secrets.token_hex(24)
    if request.method=='POST' and not hmac.compare_digest(session['csrf'],request.form.get('csrf','')): abort(400)

@app.route('/reservation/<int:rid>/modify',methods=['POST'])
@driver_required
def modify(rid):
    try:
        start=datetime.fromisoformat(request.form['start_at']);end=datetime.fromisoformat(request.form['end_at'])
        if start.tzinfo or end.tzinfo or start<riyadh_now() or not timedelta(0)<end-start<=timedelta(hours=8): raise ValueError()
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            r=db.execute("SELECT r.*,COALESCE(r.rate,s.price) AS unit_price,c.status AS charger_status FROM reservations r JOIN chargers c ON c.id=r.charger_id JOIN stations s ON s.id=c.station_id WHERE r.id=? AND r.user_id=? AND r.status='CONFIRMED'",(rid,g.user['id'])).fetchone()
            if not r or r['charger_status']=='Out of Service': raise ValueError()
            a,b=start.isoformat(timespec='minutes'),end.isoformat(timespec='minutes')
            if db.execute("SELECT 1 FROM reservations WHERE charger_id=? AND id<>? AND status IN ('CONFIRMED','ACTIVE') AND start_at<? AND end_at>?",(r['charger_id'],rid,b,a)).fetchone(): raise ValueError()
            amount=round((end-start).total_seconds()/3600*7*r['unit_price'],2)
            db.execute('UPDATE reservations SET start_at=?,end_at=?,amount=? WHERE id=?',(a,b,amount,rid))
            db.execute('UPDATE payments SET amount=? WHERE reservation_id=?',(amount,rid))
            notify(db,g.user['id'],f'Booking #{rid} modified; demo payment adjusted.')
        flash('Booking modified.','success')
    except (ValueError,KeyError): flash('Invalid or unavailable time; original booking retained.','danger')
    return redirect(url_for('reservations'))

@app.route('/reservation/<int:rid>/qr')
@driver_required
def booking_qr(rid):
    with connect() as db:
        if not db.execute('SELECT 1 FROM reservations WHERE id=? AND user_id=?',(rid,g.user['id'])).fetchone(): abort(404)
    img=qrcode.make(url_for('qr_checkin',rid=rid,_external=True));buf=io.BytesIO();img.save(buf,format='PNG');buf.seek(0);return send_file(buf,mimetype='image/png')

@app.route('/reservation/<int:rid>/qr-checkin')
@driver_required
def qr_checkin(rid):
    return redirect(url_for('reservations',_anchor=f'booking-{rid}'))

@app.route('/notifications')
@driver_required
def notifications():
    with connect() as db: rows=db.execute('SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC',(g.user['id'],)).fetchall()
    return render_template('notifications.html',rows=rows)

@app.route('/profile/account',methods=['POST'])
@driver_required
def edit_account():
    name=request.form.get('name','').strip();method=request.form.get('payment_method')
    if name and method in ('Card','Wallet'):
        with connect() as db: db.execute('UPDATE users SET name=?,payment_method=? WHERE id=?',(name,method,g.user['id']))
        flash('Profile updated.','success')
    return redirect(url_for('profile'))

@app.route('/admin/station/<int:sid>/edit',methods=['POST'])
@admin_required
def edit_station(sid):
    try:
        name=request.form['name'].strip();location=request.form['location'].strip();price=float(request.form['price'])
        if not name or not location or not math.isfinite(price) or price<=0: raise ValueError()
        with connect() as db: db.execute('UPDATE stations SET name=?,location=?,price=? WHERE id=?',(name,location,price,sid))
        flash('Station updated.','success')
    except (ValueError,KeyError): flash('Invalid details.','danger')
    return redirect(url_for('admin'))

@app.route('/admin/charger/<int:cid>/edit',methods=['POST'])
@admin_required
def edit_charger(cid):
    with connect() as db:
        if db.execute("SELECT 1 FROM reservations WHERE charger_id=? AND status IN ('CONFIRMED','ACTIVE')",(cid,)).fetchone(): flash('Current bookings must be resolved first.','danger')
        elif request.form.get('action')=='delete': db.execute('UPDATE chargers SET archived=1 WHERE id=?',(cid,));flash('Charger removed.','success')
        elif request.form.get('connector') in ('CCS2','Type 2','CHAdeMO'): db.execute('UPDATE chargers SET connector=? WHERE id=?',(request.form['connector'],cid));flash('Charger updated.','success')
    return redirect(url_for('admin'))

@app.route('/admin/user/<int:uid>/toggle',methods=['POST'])
@admin_required
def toggle_user(uid):
    with connect() as db: db.execute("UPDATE users SET disabled=1-disabled WHERE id=? AND role='driver'",(uid,))
    return redirect(url_for('admin'))

@app.route('/admin/sensor/<int:cid>',methods=['POST'])
@admin_required
def sensor(cid):
    status=request.form.get('status')
    with connect() as db:
        if status in ('Available','Occupied','Out of Service') and not db.execute("SELECT 1 FROM reservations WHERE charger_id=? AND status='ACTIVE'",(cid,)).fetchone(): apply_status(db,cid,status);flash('Simulated sensor event received.','success')
    return redirect(url_for('admin'))

def report_rows(db):
    today=riyadh_now().replace(hour=0,minute=0,second=0,microsecond=0)
    result=[]
    for label,start in [('Daily',today),('Weekly',today-timedelta(days=today.weekday())),('Monthly',today.replace(day=1))]:
        r=db.execute('SELECT COUNT(*) AS sessions,COALESCE(SUM(energy_kwh),0) AS energy,COALESCE(SUM(total_cost),0) AS revenue,COALESCE(SUM((julianday(ended_at)-julianday(started_at))*24),0) AS hours FROM charging_sessions WHERE ended_at>=?',(start.isoformat(),)).fetchone()
        result.append(dict(r,period=label))
    return result

BACKUP=os.environ.get('BACKUP_PATH',DB+'.backup')
def backup_db():
    with connect() as source,sqlite3.connect(BACKUP) as target: source.backup(target)

def backup_worker():
    while True:
        try: backup_db()
        except Exception: app.logger.exception('Backup failed')
        time.sleep(86400)

@app.route('/admin/backup',methods=['POST'])
@admin_required
def download_backup():
    backup_db();return send_file(BACKUP,as_attachment=True,download_name='chargespot-backup.db')

init_db()
threading.Thread(target=expiry_worker,daemon=True).start()
threading.Thread(target=backup_worker,daemon=True).start()
if __name__=='__main__':
    app.run(debug=False,host='127.0.0.1',port=5000)
