# -*- coding: utf-8 -*-
"""
app_core/services/cache_service.py
==================================
Serviço de Cache em Memória com TTL e Invalidação Orientada a Eventos.
Otimiza a performance de consultas agregadas do Dashboard e listas do sistema.
"""
from __future__ import annotations

import time
import threading
from typing import Any, Callable


class SimpleCacheService:
    def __init__(self, default_ttl_seconds: int = 30):
        self._cache: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()
        self._default_ttl = default_ttl_seconds

    def get(self, key: str) -> Any | None:
        """Recupera valor do cache caso ainda esteja válido dentro do TTL."""
        with self._lock:
            if key not in self._cache:
                return None
            val, expire_at = self._cache[key]
            if time.time() > expire_at:
                del self._cache[key]
                return None
            return val

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        """Armazena um valor no cache com tempo de expiração em segundos."""
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        expire_at = time.time() + ttl
        with self._lock:
            self._cache[key] = (value, expire_at)

    def get_or_set(self, key: str, fetch_fn: Callable[[], Any], ttl_seconds: int | None = None) -> Any:
        """Retorna o valor em cache ou executa fetch_fn() para preencher o cache."""
        val = self.get(key)
        if val is not None:
            return val
        new_val = fetch_fn()
        if new_val is not None:
            self.set(key, new_val, ttl_seconds=ttl_seconds)
        return new_val

    def delete(self, key: str) -> None:
        """Remove uma chave específica do cache."""
        with self._lock:
            self._cache.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> int:
        """Invalida todas as chaves que começam com um determinado prefixo."""
        with self._lock:
            keys_to_del = [k for k in self._cache if k.startswith(prefix)]
            for k in keys_to_del:
                del self._cache[k]
            return len(keys_to_del)

    def clear(self) -> None:
        """Limpa completamente o cache."""
        with self._lock:
            self._cache.clear()


# Instância global do serviço de cache em memória
GLOBAL_CACHE = SimpleCacheService(default_ttl_seconds=30)
