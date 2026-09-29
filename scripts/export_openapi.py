"""Export the FastAPI contract without reading a deployment .env or touching its DB."""

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.main import create_app


PUBLIC_URL = 'https://85-159-231-99.sslip.io'


def main():
    with TemporaryDirectory() as directory:
        settings = Settings(
            _env_file=None,
            database_path=str(Path(directory) / 'openapi.db'),
            bot_mode='disabled',
            demo_mode=False,
            max_bot_token='',
            routerai_api_key='',
            admin_password='',
        )
        schema = create_app(settings).openapi()

    schema['servers'] = [{'url': PUBLIC_URL, 'description': 'Public hackathon deployment'}]
    schema['info']['description'] = (
        'Student mini-app API. MAX initData is exchanged for a short-lived bearer '
        'session. Schedules are synthetic, and lesson generation uses RouterAI.'
    )
    security = schema.setdefault('components', {}).setdefault('securitySchemes', {})
    security['StudentSession'] = {
        'type': 'http', 'scheme': 'bearer',
        'description': 'Session returned by POST /api/auth/max; valid for 24 hours.',
    }
    security['AdminSession'] = {
        'type': 'apiKey', 'in': 'cookie', 'name': 'luchik_admin',
        'description': 'Separate admin session; only needed for schedule administration.',
    }
    security['MaxWebhookSecret'] = {
        'type': 'apiKey', 'in': 'header', 'name': 'X-Max-Bot-Api-Secret',
    }
    for path, methods in schema['paths'].items():
        for method, operation in methods.items():
            if not isinstance(operation, dict):
                continue
            if path.startswith('/api/admin/') and path != '/api/admin/login':
                operation['security'] = [{'AdminSession': []}]
            elif path.startswith('/api/') and path not in (
                '/api/auth/max', '/api/auth/demo', '/api/config', '/api/admin/login'
            ):
                operation['security'] = [{'StudentSession': []}]
            elif path == '/webhook/max':
                operation['security'] = [{'MaxWebhookSecret': []}]
            if path.startswith('/api/admin/') and method in ('post', 'put', 'patch', 'delete'):
                operation.setdefault('parameters', []).append({
                    'name': 'X-Admin-Action', 'in': 'header', 'required': True,
                    'schema': {'type': 'string', 'const': '1'},
                })
            if path.startswith('/api/') or path in ('/health', '/webhook/max'):
                for code in ('200', '201', '202'):
                    response = operation.get('responses', {}).get(code)
                    if response is not None and 'content' not in response:
                        response['content'] = {
                            'application/json': {'schema': {'type': 'object'}}
                        }

    response_fields = {
        ('/health', 'get'): ['status'],
        ('/api/config', 'get'): ['demo_mode', 'subjects'],
        ('/api/auth/max', 'post'): ['token'],
        ('/api/register', 'post'): ['student'],
        ('/api/me', 'get'): ['name', 'student', 'subjects', 'lesson', 'mood', 'daily', 'activity'],
        ('/api/diary', 'get'): ['mock', 'entries'],
        ('/api/history', 'get'): ['topics'],
        ('/api/lesson-jobs/{job_id}', 'get'): ['id', 'status'],
        ('/api/lessons/{lid}/cancel', 'post'): ['cancelled'],
        ('/webhook/max', 'post'): ['ok'],
    }
    for (path, method), required in response_fields.items():
        schema['paths'][path][method]['responses']['200']['content']['application/json']['schema'] = {
            'type': 'object', 'required': required,
            'properties': {key: {} for key in required},
        }
    schema['paths']['/health']['get']['responses']['200']['content']['application/json']['schema']['properties']['status'] = {
        'type': 'string', 'const': 'ok'
    }
    schema['paths']['/api/auth/max']['post']['responses']['200']['content']['application/json']['schema']['properties']['token'] = {
        'type': 'string'
    }
    diary_properties = schema['paths']['/api/diary']['get']['responses']['200']['content']['application/json']['schema']['properties']
    diary_properties['mock'] = {'type': 'boolean', 'const': True}
    diary_properties['entries'] = {'type': 'array', 'items': {'type': 'object'}}
    (ROOT / 'openapi.json').write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )


if __name__ == '__main__':
    main()
