from pathlib import Path
import tomllib


ROOT = Path(__file__).parents[1]
CPU_INDEX = "https://download.pytorch.org/whl/cpu"


def _lock_packages() -> list[dict]:
    with (ROOT / "uv.lock").open("rb") as lock_file:
        return tomllib.load(lock_file)["package"]


def test_torch_packages_use_the_explicit_cpu_index_on_linux() -> None:
    with (ROOT / "pyproject.toml").open("rb") as project_file:
        project = tomllib.load(project_file)

    sources = project["tool"]["uv"]["sources"]
    assert sources["torch"] == [{"index": "pytorch-cpu", "marker": "sys_platform == 'linux'"}]
    assert sources["torchvision"] == [
        {"index": "pytorch-cpu", "marker": "sys_platform == 'linux'"}
    ]
    indexes = project["tool"]["uv"]["index"]
    assert {index["name"]: index for index in indexes}["pytorch-cpu"] == {
        "name": "pytorch-cpu",
        "url": CPU_INDEX,
        "explicit": True,
    }


def test_linux_torch_dependency_graph_has_no_cuda_or_triton_packages() -> None:
    packages = _lock_packages()
    cpu_packages = {
        package["name"]: package
        for package in packages
        if package.get("source", {}).get("registry") == CPU_INDEX
    }
    assert {"torch", "torchvision"} <= cpu_packages.keys()

    forbidden = {
        package["name"]
        for package in packages
        if package["name"].startswith("nvidia-")
        or package["name"] in {"triton", "cuda-bindings", "cuda-toolkit"}
    }
    reachable: set[str] = set()
    pending = ["torch", "torchvision"]
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        pending.extend(
            dependency["name"]
            for dependency in cpu_packages[name].get("dependencies", [])
            if dependency["name"] in cpu_packages
        )

    assert not forbidden & reachable


def test_dockerfile_installs_a_pinned_uv_without_external_stage() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "COPY --from=ghcr.io/astral-sh/uv:latest" not in dockerfile
    assert "RUN pip install --no-cache-dir uv==0.10.4" in dockerfile
