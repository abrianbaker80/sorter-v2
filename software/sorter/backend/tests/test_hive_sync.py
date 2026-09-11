from server import hive_sync


def test_correction_stream_waits_while_piece_records_are_ahead(monkeypatch):
    monkeypatch.setattr(hive_sync.piece_records, "getMaxRecordId", lambda: 11)

    assert hive_sync._recordStreamCaughtUp(10) is False


def test_correction_stream_opens_after_piece_records_catch_up(monkeypatch):
    monkeypatch.setattr(hive_sync.piece_records, "getMaxRecordId", lambda: 11)

    assert hive_sync._recordStreamCaughtUp(11) is True
    assert hive_sync._recordStreamCaughtUp(12) is True
    assert hive_sync._recordStreamCaughtUp(None) is False
