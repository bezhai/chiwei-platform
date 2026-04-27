"""Business glue: lift legacy ORM rows into Domain Data and emit().

Bridges are Phase-bound — they exist only while a legacy pipeline is being
migrated. After Phase 5 (full chat migration), this entire package is deleted.
"""
