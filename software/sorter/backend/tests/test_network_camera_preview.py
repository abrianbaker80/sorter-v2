import io
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.routers import cameras


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(cameras, '_read_machine_params_config', lambda: (None, {}))
    monkeypatch.setattr(cameras, 'getDiscoveredCameraStreams', lambda: [
        {'id': 'rpi', 'source': 'http://10.55.0.2:18082/video',
         'preview_url': 'http://10.55.0.2:18082/snapshot.jpg', 'name': 'Pi'}])
    monkeypatch.setattr(cameras, '_list_usb_cameras', lambda: [])
    app=FastAPI();app.include_router(cameras.router)
    return TestClient(app)


def test_discovery_exposes_backend_preview_without_changing_stream(client):
    camera=client.get('/api/cameras/list').json()['network'][0]
    assert camera['preview_url']=='http://testserver/api/cameras/network-preview/rpi'
    assert camera['source']=='http://10.55.0.2:18082/video'


def test_jpeg_proxy_and_timeout(client, monkeypatch):
    calls=[]
    def opened(url, timeout):
        calls.append((url,timeout));return io.BytesIO(b'\xff\xd8pixels\xff\xd9')
    monkeypatch.setattr(cameras.urllib_request, 'build_opener', lambda handler: SimpleNamespace(open=opened))
    result=client.get('/api/cameras/network-preview/rpi')
    assert result.status_code==200
    assert result.content==b'\xff\xd8pixels\xff\xd9'
    assert result.headers['content-type']=='image/jpeg'
    assert result.headers['cache-control']=='no-store'
    assert calls==[('http://10.55.0.2:18082/snapshot.jpg',3)]


@pytest.mark.parametrize('payload', [b'<html>error</html>', b'\xff\xd8'+b'x'*(8*1024*1024)+b'\xff\xd9'], ids=['not-jpeg', 'oversized'])
def test_bad_upstream_returns_clear_error(client,monkeypatch,payload):
    monkeypatch.setattr(cameras.urllib_request, 'build_opener', lambda handler: SimpleNamespace(open=lambda *a,**k: io.BytesIO(payload)))
    assert client.get('/api/cameras/network-preview/rpi').status_code==502


def test_unknown_id_never_fetches(client,monkeypatch):
    def forbidden(*a,**k):raise AssertionError('unexpected upstream access')
    monkeypatch.setattr(cameras.urllib_request,'build_opener',forbidden)
    assert client.get('/api/cameras/network-preview/unknown').status_code==404


def test_timeout_and_redirect(client,monkeypatch):
    def timeout(*a,**k):raise TimeoutError()
    monkeypatch.setattr(cameras.urllib_request,'build_opener',lambda h:SimpleNamespace(open=timeout))
    assert client.get('/api/cameras/network-preview/rpi').status_code==502
    assert cameras._NoPreviewRedirect().redirect_request(None,None,302,'',{},'http://elsewhere') is None


def test_configured_network_camera_survives_empty_discovery(client, monkeypatch):
    monkeypatch.setattr(cameras, 'getDiscoveredCameraStreams', lambda: [])
    monkeypatch.setattr(cameras, '_read_machine_params_config', lambda: (None, {'cameras': {
        'classification_channel': 'http://10.55.0.2:18082/video',
        'carousel': 'http://10.55.0.2:18082/video', 'feeder': 0, 'layout': 'split_feeder'}}))
    entries=client.get('/api/cameras/list').json()['network']
    assert len(entries)==1
    assert entries[0]['source']=='http://10.55.0.2:18082/video'
    assert entries[0]['preview_url']=='http://testserver/api/cameras/feed/classification_channel'


def test_configured_camera_does_not_duplicate_discovery(client, monkeypatch):
    monkeypatch.setattr(cameras, '_read_machine_params_config', lambda: (None, {'cameras': {
        'classification_channel': 'http://10.55.0.2:18082/video'}}))
    entries=client.get('/api/cameras/list').json()['network']
    assert len(entries)==1
    assert entries[0]['id']=='rpi'
