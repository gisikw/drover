"""Single-operator Drover. No task semantics; JSON RPC is opaque after authorization."""
import argparse
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import random
import re
import secrets
import sqlite3
import ssl
import subprocess
import pwd
import sys
import signal
import tempfile

from aiohttp import web, ClientSession, ClientTimeout, TCPConnector, WSMsgType

LIMIT = 1024 * 1024
METHODS = frozenset('ping session.snapshot workspace.list workspace.get tab.list tab.get pane.list pane.get pane.read agent.list agent.get agent.read agent.prompt agent.start agent.rename'.split())


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def private_write(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def validate_rpc(data):
    if not isinstance(data, dict) or not isinstance(data.get('method'), str) or data['method'] not in METHODS or not isinstance(data.get('params', {}), dict):
        raise web.HTTPBadRequest(text='method not allowed or invalid params')
    return {'method': data['method'], 'params': data.get('params', {})}


class Coordinator:
    def __init__(self, state, enrollment, client, first_port=24000):
        Path(state).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(Path(state) / 'registry.sqlite'))
        self.db.execute('CREATE TABLE IF NOT EXISTS machines (name TEXT PRIMARY KEY, token TEXT, port INTEGER UNIQUE, metadata TEXT)')
        self.enrollment, self.client = enrollment, client
        self.db.execute('CREATE TABLE IF NOT EXISTS allocation (next_port INTEGER)')
        if not self.db.execute('SELECT next_port FROM allocation').fetchone():
            self.db.execute('INSERT INTO allocation VALUES (?)', (first_port,))
            self.db.commit()
        self.first_port = first_port
        self.connections = {}
        self.pending = {}

    def auth(self, request, token):
        if not secrets.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + token):
            raise web.HTTPUnauthorized()

    async def register(self, request):
        self.auth(request, self.enrollment)
        data = await request.json()
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text='expected JSON object')
        name = data.get('name', '')
        if not isinstance(name, str) or not re.fullmatch('[a-z][a-z0-9-]{0,47}', name):
            raise web.HTTPBadRequest(text='invalid name')
        metadata = {k: data.get(k, '') for k in ('ssh_user', 'session', 'host_key')}
        if any(not isinstance(v, str) or len(v) > 1024 or '\n' in v for v in metadata.values()):
            raise web.HTTPBadRequest()
        if not re.fullmatch('[a-z_][a-z0-9_-]*', metadata['ssh_user']) or not re.fullmatch('[a-z][a-z0-9-]*', metadata['session']):
            raise web.HTTPBadRequest(text='invalid namespace')
        token = secrets.token_urlsafe(32)
        port = self.db.execute('SELECT next_port FROM allocation').fetchone()[0]
        if port > 65000:
            raise web.HTTPServiceUnavailable()
        try:
            self.db.execute('INSERT INTO machines VALUES (?,?,?,?)', (name, digest(token), port, json.dumps(metadata)))
            self.db.execute('UPDATE allocation SET next_port=?', (port + 1,))
            self.db.commit()
        except sqlite3.IntegrityError:
            raise web.HTTPConflict(text='name already enrolled; use persisted identity')
        return web.json_response({'name': name, 'token': token, 'port': port})

    async def catalog(self, request):
        self.auth(request, self.client)
        return web.json_response([dict(name=n, port=p, online=n in self.connections, **json.loads(m)) for n, p, m in self.db.execute('SELECT name,port,metadata FROM machines')])

    async def revoke(self, request):
        self.auth(request, self.client)
        name = request.match_info['name']
        self.db.execute('DELETE FROM machines WHERE name=?', (name,))
        self.db.commit()
        if name in self.connections:
            await self.connections[name].close()
        return web.json_response({'revoked': name})

    async def control(self, request):
        name = request.match_info['name']
        row = self.db.execute('SELECT token FROM machines WHERE name=?', (name,)).fetchone()
        supplied = request.headers.get('Authorization', '').removeprefix('Bearer ')
        if not row or not secrets.compare_digest(row[0], digest(supplied)):
            raise web.HTTPUnauthorized()
        if name in self.connections:
            raise web.HTTPConflict(text='already connected')
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=LIMIT)
        await ws.prepare(request)
        self.connections[name] = ws
        print('online:', name, flush=True)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    future = self.pending.get((name, data.get('id')))
                    if future and not future.done():
                        future.set_result(data.get('response'))
        finally:
            self.connections.pop(name, None)
            for (node, _), future in list(self.pending.items()):
                if node == name and not future.done():
                    future.set_exception(ConnectionError('node disconnected; outcome unknown'))
            print('offline:', name, flush=True)
        return ws

    async def rpc(self, request):
        self.auth(request, self.client)
        data = validate_rpc(await request.json())
        name = request.match_info['name']
        ws = self.connections.get(name)
        if ws is None:
            raise web.HTTPServiceUnavailable(text='offline')
        if len(self.pending) >= 128:
            raise web.HTTPTooManyRequests()
        rid = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self.pending[name, rid] = future
        try:
            await ws.send_json(dict(id=rid, **data))
            return web.json_response(await asyncio.wait_for(future, 30))
        except (TimeoutError, ConnectionError):
            raise web.HTTPGatewayTimeout(text='outcome unknown; not retried')
        finally:
            self.pending.pop((name, rid), None)

    def app(self):
        @web.middleware
        async def json_errors(request, handler):
            try:
                return await handler(request)
            except json.JSONDecodeError:
                raise web.HTTPBadRequest(text='invalid JSON')
        app = web.Application(client_max_size=LIMIT, middlewares=[json_errors])
        app.add_routes([web.post('/v1/machines/register', self.register), web.get('/v1/machines', self.catalog), web.get('/v1/machines/{name}/control', self.control), web.post('/v1/machines/{name}/rpc', self.rpc), web.delete('/v1/machines/{name}', self.revoke)])
        return app


async def local_rpc(path, request):
    validate_rpc(request)
    reader, writer = await asyncio.open_unix_connection(path, limit=LIMIT)
    try:
        writer.write((json.dumps(request) + '\n').encode())
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), 25))
    finally:
        writer.close()
        await writer.wait_closed()


def herdr():
    binary = os.environ.get('DROVER_HERDR', '')
    if not os.path.isabs(binary):
        raise ValueError('DROVER_HERDR must be an absolute pinned Herdr 0.9 path (use nix run)')
    version = subprocess.check_output([binary, '--version'], text=True).strip()
    if not re.search(r'\b0\.9\.\d+\b', version):
        raise ValueError('Herdr 0.9 required: ' + version)
    return binary


async def tunnel(config, port):
    delay = 1
    while True:
        proc = await asyncio.create_subprocess_exec('ssh', '-F', config['ssh_config'], '-NT', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', '-R', f'127.0.0.1:{port}:127.0.0.1:{config["local_ssh_port"]}', config['tunnel_alias'])
        try:
            await proc.wait()
        finally:
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()
        await asyncio.sleep(delay + random.random())
        delay = min(delay * 2, 30)


def connector(config):
    return TCPConnector(ssl=ssl.create_default_context(cafile=config.get('ca_file')))


async def serve(config):
    herdr()  # Never silently delegate to an ambient executable.
    user = pwd.getpwuid(os.getuid())
    expected = Path(user.pw_dir) / '.config/herdr/sessions' / config['session'] / 'herdr.sock'
    if config['ssh_user'] != user.pw_name or Path(config['socket']) != expected:
        raise ValueError('SSH user and conventional named-session socket must match the serving OS identity')
    if os.environ.get('XDG_CONFIG_HOME') or any(os.environ.get(k) for k in ('HERDR_SOCKET_PATH', 'HERDR_CONFIG_PATH', 'HERDR_SESSION')) or os.environ.get('HOME') != user.pw_dir:
        raise ValueError('serve requires conventional HOME and no XDG_CONFIG_HOME/HERDR_SOCKET_PATH override')
    # Native Herdr owns daemon startup. Never replace a running server.
    if not Path(config['socket']).exists():
        proc = await asyncio.create_subprocess_exec(herdr(), '--session', config['session'], 'remote-client-bridge', stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL)
        if await proc.wait():
            raise RuntimeError('native Herdr daemon bootstrap failed')
    response = await local_rpc(config['socket'], {'id': 'probe', 'method': 'ping', 'params': {}})
    result = response.get('result', {})
    if not str(result.get('version', '')).startswith('0.9.') or not result.get('capabilities', {}).get('detached_server_daemon'):
        raise RuntimeError('requires an existing Herdr 0.9 detached daemon; refusing to replace this server')
    print('Herdr namespace probe:', json.dumps(response), flush=True)
    identity_file = Path(config['identity_file'])
    async with ClientSession(connector=connector(config), timeout=ClientTimeout(total=40)) as http:
        if identity_file.exists():
            identity = json.loads(identity_file.read_text())
        else:
            async with http.post(config['url'] + '/v1/machines/register', json=config, headers={'Authorization': 'Bearer ' + os.environ['DROVER_ENROLLMENT_TOKEN']}) as resp:
                resp.raise_for_status()
                identity = await resp.json()
            private_write(identity_file, json.dumps(identity))
        task = asyncio.create_task(tunnel(config, identity['port']))
        delay = 1
        try:
            while True:
                try:
                    async with http.ws_connect(config['url'] + '/v1/machines/' + identity['name'] + '/control', headers={'Authorization': 'Bearer ' + identity['token']}, heartbeat=15, max_msg_size=LIMIT) as ws:
                        delay = 1
                        print('registered/control connected:', identity['name'], 'route:', identity['port'], flush=True)
                        queue = asyncio.Queue(maxsize=128)
                        async def worker():
                            while True:
                                request = await queue.get()
                                try:
                                    response = await local_rpc(config['socket'], request)
                                except Exception as exc:
                                    response = {'error': type(exc).__name__ + ': local RPC failed'}
                                await ws.send_json({'id': request['id'], 'response': response})
                        work = asyncio.create_task(worker())
                        try:
                            async for msg in ws:
                                if msg.type == WSMsgType.TEXT:
                                    # Keep receiving WebSocket ping/pong during slow local RPC.
                                    queue.put_nowait(json.loads(msg.data))
                        finally:
                            work.cancel()
                            with contextlib.suppress(asyncio.CancelledError, ConnectionError):
                                await work
                except Exception as exc:
                    if getattr(exc, 'status', None) in (401, 403):
                        raise RuntimeError('machine revoked or credential invalid; re-enroll explicitly') from exc
                    print('control reconnect:', type(exc).__name__, file=sys.stderr)
                await asyncio.sleep(delay + random.random())
                delay = min(delay * 2, 30)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def client_command(args, config):
    headers = {'Authorization': 'Bearer ' + os.environ['DROVER_CLIENT_TOKEN']}
    async with ClientSession(headers=headers, connector=connector(config), timeout=ClientTimeout(total=40)) as http:
        if args.command == 'rpc':
            payload = validate_rpc({'method': args.method, 'params': json.loads(args.params)})
            async with http.post(config['url'] + '/v1/machines/' + args.name + '/rpc', json=payload) as r:
                r.raise_for_status()
                print(json.dumps(await r.json(), indent=2))
            return
        async with http.get(config['url'] + '/v1/machines') as r:
            r.raise_for_status()
            machines = await r.json()
        if args.command == 'list':
            print(json.dumps(machines, indent=2))
            return
        binary = herdr()
        home = Path(config['client_home']).resolve()
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('HERDR_', 'XDG_'))}
        env['HOME'] = str(home)
        # Herdr reads this dedicated HOME's SSH config when building its -F file.
        private_write(home / '.ssh/config', f'Include {config["generated_ssh_config"]}\n')
        lines = [f'Include {config["jump_config"]}\n']
        hosts = []
        for m in machines:
            alias = 'drover-' + m['name']
            lines.append(f'Host {alias}\n  HostName 127.0.0.1\n  Port {m["port"]}\n  User {m["ssh_user"]}\n  ProxyJump {config["jump_alias"]}\n  HostKeyAlias {alias}\n  StrictHostKeyChecking yes\n  UserKnownHostsFile {config["known_hosts"]}\n  IdentityFile {config["identity_key"]}\n  IdentitiesOnly yes\n')
            hosts.append(alias + ' ' + m['host_key'])
        private_write(config['generated_ssh_config'], '\n'.join(lines))
        private_write(config['known_hosts'], '\n'.join(hosts) + '\n')
        existing = json.loads(subprocess.check_output([binary, 'machine', 'list', '--json'], env=env))
        saved = {(m['target'], m['session']) for m in existing}
        for m in machines:
            if m['online'] and ('drover-' + m['name'], m['session']) not in saved:
                # No stdin means Herdr cannot approve upgrades/replacements.
                subprocess.run([binary, 'machine', 'add', 'drover-' + m['name'], '--label', m['name'], '--remote-session', m['session']], check=True, env=env, stdin=subprocess.DEVNULL)
        process = subprocess.Popen([binary], env=env)
        try:
            if process.wait():
                raise RuntimeError('native Herdr client exited unsuccessfully')
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait()


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True, help='private JSON configuration')
    sub = p.add_subparsers(dest='command', required=True)
    for cmd in ('coordinator', 'serve', 'list', 'client'):
        sub.add_parser(cmd)
    rpc = sub.add_parser('rpc')
    rpc.add_argument('name')
    rpc.add_argument('method', choices=sorted(METHODS))
    rpc.add_argument('params', nargs='?', default='{}')
    args = p.parse_args()
    config = json.loads(Path(args.config).read_text())
    if args.command == 'coordinator':
        host, port = config.get('listen', '127.0.0.1:9840').rsplit(':', 1)
        tls = None
        if config.get('tls_cert'):
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(config['tls_cert'], config['tls_key'])
        if not tls and host not in ('127.0.0.1', '::1', 'localhost'):
            raise ValueError('non-loopback control listener requires TLS')
        enrollment, client = os.environ['DROVER_ENROLLMENT_TOKEN'], os.environ['DROVER_CLIENT_TOKEN']
        if min(len(enrollment), len(client)) < 32 or enrollment == client:
            raise ValueError('distinct enrollment/client secrets of at least 32 characters required')
        c = Coordinator(config['state'], enrollment, client, config.get('first_port', 24000))
        web.run_app(c.app(), host=host, port=int(port), ssl_context=tls, access_log=None)
    else:
        if not config['url'].startswith(('https://', 'http://127.0.0.1:', 'http://localhost:')):
            raise ValueError('control URL requires HTTPS (except loopback development)')
        def stop(_signum, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        try:
            asyncio.run(serve(config) if args.command == 'serve' else client_command(args, config))
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
