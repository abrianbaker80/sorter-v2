"""Test-only cleanup for v0.3.0's process-lifetime WAL keeper."""

import local_state


def close_keeper():
    with local_state._KEEPER_LOCK:
        if local_state._keeper_conn is not None:
            local_state._keeper_conn.close()
            local_state._keeper_conn = None
