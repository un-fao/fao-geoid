"""HTTP layer — routers and error mapping.

The database is the source of truth for conflicts: ``errors.py`` switches on the
PostgreSQL ``constraint_name`` (SQLSTATE 23505) to map each violation to its HTTP
status per the plan's constraint→HTTP table.
"""
