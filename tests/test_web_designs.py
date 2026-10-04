"""The five reviewable designs are served as bounded local static assets."""
import urllib.error
import urllib.request

import pytest

from tests.test_web_server import http_server


def test_knowledge_management_shares_the_main_workbench(http_server):
    base, _, _ = http_server
    with urllib.request.urlopen(base + '/') as response:
        html = response.read().decode('utf-8')
    assert 'data-page-panel="home"' in html
    assert 'data-page-panel="memories"' in html
    assert 'data-page-panel="experiences"' in html
    assert 'data-page-panel="progress"' in html
    assert '>知识库</a>' in html
    assert 'href="/organize"' not in html
    assert 'href="/workflow"' in html
    assert 'src="/knowledge.js"' in html
    assert 'href="/knowledge"' not in html
    assert 'href="/legacy"' not in html
    assert 'src="/designs/app.js"' in html
    assert '设计预览' not in html


@pytest.mark.parametrize('path', ['/organize', '/organize/'])
def test_organizer_bookmarks_open_knowledge_intake(http_server, path):
    base, _, _ = http_server
    with urllib.request.urlopen(base + path) as response:
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
    with urllib.request.urlopen(base + path) as response:
        assert response.geturl() == base + target
        assert 'data-slot="memory-browser"' in response.read().decode('utf-8')


@pytest.mark.parametrize('path', ['/workflow', '/workflow/', '/workflow-diagram'])
def test_working_principle_is_a_separate_page(http_server, path):
    base, _, _ = http_server
    with urllib.request.urlopen(base + path) as response:
        html = response.read().decode('utf-8')
    assert response.headers.get_content_type() == 'text/html'
    assert 'EvolvMem' in html
    if path != '/workflow-diagram':
        assert 'src="/workflow-diagram?theme=light&amp;present=1"' in html
        assert 'href="/"' in html


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
    with urllib.request.urlopen(base + path) as response:
        assert response.headers.get_content_type() == mime
        assert len(response.read()) > 100


@pytest.mark.parametrize('path', ['/designs/unknown.html', '/designs/../../config.py',
                                '/designs/%2e%2e/config.py'])
def test_design_routes_do_not_expose_arbitrary_files(http_server, path):
    base, _, _ = http_server
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(base + path)
    assert error.value.code == 404
def test_extraction_preview_asset_is_served(http_server):
    base, _, _ = http_server
    with urllib.request.urlopen(base + '/extraction.js') as response:
        assert response.status == 200
        assert 'javascript' in response.headers['Content-Type']
