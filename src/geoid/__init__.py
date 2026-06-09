"""GeoID — a global, federated registry that mints immutable, secure identifiers
for geospatial places and serves them over an OGC API Features read surface.

The product's value is the *write path*: mint an immutable geoid (UUIDv7),
deduplicate by canonical geometry hash, attach provenance, and accept anonymous
contributions. The read path is standard OGC API Features over PostGIS.
"""

__version__ = "0.1.0"
