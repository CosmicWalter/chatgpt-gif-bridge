"""Separate management authentication from client API keys."""
import hmac
import json
import os
from pathlib import Path
import secrets
import threading

import bcrypt


class Credentials:
    def __init__(self, directory, legacy_key, reset_management=False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'credentials.json'
        self.lock = threading.RLock()
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        else:
            self.data = {'api_keys': [legacy_key]}
        if reset_management or 'management_hash' not in self.data:
            secret = secrets.token_hex(32)
            self.data['management_hash'] = bcrypt.hashpw(secret.encode(), bcrypt.gensalt()).decode()
            bootstrap = self.directory / 'management-key'
            bootstrap.write_text(secret)
            bootstrap.chmod(0o600)
            self._save()

    def _save(self):
        temporary = self.path.with_suffix('.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as file:
            json.dump(self.data, file)
        temporary.replace(self.path)

    def is_management(self, key):
        if not isinstance(key, str) or not 1 <= len(key.encode()) <= 72:
            return False
        with self.lock:
            encoded = self.data['management_hash'].encode()
        return bcrypt.checkpw(key.encode(), encoded)

    def is_api(self, key):
        with self.lock:
            return any(hmac.compare_digest(key.encode(), item.encode()) for item in self.data['api_keys'])

    def keys(self):
        with self.lock:
            return list(self.data['api_keys'])

    @staticmethod
    def validate(key):
        if not isinstance(key, str) or not 8 <= len(key) <= 256 or not key.isascii() or any(c.isspace() for c in key):
            raise ValueError('API key 须为 8–256 位 ASCII 字符，不能包含空格。')
        return key

    def add(self, key=None):
        key = self.validate(key if key is not None else 'sk-bridge-' + secrets.token_hex(24))
        with self.lock:
            if key in self.data['api_keys']:
                raise ValueError('该 API key 已存在。')
            if len(self.data['api_keys']) >= 100:
                raise ValueError('最多保留 100 个 API key。')
            self.data['api_keys'].append(key)
            self._save()
        return key

    def edit(self, old, new):
        new = self.validate(new)
        with self.lock:
            if old not in self.data['api_keys']:
                raise ValueError('API key 不存在。')
            if new != old and new in self.data['api_keys']:
                raise ValueError('该 API key 已存在。')
            self.data['api_keys'][self.data['api_keys'].index(old)] = new
            self._save()

    def delete(self, key):
        with self.lock:
            if key not in self.data['api_keys']:
                raise ValueError('API key 不存在。')
            self.data['api_keys'].remove(key)
            self._save()

    def rotate_management(self, old, new):
        if not self.is_management(old):
            raise ValueError('当前管理密钥不正确。')
        if not isinstance(new, str) or not 12 <= len(new.encode()) <= 72:
            raise ValueError('管理密钥须为 12–72 字节。')
        with self.lock:
            self.data['management_hash'] = bcrypt.hashpw(new.encode(), bcrypt.gensalt()).decode()
            self._save()
            (self.directory / 'management-key').unlink(missing_ok=True)
