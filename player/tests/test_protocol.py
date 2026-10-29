import asyncio
import unittest
from emosonic_player.protocol import CAPABILITIES, ProtocolClient, ProtocolError


class Socket:
    def __init__(self):
        self.handlers = {}
        self.connected = False
        self.messages = []
        self.suppress = False
        self.bad_registration = False

    def on(self, event, handler, namespace):
        self.handlers[event] = handler

    async def connect(self, url, **kwargs):
        self.kwargs = kwargs
        self.connected = True

    async def disconnect(self):
        self.connected = False
        await self.handlers['disconnect']()

    async def emit(self, event, data, namespace):
        self.messages.append(data)
        if self.suppress:
            return
        action = data['action']
        payload = {'action': action}
        reply = {'type': 'system', 'action': 'system.ack',
                 'requestId': data['requestId'], 'payload': payload}
        if action == 'auth.login':
            payload['authenticated'] = True
        elif action == 'device.register':
            payload.update(clientId='wrong' if self.bad_registration else 'box',
                           deviceSessionId='instance', negotiatedCapabilities=dict(CAPABILITIES),
                           strictV2={'protocolVersion': '2.9.0', 'connectionNonce': 'nonce',
                                     'connectionEpoch': 1, 'schemaHash': 'irrelevant'})
            reply.update(connectionNonce='nonce', connectionEpoch=1)
        else:
            reply.update(connectionNonce='nonce', connectionEpoch=1)
            if action == 'system.ping':
                reply['action'] = 'system.pong'
                reply['payload'] = {'serverTimeMs': 100000}
        await self.handlers['message'](reply)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.socket = Socket()
        self.client = ProtocolClient('https://example.test', 'user', 'secret', 'box',
                                     'instance', 'Speaker', socket_factory=lambda: self.socket,
                                     timeout=0.05)
        await self.client.connect()

    async def asyncTearDown(self):
        await self.client.close()

    def event(self, action='queue.context.sync', **payload):
        return {'type': 'state', 'action': action, 'payload': payload,
                'connectionNonce': 'nonce', 'connectionEpoch': 1}

    async def test_register_and_clock(self):
        self.assertTrue(self.client.ready)
        self.assertEqual(self.socket.kwargs['socketio_path'], 'emo/ws')
        self.assertEqual(self.socket.messages[1]['payload']['roles'], ['player', 'controller'])
        self.assertEqual(self.socket.messages[1]['payload']['capabilities'], CAPABILITIES)
        self.assertLess(abs(self.client.server_time_ms() - 100000), 100)

    async def test_canonical_before_ack_and_wrong_provenance(self):
        waiting = self.client.expect_event('queue.context.sync', lambda x: x['payload'].get('queueRevision') == 2)
        wrong = self.event(queueRevision=2)
        wrong['connectionNonce'] = 'old'
        await self.socket.handlers['message'](wrong)
        self.assertFalse(waiting.done())
        await self.socket.handlers['message'](self.event(queueRevision=1))
        self.assertFalse(waiting.done())
        await self.socket.handlers['message'](self.event(queueRevision=2))
        self.assertEqual((await waiting)['payload']['queueRevision'], 2)

    async def test_request_timeout_and_pending_cleanup(self):
        self.socket.suppress = True
        with self.assertRaisesRegex(ProtocolError, 'request_timeout'):
            await self.client.request('device.list', {})
        self.assertFalse(self.client._pending)

    async def test_disconnect_fails_pending_and_waiters(self):
        self.socket.suppress = True
        request = asyncio.create_task(self.client.request('device.list', {}))
        waiter = self.client.expect_event('queue.context.sync')
        await asyncio.sleep(0)
        await self.socket.disconnect()
        with self.assertRaisesRegex(ProtocolError, 'connection_lost'):
            await request
        with self.assertRaisesRegex(ProtocolError, 'connection_lost'):
            await waiter
        self.assertFalse(self.client.ready)
        self.assertIsNone(self.client.nonce)

    async def test_canonical_timeout(self):
        with self.assertRaisesRegex(ProtocolError, 'canonical_timeout'):
            await self.client.expect_event('never', timeout=0.001)
        await asyncio.sleep(0)
        self.assertFalse(self.client._waiters)

    async def test_callback_can_send_without_deadlock(self):
        done = asyncio.Event()
        async def handler(message):
            await self.client.request('device.list', {})
            done.set()
        self.client.on_event = handler
        await self.socket.handlers['message'](self.event())
        await asyncio.wait_for(done.wait(), 0.1)

    async def test_bad_registration_fails_closed(self):
        await self.client.close()
        self.socket.bad_registration = True
        with self.assertRaisesRegex(ProtocolError, 'registration_mismatch'):
            await self.client.connect()
        self.assertFalse(self.client.ready)
        self.assertIsNone(self.client.nonce)

    async def test_stale_socket_after_reconnect(self):
        old_handler = self.socket.handlers['message']
        await self.client.close()
        self.socket = Socket()
        await self.client.connect()
        waiting = self.client.expect_event('queue.context.sync')
        await old_handler(self.event())
        self.assertFalse(waiting.done())
        waiting.cancel()

    async def test_error_does_not_expose_server_message(self):
        self.socket.suppress = True
        request = asyncio.create_task(self.client.request('device.list', {}))
        await asyncio.sleep(0)
        request_id = self.socket.messages[-1]['requestId']
        reply = {
            'type': 'system', 'action': 'system.error', 'requestId': request_id,
            'connectionNonce': 'nonce', 'connectionEpoch': 1,
            'payload': {'action': 'device.list', 'code': 'token=secret', 'message': 'secret URL'}}
        await self.socket.handlers['message'](reply)
        with self.assertRaisesRegex(ProtocolError, '^server_error$'):
            await request

    async def test_send_confirmed_ignores_ack(self):
        task = asyncio.create_task(self.client.send_confirmed(
            'queue.context.sync', {}, 'queue.context.sync',
            lambda m: m['payload'].get('queueRevision') == 3, type='command'))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        await self.socket.handlers['message'](self.event(queueRevision=3))
        self.assertEqual((await task)['payload']['queueRevision'], 3)
        self.assertFalse(self.client._pending)

    async def test_send_confirmed_rejection(self):
        self.socket.suppress = True
        task = asyncio.create_task(self.client.send_confirmed(
            'playback.update', {}, 'playback.update', lambda m: True))
        await asyncio.sleep(0)
        request_id = self.socket.messages[-1]['requestId']
        message = self.event('system.error')
        message.update(requestId=request_id)
        message['payload'] = {'action': 'playback.update', 'code': 'conflict',
                              'message': 'secret data'}
        await self.socket.handlers['message'](message)
        with self.assertRaisesRegex(ProtocolError, '^conflict$'):
            await task
        self.assertFalse(self.client._pending)

    async def test_invalid_url_refuses_plaintext_remote(self):
        with self.assertRaises(ValueError):
            ProtocolClient('http://example.test', 'u', 'p', 'a', 'b', 'c')
