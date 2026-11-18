from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
WRITER = COMPOSE["services"]["writer"]
APP = COMPOSE["services"]["morning-brief"]
DOCKERFILE = (ROOT / "writer_service" / "Dockerfile").read_text()


def as_list(value):
    return [value] if isinstance(value, str) else list(value or [])


def test_writer_container_is_locked_down():
    assert WRITER["read_only"] is True
    assert "/tmp" in " ".join(as_list(WRITER["tmpfs"]))
    assert WRITER["mem_limit"] == "1g"
    assert WRITER["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in WRITER["security_opt"]
    assert "ports" not in WRITER and "expose" not in WRITER
    assert not WRITER.get("privileged") and "network_mode" not in WRITER and "cap_add" not in WRITER
    assert str(WRITER.get("user", "1000")).split(":")[0] == "1000"


def test_writer_has_only_a_named_volume_and_no_bind_mounts():
    assert WRITER["volumes"] == ["writer-home:/home/writer"]
    assert "writer-home" in COMPOSE["volumes"]
    text = yaml.safe_dump(WRITER)
    assert "docker.sock" not in text and "./" not in text


def test_writer_secrets_come_only_from_an_optional_writer_env_and_the_worker_token():
    env_files = as_list(WRITER["env_file"])
    assert env_files == [{"path": "writer.env", "required": False}]   # a missing file must not break the project
    assert set(WRITER["environment"]) == {"WORKER_TOKEN"}
    # an empty WORKER_TOKEN is legal (deep dives off): no `:?` that would fail the whole file
    assert WRITER["environment"]["WORKER_TOKEN"] == "${WORKER_TOKEN:-}"
    assert ":?" not in yaml.safe_dump(WRITER)
    assert "ANTHROPIC_API_KEY" not in yaml.safe_dump(WRITER)


def test_writer_is_opt_in_so_a_plain_up_neither_builds_nor_starts_it():
    assert WRITER["profiles"] == ["writer"]
    assert "profiles" not in APP
    text = (ROOT / "docker-compose.yml").read_text()
    assert "--profile writer" in text
    assert "--profile writer" in (ROOT / "writer.env.example").read_text()


def test_writer_has_an_init_process_and_a_pids_limit():
    assert WRITER["init"] is True
    assert WRITER["pids_limit"] == 256


def test_writer_is_alone_on_the_dedicated_network_with_a_fixed_subnet():
    assert WRITER["networks"] == ["writer_net"]
    net = COMPOSE["networks"]["writer_net"]
    assert net["ipam"]["config"] == [{"subnet": "172.29.40.0/24"}]


def test_app_joins_writer_net_at_the_fixed_address_and_keeps_its_default_network():
    nets = APP["networks"]
    assert "default" in nets
    assert nets["writer_net"] == {"ipv4_address": "172.29.40.10"}
    assert APP["ports"] and APP["volumes"] == ["./data:/srv/data"] and APP["env_file"] == ".env"


def test_writer_builds_from_the_repo_root_with_its_own_dockerfile():
    assert WRITER["build"]["dockerfile"] == "writer_service/Dockerfile"
    assert WRITER["build"]["context"] == "."


def test_dockerfile_pins_claude_code_and_runs_unprivileged():
    assert "FROM node:22-slim" in DOCKERFILE
    assert "ARG CLAUDE_CODE_VERSION=2.1.259" in DOCKERFILE
    assert "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" in DOCKERFILE
    assert "DISABLE_AUTOUPDATER=1" in DOCKERFILE and "HOME=/home/writer" in DOCKERFILE
    assert "PYTHONPATH=/opt/writer" in DOCKERFILE
    assert "USER node" in DOCKERFILE
    assert "--no-install-recommends" in DOCKERFILE and "rm -rf /var/lib/apt/lists" in DOCKERFILE
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in DOCKERFILE and "ANTHROPIC_API_KEY" not in DOCKERFILE
    assert "/opt/writer/writer_service" in DOCKERFILE and "/opt/writer/scripts/render_worker_prompt.py" in DOCKERFILE
    assert "/opt/writer/worker" in DOCKERFILE


def test_entrypoint_prepares_then_execs_the_loop():
    lines = (ROOT / "writer_service" / "entrypoint.sh").read_text().splitlines()
    assert lines[0].startswith("#!")
    body = [l for l in lines if l and not l.startswith("#")]
    assert "set -e" in " ".join(body)
    assert body[-2:] == ["python3 -m writer_service.prepare", "exec python3 -m writer_service.loop"]


def test_example_env_and_ignore_files():
    example = [l for l in (ROOT / "writer.env.example").read_text().splitlines() if l and not l.startswith("#")]
    assert example == ["CLAUDE_CODE_OAUTH_TOKEN="]
    assert "writer.env" in (ROOT / ".gitignore").read_text().splitlines()
    docker_ignore = (ROOT / ".dockerignore").read_text().splitlines()
    assert "writer.env" in docker_ignore and ".env*" in docker_ignore
    # the writer image needs scripts/ and worker/, which the app's ignore file hides, so it brings its own
    own = (ROOT / "writer_service" / "Dockerfile.dockerignore").read_text().splitlines()
    assert "scripts" not in own and "worker" not in own and "writer.env" in own and ".env*" in own


def test_image_workdir_is_not_the_home_directory():
    workdirs = [l.split()[1] for l in DOCKERFILE.splitlines() if l.startswith("WORKDIR")]
    assert workdirs and all(not w.startswith("/home/writer") for w in workdirs)
