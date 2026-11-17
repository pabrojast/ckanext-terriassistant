"""Keep caller credentials on the authorized CKAN upload; pin external downloads."""
from unittest.mock import Mock

import pytest
from ckanext.terriassistant.config import TerriassistantSettings
from ckanext.terriassistant.data_service import DataService, DataFetchError, DataTooLargeError, FetchContext
from ckanext.terriassistant.data_assistant import sources


UPLOAD = 'https://ckan.example/dataset/water/resource/resource/download/data.csv'


def response(url, code=200, location=None, chunks=(b'value\n1\n',)):
    result = Mock(status_code=code, url=url, encoding='utf-8', headers={'Location': location} if location else {})
    result.iter_content.return_value = iter(chunks)
    return result


def download(monkeypatch, responses=(), maximum=1024):
    calls = []
    def get(target, **kwargs):
        calls.append((target, kwargs))
        return responses[len(calls) - 1]
    monkeypatch.setattr('ckanext.terriassistant.data_service.requests.get', get)
    service = DataService(TerriassistantSettings(max_resource_bytes=maximum))
    context = FetchContext(site_url='https://ckan.example', authorization='test-token', cookie_header='session=test-cookie')
    return service, context, calls


def test_same_resource_upload_receives_credentials_and_response_is_closed(monkeypatch):
    r = response(UPLOAD)
    service, context, calls = download(monkeypatch, [r])
    assert service._download_bytes({'id': 'resource', 'url': UPLOAD.replace('https://ckan.example', '')}, context)[0] == b'value\n1\n'
    assert calls[0][1]['headers']['Authorization'] == 'test-token'
    assert calls[0][1]['headers']['Cookie'] == 'session=test-cookie'
    assert calls[0][1]['allow_redirects'] is False
    r.close.assert_called_once()


@pytest.mark.parametrize('target', [UPLOAD.replace('/resource/resource/', '/resource/other/'),
                                   'https://ckan.example/api/3/action/user_list',
                                   'https://ckan.example/data.csv',
                                   UPLOAD + '/../../../../api/3/action/user_list',
                                   UPLOAD + '/%2e%2e/%2e%2e/api/3/action/user_list',
                                   UPLOAD + '/%252e%252e/api/3/action/user_list',
                                   UPLOAD + '/%5c..%5capi/3/action/user_list'])
def test_other_ckan_resource_and_api_paths_never_receive_caller_credentials(monkeypatch, target):
    service, context, calls = download(monkeypatch, [response(target)])
    service._download_bytes({'id': 'resource', 'url': target}, context)
    assert 'Authorization' not in calls[0][1]['headers'] and 'Cookie' not in calls[0][1]['headers']


@pytest.mark.parametrize('target', ['https://storage.example/data.csv', 'https://ckan.example:444/data.csv',
                                   'http://ckan.example/data.csv', '//external.example/data.csv'])
def test_external_origin_uses_pinned_public_downloader_without_credentials(monkeypatch, target):
    service, context, calls = download(monkeypatch)
    public_download = Mock(return_value=(b'value\n1\n', 'https://storage.example/final.csv', {'Content-Type': 'text/csv; charset=utf-8'}))
    monkeypatch.setattr('ckanext.terriassistant.data_service.download_public_resource', public_download)
    raw, final, metadata = service._download_bytes({'id': 'resource', 'url': target}, context)
    public_download.assert_called_once_with('https:' + target if target.startswith('//') else target, 1024, include_metadata=True)
    assert not calls and raw == b'value\n1\n' and metadata.encoding == 'utf-8'


def test_same_origin_redirect_delegates_external_chain_without_credentials(monkeypatch):
    r = response(UPLOAD, 302, 'https://storage.example/redirect')
    service, context, calls = download(monkeypatch, [r])
    public_download = Mock(return_value=(b'value\n1\n', 'https://ckan.example/final.csv', {}))
    monkeypatch.setattr('ckanext.terriassistant.data_service.download_public_resource', public_download)
    service._download_bytes({'id': 'resource', 'url': UPLOAD}, context)
    assert calls[0][1]['headers']['Authorization'] == 'test-token'
    public_download.assert_called_once_with('https://storage.example/redirect', 1024, include_metadata=True)
    r.close.assert_called_once()


def test_same_resource_relative_redirect_keeps_authorized_download_working(monkeypatch):
    r1 = response(UPLOAD, 302, 'final.csv'); r2 = response(UPLOAD.replace('data.csv', 'final.csv'))
    service, context, calls = download(monkeypatch, [r1, r2])
    service._download_bytes({'id': 'resource', 'url': UPLOAD}, context)
    assert calls[1][0] == r2.url and calls[1][1]['headers']['Cookie'] == 'session=test-cookie'


def test_redirect_to_different_resource_strips_and_does_not_reattach_credentials(monkeypatch):
    other = UPLOAD.replace('/resource/resource/', '/resource/other/')
    responses = [response(UPLOAD, 302, other), response(other, 302, UPLOAD), response(UPLOAD)]
    service, context, calls = download(monkeypatch, responses)
    service._download_bytes({'id': 'resource', 'url': UPLOAD}, context)
    assert calls[0][1]['headers']['Cookie'] == 'session=test-cookie'
    for _, kwargs in calls[1:]:
        assert 'Authorization' not in kwargs['headers'] and 'Cookie' not in kwargs['headers']


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'https://user:pass@ckan.example/data.csv', 'https://ckan.example:invalid/data.csv'])
def test_invalid_or_embedded_credential_urls_never_reach_network(monkeypatch, url):
    service, context, calls = download(monkeypatch)
    with pytest.raises(DataFetchError):
        service._download_bytes({'url': url}, context)
    assert not calls


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1'])
def test_external_private_address_is_rejected_before_connection(monkeypatch, address):
    service, context, calls = download(monkeypatch)
    monkeypatch.setattr(sources.socket, 'getaddrinfo', lambda *args, **kwargs: [(None, None, None, None, (address, 443))])
    pool = Mock(side_effect=AssertionError('Private address must never be contacted'))
    monkeypatch.setattr(sources.urllib3, 'HTTPSConnectionPool', pool)
    with pytest.raises(DataFetchError):
        service._download_bytes({'url': 'https://private.example/file.csv'}, context)
    assert not calls
    pool.assert_not_called()


def test_public_redirect_rechecks_dns_and_pins_only_public_address(monkeypatch):
    service, context, calls = download(monkeypatch)
    def resolve(host, port, **kwargs):
        return [(None, None, None, None, ('8.8.8.8' if host == 'public.example' else '127.0.0.1', port))]
    monkeypatch.setattr(sources.socket, 'getaddrinfo', resolve)
    upstream = Mock(status=302, headers={'Location': 'https://private.example/data.csv'})
    pool = Mock(); pool.request.return_value = upstream
    factory = Mock(return_value=pool)
    monkeypatch.setattr(sources.urllib3, 'HTTPSConnectionPool', factory)
    with pytest.raises(DataFetchError):
        service._download_bytes({'url': 'https://public.example/data.csv'}, context)
    assert factory.call_count == 1 and factory.call_args.args == ('8.8.8.8',)
    assert factory.call_args.kwargs['assert_hostname'] == 'public.example'
    headers = pool.request.call_args.kwargs['headers']
    assert 'Authorization' not in headers and 'Cookie' not in headers
    upstream.close.assert_called_once(); pool.close.assert_called_once()


def test_oversized_stream_closes_response(monkeypatch):
    r = response(UPLOAD, chunks=(b'123456',))
    service, context, _ = download(monkeypatch, [r], maximum=5)
    with pytest.raises(DataTooLargeError):
        service._download_bytes({'url': UPLOAD}, context)
    r.close.assert_called_once()


def test_redirect_loop_is_bounded_and_connections_closed(monkeypatch):
    responses = [response(UPLOAD, 302, UPLOAD) for _ in range(6)]
    service, context, calls = download(monkeypatch, responses)
    with pytest.raises(DataFetchError, match='too many redirects'):
        service._download_bytes({'url': UPLOAD}, context)
    assert len(calls) == 6
    for r in responses:
        r.close.assert_called_once()
