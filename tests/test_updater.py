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


RUNNING = {"State": {"Running": True, "Restarting": False}}
CRASHED = {"State": {"Running": False, "Restarting": False, "Status": "exited", "ExitCode": 1}}


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(updater, "START_GRACE_SECONDS", 0)
    monkeypatch.setattr(updater, "DRY_RUN", False)
    monkeypatch.setattr(updater, "AUTO_UPDATE", True)
    monkeypatch.setattr(updater, "LABEL_ENABLE", "")
    monkeypatch.setattr(updater, "EXCLUDE_IMAGES", [])
    monkeypatch.setattr(updater, "NOTIFY_WEBHOOK", "")
    updater.NOTIFIED.clear()


def make_client():
    client = MagicMock()
    client.api.create_container.return_value = {"Id": "n" * 64}
    client.api.inspect_container.return_value = RUNNING
    client.api.create_endpoint_config.side_effect = lambda **kw: kw
    client.api.create_networking_config.side_effect = lambda d: d
    return client


def make_container(name="web", cid="a" * 64):
    c = MagicMock()
    c.name = name
    c.id = cid
    c.short_id = "abcdef012345"
    c.image.attrs = {
        "RepoDigests": ["nginx@sha256:olddigest"],
        "Config": {"Env": ["PATH=/bin"], "Labels": {"img": "1"}, "Cmd": ["nginx"]},
    }
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
    spec = updater.build_container_spec(make_client(), make_container())
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
    client = make_client()
    container = make_container()
    assert updater.update_container(client, container) is True
    client.api.start.assert_called_once_with("n" * 64)
    container.remove.assert_called_once()


def test_replacement_is_created_before_old_container_stops():
    client = make_client()
    container = make_container()
    order = []
    client.api.create_container.side_effect = lambda **kw: order.append("create") or {"Id": "n" * 64}
    container.stop.side_effect = lambda **kw: order.append("stop")
    assert updater.update_container(client, container) is True
    assert order == ["create", "stop"]


def test_create_failure_never_stops_container():
    client = make_client()
    client.api.create_container.side_effect = RuntimeError("boom")
    container = make_container()
    container.status = "running"
    assert updater.update_container(client, container) is False
    container.stop.assert_not_called()
    container.start.assert_not_called()


def test_crashing_replacement_is_rolled_back():
    client = make_client()
    client.api.inspect_container.return_value = CRASHED
    container = make_container()
    assert updater.update_container(client, container) is False
    client.api.remove_container.assert_called_once_with("n" * 64, force=True)
    container.start.assert_called_once()
    container.remove.assert_not_called()


def test_auto_remove_container_is_not_removed_twice():
    client = make_client()
    container = make_container()
    container.attrs["HostConfig"]["AutoRemove"] = True
    assert updater.update_container(client, container) is True
    container.remove.assert_not_called()


def test_stop_uses_containers_own_timeout():
    client = make_client()
    container = make_container()
    container.attrs["Config"]["StopTimeout"] = 120
    updater.update_container(client, container)
    container.stop.assert_called_once_with(timeout=120)


def test_image_exposed_ports_are_left_to_the_new_image():
    container = make_container()
    container.image.attrs["Config"]["ExposedPorts"] = {"80/tcp": {}, "443/tcp": {}}
    container.attrs["Config"]["ExposedPorts"] = {"80/tcp": {}, "443/tcp": {}, "9000/tcp": {}}
    create = updater.build_container_spec(make_client(), container)["create"]
    # 80 is published, 9000 was added by the user, 443 only comes from the image
    assert sorted(create["ports"]) == [(80, "tcp"), (9000, "tcp")]


def test_container_network_mode_references_parent_by_name():
    client = make_client()
    client.containers.get.return_value.name = "vpn"
    container = make_container()
    container.attrs["Config"]["ExposedPorts"] = {"8080/tcp": {}}
    container.attrs["HostConfig"].update(NetworkMode="container:" + "b" * 64, PortBindings={})
    container.attrs["HostConfig"]["VolumesFrom"] = ["c" * 64 + ":ro"]
    create = updater.build_container_spec(client, container, {"c" * 64: "data"})["create"]
    assert create["host_config"]["NetworkMode"] == "container:vpn"
    assert create["host_config"]["VolumesFrom"] == ["data:ro"]
    assert create["ports"] is None
    assert create["hostname"] is None
    assert create["networking_config"] is None


def test_dependents_are_recreated_after_parent(monkeypatch):
    parent = make_container("vpn", "b" * 64)
    child = make_container("app", "c" * 64)
    child.attrs["HostConfig"].update(NetworkMode="container:" + "b" * 64, PortBindings={})
    client = make_client()
    client.containers.list.return_value = [parent, child]
    monkeypatch.setattr(updater, "check_for_update", lambda cl, c: (c is parent, "sha256:old", "sha256:new"))
    updater.check_and_update(client)
    specs = [call.kwargs for call in client.api.create_container.call_args_list]
    assert [s["name"] for s in specs] == ["vpn", "app"]
    assert specs[1]["host_config"]["NetworkMode"] == "container:vpn"
    child.stop.assert_called_once()


def test_swarm_tasks_and_local_images_are_skipped():
    swarm = make_container("svc.1", "d" * 64)
    swarm.attrs["Config"]["Labels"]["com.docker.swarm.service.id"] = "x"
    local = make_container("built", "e" * 64)
    local.image.attrs["RepoDigests"] = []
    client = make_client()
    client.containers.list.return_value = [swarm, local]
    updater.check_and_update(client)
    client.images.pull.assert_not_called()
    client.images.get_registry_data.assert_not_called()


def test_notify_only_mode_does_not_pull_and_notifies_once(monkeypatch):
    monkeypatch.setattr(updater, "AUTO_UPDATE", False)
    notify = MagicMock()
    monkeypatch.setattr(updater, "send_notification", notify)
    client = make_client()
    client.containers.list.return_value = [make_container()]
    client.images.get_registry_data.return_value.id = "sha256:newdigest"
    updater.check_and_update(client)
    updater.check_and_update(client)
    client.images.pull.assert_not_called()
    client.api.create_container.assert_not_called()
    assert notify.call_count == 1
    client.images.get_registry_data.return_value.id = "sha256:newerdigest"
    updater.check_and_update(client)
    assert notify.call_count == 2


def test_prune_survives_container_with_deleted_image():
    client = make_client()
    other = make_container("other", "f" * 64)
    type(other).image = property(lambda self: (_ for _ in ()).throw(updater.docker.errors.ImageNotFound("gone")))
    client.containers.list.return_value = [other]
    updater.prune_image(client, "sha256:unused")
    client.images.remove.assert_called_once_with("sha256:unused", force=False)


def test_notification_http_error_is_not_reported_as_sent(monkeypatch, caplog):
    monkeypatch.setattr(updater, "NOTIFY_WEBHOOK", "http://hook")
    post = MagicMock()
    post.return_value.raise_for_status.side_effect = updater.requests.HTTPError("404")
    monkeypatch.setattr(updater.requests, "post", post)
    updater.send_notification("hi")
    assert "Failed to send notification" in caplog.text


def test_env_int_rejects_garbage(monkeypatch):
    monkeypatch.setenv("X_INT", "abc")
    with pytest.raises(SystemExit):
        updater.env_int("X_INT", 5)
    monkeypatch.setenv("X_INT", " 7 ")
    assert updater.env_int("X_INT", 5) == 7


@pytest.mark.parametrize("value,expected", [("true", True), ("YES", True), ("1", True), ("false", False), ("", False)])
def test_env_bool(monkeypatch, value, expected):
    monkeypatch.setenv("X_BOOL", value)
    assert updater.env_bool("X_BOOL", "false") is expected


def test_label_filter_without_value(monkeypatch):
    monkeypatch.setattr(updater, "LABEL_ENABLE", "autoupdate")
    client = MagicMock()
    client.containers.list.return_value = []
    updater.check_and_update(client)
    client.containers.list.assert_called_once_with(filters={"label": "autoupdate"})


def test_stop_flag_halts_between_containers(monkeypatch):
    monkeypatch.setattr(updater, "LABEL_ENABLE", "")
    client = MagicMock()
    client.containers.list.return_value = [make_container(), make_container()]
    updater.STOP.set()
    try:
        updater.check_and_update(client)
    finally:
        updater.STOP.clear()
    client.images.pull.assert_not_called()


def test_notification_payload_covers_gotify(monkeypatch):
    monkeypatch.setattr(updater, "NOTIFY_WEBHOOK", "http://hook")
    post = MagicMock()
    monkeypatch.setattr(updater.requests, "post", post)
    updater.send_notification("hi")
    payload = post.call_args.kwargs["json"]
    assert payload["message"] == payload["content"] == payload["text"] == "hi"
