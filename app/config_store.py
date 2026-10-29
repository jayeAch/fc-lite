"""Stub: caches/config are in-memory only in the lite build."""


def load_source_cache_by_name(source_name, *, keys=None, exclude=None):
    return {}


def load_source_cache(source_id):
    return {}


def persist_source_cache_updates(source_id, updates):
    return False


def persist_source_config_updates(source_id, updates):
    return False
