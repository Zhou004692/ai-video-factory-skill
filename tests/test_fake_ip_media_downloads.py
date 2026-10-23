"""Fake-IP compatibility must still connect only to verified public addresses."""
import socket
import unittest
from unittest import mock

import httpx
from factory.clients import provider_downloads, seedance
from tests import test_production_failure_matrix as fixtures

HOST = 'ark-acg-cn-beijing.tos-cn-beijing.volces.com'
URL = 'https://' + HOST + '/media.mp4?signature=secret'


def records(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443)) for ip in ips]


class FakeIPResolutionTests(unittest.TestCase):
    def resolve(self, answers, *, addresses=('198.18.0.7',), host=HOST, status=0):
        real_client = httpx.Client
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={'Status': status, 'Answer': answers})
        def client_factory(*args, **kwargs):
            self.assertIs(kwargs.get('trust_env'), False)
            self.assertIs(kwargs.get('follow_redirects'), False)
            return real_client(transport=httpx.MockTransport(handler))
        with mock.patch.object(provider_downloads.socket, 'getaddrinfo', return_value=records(*addresses)), \
             mock.patch.object(provider_downloads.httpx, 'Client', side_effect=client_factory):
            result = provider_downloads._validated_targets('https://' + host + '/media.mp4?signature=secret')
        return result, seen

    def test_official_fake_ip_uses_public_doh_and_pins_media_target(self):
        result, requests = self.resolve([{'name': HOST+'.', 'type': 1, 'data': '8.8.8.8'}])
        self.assertEqual(result[1], ['https://8.8.8.8/media.mp4?signature=secret'])
        self.assertEqual(result[2:], (HOST, HOST))
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.url.host, '1.1.1.1')
        self.assertEqual(request.url.params['name'], HOST)
        self.assertNotIn('signature', str(request.url))
        self.assertNotIn('authorization', request.headers)
        self.assertEqual(request.extensions['sni_hostname'], 'cloudflare-dns.com')

    def test_regular_public_dns_does_not_call_doh(self):
        result, seen = self.resolve([], addresses=('8.8.8.8',))
        self.assertEqual(result[1], ['https://8.8.8.8/media.mp4?signature=secret'])
        self.assertEqual(seen, [])

    def test_private_mixed_and_untrusted_hosts_never_use_fallback(self):
        for addresses, host in [(('127.0.0.1',), HOST), (('10.0.0.1',), HOST),
                                (('198.18.0.7','10.0.0.1'), HOST),
                                (('198.18.0.7','8.8.8.8'), HOST),
                                (('198.18.0.7',), 'evilvolces.com'),
                                (('198.18.0.7',), 'volces.com.evil.example')]:
            with self.subTest(addresses=addresses, host=host), \
                 mock.patch.object(provider_downloads.httpx, 'Client') as client, \
                 mock.patch.object(provider_downloads.socket, 'getaddrinfo', return_value=records(*addresses)):
                with self.assertRaises(ValueError):
                    provider_downloads.validate_url('https://'+host+'/media')
                client.assert_not_called()

    def test_doh_private_mixed_empty_invalid_and_failed_answers_are_rejected(self):
        cases = [([],0), ([{'type':1,'data':'127.0.0.1'}],0),
                 ([{'type':1,'data':'8.8.8.8'},{'type':1,'data':'10.0.0.1'}],0),
                 ([{'type':1,'data':'not-an-address'}],0),
                 ([{'type':1,'data':'8.8.8.8'}],3)]
        for answers, status in cases:
            with self.subTest(answers=answers,status=status), self.assertRaises(ValueError):
                self.resolve(answers,status=status)


class KnownTaskDownloadValidationTests(unittest.IsolatedAsyncioTestCase):
    _call_text_to_video = fixtures.SeedancePaidCallAmbiguityTests._call_text_to_video

    async def test_validation_failure_keeps_known_task_paused_without_repost(self):
        posts=[]
        def handler(request):
            if request.method=='POST':
                posts.append(request)
                return httpx.Response(200,json={'id':'paid-task'})
            return httpx.Response(200,json={'status':'succeeded','content':{'video_url':URL},'usage':{'completion_tokens':38830}})
        with mock.patch.object(provider_downloads,'download_bytes',new=mock.AsyncMock(side_effect=ValueError('unsafe address'))):
            error=await self._call_text_to_video(handler)
        self.assertEqual(len(posts),1)
        self.assertIsInstance(error,ValueError)
        self.assertEqual(getattr(error,'task_id',None),'paid-task')
        self.assertEqual(getattr(error,'pause_state',None),'PROVIDER_ACTION_REQUIRED')
        self.assertEqual(getattr(error,'stable_code',None),'provider_media_download_failed')
        self.assertTrue(error.non_retryable)

    async def test_inspection_validation_failure_returns_unknown_same_task(self):
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(
            200,json={'status':'succeeded','content':{'video_url':URL}})))
        with mock.patch.dict('os.environ',{'ARK_API_KEY':'test-key'}), \
             mock.patch.object(seedance.httpx,'AsyncClient',return_value=client), \
             mock.patch.object(provider_downloads,'download_bytes',new=mock.AsyncMock(side_effect=ValueError('unsafe address'))):
            result=await seedance.inspect_task('paid-task')
        self.assertEqual(result['status'],'unknown')
        self.assertEqual(result['task_id'],'paid-task')
        self.assertEqual(result['failure_code'],'provider_media_download_failed')
