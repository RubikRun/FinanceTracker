"""The Step 4 libraries import on this interpreter. No network and no keyring write."""

from __future__ import annotations

from importlib.metadata import version


def test_step4_libraries_import() -> None:
    import cryptography
    import google.auth
    import google_auth_httplib2
    import google_auth_oauthlib
    import googleapiclient
    import httplib2
    import keyring
    import platformdirs

    assert cryptography.__version__
    assert google.auth.__version__
    assert httplib2.__version__
    assert googleapiclient is not None
    assert google_auth_oauthlib is not None
    assert google_auth_httplib2 is not None
    assert platformdirs.user_config_dir("FinanceTracker", appauthor=False)
    assert keyring.get_keyring() is not None
    for distribution in (
        "google-api-python-client",
        "google-auth",
        "google-auth-oauthlib",
        "google-auth-httplib2",
        "keyring",
        "platformdirs",
        "cryptography",
        "httplib2",
    ):
        assert version(distribution)
