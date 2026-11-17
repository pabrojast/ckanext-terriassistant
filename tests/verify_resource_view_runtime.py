"""Run real CKAN/Beaker/Flask-WTF with isolated action/provider fixtures.

TERRIASSISTANT_EXPECTED_SOURCE_ROOT identifies the extension checkout/install to
verify. It defaults to /review for the read-only source mount; use the installed
extension root when checking a release image without a source override.
"""
import json
import os
from pathlib import Path
from unittest.mock import Mock
from flask import Flask, url_for
from flask_login import LoginManager, login_user
from beaker.middleware import SessionMiddleware
from ckan.config.middleware.flask_app import BeakerSessionInterface
from ckan import model
from ckan.plugins import toolkit as tk
from ckanext.terriassistant import api

expected_root = Path(os.environ.get('TERRIASSISTANT_EXPECTED_SOURCE_ROOT', '/review')).resolve()
actual_source = Path(api.__file__).resolve()
assert actual_source.is_relative_to(expected_root), (
    f'Expected extension under {expected_root}, imported {actual_source}')
tk.config.update({'ckan.site_url': 'https://ckan.example', 'beaker.session.secret': 'isolated-signing-key',
                  'ckanext.terriassistant.deepseek_api_key': 'isolated-provider', 'WTF_CSRF_FIELD_NAME': '_csrf_token'})
app = Flask(__name__)
app.secret_key = 'isolated-runtime-secret'
app.config['WTF_CSRF_FIELD_NAME'] = '_csrf_token'
app.session_interface = BeakerSessionInterface()
app.wsgi_app = SessionMiddleware(app.wsgi_app, {'session.type': 'memory', 'session.secret': 'isolated-runtime-secret'})
login = LoginManager(app)
user = model.User(id='reviewer-id', name='reviewer', email='reviewer@example.invalid', state='active')
login.user_loader(lambda uid: user if uid == user.id else None)
@app.route('/login-fixture')
def login_fixture():
    login_user(user)
    return 'ok'
app.add_url_rule('/user/login', endpoint='user.login', view_func=lambda: 'login')
app.add_url_rule('/dataset/<id>/resource/<resource_id>', endpoint='resource.read', view_func=lambda **kw: 'resource')
tk.url_for = url_for
view = {'id': 'view', 'resource_id': 'resource', 'view_type': 'terriassistant', 'title': 'Original', 'description': 'Keep'}
resource = {'id': 'resource', 'package_id': 'dataset', 'format': 'CSV', 'url': 'https://storage.example/data.csv'}
package = {'id': 'dataset', 'name': 'dataset', 'private': False}
saves = []
def check(name, context, data):
    if name == 'resource_view_update' and not context.get('user'):
        raise tk.NotAuthorized()
    return True
tk.check_access = check
def action(name):
    def run(context, data):
        assert not context.get('ignore_auth')
        if name == 'resource_view_show': return dict(view)
        if name == 'resource_show': return dict(resource)
        if name == 'package_show': return dict(package)
        if name == 'resource_view_list': return [dict(view)]
        if name == 'resource_view_update':
            check(name, context, data)
            saves.append(context['user'])
            return dict(data)
        raise AssertionError(name)
    return run
tk.get_action = action
controller = api.TerriassistantController(api.TerriassistantSettings.from_config(tk.config))
controller.data_service.get_profile = Mock(return_value={'kind': 'csv', 'terria_type': 'csv', 'columns': [], 'numeric_columns': []})
controller.llm_service.complete_chat = Mock(return_value={'action': 'apply_style', 'assistant_message': 'Preview', 'style_spec': {'opacity': 0.5}})
api._get_controller = lambda: controller
app.register_blueprint(api.terriassistant_api)
client = app.test_client()
base = '/api/terriassistant/view/view'
anonymous = client.get(base + '/bootstrap')
assert anonymous.status_code == 200, (anonymous.status_code, anonymous.data)
assert anonymous.json['can_chat'] is False
assert client.post(base + '/chat', json={'messages': [{'role': 'user', 'content': 'Preview'}]}, headers={'X-CSRFToken': anonymous.json['csrf_token']}).status_code == 401
client.get('/login-fixture')
logged = client.get(base + '/bootstrap')
assert logged.status_code == 200, logged.data
assert logged.json['can_chat'] is True and logged.json['can_save'] is True
headers = {'X-CSRFToken': logged.json['csrf_token']}
assert client.post(base + '/chat', json={'messages': [{'role': 'user', 'content': 'Preview'}]}, headers=headers).status_code == 200
assert not saves
saved = client.post(base + '/save', json={'draft_spec': {'opacity': 0.5}}, headers=headers)
assert saved.status_code == 200, saved.data
assert saves == ['reviewer']
assert app.test_client().post(base + '/save', json={'draft_spec': {}}, headers=headers).status_code == 403
print(json.dumps({'runtime': 'production CKAN Flask 2.0.3 / Flask-WTF 1.0.1 / BeakerSessionInterface',
                  'anonymous_bootstrap': anonymous.status_code, 'active_user_chat': 200, 'editor_save': saved.status_code,
                  'cross_session_csrf': 403, 'llm': 'mocked', 'database': 'isolated action fixtures', 'network': 'disabled'}))
