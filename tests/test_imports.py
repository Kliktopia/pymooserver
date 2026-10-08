def test_public_imports():
    import pymooserver
    import mooapi

    assert pymooserver.Moo12Server is not None
    assert pymooserver.__version__ == "0.1.2"
    assert mooapi.MooServer is not None
