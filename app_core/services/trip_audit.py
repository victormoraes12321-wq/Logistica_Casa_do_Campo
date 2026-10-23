"""Histórico original de viagens. Não depende de cadastros ou ERP futuros."""
from __future__ import annotations
import base64
import hashlib
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo


def ensure_schema(db):
    # Sem FK para cadastros operacionais: apagar/desvincular uma carga não pode
    # apagar o histórico original da entrega. O vínculo interno possui FK.
    db.execute("""CREATE TABLE IF NOT EXISTS trip_registry_audits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, route_id INTEGER NOT NULL,
        recorded_at TEXT NOT NULL, actor_id INTEGER, actor_name TEXT,
        declared_weight_kg REAL NOT NULL, erp_weight_kg REAL, erp_partial_weight_kg REAL NOT NULL,
        order_count INTEGER NOT NULL, erp_compared_count INTEGER NOT NULL,
        expense_total REAL NOT NULL, snapshot_json TEXT NOT NULL, sha256 TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS trip_registry_audit_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, audit_id INTEGER NOT NULL,
        route_id INTEGER NOT NULL, sequence INTEGER NOT NULL, order_id INTEGER NOT NULL,
        order_number TEXT NOT NULL, invoice_number TEXT, entered_weight_kg REAL,
        saved_weight_kg REAL NOT NULL, erp_weight_kg REAL, weight_difference_kg REAL,
        has_difference INTEGER, screen_weight_alert INTEGER, comparison_status TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        FOREIGN KEY(audit_id) REFERENCES trip_registry_audits(id), UNIQUE(audit_id,sequence)
    )""")
    db.execute('CREATE INDEX IF NOT EXISTS idx_trip_audit_route ON trip_registry_audits(route_id,id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_trip_audit_order ON trip_registry_audit_orders(order_id,route_id)')
    for table in ('trip_registry_audits','trip_registry_audit_orders'):
        for operation in ('UPDATE','DELETE'):
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()}
                BEFORE {operation} ON {table} BEGIN
                SELECT RAISE(ABORT,'O histórico original da viagem não pode ser alterado ou apagado'); END""")


def _default(value):
    if isinstance(value, bytes):
        return {'encoding':'base64','data':base64.b64encode(value).decode('ascii')}
    if hasattr(value,'isoformat'):
        return value.isoformat()
    return str(value)


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=_default,allow_nan=False)


def _row(db,table,record_id):
    row=db.execute(f'SELECT * FROM {table} WHERE id=?',(record_id,)).fetchone()
    # Dados operacionais, sem credenciais de usuários/motoristas.
    return {key:row[key] for key in row.keys() if key not in ('password_hash','pin')} if row else None


def _optional(db,query,params):
    try:
        return [dict(r) for r in db.execute(query,params).fetchall()]
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):
            raise
        return []


def record_trip(db,route_id,user,raw_payload,normalized_expenses,lines):
    """Escreve na mesma transação da carga; falha de auditoria cancela a viagem."""
    route=_row(db,'routes',route_id)
    actor=dict(user) if user is not None else {}
    timestamp=datetime.now(ZoneInfo('America/Sao_Paulo')).isoformat(timespec='seconds')
    entries=[]
    for sequence,line in enumerate(lines,1):
        order=_row(db,'orders',line['order_id'])
        typed=line['input'].get('weight_kg')
        entered=float(typed) if typed is not None and str(typed).strip()!='' else None
        metadata=line.get('erp')
        remote=float(metadata['weight_kg']) if metadata and metadata.get('metadata_verified') else None
        difference=float(Decimal(str(entered))-Decimal(str(remote))) if entered is not None and remote is not None else None
        entry={
            'sequence':sequence,'order_id':order['id'],'order_number':order['order_number'],
            'invoice_number':order.get('invoice_number'),'entered_weight_kg':entered,
            'saved_weight_kg':float(order.get('weight_kg') or 0),'erp_weight_kg':remote,
            'weight_difference_kg':difference,'has_difference':int(difference!=0) if difference is not None else None,
            'screen_weight_alert':int(abs(difference)>=5) if difference is not None else None,
            'comparison_status':line['comparison_status'],
            'input':line['input'],'erp':metadata,'unmatched_erp_candidate':line.get('unmatched_erp_candidate'),
            'local_before':line.get('local_before'),'local_after':order,
            'client':_row(db,'clients',order['client_id']),
            'items':[dict(r) for r in db.execute('SELECT * FROM order_items WHERE order_id=? ORDER BY id',(order['id'],))],
            'route_order':dict(db.execute('SELECT * FROM route_orders WHERE route_id=? AND order_id=?',(route_id,order['id'])).fetchone()),
            'receipts':_optional(db,'SELECT * FROM delivery_receipts WHERE route_id=? AND order_id=?',(route_id,order['id'])),
            'attachments':_optional(db,'SELECT * FROM attachments WHERE order_id=?',(order['id'],)),
        }
        entries.append(entry)
    expenses=[dict(r) for r in db.execute('SELECT * FROM trip_expenses WHERE route_id=? ORDER BY id',(route_id,))]
    expense_total=float(sum((Decimal(str(e['total_value'] or 0)) for e in expenses),Decimal('0')))
    partial=sum(e['erp_weight_kg'] for e in entries if e['erp_weight_kg'] is not None)
    compared=sum(e['erp_weight_kg'] is not None for e in entries)
    envelope={
        'schema_version':1,'source':'Registro de Viagens - gravação original','recorded_at':timestamp,
        'actor':{'id':actor.get('id'),'name':actor.get('name') or actor.get('username')},
        'raw_input':raw_payload,'normalized_expenses':normalized_expenses,
        'route':route,'driver':_row(db,'drivers',route['driver_id']),
        'vehicle':_row(db,'vehicles',route['vehicle_id']),'expenses':expenses,
        'expense_total':expense_total,'entries':entries,
    }
    payload=_json(envelope)
    audit_id=db.execute("""INSERT INTO trip_registry_audits
        (route_id,recorded_at,actor_id,actor_name,declared_weight_kg,erp_weight_kg,
         erp_partial_weight_kg,order_count,erp_compared_count,expense_total,snapshot_json,sha256)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (route_id,timestamp,actor.get('id'),actor.get('name') or actor.get('username'),
         route.get('declared_weight') or 0,partial if compared==len(entries) else None,partial,
         len(entries),compared,expense_total,payload,hashlib.sha256(payload.encode('utf-8')).hexdigest())).lastrowid
    for e in entries:
        db.execute("""INSERT INTO trip_registry_audit_orders
            (audit_id,route_id,sequence,order_id,order_number,invoice_number,entered_weight_kg,
             saved_weight_kg,erp_weight_kg,weight_difference_kg,has_difference,screen_weight_alert,
             comparison_status,snapshot_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (audit_id,route_id,e['sequence'],e['order_id'],e['order_number'],e['invoice_number'],
             e['entered_weight_kg'],e['saved_weight_kg'],e['erp_weight_kg'],e['weight_difference_kg'],
             e['has_difference'],e['screen_weight_alert'],e['comparison_status'],_json(e)))
    return audit_id


def load_trip(db,route_id):
    try:
        row=db.execute('SELECT snapshot_json,sha256 FROM trip_registry_audits WHERE route_id=? ORDER BY id DESC LIMIT 1',(route_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):raise
        return None
    if not row:return None
    if hashlib.sha256(row['snapshot_json'].encode('utf-8')).hexdigest()!=row['sha256']:
        raise ValueError('O histórico original da viagem não passou na conferência de integridade.')
    return json.loads(row['snapshot_json'])
