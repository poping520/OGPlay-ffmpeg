#!/usr/bin/env python3
"""Build, package and verify the trimmed FFmpeg SDK consumed by OGPlay.

The FFmpeg static libraries are merged into one shared library
(`ogplay_ffmpeg`) that exports only the public FFmpeg API. All facts that
define the SDK (source commit, configure arguments, enabled components,
consumer API) live in `ffmpeg.json`.

Subcommands:
  print-config  show the configure command line for a platform
  build         configure, compile and link build/<platform>/stage
  package       copy a built stage into the prebuilt repository
  smoke         compile tests/smoke.c against a packaged SDK and decode samples
  verify        check manifests and file hashes of the prebuilt repository
  all           build, package, smoke and verify one platform
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as host_platform
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "ffmpeg.json"
SOURCE = ROOT / "ffmpeg"
BUILD_ROOT = ROOT / "build"
SAMPLES = ROOT / "tests" / "samples"
SMOKE_SOURCE = ROOT / "tests" / "smoke.c"
DEFAULT_PREBUILT = ROOT.parent / "OGPlay-ffmpeg-prebuilt"
SCHEMA_VERSION = 1

# Static libraries in link order (dependents first).
LIBRARIES = ("avformat", "avcodec", "swscale", "swresample", "avutil")
HEADER_DIRS = tuple(f"lib{name}" for name in LIBRARIES)
COMPONENT_KINDS = (
    "decoder", "encoder", "demuxer", "muxer", "parser", "bsf",
    "protocol", "hwaccel", "indev", "outdev", "filter",
)
# Windows DLL dependencies that ship with the OS or the MSVC runtime.
WINDOWS_SYSTEM_DLLS = re.compile(
    r"^(kernel32|user32|advapi32|bcrypt|ole32|shell32|vcruntime140(_1)?|"
    r"api-ms-win-[a-z0-9-]+)\.dll$",
    re.IGNORECASE,
)
MACOS_SYSTEM_DYLIBS = ("/usr/lib/libSystem.B.dylib",)


class BuildError(Exception):
    pass


# --------------------------------------------------------------------------
# Generic helpers


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if config.get("schema_version") != SCHEMA_VERSION:
        raise BuildError(f"{CONFIG_PATH.name}: unsupported schema_version")
    return config


def platform_config(config: dict, name: str) -> dict:
    platforms = config["platforms"]
    if name not in platforms:
        raise BuildError(f"unknown platform {name!r}; known: {', '.join(platforms)}")
    return platforms[name]


def host_os() -> str:
    system = host_platform.system()
    return {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}.get(system, system)


def require_host(plat: dict, name: str) -> None:
    if plat["os"] != host_os():
        raise BuildError(f"{name} must be built on a {plat['os']} host, not {host_os()}")


def run(cmd, *, cwd: Path | None = None, env: dict | None = None,
        capture: bool = False) -> str:
    shown = cmd if isinstance(cmd, str) else " ".join(shlex.quote(str(c)) for c in cmd)
    print(f"+ {shown}", flush=True)
    result = subprocess.run(
        cmd if isinstance(cmd, str) else [str(c) for c in cmd],
        cwd=cwd, env=env, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    if result.returncode != 0:
        if capture and result.stdout:
            print(result.stdout)
        raise BuildError(f"command failed with exit code {result.returncode}: {shown}")
    return result.stdout or ""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_entries(root: Path, exclude: tuple[str, ...] = ("manifest.json",)) -> list[dict]:
    entries = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in exclude:
            continue
        entries.append({"path": relative, "size": path.stat().st_size, "sha256": sha256(path)})
    return entries


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def check_source(config: dict) -> None:
    if not (SOURCE / "configure").is_file():
        raise BuildError("ffmpeg/ submodule missing; run: git submodule update --init --depth 1")
    commit = run(["git", "-C", SOURCE, "rev-parse", "HEAD"], capture=True).strip()
    if commit != config["ffmpeg"]["commit"]:
        raise BuildError(
            f"ffmpeg/ is at {commit}, expected {config['ffmpeg']['commit']} "
            f"({config['ffmpeg']['tag']})")
    dirty = run(["git", "-C", SOURCE, "status", "--porcelain"], capture=True).strip()
    if dirty:
        raise BuildError(f"ffmpeg/ has local changes:\n{dirty}")


def clean_directory(path: Path) -> None:
    resolved = path.resolve()
    if BUILD_ROOT.resolve() not in resolved.parents:
        raise BuildError(f"refusing to clean {resolved}: not under {BUILD_ROOT}")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


# --------------------------------------------------------------------------
# Toolchain environments


def msys_path(path: Path) -> str:
    text = path.resolve().as_posix()
    match = re.match(r"^([A-Za-z]):/(.*)$", text)
    return f"/{match.group(1).lower()}/{match.group(2)}" if match else text


def msvc_environment() -> dict:
    env = dict(os.environ)
    if all(shutil.which(tool) for tool in ("cl", "link", "lib", "dumpbin")):
        return env
    program_files = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    vswhere = Path(program_files) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        raise BuildError("MSVC not on PATH and vswhere.exe not found; "
                         "run from an x64 Native Tools prompt")
    install = run([vswhere, "-latest", "-products", "*", "-requires",
                   "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                   "-property", "installationPath"], capture=True).strip()
    vcvars = Path(install) / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    if not vcvars.is_file():
        raise BuildError(f"vcvars64.bat not found under {install}")
    # cmd /s strips the outermost quotes, leaving "<vcvars>" >nul && set.
    output = run(f'cmd /d /s /c ""{vcvars}" >nul && set"', capture=True)
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep and key:
            env[key] = value
    return env


def msys_bash() -> Path:
    root = Path(os.environ.get("OGPLAY_MSYS2_ROOT", r"C:\msys64"))
    bash = root / "usr" / "bin" / "bash.exe"
    if not bash.is_file():
        raise BuildError(f"MSYS2 bash not found at {bash}; set OGPLAY_MSYS2_ROOT")
    return bash


def msys_environment(base: dict) -> dict:
    env = dict(base)
    env["MSYS2_PATH_TYPE"] = "inherit"
    env["CHERE_INVOKING"] = "1"
    env["MSYSTEM"] = "MSYS"
    return env


def run_bash(script: str, *, cwd: Path, env: dict, capture: bool = False) -> str:
    return run([msys_bash(), "-lc", script], cwd=cwd, env=env, capture=capture)


_APPLE_TOOLS: dict[str, str] = {}


def apple_tool(name: str) -> str:
    """Absolute path of an Xcode tool. GNU binutils on PATH (e.g. Homebrew's)
    produce archives Apple's ld rejects and corrupt Mach-O files on strip."""
    if name not in _APPLE_TOOLS:
        _APPLE_TOOLS[name] = run(["xcrun", "-f", name], capture=True).strip()
    return _APPLE_TOOLS[name]


def macos_arch(plat: dict) -> str:
    return "arm64" if plat["cpu"] == "arm64" else "x86_64"


def macos_flags(plat: dict) -> str:
    return f"-arch {macos_arch(plat)} -mmacosx-version-min={plat['deployment_target']}"


# --------------------------------------------------------------------------
# Configure / build


def configure_args(config: dict, name: str, prefix: str) -> list[str]:
    plat = platform_config(config, name)
    args = [f"--prefix={prefix}", *config["configure_common"], *plat["configure"]]
    for kind, names in config["components"].items():
        args.append(f"--enable-{kind}={','.join(names)}")
    if plat["os"] == "macos":
        flags = macos_flags(plat)
        args += [f"--cc={apple_tool('clang')}", f"--ar={apple_tool('ar')}",
                 f"--ranlib={apple_tool('ranlib')}", f"--nm={apple_tool('nm')}",
                 f"--strip={apple_tool('strip')}",
                 f"--extra-cflags={flags}", f"--extra-ldflags={flags}"]
    return args


def toolchain_description(plat: dict, env: dict) -> str:
    if plat["os"] == "windows":
        banner = subprocess.run(["cl"], env=env, text=True, capture_output=True)
        return (banner.stderr or banner.stdout).splitlines()[0].strip()
    return run([apple_tool("clang"), "--version"], capture=True).splitlines()[0].strip()


def check_nasm(plat: dict, env: dict, build_dir: Path) -> None:
    if not plat.get("requires_nasm"):
        return
    if plat["os"] == "windows":
        found = run_bash("command -v nasm || true", cwd=build_dir, env=env, capture=True).strip()
    else:
        found = shutil.which("nasm") or ""
    if not found:
        raise BuildError("nasm is required for x86 assembly (install it; building "
                         "without x86asm would silently make H.264 decoding slow)")


def configure_and_make(config: dict, name: str, build_dir: Path, prefix: Path,
                       env: dict) -> list[str]:
    plat = platform_config(config, name)
    jobs = str(os.cpu_count() or 4)
    if plat["os"] == "windows":
        args = configure_args(config, name, msys_path(prefix))
        configure = " ".join(shlex.quote(a) for a in [f"{msys_path(SOURCE)}/configure", *args])
        run_bash(configure, cwd=build_dir, env=env)
        run_bash(f"make -j{jobs} && make install", cwd=build_dir, env=env)
    else:
        args = configure_args(config, name, str(prefix))
        run([SOURCE / "configure", *args], cwd=build_dir, env=env)
        run(["make", f"-j{jobs}"], cwd=build_dir, env=env)
        run(["make", "install"], cwd=build_dir, env=env)
    return args


def enabled_components(build_dir: Path) -> dict[str, list[str]]:
    text = (build_dir / "config_components.h").read_text(encoding="utf-8")
    found: dict[str, list[str]] = {kind: [] for kind in COMPONENT_KINDS}
    pattern = re.compile(r"^#define CONFIG_(\w+)_(" +
                         "|".join(k.upper() for k in COMPONENT_KINDS) + r") 1$", re.M)
    for match in pattern.finditer(text):
        found[match.group(2).lower()].append(match.group(1).lower())
    return {kind: sorted(names) for kind, names in found.items() if names}


def expected_components(config: dict) -> dict[str, list[str]]:
    expected: dict[str, set[str]] = {}
    for source in (config["components"], config.get("implied_components", {})):
        for kind, names in source.items():
            expected.setdefault(kind, set()).update(names)
    return {kind: sorted(names) for kind, names in expected.items()}


def check_components(config: dict, actual: dict[str, list[str]]) -> None:
    expected = expected_components(config)
    if actual != expected:
        raise BuildError(
            "enabled components differ from ffmpeg.json "
            "(components + implied_components)\n"
            f"  expected: {json.dumps(expected, sort_keys=True)}\n"
            f"  actual:   {json.dumps(actual, sort_keys=True)}")


def library_versions(include: Path) -> dict[str, str]:
    versions = {}
    for name in LIBRARIES:
        parts = {}
        for header in ("version_major.h", "version.h"):
            path = include / f"lib{name}" / header
            if not path.is_file():
                continue
            for match in re.finditer(
                    rf"#define LIB{name.upper()}_VERSION_(MAJOR|MINOR|MICRO)\s+(\d+)",
                    path.read_text(encoding="utf-8")):
                parts[match.group(1)] = match.group(2)
        if set(parts) != {"MAJOR", "MINOR", "MICRO"}:
            raise BuildError(f"cannot read lib{name} version from installed headers")
        versions[name] = f"{parts['MAJOR']}.{parts['MINOR']}.{parts['MICRO']}"
    return versions


def static_libraries(prefix: Path) -> list[Path]:
    libs = []
    for name in LIBRARIES:
        candidates = [prefix / "lib" / f"lib{name}.a", prefix / "lib" / f"lib{name}.lib"]
        found = [c for c in candidates if c.is_file()]
        if not found:
            raise BuildError(f"static library for {name} not found in {prefix / 'lib'}")
        libs.append(found[0])
    return libs


def extra_libraries(build_dir: Path, plat: dict) -> list[str]:
    """Link inputs FFmpeg's configure recorded for the five libraries."""
    items: list[tuple[str, ...]] = []
    wanted = re.compile(r"^EXTRALIBS(-(" + "|".join(LIBRARIES) + r"))?=")
    for line in (build_dir / "ffbuild" / "config.mak").read_text(encoding="utf-8").splitlines():
        if not wanted.match(line):
            continue
        tokens = iter(shlex.split(line.split("=", 1)[1]))
        for token in tokens:
            # "-framework X" is one linker input on macOS.
            item: tuple[str, ...] = (token, next(tokens)) if token == "-framework" else (token,)
            if plat["os"] == "windows" and token.startswith("-l"):
                item = (f"{token[2:]}.lib",)
            if item not in items:
                items.append(item)
    return [token for item in items for token in item]


def exported_symbol_candidates(config: dict, plat: dict, libs: list[Path],
                               env: dict) -> dict[str, bool]:
    """Returns public symbol -> is_data for every global defined in the libs."""
    prefixes = tuple(config["export_prefixes"])
    symbols: dict[str, bool] = {}
    if plat["os"] == "windows":
        line_re = re.compile(r"\bSECT[0-9A-F]+\s+\w+\s*(\(\))?\s+External\s+\|\s+(\S+)")
        for lib in libs:
            for line in run(["dumpbin", "/nologo", "/symbols", lib], env=env,
                            capture=True).splitlines():
                match = line_re.search(line)
                if match and match.group(2).startswith(prefixes):
                    symbols[match.group(2)] = match.group(1) is None
    else:
        line_re = re.compile(r"^[0-9a-fA-F]+\s+([TDSBC])\s+_(\w+)$")
        for lib in libs:
            for line in run([apple_tool("nm"), "-gU", lib], capture=True).splitlines():
                match = line_re.match(line.strip())
                if match and match.group(2).startswith(prefixes):
                    symbols[match.group(2)] = match.group(1) != "T"
    if not symbols:
        raise BuildError("no public FFmpeg symbols found in static libraries")
    return dict(sorted(symbols.items()))


def link_shared(config: dict, plat: dict, build_dir: Path, prefix: Path, sdk: Path,
                env: dict) -> tuple[str, dict[str, bool]]:
    name = config["library_name"]
    libs = static_libraries(prefix)
    extras = extra_libraries(build_dir, plat)
    symbols = exported_symbol_candidates(config, plat, libs, env)
    if plat["os"] == "windows":
        (sdk / "bin").mkdir(parents=True)
        (sdk / "lib").mkdir(parents=True)
        definition = build_dir / f"{name}.def"
        lines = [f"LIBRARY {name}", "EXPORTS"]
        lines += [f"    {s} DATA" if data else f"    {s}" for s, data in symbols.items()]
        definition.write_text("\n".join(lines) + "\n", encoding="utf-8")
        library = sdk / "bin" / f"{name}.dll"
        run(["link", "/NOLOGO", "/DLL", "/MACHINE:X64", f"/OUT:{library}",
             f"/IMPLIB:{sdk / 'lib' / f'{name}.lib'}", f"/DEF:{definition}",
             "/OPT:REF", "/OPT:ICF", "/DEBUG", f"/PDB:{build_dir / f'{name}.pdb'}",
             *libs, *extras], cwd=build_dir, env=env)
        # The import library's .exp side file is not part of the SDK.
        for leftover in (sdk / "lib").glob("*.exp"):
            leftover.unlink()
        return f"bin/{name}.dll", symbols

    (sdk / "lib").mkdir(parents=True)
    export_list = build_dir / "exported_symbols.txt"
    export_list.write_text("".join(f"_{s}\n" for s in symbols), encoding="utf-8")
    library = sdk / "lib" / f"lib{name}.dylib"
    force_load = [f"-Wl,-force_load,{lib}" for lib in libs]
    run([apple_tool("clang"), "-dynamiclib", *macos_flags(plat).split(), "-o", library,
         "-install_name", f"@rpath/lib{name}.dylib", *force_load,
         "-Wl,-dead_strip", "-Wl,-dead_strip_dylibs",
         f"-Wl,-exported_symbols_list,{export_list}", *extras], cwd=build_dir, env=env)
    run([apple_tool("strip"), "-x", library])
    run([apple_tool("codesign"), "--force", "--sign", "-", library])
    return f"lib/lib{name}.dylib", symbols


def shared_library_exports(plat: dict, library: Path, env: dict) -> set[str]:
    if plat["os"] == "windows":
        exports = set()
        for line in run(["dumpbin", "/nologo", "/exports", library], env=env,
                        capture=True).splitlines():
            match = re.match(r"^\s+\d+\s+[0-9A-F]+\s+[0-9A-F]+\s+(\S+)", line)
            if match:
                exports.add(match.group(1))
        return exports
    exports = set()
    for line in run([apple_tool("nm"), "-gU", library], capture=True).splitlines():
        match = re.match(r"^[0-9a-fA-F]+\s+[A-Z]\s+_(\w+)$", line.strip())
        if match:
            exports.add(match.group(1))
    return exports


def shared_library_dependencies(plat: dict, library: Path, env: dict) -> list[str]:
    if plat["os"] == "windows":
        output = run(["dumpbin", "/nologo", "/dependents", library], env=env, capture=True)
        deps = re.findall(r"^\s+(\S+\.dll)\s*$", output, re.M | re.I)
        unexpected = [d for d in deps if not WINDOWS_SYSTEM_DLLS.match(d)]
    else:
        lines = run([apple_tool("otool"), "-L", library], capture=True).splitlines()[1:]
        deps = [line.strip().split(" (")[0] for line in lines if line.strip()]
        deps = [d for d in deps if not d.endswith(library.name)]
        unexpected = [d for d in deps if d not in MACOS_SYSTEM_DYLIBS]
    if unexpected:
        raise BuildError(f"{library.name} depends on non-system libraries: {unexpected}")
    return sorted(deps)


def check_exports(config: dict, exports: set[str], expected: dict[str, bool]) -> None:
    missing_api = sorted(set(config["consumer_api"]) - exports)
    if missing_api:
        raise BuildError(f"consumer API not exported: {missing_api}")
    if exports != set(expected):
        raise BuildError(
            "exported symbols differ from the export list: "
            f"missing {sorted(set(expected) - exports)[:10]}, "
            f"extra {sorted(exports - set(expected))[:10]}")


def copy_licenses(sdk: Path) -> None:
    licenses = sdk / "licenses"
    licenses.mkdir()
    for name in ("COPYING.LGPLv2.1", "LICENSE.md"):
        shutil.copy2(SOURCE / name, licenses / f"FFmpeg-{name}")


def build(config: dict, name: str) -> Path:
    plat = platform_config(config, name)
    require_host(plat, name)
    check_source(config)
    base = BUILD_ROOT / name
    build_dir, prefix, stage = base / "obj", base / "install", base / "stage"
    for path in (build_dir, prefix, stage):
        clean_directory(path)

    env = msys_environment(msvc_environment()) if plat["os"] == "windows" else dict(os.environ)
    check_nasm(plat, env, build_dir)
    args = configure_and_make(config, name, build_dir, prefix, env)

    components = enabled_components(build_dir)
    check_components(config, components)

    shutil.copytree(prefix / "include", stage / "include")
    sdk = stage / "sdk"
    sdk.mkdir()
    library, symbols = link_shared(config, plat, build_dir, prefix, sdk, env)
    exports = shared_library_exports(plat, sdk / library, env)
    check_exports(config, exports, symbols)
    dependencies = shared_library_dependencies(plat, sdk / library, env)
    (sdk / "exports.txt").write_text("".join(f"{s}\n" for s in sorted(exports)),
                                     encoding="utf-8")
    copy_licenses(sdk)

    info = {
        "schema_version": SCHEMA_VERSION,
        "kind": "platform",
        "platform": name,
        "os": plat["os"],
        "cpu": plat["cpu"],
        "ffmpeg_tag": config["ffmpeg"]["tag"],
        "ffmpeg_commit": config["ffmpeg"]["commit"],
        "library_versions": library_versions(stage / "include"),
        "toolchain": toolchain_description(plat, env),
        "configure_args": args[1:],  # drop the machine-specific --prefix
        "components": components,
        "library": library,
        "import_library": f"lib/{config['library_name']}.lib" if plat["os"] == "windows" else None,
        "export_count": len(exports),
        "dependencies": dependencies,
    }
    if plat.get("deployment_target"):
        info["deployment_target"] = plat["deployment_target"]
    write_json(stage / "build-info.json", info)
    size = (sdk / library).stat().st_size
    print(f"built {name}: {library} {size / 1024:.0f} KiB, {len(exports)} exports, "
          f"components {json.dumps(components)}")
    return stage


# --------------------------------------------------------------------------
# Package / verify


def headers_manifest(config: dict, include: Path) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "headers",
        "ffmpeg_tag": config["ffmpeg"]["tag"],
        "ffmpeg_commit": config["ffmpeg"]["commit"],
        "files": file_entries(include),
    }


def package(config: dict, name: str, prebuilt: Path, replace_headers: bool) -> Path:
    stage = BUILD_ROOT / name / "stage"
    info_path = stage / "build-info.json"
    if not info_path.is_file():
        raise BuildError(f"{stage} has no build-info.json; run build first")
    info = read_json(info_path)
    if info["ffmpeg_commit"] != config["ffmpeg"]["commit"] or info["platform"] != name:
        raise BuildError(f"{stage} was built from a different source or platform")
    if not (prebuilt / ".git").exists():
        raise BuildError(f"{prebuilt} is not a git repository")

    include = prebuilt / "include"
    staged_headers = file_entries(stage / "include")
    if include.exists():
        current = read_json(include / "manifest.json")
        same = (current["files"] == staged_headers and
                current["ffmpeg_commit"] == config["ffmpeg"]["commit"])
        if not same and not replace_headers:
            raise BuildError("staged headers differ from prebuilt include/; pass "
                             "--replace-headers when moving to a new FFmpeg source")
        if not same:
            shutil.rmtree(include)
    if not include.exists():
        shutil.copytree(stage / "include", include)
        write_json(include / "manifest.json", headers_manifest(config, include))

    destination = prebuilt / name
    temporary = prebuilt / f".{name}.tmp"
    if temporary.exists():
        shutil.rmtree(temporary)
    shutil.copytree(stage / "sdk", temporary)
    manifest = dict(info)
    manifest["shared_headers"] = "include"
    manifest["files"] = file_entries(temporary)
    write_json(temporary / "manifest.json", manifest)
    if destination.exists():
        existing = destination / "manifest.json"
        if not existing.is_file() or read_json(existing).get("platform") != name:
            raise BuildError(f"refusing to replace {destination}: not a {name} SDK package")
        shutil.rmtree(destination)
    temporary.rename(destination)
    print(f"packaged {name} into {destination}")
    return destination


def verify_tree(root: Path, manifest: dict) -> None:
    expected = {entry["path"]: entry for entry in manifest["files"]}
    actual = {entry["path"]: entry for entry in file_entries(root)}
    if set(expected) != set(actual):
        raise BuildError(f"{root}: file set differs from manifest: "
                         f"missing {sorted(set(expected) - set(actual))}, "
                         f"extra {sorted(set(actual) - set(expected))}")
    for path, entry in expected.items():
        if actual[path] != entry:
            raise BuildError(f"{root / path}: size or sha256 differs from manifest")


def verify(config: dict, prebuilt: Path) -> None:
    include = prebuilt / "include"
    headers = read_json(include / "manifest.json")
    verify_tree(include, headers)
    platforms = []
    for name in config["platforms"]:
        root = prebuilt / name
        if not root.exists():
            continue
        manifest = read_json(root / "manifest.json")
        verify_tree(root, manifest)
        if manifest["ffmpeg_commit"] != headers["ffmpeg_commit"]:
            raise BuildError(f"{name} was built from a different commit than include/")
        if manifest["ffmpeg_commit"] != config["ffmpeg"]["commit"]:
            raise BuildError(f"{name} does not match ffmpeg.json commit")
        check_components(config, manifest["components"])
        platforms.append(name)
    if not platforms:
        raise BuildError(f"{prebuilt} has no platform packages")
    print(f"verified {prebuilt}: include + {', '.join(platforms)}")


# --------------------------------------------------------------------------
# Smoke test


def compile_smoke(plat: dict, sdk: Path, include: Path, out_dir: Path, env: dict) -> Path:
    clean_directory(out_dir)
    if plat["os"] == "windows":
        exe = out_dir / "smoke.exe"
        run(["cl", "/nologo", "/std:c11", "/MD", "/W3", "/O1", f"/I{include}",
             SMOKE_SOURCE, f"/Fe:{exe}", f"/Fo:{out_dir}\\", "/link",
             sdk / "lib" / "ogplay_ffmpeg.lib"], cwd=out_dir, env=env)
        shutil.copy2(sdk / "bin" / "ogplay_ffmpeg.dll", out_dir)
        return exe
    exe = out_dir / "smoke"
    run([apple_tool("clang"), "-std=c11", "-O1", "-Wall", "-Werror", *macos_flags(plat).split(),
         f"-I{include}", SMOKE_SOURCE, "-o", exe, f"-L{sdk / 'lib'}", "-logplay_ffmpeg",
         f"-Wl,-rpath,{sdk / 'lib'}"], env=env)
    return exe


def smoke_json(exe: Path, args: list[str], env: dict) -> dict:
    output = run([exe, *args], env=env, capture=True)
    lines = [line for line in output.splitlines() if line.startswith("{")]
    if len(lines) != 1:
        raise BuildError(f"smoke {' '.join(args)} produced no JSON result:\n{output}")
    return json.loads(lines[0])


def check_sample(sample: dict, result: dict) -> None:
    expect = sample["expect"]
    if result.get("status") != expect["status"]:
        raise BuildError(f"{sample['file']}: expected {expect['status']}, got {result}")
    for key, value in expect.items():
        if key == "status":
            continue
        if key.startswith("min_"):
            if result.get(key[4:], 0) < value:
                raise BuildError(f"{sample['file']}: {key[4:]}={result.get(key[4:])} < {value}")
        elif result.get(key) != value:
            raise BuildError(f"{sample['file']}: {key}={result.get(key)!r}, expected {value!r}")


def smoke(config: dict, name: str, prebuilt: Path) -> None:
    plat = platform_config(config, name)
    require_host(plat, name)
    sdk, include = prebuilt / name, prebuilt / "include"
    env = msvc_environment() if plat["os"] == "windows" else dict(os.environ)
    exe = compile_smoke(plat, sdk, include, BUILD_ROOT / name / "smoke", env)

    listing = smoke_json(exe, ["--list"], env)
    for library, (runtime, header) in listing["versions"].items():
        if runtime != header:
            raise BuildError(f"{library}: runtime version {runtime} != header {header}")
    expected = expected_components(config)
    for kind in ("decoder", "demuxer"):
        # Runtime demuxer names list aliases ("mov,mp4,m4a,..."); the first is
        # the configure component name.
        names = sorted(n.split(",")[0] for n in listing[f"{kind}s"])
        if names != expected.get(kind, []):
            raise BuildError(f"runtime {kind}s {listing[f'{kind}s']} != {expected.get(kind)}")
    for kind in ("encoders", "muxers", "protocols"):
        if listing[kind]:
            raise BuildError(f"SDK unexpectedly contains {kind}: {listing[kind]}")

    samples = read_json(SAMPLES / "samples.json")["samples"]
    for sample in samples:
        result = smoke_json(exe, [str(SAMPLES / sample["file"])], env)
        check_sample(sample, result)
        print(f"  {sample['file']}: {json.dumps(result, sort_keys=True)}")
    print(f"smoke {name}: {len(samples)} samples passed")


# --------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("print-config", "build", "package",
                                            "smoke", "verify", "all"))
    parser.add_argument("--platform", help="platform key from ffmpeg.json")
    parser.add_argument("--prebuilt", type=Path, default=DEFAULT_PREBUILT,
                        help="prebuilt repository (default: ../OGPlay-ffmpeg-prebuilt)")
    parser.add_argument("--replace-headers", action="store_true",
                        help="allow package to replace include/ (new FFmpeg source)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config()
    prebuilt = args.prebuilt.resolve()
    try:
        if args.command == "verify":
            verify(config, prebuilt)
            return 0
        if not args.platform:
            raise BuildError(f"{args.command} requires --platform")
        if args.command == "print-config":
            print(" ".join(shlex.quote(a)
                           for a in configure_args(config, args.platform, "<prefix>")))
        if args.command in ("build", "all"):
            build(config, args.platform)
        if args.command in ("package", "all"):
            package(config, args.platform, prebuilt, args.replace_headers)
        if args.command in ("smoke", "all"):
            smoke(config, args.platform, prebuilt)
        if args.command == "all":
            verify(config, prebuilt)
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
