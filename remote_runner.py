#!/usr/bin/env python3
"""Experimental SSH worker: policy is supplied by the worker operator, never the caller."""
import importlib.util
import json
import os
from pathlib import Path
import sys

LIMIT = 1024 * 1024


def execute(request, roots, allow_do, backend):
    if not isinstance(request, dict) or set(request) - {'task', 'workdir', 'role', 'minutes', 'verify', 'background', 'resume'}:
        return {'error': 'invalid request'}
    task, workdir, role = request.get('task'), request.get('workdir'), request.get('role', 'do')
    if (not isinstance(task, str) or not task.strip() or len(task) > 59000
            or not isinstance(workdir, str) or not Path(workdir).is_absolute()
            or role not in ('do', 'research', 'review') or (role == 'do' and not allow_do)
            or request.get('background', False) is not False
            or type(request.get('verify', True)) is not bool
            or type(request.get('minutes', 10)) is not int or not 1 <= request.get('minutes', 10) <= 30
            or not isinstance(request.get('resume', ''), str)):
        return {'error': 'request refused by worker policy'}
    try:
        workspace = Path(workdir).resolve(strict=True)
        allowed = [Path(root).resolve(strict=True) for root in roots if Path(root).is_absolute()]
        if not workspace.is_dir() or not any(workspace == root or root in workspace.parents for root in allowed):
            return {'error': 'workspace is outside worker roots'}
        args = dict(request, task=task.strip(), workdir=str(workspace), role=role, background=False)
        result = json.loads(backend.delegate(args))
        return result if isinstance(result, dict) else {'error': 'invalid worker response'}
    except (OSError, ValueError, TypeError):
        return {'error': 'worker request failed'}


def main():
    data = sys.stdin.buffer.read(LIMIT + 1)
    try:
        if len(data) > LIMIT:
            raise ValueError('request too large')
        request = json.loads(data)
        spec = importlib.util.spec_from_file_location('gogogoogle_worker', Path(__file__).with_name('__init__.py'))
        backend = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backend)
        backend.BROWSER_MODE = 'off'
        roots = [root for root in os.environ.get('GOGOGOOGLE_WORKER_ROOTS', '').split(os.pathsep) if root]
        result = execute(request, roots, os.environ.get('GOGOGOOGLE_WORKER_ALLOW_DO') == '1', backend)
    except (ValueError, TypeError):
        result = {'error': 'invalid request'}
    sys.stdout.write(json.dumps(result, ensure_ascii=True) + '\n')


if __name__ == '__main__':
    main()
