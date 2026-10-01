import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import updater  # noqa: E402


@pytest.mark.parametrize("excludes,image,expected", [
    (["nginx"], "nginx:1.25", True),
    (["nginx:1.25"], "nginx:1.25", True),
    (["nginx:1.25"], "nginx:1.26", False),
    (["nginx:latest"], "nginx", True),
    (["nginx"], "docker.io/library/nginx:latest", True),
    (["localhost:5000/foo"], "localhost:5000/foo:2", True),
    (["localhost:5000/foo"], "localhost:5000/bar", False),
    (["localhost"], "localhost:5000/foo", False),
    ([], "nginx", False),
])
def test_is_image_excluded(monkeypatch, excludes, image, expected):
    monkeypatch.setattr(updater, "EXCLUDE_IMAGES", excludes)
    assert updater.is_image_excluded(image) is expected


def test_is_pinned_image():
    assert updater.is_pinned_image("nginx@sha256:abc")
    assert updater.is_pinned_image("sha256:abc")
    assert not updater.is_pinned_image("nginx:latest")


def test_own_container_id_ignores_non_id_hostname(monkeypatch):
    monkeypatch.setattr("builtins.open", MagicMock(side_effect=OSError))
    monkeypatch.setenv("HOSTNAME", "my-host")
    assert updater.get_own_container_id() == ""
    monkeypatch.setenv("HOSTNAME", "abcdef012345")
    assert updater.get_own_container_id() == "abcdef012345"


def make_container():
    c = MagicMock()
    c.name = "web"
    c.short_id = "abcdef012345"
    c.image.attrs = {"Config": {"Env": ["PATH=/bin"], "Labels": {"img": "1"}, "Cmd": ["nginx"]}}
    c.attrs = {
        "Image": "sha256:old",
        "Config": {
            "Image": "nginx:latest", "Hostname": "abcdef012345",
            "Env": ["PATH=/bin", "FOO=bar"], "Labels": {"img": "1", "mine": "2"},
            "Cmd": ["nginx"], "Entrypoint": None, "ExposedPorts": {"80/tcp": {}},
        },
        "HostConfig": {
            "NetworkMode": "appnet", "Binds": ["/data:/data"],
            "PortBindings": {"80/tcp": [{"HostIp": "", "HostPort": "8080"}]},
            "RestartPolicy": {"Name": "always"},
        },
        "Mounts": [
            {"Type": "bind", "Destination": "/data", "RW": True},
            {"Type": "volume", "Name": "anon1", "Destination": "/var/cache", "RW": True},
        ],
        "NetworkSettings": {"Networks": {
            "appnet": {"Aliases": ["web", "abcdef012345"], "IPAMConfig": None},
            "other": {"Aliases": ["web2"]},
        }},
    }
    return c


def test_build_container_spec_keeps_config_and_drops_image_defaults():
    client = MagicMock()
    client.api.create_endpoint_config.side_effect = lambda **kw: kw
    client.api.create_networking_config.side_effect = lambda d: d
    spec = updater.build_container_spec(client, make_container())
    create = spec["create"]
    assert create["environment"] == ["FOO=bar"]
    assert create["labels"] == {"mine": "2"}
    assert create["command"] is None
    assert create["hostname"] is None
    assert create["ports"] == [(80, "tcp")]
    assert create["host_config"]["PortBindings"] == {"80/tcp": [{"HostIp": "", "HostPort": "8080"}]}
    assert create["host_config"]["RestartPolicy"] == {"Name": "always"}
    assert create["host_config"]["Binds"] == ["/data:/data", "anon1:/var/cache"]
    assert create["networking_config"]["appnet"]["aliases"] == ["web"]
    assert list(spec["extra_networks"]) == ["other"]


def test_failed_recreate_rolls_back(monkeypatch):
    monkeypatch.setattr(updater, "DRY_RUN", False)
    client = MagicMock()
    client.api.create_container.side_effect = RuntimeError("boom")
    container = make_container()
    container.name = "web"

    def rename(new):
        container.name = new
    container.rename.side_effect = rename

    assert updater.update_container(client, container) is False
    assert container.name == "web"
    container.start.assert_called_once()
    container.remove.assert_not_called()


def test_successful_recreate_removes_old(monkeypatch):
    monkeypatch.setattr(updater, "DRY_RUN", False)
    client = MagicMock()
    client.api.create_container.return_value = {"Id": "n" * 64}
    container = make_container()
    assert updater.update_container(client, container) is True
    client.api.start.assert_called_once_with("n" * 64)
    container.remove.assert_called_once()
