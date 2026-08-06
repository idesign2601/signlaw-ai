"""Infrastructure adapters behind Protocol interfaces.

Each subpackage exposes a Protocol and one or more implementations, so swapping
an embedding model or a generation backend is a configuration change rather than
a code change. Nothing above this layer imports a vendor SDK.
"""
