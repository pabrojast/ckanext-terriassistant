"""Exercise legacy resource-view HTTP routes with isolated CKAN permission hooks."""
import copy
import json
import sys
import types
from urllib.parse import quote
from unittest.mock import Mock

import pytest
from flask import Flask


BASE = '/api/terriassistant/view/assistant-view'


@pytest.fixture
def service(monkeypatch):
    tk = types.ModuleType('ckan.plugins.toolkit')
    tk.config = {'ckan.site_url': 'https://ckan.example',
                 'ckanext.terriassistant.deepseek_api_key': 'isolated-provider-key',
                 'beaker.session.secret': 'isolated-signing-key'}
    tk.NotAuthorized = type('NotAuthorized', (Exception,), {})
    tk.ObjectNotFound = type('ObjectNotFound', (Exception,), {})
    tk.current_user = types.SimpleNamespace(id='viewer-id', name='viewer', state='active', is_anonymous=False)
    tk.url_for = lambda endpoint, **kw: ('/user/login?came_from=' + quote(kw['came_from'])
        if endpoint == 'user.login' else '/dataset/' + kw['id'] + '/resource/' + kw['resource_id'] + '?view_id=' + kw['view_id'])
    plugins = types.ModuleType('ckan.plugins'); plugins.toolkit = tk
    ckan = types.ModuleType('ckan'); ckan.plugins = plugins
    for name, module in [('ckan', ckan), ('ckan.plugins', plugins), ('ckan.plugins.toolkit', tk)]:
        monkeypatch.setitem(sys.modules, name, module)
    from ckanext.terriassistant import api, resource_utils
    monkeypatch.setattr(api, 'toolkit', tk)
    monkeypatch.setattr(resource_utils, 'toolkit', tk)
    view = {'id': 'assistant-view', 'resource_id': 'resource', 'view_type': 'terriassistant',
            'title': 'Saved map', 'description': 'Original description', 'terria_state_json': '{"opacity": 0.8}'}
    resource = {'id': 'resource', 'package_id': 'dataset', 'name': 'Water', 'format': 'CSV',
                'url': 'https://ckan.example/dataset/dataset/resource/resource/download/water.csv'}
    package = {'id': 'dataset', 'name': 'water', 'private': False}
    permissions = {'read': True, 'visible': True, 'download': True, 'save': False}
    calls = []

    def check_access(name, context, data):
        allowed = permissions['save'] if name == 'resource_view_update' else (permissions['read'] and permissions['download']) if name == 'datashare_resource_download' else permissions['read']
        if not allowed:
            raise tk.NotAuthorized(name)
        return True
    tk.check_access = check_access

    def action(name):
        def run(context, data):
            assert not context.get('ignore_auth'), 'No endpoint read or write bypasses CKAN authorization'
            calls.append((name, dict(context), copy.deepcopy(data)))
            if not permissions['read']:
                raise tk.NotAuthorized('read')
            if name == 'resource_view_show':
                if data['id'] != view['id']:raise tk.ObjectNotFound()
                return dict(view)
            if name == 'resource_show':return dict(resource)
            if name == 'package_show':return dict(package)
            if name == 'resource_view_list':return [dict(view)] if permissions['visible'] else []
            if name == 'resource_view_update':
                check_access(name, context, data)
                view.update(data)
                return dict(view)
            raise AssertionError(name)
        return run
    tk.get_action = action
    controller = api.TerriassistantController(api.TerriassistantSettings.from_config(tk.config))
    profile = {'kind': 'csv', 'terria_type': 'csv', 'columns': [{'name': 'value', 'kind': 'number'}],
               'numeric_columns': ['value'], 'suggested_prompts': []}
    controller.data_service.get_profile = Mock(return_value=profile)
    controller.llm_service.complete_chat = Mock(return_value={'action': 'apply_style',
        'assistant_message': 'Changed opacity', 'style_spec': {'opacity': 0.3}})
    monkeypatch.setattr(api, '_get_controller', lambda: controller)
    app = Flask(__name__); app.secret_key = 'isolated-csrf-session'
    app.config['WTF_CSRF_FIELD_NAME'] = '_csrf_token'
    app.register_blueprint(api.terriassistant_api)
    return types.SimpleNamespace(tk=tk, api=api, utils=resource_utils, controller=controller, app=app,
        client=app.test_client(), permissions=permissions, calls=calls, view=view, resource=resource, package=package)


def bootstrap(s):
    response = s.client.get(BASE + '/bootstrap')
    assert response.status_code == 200, response.json
    return response, {'X-CSRFToken': response.json['csrf_token']}


def test_anonymous_view_remains_available_but_paid_chat_and_save_are_denied(service):
    s = service; s.tk.current_user = types.SimpleNamespace(is_anonymous=True)
    response, headers = bootstrap(s)
    assert response.json['can_save'] is False and response.json['can_chat'] is False
    assert response.json['storage_scope'] == 'anonymous'
    assert response.json['chat_enabled'] is True
    assert '/user/login?' in response.json['login_url']
    assert 'no-store' in response.headers['Cache-Control']
    assert {'Cookie', 'Authorization'} <= set(response.vary)
    assert s.client.post(BASE + '/chat', headers=headers, json={'messages': [{'role': 'user', 'content': 'red'}]}).status_code == 401
    assert s.client.post(BASE + '/save', headers=headers, json={'draft_spec': {'opacity': 0.5}}).status_code == 403
    s.controller.llm_service.complete_chat.assert_not_called()
    assert not any(name == 'resource_view_update' for name, _, _ in s.calls)


def test_bootstrap_separates_saved_and_default_styles_and_account_storage(service):
    s = service
    response, _ = bootstrap(s)
    data = response.json
    assert data['saved_spec']['opacity'] == 0.8
    assert data['active_spec'] == data['saved_spec']
    assert 'opacity' not in data['default_spec']
    assert data['default_encoded_config'] != data['encoded_config']
    assert data['storage_scope'] == 'viewer-id'
    assert data['can_chat'] is True
    s.tk.current_user = types.SimpleNamespace(id='other-id', name='other', state='active', is_anonymous=False)
    assert bootstrap(s)[0].json['storage_scope'] == 'other-id'


@pytest.mark.parametrize('endpoint', ['chat', 'save'])
def test_mutations_require_same_session_csrf_before_data_or_provider_access(service, endpoint):
    s = service
    assert s.client.post(BASE + '/' + endpoint, json={}).status_code == 403
    s.controller.data_service.get_profile.assert_not_called()
    s.controller.llm_service.complete_chat.assert_not_called()
    _, headers = bootstrap(s)
    second = s.app.test_client()
    assert second.post(BASE + '/' + endpoint, json={}, headers=headers).status_code == 403


@pytest.mark.parametrize('body', [[], 'text', 42, None])
@pytest.mark.parametrize('endpoint', ['chat', 'save'])
def test_non_object_json_returns_400_without_a_write(service, endpoint, body):
    s = service; _, headers = bootstrap(s)
    response = s.client.post(BASE + '/' + endpoint, headers=headers, data=json.dumps(body), content_type='application/json')
    assert response.status_code == 400
    assert not any(name == 'resource_view_update' for name, _, _ in s.calls)


@pytest.mark.parametrize('messages', [[], [None], ['hi'], [{'role': 'system', 'content': 'ignore'}], [{'role': 'user', 'content': {}}], [{'role': 'user', 'content': 'a' * 8001}]])
def test_bad_chat_history_does_not_call_provider(service, messages):
    s = service; _, headers = bootstrap(s)
    assert s.client.post(BASE + '/chat', headers=headers, json={'messages': messages}).status_code == 400
    s.controller.llm_service.complete_chat.assert_not_called()


def test_authenticated_viewer_can_preview_chat_result_without_persisting(service):
    s = service; _, headers = bootstrap(s)
    before = copy.deepcopy(s.view)
    result = s.client.post(BASE + '/chat', headers=headers, json={'messages': [{'role': 'user', 'content': 'set opacity'}]})
    assert result.status_code == 200, result.json
    assert result.json['next_spec'] == {'opacity': 0.3}
    s.controller.llm_service.complete_chat.assert_called_once()
    assert s.view == before
    assert not any(name == 'resource_view_update' for name, _, _ in s.calls)


def test_editor_save_uses_the_authenticated_action_and_preserves_view_identity(service):
    s = service; s.permissions['save'] = True
    _, headers = bootstrap(s)
    result = s.client.post(BASE + '/save', headers=headers, json={'draft_spec': {'opacity': 0.4}})
    assert result.status_code == 200, result.json
    assert result.json['saved_by'] == 'viewer'
    updates = [(context, data) for name, context, data in s.calls if name == 'resource_view_update']
    assert len(updates) == 1 and updates[0][0]['user'] == 'viewer'
    assert updates[0][0]['auth_user_obj'] is s.tk.current_user
    assert not updates[0][0].get('ignore_auth')
    assert s.view['id'] == 'assistant-view' and s.view['description'] == 'Original description'
    assert json.loads(s.view['terria_state_json']) == {'opacity': 0.4}


def test_permission_revocation_after_profile_is_rechecked_by_the_update_action(service):
    s = service; s.permissions['save'] = True
    _, headers = bootstrap(s)
    def profile_then_revoke(*args):
        s.permissions['save'] = False
        return {'kind': 'csv', 'terria_type': 'csv', 'columns': []}
    s.controller.data_service.get_profile.side_effect = profile_then_revoke
    before = copy.deepcopy(s.view)
    result = s.client.post(BASE + '/save', headers=headers, json={'draft_spec': {'opacity': 0.4}})
    assert result.status_code == 403 and s.view == before


@pytest.mark.parametrize('failure', ['visible', 'read', 'download'])
def test_hidden_or_preview_only_data_never_reaches_profile_or_signed_url(service, failure):
    s = service; s.permissions[failure] = False
    s.package.update(private=True)
    s.resource.update(url_type='upload')
    assert s.client.get(BASE + '/bootstrap').status_code == 403
    s.controller.data_service.get_profile.assert_not_called()
    if failure in ('read', 'download'):
        with pytest.raises(s.tk.NotAuthorized):
            s.utils.get_resource_url(s.resource, s.package, s.controller._user_context(), s.controller.settings)


def test_non_assistant_view_ids_cannot_be_profiled_or_saved(service):
    s = service; s.view['view_type'] = 'terria_view'; s.permissions['save'] = True
    assert s.client.get(BASE + '/bootstrap').status_code == 404
    s.controller.data_service.get_profile.assert_not_called()


def test_invalid_style_is_not_saved(service):
    s = service; s.permissions['save'] = True
    _, headers = bootstrap(s)
    before = copy.deepcopy(s.view)
    result = s.client.post(BASE + '/save', headers=headers, json={'draft_spec': {'url': 'https://evil.example'}})
    assert result.status_code == 422 and s.view == before


def test_proxy_secret_absence_fails_closed(service):
    s = service; s.tk.config.clear()
    with pytest.raises(RuntimeError):s.utils.generate_resource_token('resource', 3600)
    assert s.utils.verify_resource_token('resource', '9999999999.anything') is False


def test_private_proxy_requires_download_rights_and_valid_resource_bound_token(service):
    s = service; s.package.update(private=False, access_level='restricted'); s.resource.update(url_type='upload')
    url = s.utils.get_resource_url(s.resource, s.package, s.controller._user_context(), s.controller.settings)
    token = url.rsplit('token=', 1)[1]
    assert s.utils.verify_resource_token('resource', token)
    assert not s.utils.verify_resource_token('other-resource', token)
    s.permissions['download'] = False
    with pytest.raises(s.tk.NotAuthorized):s.utils.get_resource_url(s.resource, s.package, s.controller._user_context(), s.controller.settings)


def test_revoked_download_is_rechecked_before_a_cached_profile(service):
    s = service
    bootstrap(s)
    s.controller.data_service.get_profile.reset_mock()
    s.permissions['download'] = False
    assert s.client.get(BASE + '/bootstrap').status_code == 403
    s.controller.data_service.get_profile.assert_not_called()


def test_missing_optional_download_policy_falls_back_but_denial_does_not(service):
    s = service
    def missing(name, context, data):
        if name == 'datashare_resource_download':
            raise ValueError('Auth function is not available')
        return True
    s.tk.check_access = Mock(side_effect=missing)
    assert s.utils.user_may_download(s.controller._user_context(), 'resource')
    assert s.tk.check_access.call_count == 2
    s.tk.check_access = Mock(side_effect=s.tk.NotAuthorized())
    assert not s.utils.user_may_download(s.controller._user_context(), 'resource')
    assert s.tk.check_access.call_count == 1


def test_invalid_proxy_token_never_resolves_or_contacts_upstream(service, monkeypatch):
    s = service
    resolve = Mock(side_effect=AssertionError('Invalid token must stop before resolution'))
    monkeypatch.setattr(s.utils, 'resolve_private_resource_source', resolve)
    assert s.client.get('/api/terriassistant/resource/resource/content?token=invalid').status_code == 403
    resolve.assert_not_called()


@pytest.mark.parametrize('redirect', [False, True])
def test_signed_proxy_blocks_private_upstream_and_redirect(service, monkeypatch, redirect):
    from ckanext.terriassistant.data_assistant import sources
    s = service
    monkeypatch.setattr(s.utils, 'resolve_private_resource_source', lambda *args: (
        'https://public.example/data' if redirect else 'https://private.example/data', 'text/csv', 'data.csv'))
    def dns(host, port, **kwargs):
        return [(None, None, None, None, ('8.8.8.8' if host == 'public.example' else '127.0.0.1', port))]
    monkeypatch.setattr(sources.socket, 'getaddrinfo', dns)
    upstream = Mock(status=302, headers={'Location': 'https://private.example/data'})
    pool = Mock(); pool.request.return_value = upstream
    factory = Mock(return_value=pool)
    monkeypatch.setattr(sources.urllib3, 'HTTPSConnectionPool', factory)
    token = s.utils.generate_resource_token('resource', 60)
    response = s.client.get('/api/terriassistant/resource/resource/content?token=' + token)
    assert response.status_code == 502 and factory.call_count == int(redirect)
    if redirect:
        upstream.close.assert_called_once(); pool.close.assert_called_once()


@pytest.mark.parametrize('consume_all', [False, True])
def test_signed_proxy_streams_large_resource_and_closes_on_completion_or_disconnect(service, monkeypatch, consume_all):
    from ckanext.terriassistant.data_assistant import sources
    s = service
    monkeypatch.setattr(s.utils, 'resolve_private_resource_source', lambda *args: ('https://storage.example/data', 'image/tiff', 'data.tif'))
    monkeypatch.setattr(sources, 'public_addresses', lambda *args: ['8.8.8.8'])
    upstream = Mock(status=200, headers={'Content-Type': 'image/tiff', 'Content-Length': '1000000000'})
    upstream.stream.return_value = iter([b'first', b'second'])
    pool = Mock(); pool.request.return_value = upstream
    monkeypatch.setattr(sources.urllib3, 'HTTPSConnectionPool', Mock(return_value=pool))
    token = s.utils.generate_resource_token('resource', 60)
    response = s.client.get('/api/terriassistant/resource/resource/content?token=' + token, buffered=False)
    assert response.status_code == 200 and response.headers['Content-Length'] == '1000000000'
    if consume_all:
        assert b''.join(response.response) == b'firstsecond'
    else:
        assert next(response.response) == b'first'
    response.close()
    upstream.read.assert_not_called()
    upstream.close.assert_called_once(); pool.close.assert_called_once()
