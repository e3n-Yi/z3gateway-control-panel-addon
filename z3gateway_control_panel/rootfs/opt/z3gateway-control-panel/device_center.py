"""Persistent device identities, bounded discovery and serialized device operations."""
import copy
import itertools
import json
import queue
import re
import shutil
import struct
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone

BASIC = {4: 'manufacturer', 5: 'model', 6: 'dateCode', 0x4000: 'swBuildId'}
HIDDEN = {'nodeId', 'targetNode', 'srcEui', 'srcEp', 'dstEp', 'otaEndpoint'}
EXCLUDED = {'group_toggle_packet', 'send_unicast', 'send_multicast', 'ota_print_images'}

def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def number(value):
    text = str(value).strip()
    return int(text, 16 if text.lower().startswith('0x') else 10)

def ieee(value):
    text = str(value or '').replace('0x', '').replace(':', '').upper()
    return text if re.fullmatch('[0-9A-F]{16}', text) else None

def frame(event):
    data = bytes.fromhex(event.get('data', ''))
    if event.get('profile') == 0:
        return (data[0], None, data[1:]) if data else (None, None, b'')
    offset = 3 if data and data[0] & 4 else 1
    if len(data) < offset + 2:
        return None, None, b''
    return data[offset], data[offset + 1], data[offset + 2:]

def ota_header(path):
    with path.open('rb') as f:
        data = f.read(56)
    if len(data) != 56:
        raise ValueError('OTA 文件头不完整')
    magic, version, length, control, manufacturer, image_type, firmware = struct.unpack_from('<IHHHHHI', data)
    total = struct.unpack_from('<I', data, 52)[0]
    if magic != 0x0BEEF11E or version != 0x100 or length < 56 or total != path.stat().st_size or total < length:
        raise ValueError('不是完整的标准 Zigbee OTA 镜像')
    return {'manufacturer': manufacturer, 'imageType': image_type, 'firmwareVersion': firmware, 'size': total}

class DeviceCenter:
    def __init__(self, data_dir, manager, catalog, ota_dir, legacy=(), start_worker=True):
        self.path = data_dir / 'device-center.json'
        self.manager, self.catalog, self.ota_dir = manager, catalog, ota_dir
        self.lock = threading.RLock()
        self.db = {'version': 1, 'devices': {}, 'deleted': {}}
        self.jobs = {}
        self.pending = queue.PriorityQueue()
        self.counter = itertools.count()
        self.current = None
        self.buffer = ''
        self.session = None
        self.events = deque(maxlen=400)
        self.early = deque(maxlen=40)
        self.last_probe = {}
        self.commands = {}
        for group in catalog['groups']:
            for command in group['commands']:
                if group['name'] != '网络' and command['id'] not in EXCLUDED:
                    self.commands[command['id']] = dict(command, group=group['name'])
        if self.path.exists():
            self.db = json.loads(self.path.read_text())
            backup = self.path.with_suffix('.json.backup')
            if not backup.exists():
                shutil.copy2(self.path, backup)
        else:
            old = data_dir / 'devices.json'
            if old.exists() and not old.with_suffix('.json.pre-device-center').exists():
                shutil.copy2(old, old.with_suffix('.json.pre-device-center'))
            for entry in legacy:
                self.import_legacy(entry)
            self.save()
        for device in self.db['devices'].values():
            device['addressVerified'] = False
        if start_worker:
            threading.Thread(target=self.worker, daemon=True, name='device-operations').start()

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(self.db, ensure_ascii=False, indent=2))
            temp.replace(self.path)

    def import_legacy(self, entry):
        key = ieee(entry.get('eui64')) or 'pending-' + str(entry.get('nodeId'))
        if not ieee(key) and any(t.get('nodeId') == entry.get('nodeId') for t in self.db['deleted'].values()):
            return
        if key in self.db['deleted'] or key in self.db['devices']:
            return
        self.db['devices'][key] = dict(entry, id=key, generation=uuid.uuid4().hex,
                                      identity='known' if ieee(key) else 'pending',
                                      endpoints={}, attributes={}, discovery='pending')

    def list(self):
        with self.lock:
            return copy.deepcopy(list(self.db['devices'].values()))

    def get(self, key):
        with self.lock:
            if key not in self.db['devices']:
                raise ValueError('设备已删除或不存在')
            return copy.deepcopy(self.db['devices'][key])

    def update_name(self, key, name):
        if not isinstance(name, str) or len(name) > 80:
            raise ValueError('名称最多 80 个字符')
        with self.lock:
            self.get(key)
            self.db['devices'][key]['name'] = name.strip()
            self.save()
        return self.get(key)

    def delete(self, key):
        # Same lock as execution: deletion cannot race target resolution/write.
        with self.manager.command_lock, self.lock:
            d = self.get(key)
            if d.get('role') == 'gateway':
                raise ValueError('不能删除网关自身')
            self.db['deleted'][key] = {'at': now(), 'generation': d['generation'], 'nodeId': d.get('nodeId')}
            del self.db['devices'][key]
            self.last_probe.pop(key, None)
            self.events = deque((e for e in self.events if e.get('device') != key), maxlen=400)
            for job in self.jobs.values():
                if job['device'] == key and job['state'] in {'queued', 'sending', 'waiting'}:
                    job['state'] = 'cancelled'
                    job['_wake'].set()
            self.save()
        return {'deleted': key, 'scope': 'host-records'}

    def valid(self, job):
        d = self.db['devices'].get(job['device'])
        return d is not None and d['generation'] == job['generation'] and job['state'] != 'cancelled'

    def join(self, key, node, fresh=True):
        key = ieee(key)
        if not key or node >= 0xFFF8:
            return
        with self.lock:
            if key in self.db['deleted'] and not fresh:
                return
            self.db['deleted'].pop(key, None)
            d = self.db['devices'].get(key)
            needs_verification = not d or not d.get('addressVerified')
            if not d:
                d = {'id': key, 'eui64': key, 'generation': uuid.uuid4().hex,
                     'endpoints': {}, 'attributes': {}, 'firstSeen': now(), 'identity': 'known'}
                self.db['devices'][key] = d
            for other, record in list(self.db['devices'].items()):
                if other != key and record.get('nodeId') == f'0x{node:04X}':
                    if record.get('identity') == 'pending':
                        d.setdefault('name', record.get('name', ''))
                        del self.db['devices'][other]
                    else:
                        record['nodeId'] = None
                        record['discovery'] = 'address-changed'
            if d.get('nodeId') and (d['nodeId'] != f'0x{node:04X}' or d.get('joined') is False):
                d['generation'] = uuid.uuid4().hex
                for job in self.jobs.values():
                    if job['device'] == key and job['state'] in {'queued','sending','waiting'}:
                        job['state'] = 'cancelled'
                        job['_wake'].set()
            d.update(nodeId=f'0x{node:04X}', lastSeen=now(), joined=True, identity='known', addressVerified=True)
            self.save()
        if needs_verification or time.monotonic() - self.last_probe.get(key, 0) > 30:
            self.last_probe[key] = time.monotonic()
            self.discover(key)

    def enqueue(self, key, action, params=None, auto=False, attempt=1):
        if params is not None and not isinstance(params, dict):
            raise ValueError('params 必须是对象')
        with self.lock:
            d = self.get(key)
            if not self.manager.running():
                raise ValueError('请先启动网关')
            if action != '_ieee' and (d.get('identity') != 'known' or not d.get('nodeId') or not d.get('addressVerified')):
                raise ValueError('设备身份或当前地址待确认，请先刷新资料')
            if auto:
                for old in self.jobs.values():
                    if old['device'] == key and old['generation'] == d['generation'] and old['action'] == action and old['params'] == (params or {}) and old['state'] in {'queued','sending','waiting'}:
                        return self.public_job(old)
            if sum(j['state'] in {'queued','sending','waiting'} for j in self.jobs.values()) >= 256:
                raise ValueError('任务队列已满，请稍后重试')
            job = {'id': uuid.uuid4().hex, 'device': key, 'generation': d['generation'],
                   'action': action, 'params': params or {}, 'auto': auto, 'attempt': attempt,
                   'state': 'queued', 'created': now(), '_wake': threading.Event()}
            self.jobs[job['id']] = job
            self.pending.put((1 if auto else 0, next(self.counter), job['id']))
            for jid, old in list(self.jobs.items()):
                if len(self.jobs) <= 500:
                    break
                if old['state'] not in {'queued','sending','waiting'}:
                    del self.jobs[jid]
            return self.public_job(job)

    def public_job(self, job):
        return copy.deepcopy({k: v for k, v in job.items() if not k.startswith('_')})

    def operations(self, key):
        with self.lock:
            return [self.public_job(j) for j in list(self.jobs.values()) if j['device'] == key][-30:]

    def discover(self, key):
        with self.lock:
            d = self.get(key)
            if not self.manager.running():
                return {'state': 'pending'}
            if d.get('role') == 'gateway':
                return {'state': 'gateway'}
            if not d.get('nodeId'):
                return {'state': 'address-changed'}
            self.db['devices'][key]['discovery'] = 'reading'
            self.save()
        if d['identity'] == 'pending' or not d.get('addressVerified'):
            self.enqueue(key, '_ieee', auto=True)
        else:
            self.enqueue(key, '_node', auto=True)
            self.enqueue(key, '_active', auto=True)
        return {'state': 'queued'}

    def build(self, job):
        d = self.get(job['device'])
        node = number(d['nodeId'])
        if node >= 0xFFF8:
            raise ValueError('设备操作仅允许单播地址')
        a, p = job['action'], dict(job['params'])
        if a in {'_node','_active','_ieee'}:
            return [f'zdo {a[1:]} 0x{node:04X}']
        if a == '_simple':
            return [f'zdo simple 0x{node:04X} {number(p["endpoint"])}']
        if a == '_basic':
            return [f'zcl global read 0 {number(p["attribute"])}', f'send 0x{node:04X} 1 {number(p["endpoint"])}']
        if a not in self.commands:
            raise ValueError('不支持的设备操作')
        if not d.get('addressVerified'):
            raise ValueError('等待本次运行确认设备地址')
        command = self.commands[a]
        values = dict(self.catalog.get('parameter_defaults', {}))
        values.update(p)
        endpoints = d.get('endpoints', {})
        ep = number(p.get('endpoint', next(iter(endpoints), '0')))
        if a != 'zdo_leave' and str(ep) not in endpoints:
            raise ValueError('请选择已发现的设备端点')
        values.update(nodeId=f'0x{node:04X}', targetNode=f'0x{node:04X}',
                      srcEui='{'+d['eui64']+'}', srcEp=str(ep if a in {'bind_custom','unbind_custom'} else 1),
                      dstEp=str(ep), otaEndpoint=str(ep))
        if a == 'ota_notify':
            name = str(p.get('otaFile', ''))
            if not name or '/' in name or '\\' in name or name.startswith('.'):
                raise ValueError('请选择 OTA 文件')
            path = self.ota_dir / name
            if path.is_symlink() or not path.is_file():
                raise ValueError('OTA 文件不存在')
            header = ota_header(path)
            values.update(otaManufacturerId=header['manufacturer'], otaImageTypeId=header['imageType'],
                          otaFirmwareVersion=header['firmwareVersion'], otaPayloadType=3)
        if a.startswith('zero_cross_'):
            k = 'zeroCrossOn' if a == 'zero_cross_on_calibration' else 'zeroCrossOff'
            n = number(values[k+'Us'])
            if not 0 <= n <= 65535:
                raise ValueError('校准值超出范围')
            values[k+'Bytes'] = f'{n >> 8:02X} {n & 255:02X}'
        def replace(match):
            k = match.group(1)
            value = str(values.get(k, '')).strip()
            if not value or any(ord(c) < 32 or ord(c) == 127 for c in value) or '"' in value or ';' in value:
                raise ValueError('参数无效: '+k)
            if k in {'srcEui','destEui'}:
                ident = ieee(value.strip('{} '))
                if not ident:
                    raise ValueError('请输入完整 IEEE 地址')
                return '{'+ident+'}'
            if k in {'groupName','sceneName'}:
                return value
            if k in {'data','extensionFields'}:
                if not re.fullmatch(r'(?:0x[0-9a-fA-F]+|\{[0-9a-fA-F ]*\})', value):
                    raise ValueError('请输入十六进制数据')
            elif k.endswith('Bytes'):
                pass
            else:
                numeric = number(value)
                limit = 0xFFFFFFFF if k == 'otaFirmwareVersion' else 255 if k in {'type','level','sceneId','srcEp','dstEp','destEp','otaEndpoint','otaQueryJitter'} else 1 if k in {'removeChildren','rejoin'} else 3 if k == 'otaPayloadType' else 65535
                if not 0 <= numeric <= limit:
                    raise ValueError('参数超出范围: '+k)
            return value
        text = re.sub(r'\{\{(\w+)\}\}', replace, command['command'])
        lines = text.splitlines()
        if text.startswith('zcl '):
            lines.append(f'send 0x{node:04X} 1 {ep}')
        return lines

    def worker(self):
        while True:
            _, _, jid = self.pending.get()
            job = self.jobs.get(jid)
            if job is None:
                continue
            try:
                with self.manager.command_lock:
                    with self.lock:
                        if not self.valid(job):
                            job['state'] = 'cancelled'
                            continue
                        commands = self.build(job)
                        job.update(state='sending', commands=commands, node=number(self.get(job['device'])['nodeId']))
                        self.early.clear()
                        self.current = job
                        if job['action'] == '_basic':
                            self.db['devices'][job['device']]['attributes'].setdefault(str(job['params']['endpoint']), {}).setdefault(str(job['params']['attribute']), {})['state'] = 'reading'
                    self.manager.send_command_sequence(commands, inter_command_delay=0.05)
                    with self.lock:
                        if job['state'] == 'sending':
                            job['state'] = 'waiting'
                    job['_wake'].wait(3)
                    with self.lock:
                        if not self.valid(job):
                            job['state'] = 'cancelled'
                        if job['state'] == 'waiting':
                            tx = job.get('_tx')
                            job['state'] = 'timeout' if tx and (job['auto'] or job['action'].startswith(('read_', 'write_', 'bind_', 'unbind_')) or job['action']=='zdo_leave') else 'sent-unconfirmed'
            except Exception as exc:
                with self.lock:
                    if job['state'] != 'cancelled':
                        job.update(state='failed', error=str(exc))
            finally:
                with self.lock:
                    if self.current is job:
                        self.current = None
                    retry = job['auto'] and job['state'] in {'timeout','failed','sent-unconfirmed'} and job['attempt'] < 3 and self.valid(job)
                    if job['auto'] and self.valid(job):
                        d = self.db['devices'][job['device']]
                        d['discovery'] = 'partial' if job['state'] != 'success' else d.get('discovery','reading')
                        if job['action'] == '_basic' and job['state'] != 'success':
                            attr = d['attributes'].setdefault(str(job['params']['endpoint']), {}).setdefault(str(job['params']['attribute']), {})
                            attr.update(state=job['state'], updated=now())
                        self.save()
                if retry:
                    try:
                        self.enqueue(job['device'], job['action'], job['params'], True, job['attempt']+1)
                    except ValueError:
                        pass
                self.pending.task_done()

    def stop(self):
        with self.lock:
            for job in self.jobs.values():
                if job['state'] in {'queued','sending','waiting'}:
                    job['state'] = 'cancelled'
                    job['_wake'].set()

    def snapshot(self, key):
        with self.lock:
            return {'device': self.get(key), 'operations': self.operations(key), 'events': copy.deepcopy([e for e in self.events if e.get('device') == key])}

    def feed(self, text, session):
        with self.lock:
            if session != self.session:
                self.session = session
                self.buffer = ''
                for job in self.jobs.values():
                    if job['state'] in {'queued','sending','waiting'}:
                        job['state'] = 'cancelled'
                        job['_wake'].set()
            self.buffer += text
            lines = self.buffer.split('\n')
            self.buffer = lines.pop()[-16384:]
        for line in lines:
            marker = line.find('@Z3 ')
            if marker >= 0:
                try:
                    self.event(json.loads(line[marker+4:]))
                except (ValueError, KeyError, IndexError, struct.error):
                    continue

    def event(self, e):
        kind = e.get('event')
        if kind == 'join':
            self.join(e['ieee'], e['node'])
            return
        if kind == 'leave':
            with self.lock:
                d = self.db['devices'].get(ieee(e['ieee']))
                if d:
                    d.update(joined=False, addressVerified=False, lastSeen=now())
                    for job in self.jobs.values():
                        if job['device'] == d['id'] and job['state'] in {'queued','sending','waiting'}:
                            if job['action'] == 'zdo_leave':
                                job.update(state='success', result={'left': True})
                            else:
                                job['state'] = 'cancelled'
                            job['_wake'].set()
                    self.save()
            return
        if kind not in {'rx','tx','sent'}:
            return
        seq, cmd, payload = frame(e)
        with self.lock:
            d = next((v for v in self.db['devices'].values() if v.get('nodeId') == f'0x{e["node"]:04X}'), None)
            if d and kind == 'rx':
                d['lastSeen'] = now()
                self.events.append(dict(e, device=d['id'], at=now()))
                if d.get('discovery') == 'partial' and time.monotonic() - self.last_probe.get(d['id'],0) > 60:
                    self.last_probe[d['id']] = time.monotonic()
                    self.discover(d['id'])
            if d and kind == 'rx' and e['cluster'] == 0x19 and cmd == 6:
                d['discovery'] = 'partial'
                self.last_probe[d['id']] = time.monotonic() - 31
            job = self.current
            if kind in {'tx','sent'} and job and self.valid(job) and job.get('node') == e['node']:
                if kind == 'tx' or (kind == 'sent' and not job.get('_tx')):
                    job['_tx'] = dict(e, seq=seq, cmd=cmd)
                if kind == 'sent' and e['status'] != 0:
                    job.update(state='failed', error='APS status '+str(e['status']))
                    job['_wake'].set()
                elif kind == 'sent':
                    job['delivery'] = 'aps-confirmed'
                    for early in list(self.early):
                        self.event(early)
                    self.early.clear()
                return
            # Device announce and IEEE responses can establish identity, but
            # cannot restore a tombstoned device without a trust-center join.
            if kind == 'rx' and e['profile'] == 0:
                if e['cluster'] == 0x13 and len(payload) >= 11:
                    self.join(payload[2:10][::-1].hex(), int.from_bytes(payload[:2],'little'), fresh=False)
                elif e['cluster'] == 0x8001 and len(payload) >= 11 and payload[0] == 0:
                    self.join(payload[1:9][::-1].hex(), int.from_bytes(payload[9:11],'little'), fresh=False)
            if not job or not self.valid(job) or job.get('node') != e['node'] or kind != 'rx':
                return
            tx = job.get('_tx')
            if not tx:
                self.early.append(e)
                return
            if seq != tx['seq'] or e['profile'] != tx['profile']:
                return
            if e['profile'] == 0:
                if e['cluster'] != (tx['cluster'] | 0x8000):
                    return
            elif e['cluster'] != tx['cluster'] or e['ep'] != tx['destEp']:
                return
            if e['profile'] != 0:
                raw = bytes.fromhex(e['data'])
                if not raw or not raw[0] & 8:
                    return
                if raw[0] & 3 == 0 and cmd not in {1,4,7,9,11}:
                    return
                if cmd == 11 and (not payload or payload[0] != tx['cmd']):
                    return
            try:
                self.response(job, e, cmd, payload)
            except (ValueError, IndexError, struct.error) as exc:
                job.update(state='failed', error='响应格式无效: '+str(exc))
            job['_wake'].set()

    def response(self, job, e, cmd, p):
        d = self.db['devices'][job['device']]
        if e['profile'] == 0:
            if not p or p[0] != 0:
                job.update(state='failed', error='ZDO status '+str(p[0] if p else 'missing'))
                return
            cluster = e['cluster']
            if cluster == 0x8002:
                d['networkRole'] = {0:'协调器',1:'路由器',2:'终端设备'}.get(p[3] & 7,'未知')
            elif cluster == 0x8005:
                count = p[3]
                if len(p) < 4+count:
                    raise ValueError('endpoint length')
                for ep in p[4:4+count]:
                    if ep not in (0,242):
                        self.enqueue(d['id'], '_simple', {'endpoint': ep}, auto=True)
            elif cluster == 0x8004:
                size = p[3]
                data = p[4:4+size]
                if len(data) < 8:
                    raise ValueError('descriptor length')
                ep, profile, device = data[0], int.from_bytes(data[1:3],'little'), int.from_bytes(data[3:5],'little')
                ni = data[6]; end = 7+2*ni
                if len(data) <= end:
                    raise ValueError('cluster length')
                ins = [int.from_bytes(data[i:i+2],'little') for i in range(7,end,2)]
                no = data[end]
                if len(data) < end+1+2*no:
                    raise ValueError('cluster length')
                outs = [int.from_bytes(data[i:i+2],'little') for i in range(end+1,end+1+2*no,2)]
                typ = '灯' if device in (0x100,0x101,0x102,0x10C,0x10D) else '开关设备' if 6 in ins else '传感器' if any(0x400<=c<=0x406 for c in ins) else f'未知设备 0x{device:04X}'
                d['endpoints'][str(ep)] = {'profile':profile,'deviceId':device,'inClusters':ins,'outClusters':outs,'type':typ}
                d['deviceType'] = ' / '.join(dict.fromkeys(v['type'] for v in d['endpoints'].values()))
                d['discovery'] = 'complete'
                if 0 in ins:
                    d['discovery'] = 'reading'
                    for attr in BASIC:
                        d['attributes'].setdefault(str(ep), {}).setdefault(str(attr), {'state':'pending'})
                        self.enqueue(d['id'], '_basic', {'endpoint':ep,'attribute':attr}, auto=True)
            elif cluster == 0x8034:
                d['leaveRequest'] = 'accepted-awaiting-leave'
            job['state'] = 'success'
        elif cmd == 1 and not (bytes.fromhex(e['data'])[0] & 3):
            records = []
            while p:
                if len(p) < 3:
                    raise ValueError('attribute length')
                attr = int.from_bytes(p[:2],'little'); status = p[2]; p=p[3:]
                record = {'attribute':attr,'status':status}
                if status == 0:
                    if not p:
                        raise ValueError('missing type')
                    typ=p[0];p=p[1:]
                    if typ in (0x41,0x42):
                        n=p[0];p=p[1:]
                        if n == 255:
                            value = None
                        else:
                            if len(p)<n: raise ValueError('string length')
                            raw=p[:n];p=p[n:];value=raw.decode('utf-8',errors='replace') if typ==0x42 else raw.hex()
                    elif 0x18<=typ<=0x2f or typ in (0x10,0x30,0x31):
                        n=(typ&7)+1 if 0x18<=typ<=0x2f else 2 if typ==0x31 else 1
                        if len(p)<n: raise ValueError('scalar length')
                        value=int.from_bytes(p[:n],'little',signed=0x28<=typ<=0x2f);p=p[n:]
                    else:
                        record.update(type=typ,raw=p.hex());records.append(record);break
                    record.update(type=typ,value=value)
                records.append(record)
                if e['cluster']==0 and attr in BASIC:
                    old=d['attributes'].setdefault(str(e['ep']),{}).setdefault(str(attr),{})
                    old.update(state='success' if status==0 else 'unsupported' if status==0x86 else 'failed', updated=now(), status=status)
                    if status==0:
                        old['value']=record.get('value');d[BASIC[attr]]=record.get('value')
            job.update(state='success', result=records)
            d['discovery']='partial' if any(v['state'] not in {'success','unsupported'} for ep in d['attributes'].values() for v in ep.values()) else 'complete'
        else:
            status = p[1] if cmd==11 and len(p)>1 else p[0] if p else None
            job.update(state='success' if status==0 else 'failed', result={'status':status,'payload':p.hex()})
        self.save()
