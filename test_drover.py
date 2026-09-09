import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from aiohttp import ClientSession, web
from drover import Coordinator, herdr, local_rpc, private_write, validate_rpc
from ssh_config import render


class Integration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.enrollment = os.urandom(24).hex()
        self.client = os.urandom(24).hex()
        self.c = Coordinator(self.temp.name, self.enrollment, self.client)
        self.runner = web.AppRunner(self.c.app())
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.url = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
        self.http = ClientSession()

    async def asyncTearDown(self):
        await self.http.close()
        await self.runner.cleanup()
        self.c.db.close()
        self.temp.cleanup()

    def auth(self, token):
        return {'Authorization': 'Bearer ' + token}

    async def enroll(self, name='node'):
        async with self.http.post(self.url + '/v1/machines/register', headers=self.auth(self.enrollment), json={'name': name, 'ssh_user': 'drover', 'session': 'drover', 'host_key': ''}) as r:
            return r.status, await r.text()

    async def test_auth_and_collision(self):
        async with self.http.get(self.url + '/v1/machines') as r:
            self.assertEqual(r.status, 401)
        self.assertEqual((await self.enroll())[0], 200)
        self.assertEqual((await self.enroll())[0], 409)
        self.assertEqual((await self.enroll('../bad'))[0], 400)
        async with self.http.get(self.url + '/v1/machines', headers=self.auth(self.client)) as r:
            data = await r.json()
            self.assertNotIn('token', data[0])
            self.assertFalse(data[0]['online'])

    async def test_stream_rpc_revoke(self):
        _, data = await self.enroll()
        identity = json.loads(data)
        ws = await self.http.ws_connect(self.url + '/v1/machines/node/control', headers=self.auth(identity['token']))
        async def reply():
            request = await ws.receive_json()
            await ws.send_json({'id': request['id'], 'response': {'result': {'type': 'pong'}}})
        task = asyncio.create_task(reply())
        async with self.http.post(self.url + '/v1/machines/node/rpc', headers=self.auth(self.client), json={'method': 'ping'}) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual((await r.json())['result']['type'], 'pong')
        await task
        async with self.http.delete(self.url + '/v1/machines/node', headers=self.auth(self.client)) as r:
            self.assertEqual(r.status, 200)
        await ws.close()
        await asyncio.sleep(.01)
        _, new = await self.enroll('other')
        self.assertGreater(json.loads(new)['port'], identity['port'])

    async def test_existing_herdr_workspace_and_input_methods_forward_unchanged(self):
        _, data = await self.enroll()
        identity = json.loads(data)
        ws = await self.http.ws_connect(self.url + '/v1/machines/node/control', headers=self.auth(identity['token']))
        requests = [
            ('workspace.create', {'cwd': "/repo O'Brien", 'label': 'visible', 'focus': False, 'env': {'PI_CODING_AGENT_DIR': '/worker/profile'}}),
            ('workspace.close', {'workspace_id': 'w2'}),
            ('agent.send_keys', {'target': 'w2:p1', 'keys': ['esc']}),
            ('pane.send_input', {'pane_id': 'w2:p1', 'text': "O'Brien; $(not-a-shell)", 'keys': ['Enter']}),
            ('pane.process_info', {'pane_id': 'w2:p1'}),
        ]
        async def node():
            for method, params in requests:
                request = await ws.receive_json()
                self.assertEqual((request['method'], request['params']), (method, params))
                await ws.send_json({'id': request['id'], 'response': {'result': {'native': params}}})
        task = asyncio.create_task(node())
        for method, params in requests:
            async with self.http.post(self.url + '/v1/machines/node/rpc', headers=self.auth(self.client), json={'method': method, 'params': params}) as r:
                self.assertEqual(r.status, 200)
                self.assertEqual(await r.json(), {'result': {'native': params}})
        await task
        await ws.close()

    async def test_offline_and_forbidden(self):
        for method, status in [('ping', 503), ('server.stop', 400), ('worktree.create', 400)]:
            async with self.http.post(self.url + '/v1/machines/node/rpc', headers=self.auth(self.client), json={'method': method}) as r:
                self.assertEqual(r.status, status)

    async def test_bad_json(self):
        async with self.http.post(self.url + '/v1/machines/register', headers=self.auth(self.enrollment), data='{bad') as r:
            self.assertEqual(r.status, 400)
        async with self.http.post(self.url + '/v1/machines/register', headers=self.auth(self.enrollment), json=[]) as r:
            self.assertEqual(r.status, 400)

    async def test_stream_identity_and_disconnect(self):
        _, data = await self.enroll()
        identity = json.loads(data)
        async with self.http.get(self.url + '/v1/machines/node/control', headers=self.auth(self.client)) as r:
            self.assertEqual(r.status, 401)
        ws = await self.http.ws_connect(self.url + '/v1/machines/node/control', headers=self.auth(identity['token']))
        async with self.http.get(self.url + '/v1/machines/node/control', headers=self.auth(identity['token'])) as r:
            self.assertEqual(r.status, 409)
        async def drop():
            await ws.receive_json()
            await ws.close()
        task = asyncio.create_task(drop())
        async with self.http.post(self.url + '/v1/machines/node/rpc', headers=self.auth(self.client), json={'method': 'ping'}) as r:
            self.assertEqual(r.status, 504)
            self.assertIn('outcome unknown', await r.text())
        await task
        self.assertEqual(self.c.pending, {})

    async def test_unix_api(self):
        path = self.temp.name + '/api.sock'
        async def handler(reader, writer):
            request = json.loads(await reader.readline())
            writer.write(json.dumps({'id': request['id'], 'result': {'type': 'pong'}}).encode() + b'\n')
            await writer.drain()
            writer.close()
        server = await asyncio.start_unix_server(handler, path)
        try:
            result = await local_rpc(path, {'id': 'test', 'method': 'ping'})
            self.assertEqual(result['id'], 'test')
        finally:
            server.close()
            await server.wait_closed()

    async def test_persistence(self):
        await self.enroll()
        c2 = Coordinator(self.temp.name, self.enrollment, self.client)
        self.assertEqual(c2.db.execute('SELECT name,port FROM machines').fetchone(), ('node', 24000))
        c2.db.close()


class Unit(unittest.TestCase):
    def test_allowlist(self):
        for payload in ([], {'method': 'exec'}, {'method': 'ping', 'params': []}):
            with self.assertRaises(web.HTTPBadRequest):
                validate_rpc(payload)

    def test_pinned_herdr_only(self):
        with patch.dict(os.environ, {'DROVER_HERDR': ''}):
            with self.assertRaises(ValueError):
                herdr()
        with patch.dict(os.environ, {'DROVER_HERDR': '/pinned/herdr'}), patch('subprocess.check_output', return_value='herdr 0.8.2'):
            with self.assertRaises(ValueError):
                herdr()
        with patch.dict(os.environ, {'DROVER_HERDR': '/pinned/herdr'}), patch('subprocess.check_output', return_value='herdr 0.9.0'):
            self.assertEqual(herdr(), '/pinned/herdr')

    def test_private_write(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'identity'
            private_write(path, 'first')
            private_write(path, 'second')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.read_text(), 'second')

    def test_ssh_restrictions(self):
        with tempfile.TemporaryDirectory() as root:
            # Deliberately not an actual key; tests only exercise rendering.
            config = dict(output=root, tunnel_user='drover-tunnel', jump_user='drover-jump', tunnel_keys={'node': 'ssh-ed25519 AAAA'}, client_key='ssh-ed25519 AAAA', ssh_listen='127.0.0.1:9841')
            text = render(config, [('node', 24000)])
            self.assertIn('GatewayPorts no', text)
            self.assertIn('MaxSessions 0', text)
            self.assertIn('AllowTcpForwarding remote', text)
            self.assertIn('permitlisten="127.0.0.1:24000"', (Path(root) / 'tunnel_keys').read_text())
            self.assertIn('permitopen="127.0.0.1:24000"', (Path(root) / 'jump_keys').read_text())
            self.assertNotIn('443', text)


if __name__ == '__main__':
    unittest.main()
