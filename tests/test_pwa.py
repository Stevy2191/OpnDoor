"""Home screen app: manifest, icons and service worker work without signing in."""
from tests.test_web import client  # noqa: F401


def test_manifest_icons_and_worker(client):
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.json()["display"] == "standalone"
    for size in (180, 192, 512):
        r = client.get(f"/icon-{size}.png")
        assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert client.get("/icon-64.png").status_code == 404
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "caches" not in sw.text
    assert 'rel="manifest"' in client.get("/login").text
