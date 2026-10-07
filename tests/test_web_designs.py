"""The five reviewable designs are served as bounded local static assets."""
import http.client
import urllib.error
import urllib.parse
import urllib.request

import pytest

from tests.test_web_server import http_server


# The legacy entry points answer with a 302 whose Location carries a
# client-side fragment (`/#principles`). Browsers keep that fragment on the
# client; urllib instead re-appends it to the request target as soon as an
# ambient HTTP proxy handles the loopback request, so the fragment reaches the
# server as a path segment (`/%23principles`) and 404s. These tests exercise
# the local server, so they open it the way a browser does: directly.
_loopback_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(url):
    return _loopback_opener.open(url)


def _raw_response(base, path):
    """Fetch `path` without following redirects or consulting proxies."""
    host = urllib.parse.urlsplit(base)
    connection = http.client.HTTPConnection(host.hostname, host.port, timeout=5)
    try:
        connection.request('GET', path)
        response = connection.getresponse()
        response.read()
        return response.status, response.getheader('Location')
    finally:
        connection.close()


def test_knowledge_management_shares_the_main_workbench(http_server):
    base, _, _ = http_server
    with _open(base + '/') as response:
        html = response.read().decode('utf-8')
    assert 'data-page-panel="home"' in html
    assert 'data-page-panel="memories"' in html
    assert 'data-page-panel="experiences"' in html
    assert 'data-page-panel="progress"' in html
    assert '>项目历史</a>' in html
    assert '>经验知识</a>' in html
    assert '>资料整理</a>' in html
    assert 'href="/organize"' not in html
    assert 'href="#principles"' in html
    assert 'data-page-panel="principles"' in html
    assert 'src="/knowledge.js"' in html
    assert 'href="/knowledge"' not in html
    assert 'href="/legacy"' not in html
    assert 'src="/designs/app.js"' in html
    assert '设计预览' not in html


@pytest.mark.parametrize('path', ['/organize', '/organize/'])
def test_organizer_bookmarks_open_knowledge_intake(http_server, path):
    base, _, _ = http_server
    with _open(base + path) as response:
        html = response.read().decode('utf-8')
    assert response.geturl().endswith('/#knowledge/intake')
    assert 'data-page-panel="memories"' in html
    assert 'id="view-archive"' not in html


@pytest.mark.parametrize('path,target', [
    ('/knowledge', '/#knowledge/projects'),
    ('/knowledge?view=rules', '/#knowledge/rules'),
    ('/knowledge?view=skill', '/#knowledge/skill'),
    ('/knowledge?view=invalid', '/#knowledge/projects'),
    ('/legacy', '/'),
])
def test_previous_workbench_links_use_the_unified_home(http_server, path, target):
    base, _, _ = http_server
    with _open(base + path) as response:
        assert response.geturl() == base + target
        assert 'data-slot="memory-browser"' in response.read().decode('utf-8')


@pytest.mark.parametrize('path', ['/workflow', '/workflow/', '/workflow-diagram'])
def test_working_principle_uses_the_main_workbench(http_server, path):
    base, _, _ = http_server
    with _open(base + path) as response:
        html = response.read().decode('utf-8')
    assert response.headers.get_content_type() == 'text/html'
    assert 'EvolvMem' in html
    assert response.geturl().endswith('/#principles')
    assert 'data-page-panel="principles"' in html


@pytest.mark.parametrize('path,target', [
    ('/organize', '/#knowledge/intake'),
    ('/organize/', '/#knowledge/intake'),
    ('/knowledge', '/#knowledge/projects'),
    ('/knowledge?view=rules', '/#knowledge/rules'),
    ('/knowledge?view=skill', '/#knowledge/skill'),
    ('/knowledge?view=invalid', '/#knowledge/projects'),
    ('/workflow', '/#principles'),
    ('/workflow/', '/#principles'),
    ('/workflow-diagram', '/#principles'),
    ('/legacy', '/'),
])
def test_legacy_entry_points_redirect_to_a_client_side_hash_route(http_server, path, target):
    """Old bookmarks keep their route in the fragment, never in a request path."""
    base, _, _ = http_server
    status, location = _raw_response(base, path)
    assert status == 302
    assert location == target
    landing = urllib.parse.urlsplit(target)
    assert '#' not in landing.path and '#' not in landing.query
    assert _raw_response(base, landing.path)[0] == 200


@pytest.mark.parametrize('path,mime', [
    ('/designs/', 'text/html'),
    *[(f'/designs/{name}.html', 'text/html')
      for name in ('orbit', 'atlas', 'halo', 'signal', 'nocturne')],
    *[(f'/designs/{name}.{ext}', mime)
      for name in ('orbit', 'atlas', 'halo', 'signal', 'nocturne')
      for ext, mime in (('css', 'text/css'), ('png', 'image/png'))],
    ('/designs/app.js', 'text/javascript'),
    ('/designs/organizer.js', 'text/javascript'),
    ('/designs/galaxy.js', 'text/javascript'),
    ('/designs/common.css', 'text/css'),
])
def test_design_pages_and_assets_are_served(http_server, path, mime):
    base, _, _ = http_server
    with _open(base + path) as response:
        assert response.headers.get_content_type() == mime
        assert len(response.read()) > 100


@pytest.mark.parametrize('path', ['/designs/unknown.html', '/designs/../../config.py',
                                '/designs/%2e%2e/config.py'])
def test_design_routes_do_not_expose_arbitrary_files(http_server, path):
    base, _, _ = http_server
    with pytest.raises(urllib.error.HTTPError) as error:
        _open(base + path)
    assert error.value.code == 404
def test_extraction_preview_asset_is_served(http_server):
    base, _, _ = http_server
    with _open(base + '/extraction.js') as response:
        assert response.status == 200
        assert 'javascript' in response.headers['Content-Type']
