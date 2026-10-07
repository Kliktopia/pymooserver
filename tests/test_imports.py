def test_public_imports():
    import pymooserver
    import mooapi

    assert pymooserver.Moo12Server is not None
    assert mooapi.MooServer is not None
