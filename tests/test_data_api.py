import importlib
import sys
import types
from unittest.mock import Mock

import fakeredis
import pytest
from flask import Flask


@pytest.fixture
def service(monkeypatch):
    tk = types.ModuleType('ckan.plugins.toolkit')
    tk.config = {'ckanext.terriassistant.data_enabled': True,
                 'ckanext.terriassistant.deepseek_api_key': 'test-provider-key'}
    tk.NotAuthorized = type('NotAuthorized', (Exception,), {})
    tk.ObjectNotFound = type('ObjectNotFound', (Exception,), {})
    tk.current_user = types.SimpleNamespace(id='alice', name='alice', state='active', is_anonymous=False)
    tk.enqueue_job = Mock()
    plugins = types.ModuleType('ckan.plugins')
    plugins.toolkit = tk
    ckan = types.ModuleType('ckan')
    ckan.plugins = plugins
    for name, module in [('ckan', ckan), ('ckan.plugins', plugins), ('ckan.plugins.toolkit', tk)]:
        monkeypatch.setitem(sys.modules, name, module)
    from ckanext.terriassistant.data_assistant import api, runtime, sources
    monkeypatch.setattr(api, 'tk', tk)
    connection = fakeredis.FakeRedis()
    monkeypatch.setattr(runtime, 'redis', lambda: connection)
    monkeypatch.setattr(sources, 'context_for', lambda uid: {'user': uid})
    monkeypatch.setattr(sources, 'resource_for', lambda *args, **kwargs: ({'id': args[1]}, {'id': 'dataset'}))
    app = Flask(__name__)
    app.secret_key = 'isolated-test-session'
    app.register_blueprint(api.blueprint)
    client = app.test_client()
    return types.SimpleNamespace(tk=tk, api=api, runtime=runtime, sources=sources, redis=connection, app=app, client=client)


BASE = '/api/terriassistant/data'


def test_catalog_exact_name_keeps_slug_and_title(service, monkeypatch):
    s = service
    package = {'id': 'dataset', 'name': 'qa-private-20261029', 'title': 'Private QA',
               'resources': [{'id': 'resource'}]}
    lookup = Mock(return_value=package)
    search = Mock(side_effect=AssertionError('Exact match must not become a broad Solr query'))
    s.tk.get_action = lambda name: {'package_show': lookup, 'package_search': search}[name]
    monkeypatch.setattr(s.sources, 'resource_for', lambda *args: ({'id': 'resource'}, package))
    result = s.sources.search('alice', 'qa-private-20261029')
    assert result['datasets_found'] == 1
    assert result['sources'][0]['dataset_name'] == 'qa-private-20261029'
    assert result['sources'][0]['dataset_title'] == 'Private QA'
    assert result['truncated'] is False


def test_catalog_results_are_bounded_and_explicitly_truncated(service):
    s = service
    packages = [{'id': str(i), 'resources': [{'id': f'{i}-{j}'} for j in range(20)]} for i in range(5)]
    s.tk.get_action = lambda name: lambda *args: {'count': 100, 'results': packages}
    result = s.sources.search('alice', 'water quality')
    assert len(result['sources']) == 20 and result['truncated'] is True


def setup_conversation(s, client=None):
    client = client or s.client
    bootstrap = client.get(BASE + '/bootstrap')
    assert bootstrap.status_code == 200
    headers = {'X-CSRFToken': bootstrap.json['csrf_token']}
    response = client.post(BASE + '/conversations', json={}, headers=headers)
    assert response.status_code == 201
    return response.json['id'], headers


def ask(s, cid, headers, request_id='request-1'):
    return s.client.post(BASE + f'/conversations/{cid}/turns', json={
        'question': 'What is the mean?', 'request_id': request_id,
        'sources': [{'id': 'layer:sample', 'name': 'Sample', 'format': 'csv'}]}, headers=headers)


def test_anonymous_and_csrf_are_rejected(service):
    s = service
    s.tk.current_user.is_anonymous = True
    assert s.client.get(BASE + '/bootstrap').status_code == 401
    s.tk.current_user.is_anonymous = False
    assert s.client.post(BASE + '/conversations', json={}).status_code == 403


def test_session_isolation_and_no_cache(service):
    s = service
    cid, headers = setup_conversation(s)
    response = ask(s, cid, headers)
    assert response.status_code == 202
    assert 'no-store' in response.headers['Cache-Control']
    assert 'Access-Control-Allow-Origin' not in response.headers
    second = s.app.test_client()
    assert second.get(BASE + '/turns/' + response.json['id']).status_code == 404
    s.tk.current_user = types.SimpleNamespace(id='bob', name='bob', state='active', is_anonymous=False)
    assert s.client.get(BASE + '/turns/' + response.json['id']).status_code == 404


def test_one_active_turn_and_idempotent_retry(service):
    s = service
    cid, headers = setup_conversation(s)
    first = ask(s, cid, headers)
    assert ask(s, cid, headers).json['id'] == first.json['id']
    assert s.tk.enqueue_job.call_count == 1
    assert ask(s, cid, headers, 'request-2').status_code == 409
    assert s.client.post(BASE + '/turns/' + first.json['id'] + '/cancel', json={}, headers=headers).status_code == 200
    assert ask(s, cid, headers, 'request-2').status_code == 202


def test_model_tool_roundtrip_and_exact_pending_ids(service, monkeypatch):
    s = service
    cid, headers = setup_conversation(s)
    first = ask(s, cid, headers)
    tid = first.json['id']
    responses = [
        {'role': 'assistant', 'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {'name': 'query_data', 'arguments': '{"source_id":"layer:sample","operation":"statistics","column":"value"}'}}]},
        {'role': 'assistant', 'content': 'The mean is 2 [E1].'},
    ]
    payloads = []
    def post(url, **kwargs):
        payloads.append(kwargs['json'])
        response = Mock(status_code=200)
        response.json.return_value = {'choices': [{'finish_reason': 'stop', 'message': responses.pop(0)}]}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response
    monkeypatch.setattr(s.runtime.requests, 'post', post)
    s.runtime.run(cid, tid)
    turn = s.client.get(BASE + '/turns/' + tid).json
    assert turn['status'] == 'awaiting_tools' and turn['pending'][0]['id'] == 'call-1'
    bad = s.client.post(BASE + '/turns/' + tid + '/tool-results', json={'results': [{'id': 'invented', 'result': {}}]}, headers=headers)
    assert bad.status_code == 400
    result = {'results': [{'id': 'call-1', 'result': {'statistics': {'mean': 2, 'count': 3}, 'complete': True}}]}
    assert s.client.post(BASE + '/turns/' + tid + '/tool-results', json=result, headers=headers).status_code == 202
    assert s.client.post(BASE + '/turns/' + tid + '/tool-results', json=result, headers=headers).status_code == 409
    s.runtime.run(cid, tid)
    turn = s.client.get(BASE + '/turns/' + tid).json
    assert turn['status'] == 'completed' and turn['evidence'][0]['statistics']['mean'] == 2
    assert all(p['model'] == 'deepseek-flash' for p in payloads)
    assert payloads[1]['messages'][-1]['role'] == 'tool'
    assert 'test-provider-key' not in str(turn)
    assert not s.redis.exists(s.runtime.PREFIX + 'active:alice')


def test_late_worker_cannot_overwrite_new_turn(service):
    s = service
    cid, headers = setup_conversation(s)
    first = ask(s, cid, headers)
    stale = s.runtime.get(cid)
    s.client.post(BASE + '/turns/' + first.json['id'] + '/cancel', json={}, headers=headers)
    new = ask(s, cid, headers, 'request-2')
    stale['turn']['answer'] = 'stale private answer'
    s.runtime.put(stale, first.json['id'])
    s.runtime.run(cid, first.json['id'])
    assert s.runtime.get(cid)['turn']['id'] == new.json['id']
    assert 'answer' not in s.runtime.get(cid)['turn']


def test_revoked_data_is_not_returned_from_cached_results(service, monkeypatch):
    s = service
    cid, headers = setup_conversation(s)
    tid = ask(s, cid, headers).json['id']
    state = s.runtime.get(cid)
    state['used_resources'] = ['private-resource']
    s.runtime.put(state)
    def deny(*args, **kwargs):
        raise s.tk.NotAuthorized()
    monkeypatch.setattr(s.sources, 'resource_for', deny)
    assert s.client.get(BASE + '/turns/' + tid).status_code == 403


def test_expiry_and_deletion(service):
    s = service
    cid, headers = setup_conversation(s)
    tid = ask(s, cid, headers).json['id']
    assert s.redis.ttl(s.runtime.key(cid)) > 0
    assert s.client.delete(BASE + '/conversations/' + cid, headers=headers).status_code == 200
    assert s.client.get(BASE + '/turns/' + tid).status_code == 404


def test_ckan_logout_removes_state_and_cancels_worker(service):
    from flask import session
    s = service
    cid, headers = setup_conversation(s)
    tid = ask(s, cid, headers).json['id']
    with s.client.session_transaction() as saved:
        cookie_state = dict(saved)
    with s.app.test_request_context():
        session.update(cookie_state)
        s.api.clear_session()
    assert s.runtime.get(cid) is None
    assert s.redis.exists(s.runtime.PREFIX + 'cancel:' + tid)
    assert not s.redis.exists(s.runtime.PREFIX + 'active:alice')


def test_download_checks_all_dns_addresses(service, monkeypatch):
    s = service
    monkeypatch.setattr(s.sources.socket, 'getaddrinfo', lambda *a, **kw: [(None, None, None, None, ('93.184.216.34', 443)), (None, None, None, None, ('127.0.0.1', 443))])
    with pytest.raises(ValueError):
        s.sources.public_addresses('rebind.invalid', 443)
    with pytest.raises(ValueError):
        s.sources.download('file:///etc/passwd', 100)


def test_provider_failure_is_safe(service, monkeypatch):
    s = service
    cid, headers = setup_conversation(s)
    tid = ask(s, cid, headers).json['id']
    response = Mock(status_code=429)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(s.runtime.requests, 'post', lambda *a, **kw: response)
    s.runtime.run(cid, tid)
    turn = s.client.get(BASE + '/turns/' + tid).json
    assert turn['status'] == 'error' and 'DeepSeek' in turn['error']
    assert not s.redis.exists(s.runtime.PREFIX + 'active:alice')
